"""Safe short-link resolution (T02.4, FR-03, architecture section 8).

Runs on the worker side so the process holding the bot token never fetches third-party URLs.

For a short link (`vm.tiktok.com/...`, `/t/...`, `instagram.com/share/...`):
* redirects are NEVER followed automatically - each hop is requested with `allow_redirects=False`;
* every hop's URL must pass the full validator again (exact allowed host, supported path, same
  platform as the original link), so a redirect to `evil.com`, another platform, a profile page,
  `file://` or an IP literal is rejected before any request is made to it;
* at most 5 hops, no loops;
* connections go through `PinnedResolver` (or the egress proxy), so every address is validated
  as public and DNS cannot be rebound between check and connect;
* only the final, exact post URL leaves this module; the submitted URL is kept for attribution.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from urllib.parse import urljoin

import aiohttp

from app.core.contracts import Platform
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.net_guard import PinnedResolver, Resolver
from app.core.url_validator import UrlKind, ValidatedUrl, validate_resolved, validate_url

MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; telegram-media-downloader)"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ShortLinkResolver:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        extra_hosts: dict[str, Platform] | None = None,
        dns: Resolver | None = None,
        max_redirects: int = MAX_REDIRECTS,
        request_timeout: float = 10.0,
        scheme: str = "https",
        connector_factory: Callable[[], aiohttp.BaseConnector] | None = None,
    ) -> None:
        self._proxy = proxy
        self._extra = extra_hosts
        self._dns = dns
        self._max = max_redirects
        self._timeout = request_timeout
        self._scheme = scheme  # production is always https; tests use http against a loopback server
        self._connector_factory = connector_factory
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._connector_factory is not None:
                connector = self._connector_factory()
            elif self._proxy:
                connector = aiohttp.TCPConnector()  # the egress proxy validates and pins destinations
            else:
                connector = aiohttp.TCPConnector(resolver=PinnedResolver(self._dns))
            self._session = aiohttp.ClientSession(connector=connector, trust_env=False)
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def resolve(self, validated: ValidatedUrl, deadline: Deadline) -> ValidatedUrl:
        if validated.kind is UrlKind.POST:
            return validated
        platform = validated.platform
        current = validated.extraction_url
        seen: set[str] = set()
        for _ in range(self._max + 1):
            deadline.check()
            hop = validate_url(current, extra_hosts=self._extra)  # exact host + supported path, every time
            if hop.platform is not platform:
                raise BotError(ErrorCode.INVALID_URL, "redirect left the original platform", reason="platform_mismatch")
            if hop.kind is UrlKind.POST:
                final = validate_resolved(current, platform, extra_hosts=self._extra)
                return dataclasses.replace(final, original_url=validated.original_url)  # keep attribution
            if hop.extraction_url in seen:
                raise BotError(ErrorCode.INVALID_URL, "redirect loop", reason="redirects")
            seen.add(hop.extraction_url)
            location = await self._hop(hop.extraction_url, deadline)
            current = urljoin(hop.extraction_url, location)
        raise BotError(ErrorCode.INVALID_URL, "too many redirects", reason="redirects")

    async def _hop(self, url: str, deadline: Deadline) -> str:
        request_url = url if self._scheme == "https" else url.replace("https://", f"{self._scheme}://", 1)
        timeout = aiohttp.ClientTimeout(total=deadline.cap(self._timeout))
        try:
            async with self._get_session().get(
                request_url,
                allow_redirects=False,
                timeout=timeout,
                proxy=self._proxy,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            ) as resp:
                status = resp.status
                location = resp.headers.get("Location")
        except BotError:
            raise
        except aiohttp.ClientConnectorError as exc:
            # aiohttp wraps exceptions raised by the resolver; unwrap our own typed rejections.
            inner = exc.__cause__ or exc.__context__
            if isinstance(inner, BotError):
                raise inner from None
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "connection failed", reason="network") from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "short link request failed", reason="network") from None
        if status in _REDIRECT_STATUSES and location:
            return location
        if status == 429:
            raise BotError(ErrorCode.RATE_LIMITED, "short link: 429")
        if status in {401, 403}:
            raise BotError(ErrorCode.ACCESS_RESTRICTED, f"short link: {status}")
        if status in {404, 410}:
            raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, f"short link: {status}")
        if status >= 500:
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, f"short link: {status}", reason="network")
        # 200 without a redirect (JS/meta redirects, interstitials) or a redirect with no Location:
        raise BotError(
            ErrorCode.UNSUPPORTED_POST, f"short link did not redirect (status {status})", reason="unresolved"
        )
