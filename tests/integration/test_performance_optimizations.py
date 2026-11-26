"""Performance optimization & invariant tests:
- worker-side DirectMediaDownloader (parallel byte-range downloads, concurrent carousels,
  non-range / 416 handling, fallback triggers, SSRF/redirect/budget guards);
- single-scrape extraction (direct CDN fetch skipping 2nd gallery-dl/yt-dlp subprocess,
  and yt-dlp `--load-info-json` + `--concurrent-fragments` when tool download runs);
- ShortLinkResolver TTL cache preserving per-request attribution;
- parallel media probing (`finalize_outcome`) & overlapped hashing (`probe_file`);
- Telegram socket tuning (`TCP_NODELAY`, enlarged buffers, keep-alive), exact `Content-Length`
  multipart uploads, and non-blocking status updates in `JobRunner`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import sys
import time
import uuid

import aiohttp
import pytest
from aiogram.methods import SendVideo
from aiohttp import web

from app.bot import messages
from app.core.contracts import (
    ExtractionRequest,
    ItemKind,
    JobState,
    PostManifest,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.net_guard import TCP_BUFFER_BYTES, tuned_socket_factory
from app.core.url_validator import validate_url
from app.extractors.base import FetchedItem, FetchOutcome
from app.extractors.direct_download import (
    RANGE_FIRST_BYTES,
    DirectDownloadUnavailable,
    DirectMediaDownloader,
    DirectTarget,
)
from app.extractors.finalize import finalize_outcome
from app.extractors.local import LocalExtractionBackend
from app.extractors.resolver import ShortLinkResolver
from app.extractors.ytdlp_runner import YtDlpRunner
from app.main import TrackingSession
from app.media.quality import select_format
from app.services.subprocess_runner import SubprocessRunner
from app.services.telegram_sender import FdInputFile
from tests.conftest import requires_ffmpeg
from tests.fake_tools import FakeToolRunner, fixture
from tests.fakes import FakeDeliverer, FakeExtractor, RecordingNotifier, make_bot, message_update
from tests.harness import build_app

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


class _MediaBenchServer:
    """Controlled loopback HTTP server supporting Range, non-Range, 416, redirects, and latency."""

    def __init__(self, payloads: dict[str, bytes], *, support_range: bool = True, delay: float = 0.0) -> None:
        self.payloads = payloads
        self.support_range = support_range
        self.delay = delay
        self.range_416_once: set[str] = set()
        self.html_paths: set[str] = set()
        self.status_paths: dict[str, int] = {}
        self.redirects: dict[str, str] = {}
        self.hits: list[tuple[str, str | None]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    async def start(self) -> _MediaBenchServer:
        app = web.Application()
        app.router.add_get("/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = "/" + request.match_info["path"]
        rng = request.headers.get("Range")
        self.hits.append((path, rng))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            if path in self.redirects:
                return web.Response(status=302, headers={"Location": self.redirects[path]})
            if path in self.status_paths:
                return web.Response(status=self.status_paths[path], text="denied")
            if path in self.html_paths:
                return web.Response(status=200, content_type="text/html", text="<html>login</html>")
            data = self.payloads.get(path)
            if data is None:
                return web.Response(status=404, text="missing")
            if rng and path in self.range_416_once:
                self.range_416_once.remove(path)
                return web.Response(status=416)
            if rng and self.support_range:
                m = _RANGE_RE.match(rng)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else len(data) - 1
                    end = min(end, len(data) - 1)
                    if start > end or start >= len(data):
                        return web.Response(status=416)
                    body = data[start : end + 1]
                    return web.Response(
                        status=206,
                        body=body,
                        headers={
                            "Content-Type": "video/mp4",
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes {start}-{end}/{len(data)}",
                            "Content-Length": str(len(body)),
                        },
                    )
            return web.Response(
                status=200,
                body=data,
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))},
            )
        finally:
            self.in_flight -= 1


def _loopback_downloader(**kw) -> DirectMediaDownloader:
    return DirectMediaDownloader(
        allow_http=True,
        connector_factory=lambda: aiohttp.TCPConnector(socket_factory=tuned_socket_factory),
        **kw,
    )


# ===================================================================================== DirectMediaDownloader
async def test_parallel_byte_range_download_is_bit_identical_and_concurrent(tmp_path):
    blob = os.urandom(3 * 1024 * 1024 + 12345)  # > 3 MiB -> 1 MiB first slice + 4 parallel slices
    srv = await _MediaBenchServer({"/video.mp4": blob}, support_range=True, delay=0.05).start()
    dl = _loopback_downloader()
    try:
        dest = tmp_path / "out.mp4"
        res = await dl.download_many([DirectTarget(1, f"{srv.base_url}/video.mp4", dest)], tmp_path, Deadline(30))
        assert res == {1: dest}
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == hashlib.sha256(blob).hexdigest()
        assert srv.max_in_flight >= 2, "remaining byte ranges must download concurrently"
        assert srv.hits[0][1] == f"bytes=0-{RANGE_FIRST_BYTES - 1}"
    finally:
        await dl.aclose()
        await srv.stop()


async def test_direct_download_handles_non_range_servers_and_416_retry(tmp_path):
    blob = b"MP4DATA" * 4000
    srv = await _MediaBenchServer({"/plain.mp4": blob, "/no_range.mp4": blob}, support_range=False).start()
    srv.range_416_once.add("/no_range.mp4")
    dl = _loopback_downloader()
    try:
        d1, d2 = tmp_path / "p.mp4", tmp_path / "nr.mp4"
        res = await dl.download_many(
            [DirectTarget(1, f"{srv.base_url}/plain.mp4", d1), DirectTarget(2, f"{srv.base_url}/no_range.mp4", d2)],
            tmp_path,
            Deadline(30),
        )
        assert res[1].read_bytes() == blob and res[2].read_bytes() == blob
    finally:
        await dl.aclose()
        await srv.stop()


async def test_carousel_items_download_concurrently_and_keep_identity(tmp_path):
    payloads = {f"/p{i}.jpg": bytes([i + 10]) * 50_000 for i in range(1, 5)}
    srv = await _MediaBenchServer(payloads, support_range=True, delay=0.06).start()
    dl = _loopback_downloader()
    try:
        targets = [DirectTarget(i, f"{srv.base_url}/p{i}.jpg", tmp_path / f"{i:03d}.jpg") for i in range(1, 5)]
        res = await dl.download_many(targets, tmp_path, Deadline(30))
        assert srv.max_in_flight >= 2, "carousel items should download concurrently"
        for i in range(1, 5):
            assert res[i].read_bytes() == payloads[f"/p{i}.jpg"]
    finally:
        await dl.aclose()
        await srv.stop()


async def test_direct_download_rejects_html_error_pages_and_403_for_clean_fallback(tmp_path):
    srv = await _MediaBenchServer({}, support_range=True).start()
    srv.html_paths.add("/login.mp4")
    srv.status_paths["/denied.mp4"] = 403
    dl = _loopback_downloader()
    try:
        for path in ("/login.mp4", "/denied.mp4"):
            dest = tmp_path / "x.mp4"
            with pytest.raises(DirectDownloadUnavailable):
                await dl.download_many([DirectTarget(1, f"{srv.base_url}{path}", dest)], tmp_path, Deadline(10))
            assert not dest.exists() and not (tmp_path / ".dl_x.mp4").exists()
    finally:
        await dl.aclose()
        await srv.stop()


async def test_direct_download_enforces_byte_budget_before_downloading_rest(tmp_path):
    blob = b"x" * (2 * 1024 * 1024)
    srv = await _MediaBenchServer({"/big.mp4": blob}, support_range=True).start()
    dl = _loopback_downloader(max_bytes=500_000)
    try:
        dest = tmp_path / "big.mp4"
        with pytest.raises(BotError) as exc:
            await dl.download_many([DirectTarget(1, f"{srv.base_url}/big.mp4", dest)], tmp_path, Deadline(10))
        assert exc.value.code is ErrorCode.LIMIT_EXCEEDED
        assert len(srv.hits) == 1, "must abort immediately on Content-Range total without fetching remaining slices"
        assert not dest.exists()
    finally:
        await dl.aclose()
        await srv.stop()


@pytest.mark.parametrize(
    "bad_url", ["http://cdn.example/a.mp4", "https://127.0.0.1/a.mp4", "https://u:p@cdn.example/a.mp4"]
)
async def test_direct_download_production_url_guards_reject_unsafe_targets(tmp_path, bad_url):
    dl = DirectMediaDownloader()  # production defaults: https only, no IP literals, PinnedResolver
    try:
        with pytest.raises(DirectDownloadUnavailable):
            await dl.download_many([DirectTarget(1, bad_url, tmp_path / "a.mp4")], tmp_path, Deadline(10))
    finally:
        await dl.aclose()


@pytest.mark.parametrize("private_ip", ["127.0.0.1", "169.254.169.254", "::1"])
async def test_direct_download_blocks_private_dns_via_pinned_resolver(tmp_path, private_ip):
    async def dns(_host: str) -> list[str]:
        return [private_ip]

    dl = DirectMediaDownloader(dns=dns)
    try:
        with pytest.raises(BotError) as exc:
            await dl.download_many(
                [DirectTarget(1, "https://video.twimg.com/a.mp4", tmp_path / "a.mp4")], tmp_path, Deadline(10)
            )
        assert exc.value.code is ErrorCode.INVALID_URL and exc.value.reason == "unsafe_destination"
    finally:
        await dl.aclose()


async def test_direct_download_rejects_redirect_to_private_target_in_production_mode(tmp_path):
    srv = await _MediaBenchServer({}).start()
    srv.redirects["/hop.mp4"] = "http://127.0.0.1:9/secret"
    dl = _loopback_downloader()
    # Flip allow_http off after constructing to verify redirect target validation rejects http://127.0.0.1
    dl._allow_http = False
    try:
        with pytest.raises(DirectDownloadUnavailable):
            dl._validate_url("http://127.0.0.1:9/secret")
    finally:
        await dl.aclose()
        await srv.stop()


# ===================================================================================== Single-scrape adapters
@requires_ffmpeg
async def test_twitter_adapter_downloads_directly_without_second_gallerydl_subprocess(tmp_path, media, make_settings):
    video_bytes = media.mp4_h264_aac.read_bytes()
    srv = await _MediaBenchServer({"/clip.mp4": video_bytes}, support_range=True).start()
    fake = FakeToolRunner()
    x_url = "https://x.com/someuser/status/1700000000000000000"
    payload = fixture("gd_twitter_video")
    payload[1][1] = f"{srv.base_url}/clip.mp4"
    fake.gd_dump(x_url, payload)

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(x_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=x_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))
        assert len(result.assets) == 1 and result.assets[0].local_path.read_bytes() == video_bytes
        # Only discover (--dump-json) invoked gallery-dl; NO 2nd gallery-dl download subprocess!
        gd_calls = fake.calls_to("gallery_dl")
        assert len(gd_calls) == 1 and "--dump-json" in gd_calls[0]
    finally:
        await backend.aclose()
        await srv.stop()


@requires_ffmpeg
async def test_twitter_adapter_falls_back_to_gallerydl_when_direct_download_fails(tmp_path, media, make_settings):
    srv = await _MediaBenchServer({}).start()
    srv.status_paths["/denied.mp4"] = 403
    fake = FakeToolRunner()
    x_url = "https://x.com/someuser/status/1700000000000000000"
    payload = fixture("gd_twitter_video")
    payload[1][1] = f"{srv.base_url}/denied.mp4"
    fake.gd_dump(x_url, payload)
    fake.gd_files(x_url, {1: media.mp4_h264_aac})

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(x_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=x_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))
        assert len(result.assets) == 1 and result.completeness == "complete"
        assert len(fake.calls_to("gallery_dl")) == 2, "must fall back cleanly to gallery-dl download"
    finally:
        await backend.aclose()
        await srv.stop()


@requires_ffmpeg
async def test_tiktok_adapter_downloads_progressive_video_directly_without_second_ytdlp_subprocess(
    tmp_path, media, make_settings
):
    video_bytes = media.mp4_h264_aac.read_bytes()
    srv = await _MediaBenchServer({"/tt.mp4": video_bytes}, support_range=True).start()
    fake = FakeToolRunner()
    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"
    payload = fixture("ytdlp_tiktok_video")
    for fmt in payload["formats"]:
        fmt["url"] = f"{srv.base_url}/tt.mp4"
        fmt["protocol"] = "http"
    fake.yt_info(tt_url, payload)

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(tt_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=tt_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))
        assert len(result.assets) == 1 and result.assets[0].local_path.read_bytes() == video_bytes
        yt_calls = fake.calls_to("yt_dlp")
        assert len(yt_calls) == 1 and "-J" in yt_calls[0], "must skip 2nd yt-dlp subprocess for progressive videos"
    finally:
        await backend.aclose()
        await srv.stop()


@requires_ffmpeg
async def test_real_ytdlp_load_info_json_downloads_without_rescrape(media, media_server, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    url = f"{media_server}/v_h264_aac.mp4"
    info = await y.discover(url, ws, Deadline(60))
    assert info.info_json is not None and info.info_json.is_file()
    choice = select_format(info.entries[0].formats)
    paths = await y.download(url, ws, Deadline(60), selector=choice.selector, info_json=info.info_json)
    assert len(paths) == 1 and paths[0].read_bytes() == media.mp4_h264_aac.read_bytes()


# ===================================================================================== Short-link cache
async def test_short_link_cache_skips_repeat_network_hops_and_keeps_attribution():
    hits = 0

    async def handle(request: web.Request) -> web.Response:
        nonlocal hits
        hits += 1
        return web.Response(status=301, headers={"Location": "https://www.tiktok.com/@u/video/7300000000000000000"})

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    class _Map(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port_=0, family=socket.AF_INET):
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]

        async def close(self):
            return None

    now = {"t": 100.0}
    resolver = ShortLinkResolver(
        scheme="http",
        connector_factory=lambda: aiohttp.TCPConnector(resolver=_Map()),
        cache_ttl=60.0,
        clock=lambda: now["t"],
    )
    try:
        r1 = await resolver.resolve(validate_url("https://vm.tiktok.com/ZMabcdEfg/?a=1"), Deadline(10))
        r2 = await resolver.resolve(validate_url("https://vm.tiktok.com/ZMabcdEfg/?b=2"), Deadline(10))
        assert hits == 1
        assert r1.original_url == "https://vm.tiktok.com/ZMabcdEfg/?a=1"
        assert r2.original_url == "https://vm.tiktok.com/ZMabcdEfg/?b=2"
        assert r1.extraction_url == r2.extraction_url

        now["t"] += 61.0  # TTL expired -> re-fetches
        await resolver.resolve(validate_url("https://vm.tiktok.com/ZMabcdEfg/"), Deadline(10))
        assert hits == 2
    finally:
        await resolver.aclose()
        await runner.cleanup()


# ===================================================================================== Parallel probing
@requires_ffmpeg
async def test_finalize_probes_multiple_items_concurrently_and_preserves_order(media, tmp_path):
    items = [
        FetchedItem("i0", 0, ItemKind.PHOTO, media.jpg),
        FetchedItem("i1", 1, ItemKind.PHOTO, media.png),
        FetchedItem("i2", 2, ItemKind.VIDEO, media.mp4_h264_aac),
        FetchedItem("i3", 3, ItemKind.PHOTO, media.tall_jpg),
    ]
    manifest = PostManifest(
        platform="instagram",
        post_id="p1",
        post_type="carousel",
        access="permitted_public",
        expected_count=4,
        count_verified=True,
        items=[],
    )  # type: ignore[arg-type]
    outcome = FetchOutcome(items=list(reversed(items)), expected_count=4, count_verified=True)
    res = await finalize_outcome(outcome, manifest, tmp_path, max_total_bytes=50_000_000, max_items=10)
    assert [a.position for a in res.assets] == [0, 1, 2, 3]
    assert res.assets[0].sha256 == hashlib.sha256(media.jpg.read_bytes()).hexdigest()
    assert res.assets[2].sha256 == hashlib.sha256(media.mp4_h264_aac.read_bytes()).hexdigest()


# ===================================================================================== Telegram session & status overlap
def test_tracking_session_keeps_os_autotuned_sockets_and_64k_streaming(tmp_path):
    session = TrackingSession()
    assert session._connector_init["keepalive_timeout"] == 60.0
    assert session._connector_init["socket_factory"] is tuned_socket_factory

    sock = tuned_socket_factory((socket.AF_INET, socket.SOCK_STREAM, 0))
    plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            if sys.platform.startswith("linux") or sys.platform == "win32":
                assert sock.getsockopt(socket.SOL_SOCKET, opt) == plain.getsockopt(socket.SOL_SOCKET, opt)
            else:
                assert sock.getsockopt(socket.SOL_SOCKET, opt) >= TCP_BUFFER_BYTES
    finally:
        sock.close()
        plain.close()

    p = tmp_path / "clip.mp4"
    p.write_bytes(b"0123456789" * 100)
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        bot, _ = make_bot()
        file_obj = FdInputFile(fd, "clip.mp4", 1000)
        assert file_obj.chunk_size == 64 << 10
        form = session.build_form_data(bot, SendVideo(chat_id=1, video=file_obj))
        assert form().size is None, "default streaming multipart avoids Windows ProactorEventLoop 1 MiB drain stalls"
    finally:
        os.close(fd)


async def test_slow_status_updates_do_not_block_extraction_or_delivery(tmp_path):
    class SlowNotifier(RecordingNotifier):
        async def status(self, job, text: str) -> None:
            await asyncio.sleep(0.25)
            await super().status(job, text)

    delivery_finished_at: dict[str, float] = {}

    class TimedDeliverer(FakeDeliverer):
        async def deliver(self, job, request, result, workspace):
            out = await super().deliver(job, request, result, workspace)
            delivery_finished_at["t"] = time.monotonic()
            return out

    notifier = SlowNotifier()
    a = await build_app(tmp_path, extractor=FakeExtractor(items=1))
    a.notifier = notifier
    a.ctx.notifier = notifier
    a.runner.notifier = notifier
    a.deliverer = TimedDeliverer(a.db)
    a.runner.deliverer = a.deliverer
    try:
        t0 = time.monotonic()
        await a.feed(message_update("https://x.com/someuser/status/1700000000000"))
        await a.drain()
        # Delivery completed well before two sequential 0.25s status RTTs (0.50s) could finish!
        assert delivery_finished_at["t"] - t0 < 0.20
        assert notifier.status_texts() == [messages.FETCHING, messages.SENDING]
        assert a.db.get_job(notifier.acks[0]).state is JobState.COMPLETED
    finally:
        await a.shutdown()


# ===================================================================================== Smart variant fallback (50 MB cap)
@requires_ffmpeg
async def test_x_oversized_top_variant_falls_back_fast_to_next_variant_with_caption_note(
    tmp_path, media, make_settings
):
    from app.core.contracts import DeliveryMode
    from app.media.delivery_plan import Caps, plan_delivery

    small_video = media.mp4_h264_aac.read_bytes()
    big_video = os.urandom(2_500_000)  # > 1.5 MiB test cap
    cap = 1_500_000
    srv = await _MediaBenchServer({"/1080p.mp4": big_video, "/720p.mp4": small_video}, support_range=True).start()

    x_url = "https://x.com/someuser/status/1700000000000000000"
    fake = FakeToolRunner()
    gd_payload = fixture("gd_twitter_video")
    gd_payload[1][1] = f"{srv.base_url}/1080p.mp4"
    fake.gd_dump(x_url, gd_payload)

    # yt-dlp exposes 1080p progressive, 1080p HLS mirror, and 720p progressive that fits under cap.
    fake.yt_info(
        x_url,
        {
            "id": "1700000000000000000",
            "extractor_key": "Twitter",
            "duration": 30.0,
            "formats": [
                {
                    "format_id": "http-832",
                    "url": f"{srv.base_url}/720p.mp4",
                    "protocol": "http",
                    "ext": "mp4",
                    "width": 1280,
                    "height": 720,
                    "tbr": 832.0,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
                {
                    "format_id": "hls-2176",
                    "url": f"{srv.base_url}/1080p.m3u8",
                    "protocol": "m3u8_native",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                    "tbr": 2176.0,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
                {
                    "format_id": "http-2176",
                    "url": f"{srv.base_url}/1080p.mp4",
                    "protocol": "http",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                    "tbr": 2176.0,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ],
        },
    )

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(hosted_file_max_bytes=cap), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(x_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=x_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))

        (asset,) = result.assets
        assert asset.local_path.read_bytes() == small_video
        assert asset.source_format == "http-832"
        assert asset.quality_note is not None and "720p" in asset.quality_note and "1080p" in asset.quality_note
        # Must NOT have fallen back to slow gallery-dl download or yt-dlp HLS download!
        assert len(fake.calls_to("gallery_dl")) == 1
        assert [c for c in fake.calls_to("yt_dlp") if "-f" in c] == []
        # 1080p was rejected on its very first Range header (never fetched remaining parallel slices).
        hits_1080 = [h for h in srv.hits if h[0] == "/1080p.mp4"]
        assert all(rng == f"bytes=0-{RANGE_FIRST_BYTES - 1}" for _, rng in hits_1080)

        plan = plan_delivery(result.assets, mode=DeliveryMode.MEDIA, caps=Caps(9_500_000, cap), url=x_url)
        assert plan.steps[0].caption == f"{x_url}\n120p"
        assert not plan.button_on_media and not plan.button_on_link
    finally:
        await backend.aclose()
        await srv.stop()


@requires_ffmpeg
async def test_tiktok_oversized_top_variant_skips_by_filesize_and_notes_lower_variant(tmp_path, media, make_settings):
    small_video = media.mp4_h264_aac.read_bytes()
    cap = 10_000_000
    srv = await _MediaBenchServer({"/540p.mp4": small_video}, support_range=True).start()

    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"
    fake = FakeToolRunner()
    payload = fixture("ytdlp_tiktok_video")
    for fmt in payload["formats"]:
        if fmt["format_id"] == "bytevc1_720p_1":
            fmt["filesize"] = 60_000_000  # exceeds cap -> skipped without even contacting URL
            fmt["url"] = f"{srv.base_url}/720p_never_requested.mp4"
            fmt["protocol"] = "http"
        elif fmt["format_id"] == "h264_540p_1":
            fmt["filesize"] = len(small_video)
            fmt["url"] = f"{srv.base_url}/540p.mp4"
            fmt["protocol"] = "http"
        else:
            fmt["filesize"] = len(small_video)
            fmt["url"] = f"{srv.base_url}/download.mp4"
            fmt["protocol"] = "http"
    fake.yt_info(tt_url, payload)

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(hosted_file_max_bytes=cap), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(tt_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=tt_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))
        (asset,) = result.assets
        assert asset.source_format == "h264_540p_1"
        assert asset.local_path.read_bytes() == small_video
        assert asset.quality_note == "Sent 576p (720p source exceeded 10 MB Telegram limit)"
        assert all(path != "/720p_never_requested.mp4" for path, _ in srv.hits)
    finally:
        await backend.aclose()
        await srv.stop()


async def test_all_variants_over_cap_fail_fast_with_file_too_large(tmp_path, make_settings):
    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"
    fake = FakeToolRunner()
    payload = fixture("ytdlp_tiktok_video")
    for fmt in payload["formats"]:
        fmt["filesize"] = 80_000_000
    fake.yt_info(tt_url, payload)

    backend = LocalExtractionBackend(make_settings(hosted_file_max_bytes=49_000_000), runner=fake)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(tt_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=tt_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        with pytest.raises(BotError) as exc:
            await backend.fetch(req, manifest, ws, Deadline(30))
        assert exc.value.code is ErrorCode.FILE_TOO_LARGE
        assert [c for c in fake.calls_to("yt_dlp") if "-f" in c] == [], "must not download when every variant > cap"
    finally:
        await backend.aclose()


def test_caption_with_resolution_preserves_exact_url_even_near_limit():
    from app.media.delivery_plan import CAPTION_LIMIT, caption_for

    url = "https://x.com/u/status/123"
    assert caption_for(url, ["720p"]) == f"{url}\n720p"

    # When URL alone fits <= 1024 but URL + resolution would overflow, keep exact URL untouched (never truncate).
    long_url = "https://x.com/u/status/" + "1" * (CAPTION_LIMIT - len("https://x.com/u/status/") - 2)
    assert len(long_url) <= CAPTION_LIMIT
    assert caption_for(long_url, ["720p"]) == long_url


@requires_ffmpeg
async def test_tiktok_split_hevc_merges_and_transcodes_in_single_ffmpeg_pass(tmp_path, media, make_settings):
    import subprocess
    from pathlib import Path

    from app.core.contracts import DeliveryMode, Processing
    from app.media.quality import is_native_video
    from tests.conftest import FFMPEG

    hevc_vonly = tmp_path / "hevc_vonly.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(media.mp4_hevc_aac),
            "-c:v",
            "copy",
            "-an",
            str(hevc_vonly),
        ],
        check=True,
    )
    srv = await _MediaBenchServer(
        {"/v_hevc.mp4": hevc_vonly.read_bytes(), "/a.m4a": media.audio_only_m4a.read_bytes()},
        support_range=True,
        delay=0.05,
    ).start()
    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"
    fake = FakeToolRunner()
    fake.yt_info(
        tt_url,
        {
            "id": "7300000000000000000",
            "duration": 1.0,
            "extractor_key": "TikTok",
            "formats": [
                {
                    "format_id": "bytevc1_1080p_v",
                    "vcodec": "hevc",
                    "acodec": "none",
                    "ext": "mp4",
                    "width": 1080,
                    "height": 1920,
                    "tbr": 2000.0,
                    "protocol": "http",
                    "url": f"{srv.base_url}/v_hevc.mp4",
                },
                {
                    "format_id": "audio_aac",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "tbr": 128.0,
                    "protocol": "http",
                    "url": f"{srv.base_url}/a.m4a",
                },
            ],
        },
    )

    dl = _loopback_downloader()
    backend = LocalExtractionBackend(make_settings(), runner=fake, downloader=dl)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(tt_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=tt_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
            delivery_mode=DeliveryMode.MEDIA,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))

        (asset,) = result.assets
        assert asset.processing is Processing.TRANSCODED
        assert (asset.container, asset.video_codec, asset.audio_codec, asset.pix_fmt) == (
            "mp4",
            "h264",
            "aac",
            "yuv420p",
        )
        assert is_native_video(
            container=asset.container,
            video_codec=asset.video_codec,
            audio_codec=asset.audio_codec,
            pix_fmt=asset.pix_fmt,
        )
        assert srv.max_in_flight >= 2, "video and audio split tracks must download concurrently"
        yt_calls = fake.calls_to("yt_dlp")
        assert len(yt_calls) == 1 and "-J" in yt_calls[0] and "--no-check-formats" in yt_calls[0]
        ffmpeg_calls = [c for c in fake.calls if c and Path(c[0]).stem.lower() == "ffmpeg"]
        assert len(ffmpeg_calls) == 1, "split HEVC must merge+transcode in ONE ffmpeg pass, not two"
        assert not (ws / "yt_1_v.mp4").exists() and not (ws / "yt_1_a.m4a").exists()
    finally:
        await backend.aclose()
        await srv.stop()


@requires_ffmpeg
async def test_split_direct_merge_stream_copies_native_media_and_preserves_original_mode(
    tmp_path, media, make_settings
):
    import subprocess
    from pathlib import Path

    from app.core.contracts import DeliveryMode, Processing
    from tests.conftest import FFMPEG, stream_hash

    hevc_vonly = tmp_path / "hevc_vonly.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(media.mp4_hevc_aac),
            "-c:v",
            "copy",
            "-an",
            str(hevc_vonly),
        ],
        check=True,
    )
    srv = await _MediaBenchServer(
        {
            "/v_h264.mp4": media.video_only_mp4.read_bytes(),
            "/v_hevc.mp4": hevc_vonly.read_bytes(),
            "/a.m4a": media.audio_only_m4a.read_bytes(),
        },
        support_range=True,
    ).start()
    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"

    def _make_payload(v_path: str, vcodec: str) -> dict:
        return {
            "id": "7300000000000000000",
            "duration": 1.0,
            "extractor_key": "TikTok",
            "formats": [
                {
                    "format_id": "v_top",
                    "vcodec": vcodec,
                    "acodec": "none",
                    "ext": "mp4",
                    "width": 160,
                    "height": 120,
                    "tbr": 1000.0,
                    "protocol": "http",
                    "url": f"{srv.base_url}{v_path}",
                },
                {
                    "format_id": "a_top",
                    "vcodec": "none",
                    "acodec": "aac",
                    "ext": "m4a",
                    "tbr": 128.0,
                    "protocol": "http",
                    "url": f"{srv.base_url}/a.m4a",
                },
            ],
        }

    try:
        # 1. Native H.264 + AAC in MEDIA mode -> lossless stream-copy merge in ONE ffmpeg pass.
        fake1 = FakeToolRunner()
        fake1.yt_info(tt_url, _make_payload("/v_h264.mp4", "h264"))
        backend1 = LocalExtractionBackend(make_settings(), runner=fake1, downloader=_loopback_downloader())
        ws1 = tmp_path / "ws1"
        ws1.mkdir()
        try:
            v = validate_url(tt_url)
            req = ExtractionRequest(
                job_id=uuid.uuid4(),
                owner_id=1,
                chat_id=1,
                origin_message_id=1,
                original_url=tt_url,
                extraction_url=v.extraction_url,
                platform=v.platform,
                post_hint=v.post_hint,
                delivery_mode=DeliveryMode.MEDIA,
            )
            m1 = await backend1.discover(req, ws1, Deadline(30))
            r1 = await backend1.fetch(req, m1, ws1, Deadline(30))
            (a1,) = r1.assets
            assert a1.processing is Processing.MERGED and a1.video_codec == "h264" and a1.audio_codec == "aac"
            assert stream_hash(a1.local_path, "0:v:0") == stream_hash(media.video_only_mp4, "0:v:0")
            assert len([c for c in fake1.calls if c and Path(c[0]).stem.lower() == "ffmpeg"]) == 1
        finally:
            await backend1.aclose()

        # 2. HEVC + AAC in ORIGINAL mode -> stream-copy merge only, never transcodes HEVC.
        fake2 = FakeToolRunner()
        fake2.yt_info(tt_url, _make_payload("/v_hevc.mp4", "hevc"))
        backend2 = LocalExtractionBackend(make_settings(), runner=fake2, downloader=_loopback_downloader())
        ws2 = tmp_path / "ws2"
        ws2.mkdir()
        try:
            v = validate_url(tt_url)
            req2 = ExtractionRequest(
                job_id=uuid.uuid4(),
                owner_id=1,
                chat_id=1,
                origin_message_id=1,
                original_url=tt_url,
                extraction_url=v.extraction_url,
                platform=v.platform,
                post_hint=v.post_hint,
                delivery_mode=DeliveryMode.ORIGINAL,
            )
            m2 = await backend2.discover(req2, ws2, Deadline(30))
            r2 = await backend2.fetch(req2, m2, ws2, Deadline(30))
            (a2,) = r2.assets
            assert a2.processing is Processing.MERGED and a2.video_codec == "hevc" and a2.audio_codec == "aac"
            assert stream_hash(a2.local_path, "0:v:0") == stream_hash(hevc_vonly, "0:v:0")
            assert len([c for c in fake2.calls if c and Path(c[0]).stem.lower() == "ffmpeg"]) == 1
        finally:
            await backend2.aclose()
    finally:
        await srv.stop()


@requires_ffmpeg
async def test_direct_404_format_advances_immediately_without_ytdlp_retry_or_size_note(tmp_path, media, make_settings):
    small_video = media.mp4_h264_aac.read_bytes()
    srv = await _MediaBenchServer({"/fallback_540p.mp4": small_video}, support_range=True).start()
    srv.status_paths["/missing_720p.mp4"] = 404

    tt_url = "https://www.tiktok.com/@someuser/video/7300000000000000000"
    fake = FakeToolRunner()
    payload = fixture("ytdlp_tiktok_video")
    for fmt in payload["formats"]:
        fmt["protocol"] = "http"
        if fmt["format_id"] == "bytevc1_720p_1":
            fmt["url"] = f"{srv.base_url}/missing_720p.mp4"
        else:
            fmt["url"] = f"{srv.base_url}/fallback_540p.mp4"
    fake.yt_info(tt_url, payload)

    backend = LocalExtractionBackend(make_settings(), runner=fake, downloader=_loopback_downloader())
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(tt_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=tt_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        result = await backend.fetch(req, manifest, ws, Deadline(30))
        (asset,) = result.assets
        assert asset.source_format == "h264_540p_1"
        assert asset.local_path.read_bytes() == small_video
        assert asset.quality_note is None, "404 fallback is not a file-size downgrade"
        assert [c for c in fake.calls_to("yt_dlp") if "-f" in c] == [], (
            "must skip straight to next format on 404 instead of spawning a 2nd yt-dlp subprocess"
        )
    finally:
        await backend.aclose()
        await srv.stop()


# ===================================================================================== TikTok cookie & mirror fixes
def test_parse_cookie_header_strips_set_cookie_attributes():
    from app.extractors.ytdlp_runner import parse_cookie_header, parse_info

    raw = (
        "ttwid=abc123; Domain=.tiktok.com; Path=/; Secure; Expires=Fri, 01 Jan 2027 00:00:00 GMT; "
        "tt_csrf_token=tok_xyz; Domain=.tiktok.com; Path=/; Secure; "
        "tt_chain_token=chn==; Domain=.tiktok.com; Path=/; HttpOnly"
    )
    cleaned = parse_cookie_header(raw)
    assert "Domain=" not in cleaned and "Path=" not in cleaned and "Expires=" not in cleaned and "Secure" not in cleaned
    assert "ttwid=abc123" in cleaned and "tt_csrf_token=tok_xyz" in cleaned and "tt_chain_token=chn==" in cleaned

    info = parse_info(
        '{"id":"1","formats":[{"format_id":"f0","url":"https://v16.tiktok.com/v.mp4","ext":"mp4",'
        f'"vcodec":"h264","acodec":"aac","cookies":"{raw}"}}'
        "]}"
    )
    assert info.entries[0].sources["f0"].headers["Cookie"] == cleaned


def test_mirror_priority_prefers_primary_tiktok_mirror_0_over_backup_mirror_1():
    from app.core.contracts import FormatDescriptor
    from app.media.quality import ranked_choices

    f0 = FormatDescriptor(
        format_id="bytevc1_1080p_1780-0",
        has_video=True,
        has_audio=True,
        width=1080,
        height=1920,
        vcodec="h265",
        tbr=1780,
    )
    f1 = FormatDescriptor(
        format_id="bytevc1_1080p_1780-1",
        has_video=True,
        has_audio=True,
        width=1080,
        height=1920,
        vcodec="h265",
        tbr=1780,
    )
    ordered = ranked_choices([f0, f1])
    assert [c.selector for c in ordered] == ["bytevc1_1080p_1780-0", "bytevc1_1080p_1780-1"]


@requires_ffmpeg
async def test_in_process_ytdlp_discovers_loopback_media_and_enforces_allowlist(media_server, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",), in_process=True)
    info = await y.discover(f"{media_server}/v_h264_aac.mp4", ws, Deadline(30))
    assert (
        len(info.entries) == 1 and info.entries[0].formats and info.info_json is not None and info.info_json.is_file()
    )

    strict = YtDlpRunner(SubprocessRunner(), in_process=True)  # production allowlist excludes generic
    with pytest.raises(BotError) as exc:
        await strict.discover(f"{media_server}/v_h264_aac.mp4", ws, Deadline(30))
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST


# ===================================================================================== X FastApiClient (FxTwitter + Syndication)
@requires_ffmpeg
async def test_x_fast_api_fxtwitter_and_syndication_fallback_skip_gallerydl(tmp_path, media, make_settings):
    import json as _json

    from app.extractors.fast_api import FastApiClient, syndication_token

    assert syndication_token("20")  # deterministic non-empty base36 token
    video_bytes = media.mp4_h264_aac.read_bytes()
    big_bytes = os.urandom(2_000_000)
    jpg_bytes = media.jpg.read_bytes()
    tid = "1700000000000000000"
    x_url = f"https://x.com/someuser/status/{tid}"

    routes: dict[str, tuple[int, dict]] = {}

    async def api_handler(request: web.Request) -> web.Response:
        path = request.path
        if path in routes:
            status, body = routes[path]
            return web.Response(status=status, text=_json.dumps(body), content_type="application/json")
        return web.Response(status=404, text="{}")

    api_app = web.Application()
    api_app.router.add_get("/{tail:.*}", api_handler)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", 0)
    await api_site.start()
    api_port = api_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    api_base = f"http://127.0.0.1:{api_port}"

    media_srv = await _MediaBenchServer(
        {"/1080p.mp4": big_bytes, "/720p.mp4": video_bytes, "/pic.jpg": jpg_bytes}, support_range=True
    ).start()
    fast = FastApiClient(
        fxtwitter_base=api_base,
        syndication_base=api_base,
        allow_http=True,
        connector_factory=lambda: aiohttp.TCPConnector(socket_factory=tuned_socket_factory),
    )
    fake = FakeToolRunner()
    backend = LocalExtractionBackend(
        make_settings(hosted_file_max_bytes=1_000_000),
        runner=fake,
        downloader=_loopback_downloader(),
        fast_api=fast,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        # 1. FxTwitter returns a video with 1080p (>cap) + 720p (<=cap) + quoted tweet media (must be ignored).
        routes[f"/someuser/status/{tid}"] = (
            200,
            {
                "code": 200,
                "tweet": {
                    "id": tid,
                    "media": {
                        "all": [
                            {
                                "type": "video",
                                "url": f"{media_srv.base_url}/1080p.mp4",
                                "width": 1920,
                                "height": 1080,
                                "duration": 5.0,
                                "variants": [
                                    {
                                        "content_type": "video/mp4",
                                        "bitrate": 2176000,
                                        "url": f"{media_srv.base_url}/1080p.mp4",
                                    },
                                    {
                                        "content_type": "video/mp4",
                                        "bitrate": 832000,
                                        "url": f"{media_srv.base_url}/720p.mp4",
                                    },
                                ],
                            }
                        ]
                    },
                    "quote": {
                        "id": "999",
                        "media": {"photos": [{"type": "photo", "url": f"{media_srv.base_url}/pic.jpg"}]},
                    },
                },
            },
        )
        v = validate_url(x_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=x_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        m1 = await backend.discover(req, ws, Deadline(30))
        r1 = await backend.fetch(req, m1, ws, Deadline(30))
        assert len(r1.assets) == 1 and r1.assets[0].local_path.read_bytes() == video_bytes
        assert r1.assets[0].quality_note and "exceeded" in r1.assets[0].quality_note
        assert fake.calls_to("gallery_dl") == [] and fake.calls_to("yt_dlp") == [], (
            "fast_api path must skip gallery-dl and yt-dlp completely"
        )

        # 2. FxTwitter 500 -> falls back to X Syndication cleanly and extracts photo.
        routes[f"/someuser/status/{tid}"] = (500, {})
        routes["/tweet-result"] = (
            200,
            {
                "id_str": tid,
                "mediaDetails": [
                    {
                        "type": "photo",
                        "media_url_https": f"{media_srv.base_url}/pic.jpg",
                        "original_info": {"width": 320, "height": 240},
                    }
                ],
            },
        )
        req2 = req.model_copy(update={"job_id": uuid.uuid4()})
        m2 = await backend.discover(req2, ws, Deadline(30))
        r2 = await backend.fetch(req2, m2, ws, Deadline(30))
        assert len(r2.assets) == 1 and r2.assets[0].local_path.read_bytes() == jpg_bytes
        assert fake.calls_to("gallery_dl") == [] and fake.calls_to("yt_dlp") == []
    finally:
        await backend.aclose()
        await media_srv.stop()
        await api_runner.cleanup()


# ===================================================================================== Pre-extraction media_cache & Direct URL send
async def test_media_cache_hit_skips_extraction_and_stale_file_id_self_heals(tmp_path, media):
    from tests.e2e import start_e2e

    e = await start_e2e(tmp_path)
    try:
        ig_url = "https://www.instagram.com/p/ABCdef12345/"
        e.tools.gd_dump(ig_url, "gd_instagram_photo")
        e.tools.gd_files(ig_url, {1: media.jpg})

        # 1st send populates media_cache.
        await e.say(f"{ig_url}?utm=1", message_id=1)
        await e.idle()
        assert len(e.tools.calls_to("gallery_dl")) == 2
        first_fid = e.server.calls_of("sendPhoto")[0].reply["photo"][-1]["file_id"]

        # 2nd send (different query string!) hits media_cache with ZERO gallery-dl calls.
        await e.say(f"{ig_url}?utm=2", message_id=2)
        await e.server.wait_for(lambda: len(e.server.calls_of("sendPhoto")) == 2)
        await e.idle()
        assert len(e.tools.calls_to("gallery_dl")) == 2
        second = e.server.calls_of("sendPhoto")[1]
        assert second.fields["photo"] == first_fid and not second.files
        assert second.fields["caption"] == f"{ig_url}\n240p"

        # 3rd send: Telegram rejects the stale cached file_id (400) -> evicts cache & extracts fresh!
        e.server.fail("sendPhoto", 400, "Bad Request: wrong file identifier", times=1)
        await e.say(f"{ig_url}?utm=3", message_id=3)
        await e.server.wait_for(lambda: len(e.server.calls_of("sendPhoto")) == 4)
        await e.idle()
        assert len(e.tools.calls_to("gallery_dl")) == 4  # re-extracted cleanly
        assert e.server.calls_of("sendPhoto")[-1].files  # uploaded fresh bytes
    finally:
        await e.stop()


@requires_ffmpeg
async def test_direct_telegram_url_delivery_skips_download_and_falls_back_on_400(tmp_path, media):
    from tests.e2e import start_e2e

    x_url = "https://x.com/someuser/status/1700000000000000000"
    e = await start_e2e(tmp_path, media_cache_ttl_seconds=0)
    try:
        # Turn on direct URL fast-send on this test's LocalExtractionBackend.
        e.backend._direct_telegram_url = True  # type: ignore[attr-defined]
        e.tools.gd_dump(x_url, "gd_twitter_video")
        e.tools.gd_files(x_url, {1: media.mp4_h264_aac})

        # 1. Direct URL succeeds: Telegram receives the HTTPS CDN URL directly (no download subprocess, no file upload!).
        await e.say(x_url, message_id=1)
        await e.idle()
        assert len(e.tools.calls_to("gallery_dl")) == 1  # discover only, NO download!
        v1 = e.server.calls_of("sendVideo")[0]
        assert v1.fields["video"].startswith("https://") and not v1.files
        assert e.jobs()[0]["state"] == "COMPLETED"

        # 2. Force Telegram 400 on direct URL -> falls back atomically to local download+upload!
        e.server.fail("sendVideo", 400, "Bad Request: failed to get HTTP URL content", times=1)
        await e.say(x_url, message_id=2)
        await e.server.wait_for(lambda: len(e.server.calls_of("sendVideo")) == 3)
        await e.idle()
        assert len(e.tools.calls_to("gallery_dl")) == 3  # 1st discover + 2nd discover + 2nd download fallback
        v_fb = e.server.calls_of("sendVideo")[-1]
        assert v_fb.files and next(iter(v_fb.files.values()))[1] == media.mp4_h264_aac.read_bytes()

        # 3. Ambiguous 500 on direct URL -> DELIVERY_UNKNOWN, NO fallback replay!
        e.server.fail("sendVideo", 500, "Internal Server Error", times=10)
        await e.say(x_url, message_id=3)
        await e.idle()
        assert e.jobs()[-1]["state"] == "DELIVERY_UNKNOWN"
        assert len(e.tools.calls_to("gallery_dl")) == 4  # discover only; download fallback MUST NOT run!
    finally:
        await e.stop()


class _SizedStubDownloader:
    """Stands in for DirectMediaDownloader without any network: `probe_size` reports a fixed CDN size (or
    raises), and direct downloads are refused so `fetch()` falls back to the (fake) extractor tool."""

    def __init__(self, size: int | BaseException | None) -> None:
        self.size = size
        self.probed: list[str] = []

    async def probe_size(self, url, deadline, *, headers=None, timeout=3.0):
        self.probed.append(url)
        if isinstance(self.size, BaseException):
            raise self.size
        return self.size

    async def download_many(self, targets, workspace, deadline, *, max_file_bytes=None):
        raise DirectDownloadUnavailable("stub: direct downloads disabled")

    async def aclose(self) -> None:
        return None


async def test_probe_size_reads_total_from_one_byte_range_without_downloading(tmp_path):
    blob = os.urandom(3_000_000)
    srv = await _MediaBenchServer({"/big.mp4": blob}, support_range=True).start()
    plain = await _MediaBenchServer({"/big.mp4": blob}, support_range=False).start()
    slow = await _MediaBenchServer({"/big.mp4": blob}, delay=2.0).start()
    srv.redirects["/hop.mp4"] = "/big.mp4"
    dl = _loopback_downloader()
    try:
        assert await dl.probe_size(f"{srv.base_url}/big.mp4", Deadline(10)) == len(blob)
        assert srv.hits == [("/big.mp4", "bytes=0-0")], "one 1-byte request, never the body"
        assert await dl.probe_size(f"{srv.base_url}/hop.mp4", Deadline(10)) == len(blob)  # validated redirect
        assert await dl.probe_size(f"{plain.base_url}/big.mp4", Deadline(10)) == len(blob)  # 200 + Content-Length
        assert await dl.probe_size(f"{srv.base_url}/missing.mp4", Deadline(10)) is None  # unknown, not "small"
        t0 = time.monotonic()
        assert await dl.probe_size(f"{slow.base_url}/big.mp4", Deadline(10), timeout=0.2) is None
        assert time.monotonic() - t0 < 1.5, "a slow CDN must not hold the job up"
    finally:
        await dl.aclose()
        await srv.stop()
        await plain.stop()
        await slow.stop()


async def test_direct_url_candidates_skip_url_send_over_telegram_fetch_limit(make_settings):
    from app.core.contracts import Access, ItemDescriptor, Platform, PostType
    from app.extractors.base import Discovered

    async def candidates(size: int | BaseException | None, kind: ItemKind = ItemKind.VIDEO, ext: str = "mp4"):
        stub = _SizedStubDownloader(size)
        backend = LocalExtractionBackend(
            make_settings(), runner=FakeToolRunner(), downloader=stub, enable_direct_telegram_url=True
        )
        try:
            job_id = uuid.uuid4()
            manifest = PostManifest(
                platform=Platform.X,
                post_id="1700000000000000000",
                post_type=PostType.VIDEO if kind is ItemKind.VIDEO else PostType.PHOTO,
                access=Access.PERMITTED_PUBLIC,
                expected_count=1,
                count_verified=True,
                items=[ItemDescriptor(source_item_id="1:1", position=0, kind=kind, width=1920, height=1080)],
            )
            cdn = f"https://video.twimg.com/ext_tw_video/1/pu/vid/1920x1080/a.{ext}"
            cache = backend.adapters[Platform.X].cache  # type: ignore[attr-defined]
            cache.put(
                job_id, Discovered(manifest, "fast_api", nums={0: 1}, urls={0: (cdn, ext)}, direct_telegram_ok=True)
            )
            out = await backend.direct_url_candidates(job_id, manifest, Deadline(5))
            assert cache.peek(job_id) is not None, "the fallback fetch() must still find the discovery"
            assert stub.probed == [cdn]
            return out
        finally:
            await backend.aclose()

    # The real 1080p X video from the latency investigation (24,337,627 bytes): Telegram cannot fetch it.
    assert await candidates(24_337_627) is None
    at_limit = await candidates(20_000_000)
    assert at_limit is not None and at_limit[0].size_bytes == 20_000_000
    small = await candidates(1_200_000)
    assert small is not None and small[0].size_bytes == 1_200_000
    unknown = await candidates(None)  # CDN did not say: keep the URL attempt (Telegram decides, as before)
    assert unknown is not None and unknown[0].size_bytes is None
    assert await candidates(BotError(ErrorCode.INVALID_URL, "private", reason="unsafe_destination")) is None
    assert await candidates(6_000_000, kind=ItemKind.PHOTO, ext="jpg") is None  # photo ceiling is 5 MB


@requires_ffmpeg
async def test_e2e_oversized_video_skips_doomed_url_attempt_and_small_one_still_uses_it(tmp_path, media):
    from tests.e2e import start_e2e

    x_url = "https://x.com/someuser/status/1700000000000000000"
    stub = _SizedStubDownloader(24_337_627)

    async def factory(settings, tools):
        return LocalExtractionBackend(settings, runner=tools, downloader=stub, enable_direct_telegram_url=True)

    e = await start_e2e(tmp_path, media_cache_ttl_seconds=0, extractor_factory=factory)
    try:
        e.tools.gd_dump(x_url, "gd_twitter_video")
        e.tools.gd_files(x_url, {1: media.mp4_h264_aac})

        # Over 20 MB: exactly ONE sendVideo, and it is the multipart upload (no URL attempt first).
        await e.say(x_url, message_id=1)
        await e.idle()
        sends = e.server.calls_of("sendVideo")
        assert len(sends) == 1 and sends[0].files
        assert e.jobs()[-1]["state"] == "COMPLETED"

        # Under 20 MB: Telegram still fetches the CDN URL itself (no download, no upload).
        stub.size = 1_200_000
        await e.say(x_url, message_id=2)
        await e.server.wait_for(lambda: len(e.server.calls_of("sendVideo")) == 2)
        await e.idle()
        second = e.server.calls_of("sendVideo")[1]
        assert second.fields["video"].startswith("https://") and not second.files
        assert stub.probed == ["https://pbs.example.invalid/1.mp4"] * 2
    finally:
        await e.stop()


def test_media_cache_ttl_expiry_and_purge(tmp_path):
    from app.core.contracts import DeliveryMode, Platform
    from app.storage.db import CachedMediaItem, CachedPost, Database
    from tests.helpers import Clock

    clk = Clock()
    db = Database(":memory:", clock=clk)
    try:
        db.migrate()
        entry = CachedPost(
            platform=Platform.X,
            post_id="12345",
            delivery_mode=DeliveryMode.MEDIA,
            expected_count=1,
            items=[CachedMediaItem(0, ItemKind.VIDEO, "video", "fid-1", 1280, 720, 5.0)],
        )
        db.put_media_cache(entry, ttl_seconds=60)
        assert db.get_media_cache(Platform.X, "12345", DeliveryMode.MEDIA) == entry
        assert db.get_media_cache(Platform.X, "12345", DeliveryMode.ORIGINAL) is None

        clk.advance(seconds=61)
        assert db.get_media_cache(Platform.X, "12345", DeliveryMode.MEDIA) is None
        db.purge_expired()
        assert db._conn.execute("SELECT COUNT(*) FROM media_cache").fetchone()[0] == 0
    finally:
        db.close()


# ===================================================================================== Instagram Reels progressive H.264 & direct URL
async def test_instagram_reel_prefers_equal_res_progressive_h264_and_enables_direct_url(tmp_path, make_settings):
    ig_url = "https://www.instagram.com/reel/Chunk8-jurw/"
    fake = FakeToolRunner()
    fake.yt_info(
        ig_url,
        {
            "id": "Chunk8-jurw",
            "extractor_key": "Instagram",
            "width": 720,
            "height": 1280,
            "duration": 5.0,
            "formats": [
                {
                    "format_id": "1",
                    "url": "https://scontent.cdninstagram.com/v/prog_720.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "avc1.64001f",
                    "acodec": "mp4a.40.2",
                    "tbr": 1400.0,
                },
                {
                    "format_id": "dash-vp9",
                    "url": "https://scontent.cdninstagram.com/v/vp9_720.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "vp09.00.31.08",
                    "acodec": "none",
                    "tbr": 2600.0,
                },
                {
                    "format_id": "dash-a",
                    "url": "https://scontent.cdninstagram.com/v/audio.m4a",
                    "protocol": "https",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "tbr": 128.0,
                },
            ],
        },
    )
    backend = LocalExtractionBackend(make_settings(), runner=fake, enable_direct_telegram_url=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        v = validate_url(ig_url)
        req = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=ig_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )
        manifest = await backend.discover(req, ws, Deadline(30))
        cands = await backend.direct_url_candidates(req.job_id, manifest, Deadline(30))
        assert cands is not None and len(cands) == 1
        assert cands[0].url == "https://scontent.cdninstagram.com/v/prog_720.mp4"
        assert (cands[0].width, cands[0].height, cands[0].duration) == (720, 1280, 5.0)
    finally:
        await backend.aclose()


async def test_instagram_reel_never_downscales_dash_to_dimensionless_progressive_and_guards_direct_url(
    tmp_path, make_settings
):
    ig_url = "https://www.instagram.com/reel/Chunk8-jurw/"
    ws = tmp_path / "ws"
    ws.mkdir()
    v = validate_url(ig_url)

    def _req() -> ExtractionRequest:
        return ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=ig_url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )

    # 1. Dimensionless progressive `video_versions` must NOT replace higher-res avc1 DASH;
    #    when silent avc1 DASH is selected (no merge), it is still eligible for direct URL.
    fake = FakeToolRunner()
    fake.yt_info(
        ig_url,
        {
            "id": "Chunk8-jurw",
            "extractor_key": "Instagram",
            "width": 720,
            "height": 1280,
            "formats": [
                {
                    "format_id": "0",
                    "url": "https://scontent.cdninstagram.com/v/prog_480.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": None,
                    "acodec": "none",
                },
                {
                    "format_id": "dash-720-avc",
                    "url": "https://scontent.cdninstagram.com/v/dash_720.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "avc1.64001F",
                    "acodec": "none",
                    "tbr": 3140.0,
                },
            ],
        },
    )
    backend = LocalExtractionBackend(make_settings(), runner=fake, enable_direct_telegram_url=True)
    try:
        r1 = _req()
        m1 = await backend.discover(r1, ws, Deadline(30))
        c1 = await backend.direct_url_candidates(r1.job_id, m1, Deadline(30))
        assert c1 is not None and len(c1) == 1
        assert c1[0].url == "https://scontent.cdninstagram.com/v/dash_720.mp4"
        assert (m1.items[0].width, m1.items[0].height) == (720, 1280)

        # 2. Non-native VP9-only track or cookie-gated URL must NEVER mark direct_telegram_ok.
        for bad_fmt in (
            {
                "format_id": "dash-vp9",
                "url": "https://scontent.cdninstagram.com/v/vp9.mp4",
                "protocol": "https",
                "ext": "mp4",
                "width": 720,
                "height": 1280,
                "vcodec": "vp09.00.31.08",
                "acodec": "none",
            },
            {
                "format_id": "1",
                "url": "https://scontent.cdninstagram.com/v/gated.mp4",
                "protocol": "https",
                "ext": "mp4",
                "width": 720,
                "height": 1280,
                "vcodec": "avc1.64001f",
                "acodec": "mp4a.40.2",
                "cookies": "sessionid=secret_tok",
            },
        ):
            fake.yt_info(ig_url, {"id": "Chunk8-jurw", "extractor_key": "Instagram", "formats": [bad_fmt]})
            r_bad = _req()
            m_bad = await backend.discover(r_bad, ws, Deadline(30))
            assert await backend.direct_url_candidates(r_bad.job_id, m_bad, Deadline(30)) is None
    finally:
        await backend.aclose()
