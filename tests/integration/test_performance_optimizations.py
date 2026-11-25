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
        assert srv.hits[0][1] == "bytes=0-1048575"
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
def test_tracking_session_tunes_sockets_and_sends_known_content_length(tmp_path):
    session = TrackingSession()
    assert session._connector_init["keepalive_timeout"] == 60.0
    assert session._connector_init["socket_factory"] is tuned_socket_factory

    sock = tuned_socket_factory((socket.AF_INET, socket.SOCK_STREAM, 0))
    try:
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) >= TCP_BUFFER_BYTES
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) >= TCP_BUFFER_BYTES
    finally:
        sock.close()

    p = tmp_path / "clip.mp4"
    p.write_bytes(b"0123456789" * 100)
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        bot, _ = make_bot()
        form = session.build_form_data(bot, SendVideo(chat_id=1, video=FdInputFile(fd, "clip.mp4", 1000)))
        mp = form()
        assert mp.size is not None and mp.size > 1000, "FdInputFile uploads should carry exact Content-Length"
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
        assert all(rng == "bytes=0-1048575" for _, rng in hits_1080)

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
