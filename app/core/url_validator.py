"""URL extraction and validation (T02.4, FR-02, FR-03, architecture section 8).

Purely syntactic: no DNS, no network. Safe short-link *resolution* happens inside the extractor
worker (see `app.extractors.resolver`), so the process that holds the bot token never fetches
third-party URLs.

Design rules enforced here:

* exactly one http(s) URL per message (embedded in text is fine);
* exact-host allowlist (never substring/suffix checks), no userinfo, no nonstandard ports,
  no IP literals, ASCII hosts only (blocks homograph spoofing), no control chars/backslashes;
* only individual item paths are accepted - profiles, Highlights, hashtags, Story trays etc.
  are rejected;
* the exact submitted URL is preserved for attribution and kept strictly separate from the
  normalised extraction URL (https, canonical host, no query/fragment).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from app.core.contracts import Platform
from app.core.errors import BotError, ErrorCode

MAX_URL_LENGTH = 2048

# Exact hostnames. `EXTRA_HOSTS` (config) may add explicitly approved variants.
DEFAULT_HOSTS: dict[str, Platform] = {
    "tiktok.com": Platform.TIKTOK,
    "www.tiktok.com": Platform.TIKTOK,
    "m.tiktok.com": Platform.TIKTOK,
    "vm.tiktok.com": Platform.TIKTOK,
    "vt.tiktok.com": Platform.TIKTOK,
    "instagram.com": Platform.INSTAGRAM,
    "www.instagram.com": Platform.INSTAGRAM,
    "m.instagram.com": Platform.INSTAGRAM,
    "x.com": Platform.X,
    "www.x.com": Platform.X,
    "mobile.x.com": Platform.X,
    "twitter.com": Platform.X,
    "www.twitter.com": Platform.X,
    "mobile.twitter.com": Platform.X,
}
_SHORT_HOSTS = frozenset({"vm.tiktok.com", "vt.tiktok.com"})

_CANONICAL_HOST = {
    Platform.TIKTOK: "www.tiktok.com",
    Platform.INSTAGRAM: "www.instagram.com",
    Platform.X: "x.com",
}

# Candidate-URL tokenisation inside free text. Quotes/guillemets terminate a URL.
_URL_TOKEN = re.compile(r"https?://[^\s<>\"'`“”‘’«»]+", re.IGNORECASE)
_TRAILING = ".,;:!?"

_TT_ITEM = re.compile(
    r"^/@(?P<user>[A-Za-z0-9_.\-]{1,64})/(?P<kind>video|photo)/(?P<id>\d{6,25})/?$"
)
_TT_SHORT_PATH = re.compile(r"^/(?P<code>[A-Za-z0-9]{5,32})/?$")
_TT_T_SHORT = re.compile(r"^/t/(?P<code>[A-Za-z0-9]{5,32})/?$")

_IG_POST = re.compile(
    r"^/(?:(?P<user>[A-Za-z0-9._]{1,30})/)?(?P<kind>p|reel|reels|tv)/(?P<code>[A-Za-z0-9_\-]{5,32})/?$"
)
_IG_STORY = re.compile(r"^/stories/(?P<user>[A-Za-z0-9._]{1,30})/(?P<id>\d{8,25})/?$")
_IG_STORY_NO_ID = re.compile(r"^/stories/(?P<user>[A-Za-z0-9._]{1,30})/?$")
_IG_HIGHLIGHT = re.compile(r"^/stories/highlights(?:/|$)")
_IG_SHARE = re.compile(r"^/share/(?:(?:p|reel|reels)/)?(?P<code>[A-Za-z0-9_\-]{5,40})/?$")

_X_STATUS = re.compile(
    r"^/(?P<user>[A-Za-z0-9_]{1,15}|i(?:/web)?)/status/(?P<id>\d{5,25})"
    r"(?:/(?:photo|video)/\d{1,2})?/?$"
)


class UrlKind(StrEnum):
    POST = "post"
    SHORT_LINK = "short_link"


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    original_url: str  # exact submitted URL - used only for attribution
    extraction_url: str  # normalised: https, canonical host, no query/fragment
    platform: Platform
    kind: UrlKind
    post_hint: str | None  # video | photo | reel | post | story | tweet | None (short link)
    post_id: str | None
    username: str | None


def _invalid(detail: str, reason: str = "invalid") -> BotError:
    return BotError(ErrorCode.INVALID_URL, detail, reason=reason)


def _unsupported(detail: str, reason: str = "unsupported") -> BotError:
    return BotError(ErrorCode.UNSUPPORTED_POST, detail, reason=reason)


def _strip_trailing(token: str) -> str:
    token = token.rstrip(_TRAILING)
    pairs = {")": "(", "]": "[", "}": "{"}
    while token and token[-1] in pairs and token.count(token[-1]) > token.count(pairs[token[-1]]):
        token = token[:-1].rstrip(_TRAILING)
    return token


def find_urls(text: str, entity_urls: Iterable[str] = ()) -> list[str]:
    """All distinct http(s) URLs found in message text plus any Telegram entity URLs."""
    found: list[str] = []
    for token in _URL_TOKEN.findall(text or ""):
        cleaned = _strip_trailing(token)
        if cleaned:
            found.append(cleaned)
    for extra in entity_urls:
        if extra:
            found.append(extra.strip())
    return list(dict.fromkeys(found))


def parse_hosts_config(pairs: Iterable[str]) -> dict[str, Platform]:
    """Parse `host=platform` pairs from configuration into extra allowed hosts."""
    extra: dict[str, Platform] = {}
    for pair in pairs:
        host, sep, platform = pair.partition("=")
        host = host.strip().lower()
        if not sep or not host or "/" in host or ":" in host or "@" in host:
            raise ValueError(f"invalid EXTRA_ALLOWED_HOSTS entry: {pair!r}")
        try:
            extra[host] = Platform(platform.strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown platform in EXTRA_ALLOWED_HOSTS entry: {pair!r}") from exc
    return extra


def validate_url(url: str, *, extra_hosts: dict[str, Platform] | None = None) -> ValidatedUrl:
    """Validate ONE candidate URL string. Raises `BotError` (INVALID_URL / UNSUPPORTED_POST)."""
    original = url
    if not url or len(url) > MAX_URL_LENGTH:
        raise _invalid("empty or over-long url", "too_long")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url) or "\\" in url:
        raise _invalid("control characters, whitespace or backslash in url", "malformed")

    try:
        parts = urlsplit(url)
        port = parts.port  # may raise ValueError on garbage ports
    except ValueError as exc:
        raise _invalid("unparseable url", "malformed") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _invalid("non-http(s) scheme", "scheme")
    netloc = parts.netloc
    if "@" in netloc:
        raise _invalid("credentials/userinfo in url", "userinfo")
    expected_port = 443 if scheme == "https" else 80
    if port is not None and port != expected_port:
        raise _invalid("nonstandard port", "port")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not host.isascii():
        raise _invalid("missing or non-ascii host", "host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid("ip literal host", "ip_literal")

    hosts = {**DEFAULT_HOSTS, **(extra_hosts or {})}
    platform = hosts.get(host)  # exact match only
    if platform is None:
        raise _invalid("host not allowed", "host_not_allowed")

    path = parts.path or "/"
    canonical = _CANONICAL_HOST[platform]

    if platform is Platform.TIKTOK:
        return _validate_tiktok(original, host, path, canonical)
    if platform is Platform.INSTAGRAM:
        return _validate_instagram(original, path, canonical)
    return _validate_x(original, path, canonical)


def _validate_tiktok(original: str, host: str, path: str, canonical: str) -> ValidatedUrl:
    if host in _SHORT_HOSTS:
        m = _TT_SHORT_PATH.match(path)
        if not m:
            raise _unsupported("not a tiktok short link", "tiktok_short")
        return ValidatedUrl(
            original, f"https://{host}/{m['code']}/", Platform.TIKTOK, UrlKind.SHORT_LINK,
            None, None, None,
        )
    if m := _TT_T_SHORT.match(path):
        return ValidatedUrl(
            original, f"https://{canonical}/t/{m['code']}/", Platform.TIKTOK, UrlKind.SHORT_LINK,
            None, None, None,
        )
    if m := _TT_ITEM.match(path):
        return ValidatedUrl(
            original,
            f"https://{canonical}/@{m['user']}/{m['kind']}/{m['id']}",
            Platform.TIKTOK, UrlKind.POST, m["kind"], m["id"], m["user"],
        )
    raise _unsupported("tiktok path is not an individual video/photo post", "tiktok_path")


def _validate_instagram(original: str, path: str, canonical: str) -> ValidatedUrl:
    if _IG_HIGHLIGHT.match(path):
        raise _unsupported("highlights are not supported", "highlights_unsupported")
    if m := _IG_STORY.match(path):
        return ValidatedUrl(
            original, f"https://{canonical}/stories/{m['user']}/{m['id']}/",
            Platform.INSTAGRAM, UrlKind.POST, "story", m["id"], m["user"],
        )
    if _IG_STORY_NO_ID.match(path):
        raise _unsupported("story link without an item id", "story_id_required")
    if m := _IG_SHARE.match(path):
        return ValidatedUrl(
            original, f"https://{canonical}{path if path.endswith('/') else path + '/'}",
            Platform.INSTAGRAM, UrlKind.SHORT_LINK, None, None, None,
        )
    if m := _IG_POST.match(path):
        hint = "post" if m["kind"] == "p" else "reel"
        return ValidatedUrl(
            original, f"https://{canonical}/{m['kind']}/{m['code']}/",
            Platform.INSTAGRAM, UrlKind.POST, hint, m["code"], m["user"],
        )
    raise _unsupported("instagram path is not an individual post, reel or story", "ig_path")


def _validate_x(original: str, path: str, canonical: str) -> ValidatedUrl:
    if m := _X_STATUS.match(path):
        user = m["user"]
        username = None if user in {"i", "i/web"} else user
        return ValidatedUrl(
            original, f"https://{canonical}/{user}/status/{m['id']}",
            Platform.X, UrlKind.POST, "tweet", m["id"], username,
        )
    raise _unsupported("x path is not an individual status", "x_path")


def parse_message_url(
    text: str,
    entity_urls: Iterable[str] = (),
    *,
    extra_hosts: dict[str, Platform] | None = None,
) -> ValidatedUrl:
    """Extract exactly one URL from a message and validate it (FR-02)."""
    urls = find_urls(text, entity_urls)
    if not urls:
        raise _invalid("no url in message", "no_url")
    if len(urls) > 1:
        raise _invalid("multiple urls in message", "multiple_urls")
    return validate_url(urls[0], extra_hosts=extra_hosts)


def validate_resolved(
    resolved: str, expected: Platform, *, extra_hosts: dict[str, Platform] | None = None
) -> ValidatedUrl:
    """Validate the destination of a resolved short link: same platform, exact post path."""
    result = validate_url(resolved, extra_hosts=extra_hosts)
    if result.platform is not expected:
        raise _invalid("short link resolved to a different platform", "platform_mismatch")
    if result.kind is not UrlKind.POST:
        raise _unsupported("short link did not resolve to an individual post", "unresolved")
    return result
