"""T05.2 / T06.4 / AT13: the egress allowlist proxy, with controlled local fixtures only.

A fake resolver supplies DNS answers and a fake dialer maps the VALIDATED address to a loopback echo server, so
the policy is exercised end to end (real sockets, real CONNECT parsing) without any external host and without
ever relying on a real internal service.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from app.services.egress_proxy import DEFAULT_ALLOWED_SUFFIXES, EgressPolicy, EgressProxy

PUBLIC_IP = "93.184.216.34"


class Env:
    def __init__(self) -> None:
        self.dialed: list[tuple[str, int]] = []
        self.resolved: list[str] = []
        self.answers: dict[str, list[str]] = {}
        self.echo_port = 0
        self.proxy: EgressProxy | None = None
        self.server: asyncio.Server | None = None
        self.echo: asyncio.Server | None = None
        self.port = 0

    async def dns(self, host: str) -> list[str]:
        self.resolved.append(host)
        if host not in self.answers:
            raise socket.gaierror("nxdomain")
        return self.answers[host]

    async def dial(self, ip: str, port: int):
        self.dialed.append((ip, port))
        return await asyncio.open_connection("127.0.0.1", self.echo_port)  # the validated IP is "mapped" to loopback

    async def request(self, raw: bytes, *, read: int = 4096) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(raw)
        await w.drain()
        data = await asyncio.wait_for(r.read(read), 5)
        w.close()
        return data


@pytest.fixture
async def env():
    e = Env()

    async def echo(reader, writer):
        try:
            while data := await reader.read(4096):
                writer.write(b"echo:" + data)
                await writer.drain()
        finally:
            writer.close()

    e.echo = await asyncio.start_server(echo, "127.0.0.1", 0)
    e.echo_port = e.echo.sockets[0].getsockname()[1]
    e.answers = {"www.tiktok.com": [PUBLIC_IP], "pbs.twimg.com": [PUBLIC_IP]}
    e.proxy = EgressProxy(
        EgressPolicy(suffixes=("tiktok.com", "twimg.com"), resolver=e.dns),
        dialer=e.dial,
        idle_timeout=2,
        connect_timeout=2,
    )
    e.server = await e.proxy.start("127.0.0.1", 0)
    e.port = e.server.sockets[0].getsockname()[1]
    yield e
    e.server.close()
    e.echo.close()
    await e.server.wait_closed()


def connect(host_port: str, extra: str = "") -> bytes:
    return f"CONNECT {host_port} HTTP/1.1\r\nHost: {host_port}\r\n{extra}\r\n".encode()


async def tunnel_works(env: Env, host: str) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", env.port)
    w.write(connect(f"{host}:443"))
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    if b" 200 " not in head:
        w.close()
        return head
    w.write(b"hello")
    await w.drain()
    body = await asyncio.wait_for(r.read(100), 5)
    w.close()
    return body


# --- allowed traffic ---------------------------------------------------------------------------------------
async def test_an_allowed_host_is_tunnelled_to_the_validated_address(env):
    assert await tunnel_works(env, "www.tiktok.com") == b"echo:hello"
    assert env.dialed == [(PUBLIC_IP, 443)], "the proxy dials the exact IP it validated, not the name"
    assert env.proxy.stats.allowed == 1


async def test_subdomains_and_case_variants_of_an_allowed_domain_pass(env):
    assert await tunnel_works(env, "PBS.TWIMG.COM") == b"echo:hello"


# --- denied traffic: nothing is dialled -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("evil.example.com:443", "host_not_allowed"),
        ("tiktok.com.evil.example:443", "host_not_allowed"),  # suffix spoof
        ("eviltiktok.com:443", "host_not_allowed"),  # substring spoof
        ("www.tiktok.com:80", "port_not_allowed"),
        ("www.tiktok.com:8443", "port_not_allowed"),
        ("93.184.216.34:443", "ip_literal"),
        ("127.0.0.1:443", "ip_literal"),
        ("169.254.169.254:443", "ip_literal"),
        ("[::1]:443", "ip_literal"),
        ("localhost:443", "host_not_allowed"),
        ("metadata.google.internal:443", "host_not_allowed"),
    ],
)
async def test_disallowed_destinations_get_403_and_are_never_dialled(env, caplog, target, reason):
    caplog.set_level(logging.WARNING, logger="app.egress")
    reply = await env.request(connect(target))
    assert reply.startswith(b"HTTP/1.1 403")
    assert env.dialed == [], "a denied destination must never be connected to"
    assert env.proxy.stats.reasons.get(reason) == 1
    assert any(reason in r.getMessage() or reason in str(getattr(r, "fields", "")) for r in caplog.records)


async def test_hostname_policy_is_checked_before_any_dns_lookup(env):
    await env.request(connect("not-allowed.example.org:443"))
    assert env.resolved == [], "the proxy must not be usable to resolve arbitrary names"


@pytest.mark.parametrize(
    "bad_ip", ["127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.169.254", "::1", "fd00::5", "100.64.0.1"]
)
async def test_an_allowed_name_resolving_to_an_internal_address_is_refused(env, bad_ip):
    env.answers["www.tiktok.com"] = [bad_ip]
    reply = await env.request(connect("www.tiktok.com:443"))
    assert reply.startswith(b"HTTP/1.1 403") and env.dialed == []
    assert env.proxy.stats.reasons == {"unsafe_destination": 1}


async def test_a_single_internal_record_among_public_ones_poisons_the_answer(env):
    env.answers["www.tiktok.com"] = [PUBLIC_IP, "10.0.0.8"]
    assert (await env.request(connect("www.tiktok.com:443"))).startswith(b"HTTP/1.1 403")
    assert env.dialed == []


async def test_nxdomain_is_a_gateway_error_not_a_crash(env):
    env.proxy.policy.suffixes = ("tiktok.com", "twimg.com", "gone.example")
    assert (await env.request(connect("a.gone.example:443"))).startswith(b"HTTP/1.1 502")


async def test_dns_rebinding_cannot_change_where_an_authorised_connection_goes(env):
    """Each CONNECT resolves ONCE and dials that answer. A later flip to an internal address only affects
    the NEXT request, which is then judged (and refused) on its own."""
    answers = [[PUBLIC_IP], ["127.0.0.1"]]

    async def flip(host):
        return answers.pop(0)

    env.proxy.policy.resolver = flip
    assert await tunnel_works(env, "www.tiktok.com") == b"echo:hello"
    assert env.dialed == [(PUBLIC_IP, 443)]
    assert (await env.request(connect("www.tiktok.com:443"))).startswith(b"HTTP/1.1 403")
    assert env.dialed == [(PUBLIC_IP, 443)]  # still only the first, validated address


# --- protocol hygiene ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        b"GET http://www.tiktok.com/ HTTP/1.1\r\nHost: www.tiktok.com\r\n\r\n",
        b"POST http://www.tiktok.com/ HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
    ],
)
async def test_plain_http_proxying_is_refused(env, raw):
    assert (await env.request(raw)).startswith(b"HTTP/1.1 405") and env.dialed == [] and env.resolved == []


@pytest.mark.parametrize(
    "raw",
    [
        b"\r\n\r\n",
        b"CONNECT\r\n\r\n",
        b"CONNECT www.tiktok.com:443\r\n\r\n",
        b"CONNECT www.tiktok.com:443 FTP/1.0\r\n\r\n",
        b"CONNECT www.tiktok.com:99999 HTTP/1.1\r\n\r\n",
        b"CONNECT www.tiktok.com HTTP/1.1\r\n\r\n",
        b"CONNECT www.tiktok.com:443/path HTTP/1.1\r\n\r\n",
        b"CONNECT user@www.tiktok.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT www.tiktok.com:443 HTTP/1.1\r\n" + b"X: " + b"a" * 20000 + b"\r\n\r\n",
    ],
)
async def test_malformed_requests_get_an_error_and_never_reach_the_network(env, raw):
    reply = await env.request(raw)
    assert reply.startswith((b"HTTP/1.1 400", b"HTTP/1.1 403", b"HTTP/1.1 431")) or reply == b""
    assert env.dialed == []


async def test_a_client_that_never_finishes_its_headers_is_dropped(env, monkeypatch):
    monkeypatch.setattr("app.services.egress_proxy.HEADER_TIMEOUT", 0.3)
    r, w = await asyncio.open_connection("127.0.0.1", env.port)
    w.write(b"CONNECT www.tiktok.com:443 HTTP/1.1\r\nHost: x\r\n")  # no terminating blank line
    await w.drain()
    reply = await asyncio.wait_for(r.read(200), 3)
    assert reply.startswith(b"HTTP/1.1 400")
    w.close()


async def test_unreachable_upstream_is_a_502(env):
    async def refuse(ip, port):
        raise ConnectionRefusedError

    env.proxy._dial = refuse
    assert (await env.request(connect("www.tiktok.com:443"))).startswith(b"HTTP/1.1 502")


async def test_idle_tunnels_are_closed(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.port)
    w.write(connect("www.tiktok.com:443"))
    await w.drain()
    await r.readuntil(b"\r\n\r\n")
    assert await asyncio.wait_for(r.read(10), 6) == b""  # the proxy hung up after the 2 s idle limit
    w.close()


async def test_connection_cap_sheds_load_with_503():
    async def hold(ip, port):
        await asyncio.sleep(10)

    proxy = EgressProxy(
        EgressPolicy(suffixes=("tiktok.com",), resolver=lambda h: asyncio.sleep(0, [PUBLIC_IP])), dialer=hold,
        max_connections=1, connect_timeout=5,
    )  # fmt: skip
    server = await proxy.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(connect("www.tiktok.com:443"))
        await w1.drain()
        await asyncio.sleep(0.3)  # the first request now occupies the only slot
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        assert (await asyncio.wait_for(r2.read(100), 3)).startswith(b"HTTP/1.1 503")
        w1.close()
        w2.close()
    finally:
        server.close()


async def test_a_denied_request_does_not_disturb_other_tunnels(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.port)
    w.write(connect("www.tiktok.com:443"))
    await w.drain()
    await r.readuntil(b"\r\n\r\n")
    await env.request(connect("evil.example:443"))
    w.write(b"still alive")
    await w.drain()
    assert await asyncio.wait_for(r.read(100), 3) == b"echo:still alive"
    w.close()


# --- the shipped default allowlist ----------------------------------------------------------------------------------
def test_default_allowlist_contains_only_platform_domains():
    assert {"tiktok.com", "instagram.com", "x.com", "twitter.com", "twimg.com", "cdninstagram.com"} <= set(
        DEFAULT_ALLOWED_SUFFIXES
    )
    assert not [
        s for s in DEFAULT_ALLOWED_SUFFIXES if s in {"com", "net", "org", "co", "io"} or s.startswith(".") or "*" in s
    ]
    assert all(s == s.lower() and "." in s for s in DEFAULT_ALLOWED_SUFFIXES)
