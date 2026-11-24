"""T05.2 / T06.4: IP classification, DNS validation, DNS-rebinding defence (AT13).

Uses controlled fixtures only: addresses are parsed, DNS is a fake resolver. Nothing here ever
contacts a real internal service.
"""

from __future__ import annotations

import socket

import pytest

from app.core.errors import BotError, ErrorCode
from app.core.net_guard import (
    PinnedResolver,
    host_matches,
    is_ip_literal,
    is_public_ip,
    resolve_public,
)

PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "2a00:1450:4001:81b::200e",
    "::ffff:8.8.8.8",  # IPv4-mapped public
]

NOT_PUBLIC = [
    # IPv4
    "0.0.0.0",
    "127.0.0.1",
    "127.1.2.3",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "169.254.0.1",
    "100.64.0.1",  # CGNAT
    "100.100.100.200",  # Alibaba metadata
    "168.63.129.16",  # Azure wire server
    "192.0.0.192",  # Oracle metadata / IETF protocol assignments
    "198.18.0.1",
    "224.0.0.1",  # multicast
    "239.255.255.250",
    "240.0.0.1",  # reserved
    "255.255.255.255",
    # IPv6
    "::",
    "::1",
    "fe80::1",
    "fc00::1",
    "fd00:ec2::254",  # AWS IPv6 metadata
    "ff02::1",
    "2001:db8::1",  # documentation
    # IPv4 smuggled inside IPv6
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254
    "2002:7f00:0001::1",  # 6to4 -> 127.0.0.1
    "2002:0a00:0001::1",  # 6to4 -> 10.0.0.1
    "::7f00:1",  # IPv4-compatible -> 127.0.0.1
]


@pytest.mark.parametrize("ip", PUBLIC)
def test_public_addresses_are_allowed(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize("ip", NOT_PUBLIC)
def test_non_public_addresses_are_denied(ip):
    assert not is_public_ip(ip)


def test_garbage_is_not_public():
    assert not is_public_ip("not-an-ip")
    assert not is_public_ip("999.1.1.1")


def test_ip_literal_detection():
    assert is_ip_literal("127.0.0.1")
    assert is_ip_literal("[::1]")
    assert not is_ip_literal("x.com")


def test_host_matching_is_not_substring():
    assert host_matches("x.com", ["x.com"])
    assert host_matches("pbs.twimg.com", ["twimg.com"])
    assert host_matches("PBS.TWIMG.COM.", ["twimg.com"])
    assert not host_matches("eviltwimg.com", ["twimg.com"])
    assert not host_matches("twimg.com.evil.com", ["twimg.com"])
    assert not host_matches("x.com", [])


def fake(mapping: dict[str, list[str]]):
    async def resolver(host: str) -> list[str]:
        if host not in mapping:
            raise socket.gaierror("no such host")
        return mapping[host]

    return resolver


async def test_resolve_public_accepts_all_public():
    got = await resolve_public("www.tiktok.com", resolver=fake({"www.tiktok.com": ["8.8.8.8", "1.1.1.1"]}))
    assert got == ["8.8.8.8", "1.1.1.1"]


async def test_one_private_record_poisons_the_answer():
    resolver = fake({"cdn.example": ["8.8.8.8", "10.0.0.5"]})
    with pytest.raises(BotError) as exc:
        await resolve_public("cdn.example", resolver=resolver)
    assert exc.value.code is ErrorCode.INVALID_URL
    assert exc.value.reason == "unsafe_destination"


@pytest.mark.parametrize(
    "host", ["localhost", "metadata.google.internal", "foo.localhost", "db.internal", "printer.local"]
)
async def test_blocked_hostnames(host):
    with pytest.raises(BotError):
        await resolve_public(host, resolver=fake({host: ["8.8.8.8"]}))


@pytest.mark.parametrize("literal", ["127.0.0.1", "[::1]", "10.1.2.3", "169.254.169.254"])
async def test_ip_literals_must_be_public(literal):
    with pytest.raises(BotError):
        await resolve_public(literal)


async def test_nxdomain_is_not_found_not_unsafe():
    with pytest.raises(BotError) as exc:
        await resolve_public("nope.example", resolver=fake({}))
    assert exc.value.code is ErrorCode.NOT_FOUND_OR_EXPIRED


async def test_dns_rebinding_second_answer_is_never_used():
    """First lookup is public, a later lookup would be internal. The pinned resolver asks DNS
    exactly once per connection and hands the connector only what it validated."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]
    calls = 0

    async def rebinding(host: str) -> list[str]:
        nonlocal calls
        calls += 1
        return answers.pop(0)

    resolver = PinnedResolver(rebinding)
    first = await resolver.resolve("evil-but-allowed.example", 443, socket.AF_INET)
    assert [r["host"] for r in first] == ["93.184.216.34"]
    assert calls == 1
    # The connector never re-resolves; a *new* resolution that flips to loopback is rejected.
    with pytest.raises(BotError):
        await resolver.resolve("evil-but-allowed.example", 443, socket.AF_INET)


async def test_pinned_resolver_filters_by_family():
    resolver = PinnedResolver(fake({"h.example": ["8.8.8.8", "2606:4700:4700::1111"]}))
    v4 = await resolver.resolve("h.example", 443, socket.AF_INET)
    v6 = await resolver.resolve("h.example", 443, socket.AF_INET6)
    both = await resolver.resolve("h.example", 443, socket.AF_UNSPEC)
    assert [r["host"] for r in v4] == ["8.8.8.8"]
    assert [r["host"] for r in v6] == ["2606:4700:4700::1111"]
    assert len(both) == 2
    assert all(r["port"] == 443 and r["hostname"] == "h.example" for r in both)
