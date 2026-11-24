"""T02.4 / T05.2 / T06.4: safe short-link resolution (AT13): spoofed hosts, private-IP redirects,
DNS rebinding, redirect caps/loops, scheme tricks - all against controlled loopback fixtures only."""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest
from aiohttp import web

from app.core.contracts import Platform
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.url_validator import UrlKind, validate_url
from app.extractors.resolver import ShortLinkResolver

TT_POST = "https://www.tiktok.com/@someuser/video/7300000000000000000"
SHORT = "https://vm.tiktok.com/ZMabcdEfg/"


class MapResolver(aiohttp.abc.AbstractResolver):
    """Test connector resolver: every hostname maps to the loopback test server (bypasses the
    public-IP rule on purpose; the rule itself is exercised separately with PinnedResolver)."""

    def __init__(self, port: int) -> None:
        self.port = port

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": self.port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self):
        return None


class Server:
    """Tiny redirect server. `routes` maps (host, path) -> (status, Location | None)."""

    def __init__(self, routes):
        self.routes = routes
        self.hits: list[tuple[str, str]] = []
        self.runner = None
        self.port = 0

    async def handler(self, request: web.Request):
        host = request.headers.get("Host", "").split(":")[0]
        self.hits.append((host, request.path))
        status, location = self.routes.get((host, request.path), (404, None))
        headers = {"Location": location} if location else {}
        return web.Response(status=status, headers=headers, text="x")

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self.runner.cleanup()


@pytest.fixture
async def make():
    created = []

    async def _make(routes, **kw):
        srv = await Server(routes).start()
        created.append(srv)
        resolver = ShortLinkResolver(
            scheme="http",
            connector_factory=lambda: aiohttp.TCPConnector(resolver=MapResolver(srv.port)),
            **kw,
        )
        created.append(resolver)
        return srv, resolver

    yield _make
    for c in reversed(created):
        if isinstance(c, ShortLinkResolver):
            await c.aclose()
        else:
            await c.stop()


def dl():
    return Deadline(30)


async def test_post_urls_need_no_network(make):
    srv, r = await make({})
    v = validate_url(TT_POST)
    assert await r.resolve(v, dl()) is v
    assert srv.hits == []


async def test_short_link_resolves_to_the_exact_post_and_keeps_the_original(make):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (301, TT_POST + "?is_from_webapp=1&sender_device=pc")})
    submitted = validate_url(SHORT)
    out = await r.resolve(submitted, dl())
    assert out.kind is UrlKind.POST and out.post_hint == "video" and out.post_id == "7300000000000000000"
    assert out.extraction_url == TT_POST  # tracking parameters dropped for extraction...
    assert out.original_url == SHORT  # ...but the user's own link is what gets attributed
    assert srv.hits == [("vm.tiktok.com", "/ZMabcdEfg/")]


async def test_multi_hop_chain_with_relative_location(make):
    srv, r = await make(
        {
            ("vm.tiktok.com", "/ZMabcdEfg/"): (302, "https://www.tiktok.com/t/ZTmiddle1/"),
            ("www.tiktok.com", "/t/ZTmiddle1/"): (301, "/@someuser/video/7300000000000000000"),
        }
    )
    out = await r.resolve(validate_url(SHORT), dl())
    assert out.extraction_url == TT_POST and len(srv.hits) == 2


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/@someuser/video/7300000000000000000",  # not an allowed host
        "https://www.tiktok.com.evil.example/@u/video/7300000000000000000",  # suffix spoof
        "https://x.com/someuser/status/1700000000000",  # another platform
        "https://www.tiktok.com/@someuser",  # a profile, not a post
        "https://www.instagram.com/p/ABCdef12345/",
        "http://127.0.0.1:8080/admin",  # loopback literal
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "https://localhost/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "//evil.example/@u/video/7300000000000000000",  # protocol-relative to another host
        "https://user:pw@www.tiktok.com/@u/video/7300000000000000000",  # userinfo
        "https://www.tiktok.com:8443/@u/video/7300000000000000000",  # odd port
    ],
)
async def test_hostile_redirect_targets_are_rejected_without_being_requested(make, location):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (302, location)})
    with pytest.raises(BotError) as exc:
        await r.resolve(validate_url(SHORT), dl())
    assert exc.value.code in {ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_POST}
    assert srv.hits == [("vm.tiktok.com", "/ZMabcdEfg/")], "the hostile target must never be contacted"


async def test_redirect_hop_limit_is_enforced(make):
    routes = {("vm.tiktok.com", f"/ZM{i:06d}/"): (302, f"https://vm.tiktok.com/ZM{i + 1:06d}/") for i in range(20)}
    srv, r = await make(routes)
    with pytest.raises(BotError) as exc:
        await r.resolve(validate_url("https://vm.tiktok.com/ZM000000/"), dl())
    assert exc.value.reason == "redirects" and len(srv.hits) <= 6  # 1 + 5 hops, never more


async def test_redirect_loop_is_detected(make):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (302, "https://vm.tiktok.com/ZMabcdEfg/")})
    with pytest.raises(BotError) as exc:
        await r.resolve(validate_url(SHORT), dl())
    assert exc.value.reason == "redirects" and len(srv.hits) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, ErrorCode.NOT_FOUND_OR_EXPIRED),
        (410, ErrorCode.NOT_FOUND_OR_EXPIRED),
        (429, ErrorCode.RATE_LIMITED),
        (403, ErrorCode.ACCESS_RESTRICTED),
        (503, ErrorCode.DOWNLOAD_TIMEOUT),
        (200, ErrorCode.UNSUPPORTED_POST),  # an interstitial page instead of a redirect
        (302, ErrorCode.UNSUPPORTED_POST),  # a redirect with no Location header
    ],
)
async def test_non_redirect_responses_map_to_typed_errors(make, status, code):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (status, None)})
    with pytest.raises(BotError) as exc:
        await r.resolve(validate_url(SHORT), dl())
    assert exc.value.code is code


async def test_expired_deadline_stops_resolution(make):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (301, TT_POST)})
    with pytest.raises(BotError) as exc:
        await r.resolve(validate_url(SHORT), Deadline(0))
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT and srv.hits == []


async def test_instagram_share_links_resolve_like_any_other(make):
    srv, r = await make(
        {("www.instagram.com", "/share/p/BAbcdefGh/"): (302, "https://www.instagram.com/p/ABCdef12345/?igsh=x")}
    )
    out = await r.resolve(validate_url("https://www.instagram.com/share/p/BAbcdefGh/"), dl())
    assert out.platform is Platform.INSTAGRAM and out.post_hint == "post" and out.post_id == "ABCdef12345"


# --- network-level SSRF defence with the REAL pinned resolver --------------------------------------------------------
@pytest.mark.parametrize("private_ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1"])
async def test_a_short_link_host_resolving_to_a_private_address_is_never_connected_to(private_ip):
    async def dns(host):
        return [private_ip]

    r = ShortLinkResolver(dns=dns)  # real PinnedResolver, default https
    try:
        with pytest.raises(BotError) as exc:
            await r.resolve(validate_url(SHORT), dl())
        assert exc.value.code is ErrorCode.INVALID_URL and exc.value.reason == "unsafe_destination"
    finally:
        await r.aclose()


async def test_mixed_public_and_private_dns_answers_are_rejected():
    async def dns(host):
        return ["93.184.216.34", "10.0.0.7"]  # one good record must not legitimise the bad one

    r = ShortLinkResolver(dns=dns)
    try:
        with pytest.raises(BotError) as exc:
            await r.resolve(validate_url(SHORT), dl())
        assert exc.value.reason == "unsafe_destination"
    finally:
        await r.aclose()


async def test_dns_rebinding_between_resolutions_cannot_redirect_the_connection():
    """First answer public, second answer internal. Each connection resolves once and is pinned,
    so the second resolution is judged on its own and rejected."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]

    async def dns(host):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    from app.core.net_guard import PinnedResolver

    pinned = PinnedResolver(dns)
    first = await pinned.resolve("vm.tiktok.com", 443, socket.AF_INET)
    assert first[0]["host"] == "93.184.216.34"
    with pytest.raises(BotError):
        await pinned.resolve("vm.tiktok.com", 443, socket.AF_INET)


async def test_unreachable_host_is_a_retryable_network_error():
    async def dns(host):
        return ["203.0.113.1"]  # TEST-NET-3: documentation range, public-class but unroutable

    r = ShortLinkResolver(dns=dns, request_timeout=0.5)
    try:
        with pytest.raises(BotError) as exc:
            await r.resolve(validate_url(SHORT), Deadline(5))
        assert exc.value.code in {ErrorCode.DOWNLOAD_TIMEOUT, ErrorCode.INVALID_URL}
    finally:
        await r.aclose()


async def test_session_is_reused_and_closed_cleanly(make):
    srv, r = await make({("vm.tiktok.com", "/ZMabcdEfg/"): (301, TT_POST)})
    await r.resolve(validate_url(SHORT), dl())
    first = r._session
    await r.resolve(validate_url(SHORT), dl())
    assert r._session is first
    await r.aclose()
    assert first.closed
    await asyncio.sleep(0)
