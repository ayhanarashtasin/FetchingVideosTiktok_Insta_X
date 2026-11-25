"""Allowlisting egress proxy (T05.2, architecture section 8): the extractor worker's ONLY way out.

    python -m app.services.egress_proxy        # EGRESS_ALLOWED_SUFFIXES, EGRESS_LISTEN_HOST/PORT

An optional allowlisting proxy for extractor tools pointed at it via `HTTPS_PROXY` / `--proxy`
(e.g. a separate worker process with restricted outbound networking). For each `CONNECT host:port`
the proxy:

1. accepts only CONNECT to port 443 (plain-HTTP proxying is refused);
2. refuses IP literals and any hostname that is not EXACTLY an allowed domain or a proper subdomain of one
   (checked BEFORE any DNS lookup, so it cannot be used to probe arbitrary names);
3. resolves the name and requires EVERY A/AAAA answer to be a public address (no loopback, private,
   link-local, CGNAT, cloud-metadata, IPv4-in-IPv6 tricks - see `core.net_guard`);
4. dials the exact IP it validated (never the name again), so DNS cannot be rebound between check and connect;
5. then blindly relays bytes - TLS stays end to end between the tool and the platform.

Denied hosts are logged (host name only, never a path or query) so the allowlist can be reviewed and extended
after live runs: the default list is a best-effort starting point, NOT yet verified against live traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.errors import BotError
from app.core.net_guard import Resolver, host_matches, is_ip_literal, resolve_public, tune_socket
from app.logging_setup import log_event, setup_logging

log = logging.getLogger("app.egress")
RELAY_CHUNK_BYTES = 262_144

DEFAULT_ALLOWED_SUFFIXES: tuple[str, ...] = (
    # TikTok (pages, API, media CDNs)
    "tiktok.com", "tiktokv.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "muscdn.com",
    "byteoversea.com", "ibytedtos.com", "ibyteimg.com",
    # Instagram (pages, API, media CDNs)
    "instagram.com", "cdninstagram.com", "fbcdn.net",
    # X / Twitter
    "x.com", "twitter.com", "twimg.com",
)  # fmt: skip
MAX_HEADER_BYTES = 8192
HEADER_TIMEOUT = 10.0
_TARGET = re.compile(r"^(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+):(?P<port>\d{1,5})$")

Dialer = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class Denied(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class EgressPolicy:
    suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES
    ports: frozenset[int] = frozenset({443})
    resolver: Resolver | None = None

    async def authorize(self, host: str, port: int) -> list[str]:
        """Validated addresses to dial, or raise `Denied`. Order matters: cheap string checks first, DNS last."""
        host = host.lower().rstrip(".")
        if port not in self.ports:
            raise Denied(403, "port_not_allowed")
        if is_ip_literal(host):
            raise Denied(403, "ip_literal")
        if not host_matches(host, self.suffixes):
            raise Denied(403, "host_not_allowed")
        try:
            return await resolve_public(host, resolver=self.resolver)
        except BotError as err:
            raise Denied(403 if err.reason == "unsafe_destination" else 502, err.reason or "dns") from None


@dataclass
class Stats:
    allowed: int = 0
    denied: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


class EgressProxy:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        max_connections: int = 64,
        connect_timeout: float = 10.0,
        idle_timeout: float = 120.0,
        max_duration: float = 900.0,
        dialer: Dialer | None = None,
    ) -> None:
        self.policy = policy
        self.stats = Stats()
        self._sem = asyncio.Semaphore(max_connections)
        self._connect_timeout = connect_timeout
        self._idle = idle_timeout
        self._max_duration = max_duration
        self._dial = dialer or self._default_dial

    @staticmethod
    async def _default_dial(ip: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(ip, port)

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEADER_BYTES * 2)

    # ------------------------------------------------------------------ one client connection
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._sem.locked():
                await self._reply(writer, 503, "Service Unavailable")
                return
            async with self._sem:
                await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log_event(log, logging.ERROR, "egress proxy error", error_type=type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT)
        except (asyncio.LimitOverrunError, TimeoutError, asyncio.IncompleteReadError):
            await self._reply(writer, 400, "Bad Request")
            return
        if len(head) > MAX_HEADER_BYTES:
            await self._reply(writer, 431, "Request Header Fields Too Large")
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = request_line.split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            await self._reply(writer, 400, "Bad Request")
            return
        method, target = parts[0].upper(), parts[1]
        if method != "CONNECT":
            await self._deny(writer, 405, "method_not_allowed", host="-")
            return
        m = _TARGET.fullmatch(target)
        if not m or int(m["port"]) > 65535:
            await self._deny(writer, 400, "bad_target", host="-")
            return
        host, port = m["host"].strip("[]"), int(m["port"])
        try:
            addresses = await self.policy.authorize(host, port)
        except Denied as d:
            await self._deny(writer, d.status, d.reason, host=host)
            return
        upstream = await self._connect(addresses, port)
        if upstream is None:
            await self._deny(writer, 502, "upstream_unreachable", host=host)
            return
        up_reader, up_writer = upstream
        for w in (writer, up_writer):
            sock = w.get_extra_info("socket")
            if sock is not None:
                tune_socket(sock)
        self.stats.allowed += 1
        log_event(log, logging.DEBUG, "egress allowed", host=host)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.wait_for(self._pipe(reader, writer, up_reader, up_writer), self._max_duration)
        except TimeoutError:
            pass
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _connect(self, addresses: list[str], port: int):
        for ip in addresses:  # only addresses that passed the public-address check are ever dialled
            try:
                return await asyncio.wait_for(self._dial(ip, port), self._connect_timeout)
            except (OSError, TimeoutError):
                continue
        return None

    async def _pipe(self, c_r, c_w, u_r, u_w) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await asyncio.wait_for(src.read(RELAY_CHUNK_BYTES), self._idle)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (TimeoutError, ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.write_eof() if dst.can_write_eof() else dst.close()

        tasks = [asyncio.create_task(pump(c_r, u_w)), asyncio.create_task(pump(u_r, c_w))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ responses
    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, text: str) -> None:
        writer.write(f"HTTP/1.1 {status} {text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _deny(self, writer: asyncio.StreamWriter, status: int, reason: str, *, host: str) -> None:
        self.stats.denied += 1
        self.stats.reasons[reason] = self.stats.reasons.get(reason, 0) + 1
        log_event(log, logging.WARNING, "egress denied", host=host[:200], reason=reason)
        await self._reply(writer, status, "Forbidden" if status == 403 else "Error")


DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 3128


def _suffixes_from_env() -> tuple[str, ...]:
    raw = os.environ.get("EGRESS_ALLOWED_SUFFIXES", "").strip()
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip()) or DEFAULT_ALLOWED_SUFFIXES


async def _amain() -> None:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    proxy = EgressProxy(EgressPolicy(suffixes=_suffixes_from_env()))
    host = os.environ.get("EGRESS_LISTEN_HOST", DEFAULT_LISTEN_HOST)
    port = int(os.environ.get("EGRESS_LISTEN_PORT", str(DEFAULT_LISTEN_PORT)))
    server = await proxy.start(host, port)
    log_event(log, logging.INFO, "egress proxy listening", port=port, suffixes=len(proxy.policy.suffixes))
    async with server:
        await server.serve_forever()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
