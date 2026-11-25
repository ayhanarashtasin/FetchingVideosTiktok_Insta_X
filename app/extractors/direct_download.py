"""Worker-side direct media downloader for already-discovered CDN URLs.

Eliminates the second extractor subprocess + second platform API/page scrape whenever `discover()`
already returned direct media URLs:
* downloads multi-item carousels concurrently over a pooled keep-alive `aiohttp` session;
* splits large files (> 1 MiB) across parallel HTTP Range connections with zero extra HEAD RTT;
* stays strictly inside the worker, honours `egress_proxy_url` (or `PinnedResolver` when direct),
  validates every redirect hop manually, enforces byte/disk budgets and deadlines, and never leaks
  CDN URLs to logs or wire contracts.

Any non-budget / non-deadline HTTP failure raises `DirectDownloadUnavailable` so the caller falls
back seamlessly to `gallery-dl` or `yt-dlp`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.net_guard import PinnedResolver, Resolver, is_ip_literal, tuned_socket_factory

DEFAULT_USER_AGENT = "Mozilla/5.0"
MAX_REDIRECTS = 5
MAX_ITEM_CONCURRENCY = 4
RANGE_FIRST_BYTES = 1 << 20  # 1 MiB: small photos/clips finish in a single request
RANGE_MIN_PART_BYTES = 512 << 10  # 512 KiB minimum slice per parallel range connection
RANGE_CONCURRENCY = 4
STREAM_CHUNK_BYTES = 256 << 10
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)
_BAD_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


class DirectDownloadUnavailable(Exception):
    """Direct CDN fetch did not succeed; caller should fall back to the extractor tool."""


class FileTooLargeDirect(Exception):
    """Server declared or streamed a file larger than the per-file upload cap."""

    def __init__(self, actual_bytes: int) -> None:
        super().__init__(f"{actual_bytes} bytes exceeds upload cap")
        self.actual_bytes = actual_bytes


@dataclass(frozen=True)
class DirectTarget:
    key: int
    url: str
    dest: Path
    headers: Mapping[str, str] = field(default_factory=dict)


class _BudgetTracker:
    """Tracks cumulative bytes across concurrent downloads in one job."""

    def __init__(self, *, max_bytes: int, min_free_bytes: int, watch_dir: Path) -> None:
        self._max = max_bytes
        self._min_free = min_free_bytes
        self._dir = watch_dir
        self._used = 0

    def reserve(self, n: int) -> None:
        if n < 0:
            return
        if self._used + n > self._max:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "download exceeded the byte budget")
        if self._min_free > 0 and shutil.disk_usage(self._dir).free - n < self._min_free:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "insufficient free disk space")
        self._used += n

    def add_streamed(self, n: int) -> None:
        self._used += n
        if self._used > self._max:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "download exceeded the byte budget")
        if self._min_free > 0 and shutil.disk_usage(self._dir).free < self._min_free:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "insufficient free disk space")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])


def _write_at(fd: int, offset: int, data: bytes, lock: threading.Lock) -> None:
    if hasattr(os, "pwrite"):
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.pwrite(fd, view[written:], offset + written)
        return
    with lock:  # Windows: pwrite unavailable
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])


class DirectMediaDownloader:
    """Pooled, SSRF-safe concurrent/ranged CDN downloader living inside the worker."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        dns: Resolver | None = None,
        max_bytes: int = 4_000_000_000,
        min_free_bytes: int = 0,
        request_timeout: float = 30.0,
        allow_http: bool = False,
        connector_factory: Callable[[], aiohttp.BaseConnector] | None = None,
    ) -> None:
        self._proxy = proxy
        self._dns = dns
        self._max_bytes = max_bytes
        self._min_free = min_free_bytes
        self._request_timeout = request_timeout
        self._allow_http = allow_http
        self._connector_factory = connector_factory
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._connector_factory is not None:
                connector = self._connector_factory()
            elif self._proxy:
                connector = aiohttp.TCPConnector(
                    limit=24,
                    keepalive_timeout=60.0,
                    socket_factory=tuned_socket_factory,
                )
            else:
                connector = aiohttp.TCPConnector(
                    resolver=PinnedResolver(self._dns),
                    limit=24,
                    keepalive_timeout=60.0,
                    socket_factory=tuned_socket_factory,
                )
            self._session = aiohttp.ClientSession(connector=connector, trust_env=False)
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ validation
    def _validate_url(self, url: str) -> str:
        if not url or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
            raise DirectDownloadUnavailable("malformed media url")
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        allowed_schemes = ("https", "http") if self._allow_http else ("https",)
        if scheme not in allowed_schemes:
            raise DirectDownloadUnavailable("disallowed media scheme")
        if "@" in parts.netloc:
            raise DirectDownloadUnavailable("userinfo in media url")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or not host.isascii():
            raise DirectDownloadUnavailable("invalid media host")
        if not self._allow_http and is_ip_literal(host):
            raise DirectDownloadUnavailable("ip literal media host")
        try:
            port = parts.port
        except ValueError as exc:
            raise DirectDownloadUnavailable("invalid port") from exc
        if not self._allow_http and port is not None and port != 443:
            raise DirectDownloadUnavailable("nonstandard port")
        return url

    @staticmethod
    def _headers(extra: Mapping[str, str], *, range_header: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
        for k, v in extra.items():
            if k.lower() != "range":
                headers[k] = v
        if range_header is not None:
            headers["Range"] = range_header
        return headers

    @staticmethod
    def _check_content_type(resp: aiohttp.ClientResponse) -> None:
        ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if any(ctype.startswith(bad) for bad in _BAD_CONTENT_TYPES):
            raise DirectDownloadUnavailable(f"unexpected content-type: {ctype}")

    # ------------------------------------------------------------------ public
    async def download_many(
        self,
        targets: Sequence[DirectTarget],
        workspace: Path,
        deadline: Deadline,
        *,
        max_file_bytes: int | None = None,
    ) -> dict[int, Path]:
        """Download all `targets` concurrently. Raises `DirectDownloadUnavailable` on fallbackable errors."""
        if not targets:
            return {}
        budget = _BudgetTracker(max_bytes=self._max_bytes, min_free_bytes=self._min_free, watch_dir=workspace)
        if self._min_free > 0 and shutil.disk_usage(workspace).free < self._min_free:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "insufficient free disk space")

        sem = asyncio.Semaphore(min(MAX_ITEM_CONCURRENCY, len(targets)))
        results: dict[int, Path] = {}
        created: list[Path] = []

        async def run_one(target: DirectTarget) -> None:
            async with sem:
                path = await self._download_one(target, budget, deadline, max_file_bytes=max_file_bytes)
                results[target.key] = path
                created.append(path)

        try:
            try:
                async with asyncio.TaskGroup() as tg:
                    for target in targets:
                        tg.create_task(run_one(target))
            except* BotError as eg:
                raise eg.exceptions[0] from None
            except* FileTooLargeDirect as eg:
                raise eg.exceptions[0] from None
            except* DirectDownloadUnavailable as eg:
                raise eg.exceptions[0] from None
        except BaseException:
            self._cleanup(created)
            raise
        return results

    @staticmethod
    def _cleanup(paths: Sequence[Path]) -> None:
        for p in paths:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)

    # ------------------------------------------------------------------ single file
    async def _open_following_redirects(
        self,
        url: str,
        headers: Mapping[str, str],
        deadline: Deadline,
        *,
        range_header: str | None,
    ) -> tuple[str, aiohttp.ClientResponse]:
        """GET `url`, manually validating up to MAX_REDIRECTS hops. Caller must close the response."""
        current = self._validate_url(url)
        seen: set[str] = set()
        session = self._get_session()
        req_headers = self._headers(headers, range_header=range_header)
        for _ in range(MAX_REDIRECTS + 1):
            deadline.check()
            if current in seen:
                raise DirectDownloadUnavailable("redirect loop")
            seen.add(current)
            timeout = aiohttp.ClientTimeout(
                total=deadline.remaining(),
                sock_connect=min(self._request_timeout, deadline.remaining()),
                sock_read=min(self._request_timeout, deadline.remaining()),
            )
            try:
                resp = await session.get(
                    current,
                    headers=req_headers,
                    allow_redirects=False,
                    proxy=self._proxy,
                    timeout=timeout,
                )
            except aiohttp.ClientConnectorError as exc:
                inner = exc.__cause__ or exc.__context__
                if isinstance(inner, BotError):
                    raise inner from None
                deadline.check()
                raise DirectDownloadUnavailable(type(exc).__name__) from None
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                deadline.check()
                raise DirectDownloadUnavailable(type(exc).__name__) from None
            if resp.status in _REDIRECT_STATUSES:
                location = resp.headers.get("Location")
                resp.release()
                if not location:
                    raise DirectDownloadUnavailable("redirect without location")
                current = self._validate_url(urljoin(current, location))
                continue
            return current, resp
        raise DirectDownloadUnavailable("too many redirects")

    async def _download_one(
        self,
        target: DirectTarget,
        budget: _BudgetTracker,
        deadline: Deadline,
        *,
        max_file_bytes: int | None = None,
    ) -> Path:
        target.dest.parent.mkdir(parents=True, exist_ok=True)
        part = target.dest.with_name(f".dl_{target.dest.name}")
        part.unlink(missing_ok=True)
        try:
            final_url, resp = await self._open_following_redirects(
                target.url, target.headers, deadline, range_header=f"bytes=0-{RANGE_FIRST_BYTES - 1}"
            )
            try:
                if resp.status == 416:
                    resp.release()
                    final_url, resp = await self._open_following_redirects(
                        final_url, target.headers, deadline, range_header=None
                    )
                if resp.status == 206:
                    await self._handle_partial(
                        final_url, resp, target.headers, part, budget, deadline, max_file_bytes=max_file_bytes
                    )
                elif resp.status == 200:
                    await self._stream_full(resp, part, budget, deadline, max_file_bytes=max_file_bytes)
                else:
                    raise DirectDownloadUnavailable(f"http {resp.status}")
            finally:
                resp.release()
            os.replace(part, target.dest)
            return target.dest
        except BaseException:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            raise

    async def _stream_full(
        self,
        resp: aiohttp.ClientResponse,
        dest: Path,
        budget: _BudgetTracker,
        deadline: Deadline,
        *,
        max_file_bytes: int | None = None,
    ) -> None:
        self._check_content_type(resp)
        cl = resp.headers.get("Content-Length")
        declared = int(cl) if cl and cl.isdigit() else None
        if declared is not None:
            if declared == 0:
                raise DirectDownloadUnavailable("empty response")
            if max_file_bytes is not None and declared > max_file_bytes:
                raise FileTooLargeDirect(declared)
            budget.reserve(declared)
        written = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        fd = os.open(dest, flags, 0o660)
        try:
            async for chunk in resp.content.iter_chunked(STREAM_CHUNK_BYTES):
                deadline.check()
                if not chunk:
                    continue
                if max_file_bytes is not None and written + len(chunk) > max_file_bytes:
                    raise FileTooLargeDirect(written + len(chunk))
                if declared is None:
                    budget.add_streamed(len(chunk))
                elif written + len(chunk) > declared:
                    raise DirectDownloadUnavailable("body exceeded content-length")
                await asyncio.to_thread(_write_all, fd, chunk)
                written += len(chunk)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            deadline.check()
            raise DirectDownloadUnavailable(type(exc).__name__) from None
        finally:
            os.close(fd)
        if written == 0 or (declared is not None and written != declared):
            raise DirectDownloadUnavailable("truncated download")

    async def _handle_partial(
        self,
        final_url: str,
        resp: aiohttp.ClientResponse,
        headers: Mapping[str, str],
        dest: Path,
        budget: _BudgetTracker,
        deadline: Deadline,
        *,
        max_file_bytes: int | None = None,
    ) -> None:
        self._check_content_type(resp)
        match = _CONTENT_RANGE_RE.match(resp.headers.get("Content-Range") or "")
        if not match:
            raise DirectDownloadUnavailable("missing or invalid content-range")
        start, end, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if start != 0 or end < 0 or total <= 0 or end >= total:
            raise DirectDownloadUnavailable("unexpected content-range bounds")
        if max_file_bytes is not None and total > max_file_bytes:
            raise FileTooLargeDirect(total)
        expected_first = end - start + 1
        budget.reserve(total)

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        fd = os.open(dest, flags, 0o660)
        lock = threading.Lock()
        try:
            # Write the already-in-flight first chunk directly at offset 0.
            offset = 0
            try:
                async for chunk in resp.content.iter_chunked(STREAM_CHUNK_BYTES):
                    deadline.check()
                    if not chunk:
                        continue
                    if offset + len(chunk) > expected_first:
                        raise DirectDownloadUnavailable("first range exceeded content-range")
                    await asyncio.to_thread(_write_at, fd, offset, chunk, lock)
                    offset += len(chunk)
            except aiohttp.ClientConnectorError as exc:
                inner = exc.__cause__ or exc.__context__
                if isinstance(inner, BotError):
                    raise inner from None
                deadline.check()
                raise DirectDownloadUnavailable(type(exc).__name__) from None
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                deadline.check()
                raise DirectDownloadUnavailable(type(exc).__name__) from None
            if offset != expected_first:
                raise DirectDownloadUnavailable("truncated first range")
            resp.release()  # return connection to pool immediately so a parallel range can reuse it

            remaining = total - expected_first
            if remaining <= 0:
                return

            # Split remaining bytes into parallel TCP streams.
            parts_count = min(RANGE_CONCURRENCY, max(1, (remaining + RANGE_MIN_PART_BYTES - 1) // RANGE_MIN_PART_BYTES))
            step = (remaining + parts_count - 1) // parts_count
            ranges: list[tuple[int, int]] = []
            pos = expected_first
            while pos < total:
                r_end = min(total - 1, pos + step - 1)
                ranges.append((pos, r_end))
                pos = r_end + 1

            async with asyncio.TaskGroup() as tg:
                for r_start, r_end in ranges:
                    tg.create_task(self._fetch_range(final_url, headers, fd, lock, r_start, r_end, deadline))
            if os.fstat(fd).st_size != total:
                raise DirectDownloadUnavailable("assembled size mismatch")
        except* DirectDownloadUnavailable as eg:
            raise eg.exceptions[0] from None
        except* BotError as eg:
            raise eg.exceptions[0] from None
        finally:
            os.close(fd)

    async def _fetch_range(
        self,
        final_url: str,
        headers: Mapping[str, str],
        fd: int,
        lock: threading.Lock,
        r_start: int,
        r_end: int,
        deadline: Deadline,
    ) -> None:
        expected = r_end - r_start + 1
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(
            total=deadline.remaining(),
            sock_connect=min(self._request_timeout, deadline.remaining()),
            sock_read=min(self._request_timeout, deadline.remaining()),
        )
        try:
            async with session.get(
                final_url,
                headers=self._headers(headers, range_header=f"bytes={r_start}-{r_end}"),
                allow_redirects=False,
                proxy=self._proxy,
                timeout=timeout,
            ) as resp:
                if resp.status != 206:
                    raise DirectDownloadUnavailable(f"range request got {resp.status}")
                offset = r_start
                async for chunk in resp.content.iter_chunked(STREAM_CHUNK_BYTES):
                    deadline.check()
                    if not chunk:
                        continue
                    if (offset - r_start) + len(chunk) > expected:
                        raise DirectDownloadUnavailable("range body exceeded requested slice")
                    await asyncio.to_thread(_write_at, fd, offset, chunk, lock)
                    offset += len(chunk)
                if offset - r_start != expected:
                    raise DirectDownloadUnavailable("truncated range slice")
        except DirectDownloadUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            deadline.check()
            raise DirectDownloadUnavailable(type(exc).__name__) from None
