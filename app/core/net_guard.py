"""Network boundary helpers (architecture section 8).

* `is_public_ip` - strict "is this address globally routable" check, including IPv4-embedding
  IPv6 forms (IPv4-mapped, NAT64, 6to4, Teredo, IPv4-compatible) and cloud metadata addresses.
* `resolve_public` - resolve a host and require EVERY returned A/AAAA record to be public.
* `PinnedResolver` - an aiohttp resolver that only ever hands the connector validated
  addresses. Because the connector dials exactly what we validated (no second DNS lookup),
  there is no rebinding window between "check" and "connect".
* `host_matches` - exact-or-subdomain suffix matching (never substring matching).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import sys
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import aiohttp.abc

from app.core.errors import BotError, ErrorCode

TCP_BUFFER_BYTES = 1 << 20  # 1 MiB (vs Windows' 64 KiB default) for high-RTT uploads/downloads
# Linux autotunes each TCP connection's buffers up to net.ipv4.tcp_wmem/tcp_rmem max (4 MiB / 6 MiB by default).
# An explicit SO_SNDBUF/SO_RCVBUF LOCKS the buffer (autotuning off) and is silently clamped to
# net.core.wmem_max/rmem_max - 208 KiB on stock kernels - so on Linux "enlarging" would actually cap a long-RTT
# upload (e.g. a VPS in Singapore -> api.telegram.org in Amsterdam, ~160 ms) to a fraction of the link.
_KERNEL_AUTOTUNES_BUFFERS = sys.platform.startswith("linux")


def tune_socket(sock: socket.socket) -> None:
    """Enable TCP_NODELAY and enlarge send/recv buffers (never shrinking larger OS defaults).

    On Linux only TCP_NODELAY is set: the kernel's per-connection autotuning already grows past 1 MiB,
    and setting a size would disable it (see `_KERNEL_AUTOTUNES_BUFFERS`).
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if _KERNEL_AUTOTUNES_BUFFERS:
        return
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        with contextlib.suppress(OSError):
            if sock.getsockopt(socket.SOL_SOCKET, opt) < TCP_BUFFER_BYTES:
                sock.setsockopt(socket.SOL_SOCKET, opt, TCP_BUFFER_BYTES)


def tuned_socket_factory(addr_info: tuple[Any, ...]) -> socket.socket:
    """aiohttp / aiohappyeyeballs socket_factory: TCP_NODELAY, plus >= 1 MiB buffers where the OS needs it."""
    sock = socket.socket(family=addr_info[0], type=addr_info[1], proto=addr_info[2])
    tune_socket(sock)
    return sock


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

Resolver = Callable[[str], Awaitable[list[str]]]

# Never reachable, whatever DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "kubernetes.default.svc",
    }
)

# Ranges that `is_global` may report as global (or that deserve an explicit rule) but that we
# never want to reach. 168.63.129.16 is the Azure wire server / metadata helper.
_EXTRA_DENY_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "168.63.129.16/32",
        "100.64.0.0/10",  # carrier-grade NAT (also Alibaba metadata 100.100.100.200)
        "192.0.0.0/24",
        "198.18.0.0/15",
        "192.88.99.0/24",
        "0.0.0.0/8",
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")
_V4_COMPAT = ipaddress.ip_network("::/96")


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    packed = addr.packed
    if addr in _NAT64:
        return ipaddress.IPv4Address(packed[12:16])
    if addr in _SIXTOFOUR:
        return ipaddress.IPv4Address(packed[2:6])
    if addr in _TEREDO:
        # Teredo stores the client IPv4 obfuscated (bitwise NOT) in the last 4 bytes.
        return ipaddress.IPv4Address(bytes(b ^ 0xFF for b in packed[12:16]))
    if addr in _V4_COMPAT and int(addr) > 1:
        return ipaddress.IPv4Address(packed[12:16])
    return None


def is_public_ip(value: str | IPAddress) -> bool:
    """True only for a globally routable, non-special-purpose address."""
    try:
        ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_LOCAL:
            return False
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_ip(embedded):
            return False
        if embedded is not None and ip.ipv4_mapped is not None:
            return True  # public IPv4 in mapped form
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
        or not ip.is_global
    ):
        return False
    return not (isinstance(ip, ipaddress.IPv4Address) and any(ip in n for n in _EXTRA_DENY_V4))


def is_ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def host_matches(host: str, suffixes: Iterable[str]) -> bool:
    """True if `host` equals a suffix or is a proper subdomain of it (never a substring match)."""
    host = host.lower().rstrip(".")
    for raw in suffixes:
        suffix = raw.lower().strip(".")
        if suffix and (host == suffix or host.endswith("." + suffix)):
            return True
    return False


async def _system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _unsafe(detail: str) -> BotError:
    return BotError(ErrorCode.INVALID_URL, detail, reason="unsafe_destination")


async def resolve_public(host: str, *, resolver: Resolver | None = None) -> list[str]:
    """Resolve `host` and return its addresses, raising unless ALL of them are public.

    Requiring every A/AAAA record to be public closes the "one good record, one internal
    record" trick; connecting only to the returned addresses closes DNS rebinding.
    """
    host = host.strip().rstrip(".").lower()
    if not host or host in BLOCKED_HOSTNAMES or host.endswith((".localhost", ".internal", ".local")):
        raise _unsafe("blocked hostname")
    if is_ip_literal(host):
        if not is_public_ip(host.strip("[]")):
            raise _unsafe("non-public IP literal")
        return [host.strip("[]")]
    try:
        addresses = await (resolver or _system_resolver)(host)
    except (OSError, UnicodeError) as exc:
        raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, f"dns failure: {type(exc).__name__}", reason="dns") from exc
    if not addresses:
        raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "no addresses", reason="dns")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise _unsafe("host resolves to a non-public address")
    return addresses


class PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that returns only validated public addresses (anti-DNS-rebinding)."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        addresses = await resolve_public(host, resolver=self._resolver)
        result: list[dict[str, Any]] = []
        for address in addresses:
            fam = socket.AF_INET6 if ":" in address else socket.AF_INET
            if family not in (socket.AF_UNSPEC, 0) and fam != family:
                continue
            result.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
            )
        if not result:
            raise _unsafe("no usable validated address for the requested family")
        return result

    async def close(self) -> None:
        return None
