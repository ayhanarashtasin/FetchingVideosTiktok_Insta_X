"""Fast in-process async HTTP metadata extractors (FxTwitter + X Syndication).

Eliminates cold subprocess startup and uncached X guest-token rate limits for public X/Twitter
posts while preserving strict single-post identity verification:
* every response must match the exact requested `tweet_id`;
* retweets and quoted-tweet media are never returned (only direct attachments of the linked post);
* uses a pooled, SSRF-safe `aiohttp` session (`PinnedResolver` or configured `egress_proxy_url`);
* any error or ambiguity returns `None` so the caller falls back cleanly to `gallery-dl` / `yt-dlp`.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp

from app.core.contracts import ItemKind
from app.core.deadline import Deadline
from app.core.errors import BotError
from app.core.net_guard import PinnedResolver, Resolver, is_ip_literal, tuned_socket_factory

log = logging.getLogger(__name__)

FXTWITTER_BASE = "https://api.fxtwitter.com"
SYNDICATION_BASE = "https://cdn.syndication.twimg.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; telegram-media-downloader)"
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_VID_DIM_RE = re.compile(r"/vid/(?:avc1/)?(\d{2,4})x(\d{2,4})/")
_SAFE_EXT = re.compile(r"^[a-z0-9]{1,8}$")
_PHOTO_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})


@dataclass(frozen=True)
class FastVideoVariant:
    url: str
    bitrate: int = 0
    width: int | None = None
    height: int | None = None
    ext: str = "mp4"


@dataclass(frozen=True)
class FastMediaItem:
    num: int  # 1-based source order
    kind: ItemKind
    source_item_id: str
    url: str
    extension: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    variants: tuple[FastVideoVariant, ...] = ()


@dataclass(frozen=True)
class FastXResult:
    tweet_id: str
    items: list[FastMediaItem] = field(default_factory=list)
    text_only: bool = False  # True only when the tweet was positively confirmed to have no media/card
    not_found: bool = False  # True only when both FxTwitter and Syndication returned HTTP 404


def syndication_token(tweet_id: str) -> str:
    """Compute X syndication's public token: `((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')`."""
    val = (int(tweet_id) / 1e15) * math.pi
    int_part = int(val)
    frac = val - int_part
    digits: list[str] = []
    n = int_part
    if n == 0:
        digits.append("0")
    else:
        while n > 0:
            digits.append(_B36[n % 36])
            n //= 36
        digits.reverse()
    for _ in range(12):
        frac *= 36.0
        d = int(frac)
        digits.append(_B36[d])
        frac -= d
    return re.sub(r"0+", "", "".join(digits)) or "1"


def _pos_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float) and v > 0:
        return int(v)
    return None


def _pos_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float) and v > 0:
        return float(v)
    return None


def _photo_url_and_ext(raw_url: str) -> tuple[str, str]:
    """Ensure `pbs.twimg.com/media/` photos request original resolution (`name=orig`) and extract extension."""
    parts = urlsplit(raw_url)
    qs = parse_qs(parts.query, keep_blank_values=False)
    ext = ""
    if qs.get("format"):
        cand = qs["format"][0].lower()
        if cand in _PHOTO_EXTS:
            ext = cand
    if not ext:
        tail = parts.path.rsplit("/", 1)[-1]
        if "." in tail:
            cand = tail.rsplit(".", 1)[-1].lower()
            if cand in _PHOTO_EXTS:
                ext = cand
    ext = ext or "jpg"
    if (parts.hostname or "").lower() == "pbs.twimg.com" and "/media/" in parts.path:
        flat = {k: vals[0] for k, vals in qs.items() if vals}
        if flat.get("name") != "orig":
            flat["name"] = "orig"
            raw_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(flat), ""))
    return raw_url, ext


def _dims_from_url(url: str) -> tuple[int | None, int | None]:
    m = _VID_DIM_RE.search(url)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _url_allowed(url: str, *, allow_http: bool) -> bool:
    if not url or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        return False
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in (("https", "http") if allow_http else ("https",)):
        return False
    if "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not host.isascii():
        return False
    return bool(allow_http or not is_ip_literal(host))


def _sort_mp4_variants(
    raw_variants: list[dict[str, Any]],
    fallback_url: str | None,
    *,
    width: int | None,
    height: int | None,
    allow_http: bool,
) -> list[FastVideoVariant]:
    by_url: dict[str, FastVideoVariant] = {}
    for v in raw_variants:
        if not isinstance(v, dict):
            continue
        ctype = str(v.get("content_type") or "").lower()
        u = v.get("url")
        if ctype != "video/mp4" or not isinstance(u, str) or not _url_allowed(u, allow_http=allow_http):
            continue
        vw, vh = _dims_from_url(u)
        br = _pos_int(v.get("bitrate")) or 0
        existing = by_url.get(u)
        if existing is None or br > existing.bitrate:
            by_url[u] = FastVideoVariant(url=u, bitrate=br, width=vw or width, height=vh or height, ext="mp4")
    if (
        isinstance(fallback_url, str)
        and fallback_url not in by_url
        and _url_allowed(fallback_url, allow_http=allow_http)
        and ".m3u8" not in fallback_url.lower()
    ):
        vw, vh = _dims_from_url(fallback_url)
        by_url[fallback_url] = FastVideoVariant(
            url=fallback_url, bitrate=0, width=vw or width, height=vh or height, ext="mp4"
        )
    return sorted(
        by_url.values(),
        key=lambda x: ((x.width or 0) * (x.height or 0), x.bitrate),
        reverse=True,
    )


def parse_fxtwitter(data: Any, tweet_id: str, *, allow_http: bool = False) -> FastXResult | None:
    """Parse `api.fxtwitter.com` JSON for ONE exact tweet. Returns `None` if unusable."""
    if not isinstance(data, dict) or data.get("code") != 200:
        return None
    tweet = data.get("tweet")
    if not isinstance(tweet, dict):
        return None
    if str(tweet.get("id") or "") != tweet_id:
        return None
    if tweet.get("retweeted") or tweet.get("retweeted_status") or tweet.get("is_retweet"):
        return None
    media = tweet.get("media")
    if not isinstance(media, dict):
        # Confirm it has no external card/poll/unknown attachment before calling it text-only.
        if tweet.get("external") or tweet.get("card") or tweet.get("possibly_sensitive"):
            return None
        return FastXResult(tweet_id=tweet_id, items=[], text_only=True)
    raw_all = media.get("all")
    if not isinstance(raw_all, list):
        raw_all = [*(media.get("photos") or []), *(media.get("videos") or [])]
    items: list[FastMediaItem] = []
    for idx, rec in enumerate(raw_all, 1):
        if not isinstance(rec, dict):
            continue
        mtype = str(rec.get("type") or "").lower()
        w, h = _pos_int(rec.get("width")), _pos_int(rec.get("height"))
        if mtype == "photo":
            raw_url = rec.get("url")
            if not isinstance(raw_url, str) or not _url_allowed(raw_url, allow_http=allow_http):
                return None
            url, ext = _photo_url_and_ext(raw_url)
            if not _SAFE_EXT.fullmatch(ext):
                return None
            items.append(
                FastMediaItem(
                    num=idx,
                    kind=ItemKind.PHOTO,
                    source_item_id=f"{tweet_id}:{idx}",
                    url=url,
                    extension=ext,
                    width=w,
                    height=h,
                )
            )
        elif mtype in {"video", "gif", "animated_gif"}:
            raw_variants = rec.get("variants") if isinstance(rec.get("variants"), list) else []
            variants = _sort_mp4_variants(
                raw_variants,
                rec.get("url") if isinstance(rec.get("url"), str) else None,
                width=w,
                height=h,
                allow_http=allow_http,
            )
            if not variants:
                return None
            top = variants[0]
            items.append(
                FastMediaItem(
                    num=idx,
                    kind=ItemKind.VIDEO,
                    source_item_id=f"{tweet_id}:{idx}",
                    url=top.url,
                    extension="mp4",
                    width=w or top.width,
                    height=h or top.height,
                    duration=_pos_float(rec.get("duration")),
                    variants=tuple(variants),
                )
            )
        else:
            return None  # unknown media kind -> let gallery-dl handle
    if not items:
        if media.get("external") or tweet.get("card"):
            return None
        return FastXResult(tweet_id=tweet_id, items=[], text_only=True)
    return FastXResult(tweet_id=tweet_id, items=items, text_only=False)


def parse_syndication(data: Any, tweet_id: str, *, allow_http: bool = False) -> FastXResult | None:
    """Parse X's public `cdn.syndication.twimg.com/tweet-result` JSON for ONE exact tweet."""
    if not isinstance(data, dict):
        return None
    if data.get("__typename") == "TweetTombstone":
        return None  # let gallery-dl classify the exact restriction / deletion error
    if str(data.get("id_str") or "") != tweet_id:
        return None
    if data.get("retweeted_status"):
        return None
    details = data.get("mediaDetails")
    if not isinstance(details, list) or not details:
        if data.get("card") or data.get("video") or data.get("possibly_sensitive"):
            return None
        return FastXResult(tweet_id=tweet_id, items=[], text_only=True)
    items: list[FastMediaItem] = []
    for idx, rec in enumerate(details, 1):
        if not isinstance(rec, dict):
            continue
        mtype = str(rec.get("type") or "").lower()
        orig = rec.get("original_info") if isinstance(rec.get("original_info"), dict) else {}
        w, h = _pos_int(orig.get("width")), _pos_int(orig.get("height"))
        if mtype == "photo":
            raw_url = rec.get("media_url_https") or rec.get("media_url")
            if not isinstance(raw_url, str) or not _url_allowed(raw_url, allow_http=allow_http):
                return None
            url, ext = _photo_url_and_ext(raw_url)
            if not _SAFE_EXT.fullmatch(ext):
                return None
            items.append(
                FastMediaItem(
                    num=idx,
                    kind=ItemKind.PHOTO,
                    source_item_id=f"{tweet_id}:{idx}",
                    url=url,
                    extension=ext,
                    width=w,
                    height=h,
                )
            )
        elif mtype in {"video", "animated_gif"}:
            vinfo = rec.get("video_info") if isinstance(rec.get("video_info"), dict) else {}
            raw_variants = vinfo.get("variants") if isinstance(vinfo.get("variants"), list) else []
            variants = _sort_mp4_variants(raw_variants, None, width=w, height=h, allow_http=allow_http)
            if not variants:
                return None
            top = variants[0]
            dur_ms = _pos_float(vinfo.get("duration_millis"))
            items.append(
                FastMediaItem(
                    num=idx,
                    kind=ItemKind.VIDEO,
                    source_item_id=f"{tweet_id}:{idx}",
                    url=top.url,
                    extension="mp4",
                    width=w or top.width,
                    height=h or top.height,
                    duration=(dur_ms / 1000.0) if dur_ms else None,
                    variants=tuple(variants),
                )
            )
        else:
            return None
    if not items:
        return None
    return FastXResult(tweet_id=tweet_id, items=items, text_only=False)


class FastApiClient:
    """Pooled async HTTP fast-path metadata client (keyless, SSRF-safe, zero-subprocess)."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        dns: Resolver | None = None,
        request_timeout: float = 5.0,
        fxtwitter_base: str = FXTWITTER_BASE,
        syndication_base: str = SYNDICATION_BASE,
        allow_http: bool = False,
        connector_factory: Callable[[], aiohttp.BaseConnector] | None = None,
    ) -> None:
        self._proxy = proxy
        self._dns = dns
        self._timeout = request_timeout
        self._fx_base = fxtwitter_base.rstrip("/")
        self._syn_base = syndication_base.rstrip("/")
        self._allow_http = allow_http
        self._connector_factory = connector_factory
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._connector_factory is not None:
                connector = self._connector_factory()
            elif self._proxy:
                connector = aiohttp.TCPConnector(limit=16, keepalive_timeout=60.0, socket_factory=tuned_socket_factory)
            else:
                connector = aiohttp.TCPConnector(
                    resolver=PinnedResolver(self._dns),
                    limit=16,
                    keepalive_timeout=60.0,
                    socket_factory=tuned_socket_factory,
                )
            self._session = aiohttp.ClientSession(connector=connector, trust_env=False)
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_json(self, url: str, deadline: Deadline) -> tuple[int, Any | None]:
        deadline.check()
        cap = min(self._timeout, deadline.remaining())
        timeout = aiohttp.ClientTimeout(total=cap, sock_connect=min(3.0, cap), sock_read=cap)
        try:
            async with self._get_session().get(
                url,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
                allow_redirects=False,
                proxy=self._proxy,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    return resp.status, None
                return 200, await resp.json(content_type=None)
        except aiohttp.ClientConnectorError as exc:
            inner = exc.__cause__ or exc.__context__
            if isinstance(inner, BotError):
                raise inner from None
            return 0, None
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return 0, None

    async def discover_x(self, tweet_id: str, username: str | None, deadline: Deadline) -> FastXResult | None:
        """Try FxTwitter first, then X Syndication; return `None` to fall back to `gallery-dl`."""
        user_seg = username if (username and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username)) else "i"
        fx_status, fx_data = await self._get_json(f"{self._fx_base}/{user_seg}/status/{tweet_id}", deadline)
        fx_res = parse_fxtwitter(fx_data, tweet_id, allow_http=self._allow_http) if fx_data is not None else None
        if fx_res is not None and fx_res.items:
            return fx_res

        tok = syndication_token(tweet_id)
        syn_status, syn_data = await self._get_json(
            f"{self._syn_base}/tweet-result?id={tweet_id}&lang=en&token={tok}",
            deadline,
        )
        syn_res = parse_syndication(syn_data, tweet_id, allow_http=self._allow_http) if syn_data is not None else None
        if syn_res is not None and syn_res.items:
            return syn_res

        # Short-circuit only when BOTH endpoints positively agree (avoids burning 2-4s on gallery-dl).
        if fx_res is not None and fx_res.text_only and syn_res is not None and syn_res.text_only:
            return fx_res
        if fx_status == 404 and syn_status == 404:
            return FastXResult(tweet_id=tweet_id, items=[], not_found=True)
        return None
