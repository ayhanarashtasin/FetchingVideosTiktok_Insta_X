"""Unit tests for TikTok web JSON fast-path (`FastApiClient.discover_tiktok`) and `TikTokAdapter` wiring."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from app.core.contracts import Access, ExtractionRequest, Platform, PostType
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.net_guard import tuned_socket_factory
from app.core.url_validator import validate_url
from app.extractors.base import ExtractionContext
from app.extractors.direct_download import DirectMediaDownloader
from app.extractors.fast_api import (
    TIKTOK_REFERER,
    TIKTOK_USER_AGENT,
    FastApiClient,
    _extract_cookie_header,
    _tiktok_redirect_allowed,
    parse_tiktok_html,
    parse_tiktok_rehydration,
)
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.tiktok import TikTokAdapter
from app.extractors.ytdlp_runner import YtDlpRunner
from app.media.quality import is_unplayable_format, ranked_choices, select_format
from tests.fake_tools import FakeToolRunner, fixture

POST_ID = "7300000000000000000"
TT_VIDEO_URL = f"https://www.tiktok.com/@someuser/video/{POST_ID}"
TT_PHOTO_URL = f"https://www.tiktok.com/@someuser/photo/{POST_ID}"


def _rehydration_html(payload: dict[str, Any]) -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(payload)}</script>'
        "</head><body></body></html>"
    )


def _video_payload(
    *,
    post_id: str = POST_ID,
    status_code: Any = 0,
    private_item: bool = False,
    secret: bool = False,
    private_account: bool = False,
    bitrate_info: list[dict[str, Any]] | None = None,
    play_addr: Any = None,
    download_addr: Any = None,
) -> dict[str, Any]:
    video: dict[str, Any] = {"width": 720, "height": 1280, "duration": 15, "codecType": "h264"}
    if bitrate_info is not None:
        video["bitrateInfo"] = bitrate_info
    if play_addr is not None:
        video["playAddr"] = play_addr
    if download_addr is not None:
        video["downloadAddr"] = download_addr
    item: dict[str, Any] = {
        "id": post_id,
        "privateItem": private_item,
        "secret": secret,
        "author": {"uniqueId": "someuser", "privateAccount": private_account},
        "video": video,
    }
    return {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "statusCode": status_code,
                "itemInfo": {"itemStruct": item},
            }
        }
    }


def _photo_payload(images: list[Any], *, post_id: str = POST_ID, status_code: Any = 0) -> dict[str, Any]:
    return {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "statusCode": status_code,
                "itemInfo": {
                    "itemStruct": {
                        "id": post_id,
                        "privateItem": False,
                        "secret": False,
                        "video": {"duration": 0},
                        "imagePost": {"images": images},
                    }
                },
            }
        }
    }


def _make_request(url: str) -> ExtractionRequest:
    v = validate_url(url)
    return ExtractionRequest(
        job_id=uuid.uuid4(),
        owner_id=1,
        chat_id=1,
        origin_message_id=1,
        original_url=url,
        extraction_url=v.extraction_url,
        platform=v.platform,
        post_hint=v.post_hint,
    )


class _LoopbackServer:
    def __init__(self, handler) -> None:
        self._handler = handler
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    async def start(self) -> _LoopbackServer:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handler)
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


def _loopback_fast_api(base_url: str) -> FastApiClient:
    return FastApiClient(
        tiktok_base=base_url,
        allow_http=True,
        connector_factory=lambda: aiohttp.TCPConnector(socket_factory=tuned_socket_factory),
    )


def _loopback_downloader() -> DirectMediaDownloader:
    return DirectMediaDownloader(
        allow_http=True,
        connector_factory=lambda: aiohttp.TCPConnector(socket_factory=tuned_socket_factory),
    )


# ===================================================================================== Video parsing
def test_parse_tiktok_video_codec_mapping_bytevc2_skip_mirror_priority_and_cookies():
    cookie_header = _extract_cookie_header(
        [
            "ttwid=1%7Cabc; Domain=.tiktok.com; Path=/; Expires=Sun, 21 Nov 2027 14:06:09 GMT; HttpOnly; Secure",
            "tt_chain_token=tok==; path=/; domain=.tiktok.com; secure; httponly",
        ]
    )
    assert cookie_header == "ttwid=1%7Cabc; tt_chain_token=tok=="

    payload = _video_payload(
        bitrate_info=[
            {
                "GearName": "bytevc2_1080p_1",
                "CodecType": "bytevc2",
                "Bitrate": 2500000,
                "PlayAddr": {
                    "Width": 1080,
                    "Height": 1920,
                    "DataSize": "4500000",
                    "UrlList": ["https://v16-webapp-prime.tiktok.com/v_bytevc2.mp4"],
                },
            },
            {
                "GearName": "adapt_lowest_1080_1",
                "CodecType": "h265_hvc1",
                "Bitrate": 1800000,
                "PlayAddr": {
                    "Width": 1080,
                    "Height": 1920,
                    "DataSize": "3200000",
                    "UrlList": [
                        "https://v16-webapp-prime.tiktok.com/v_hevc_0.mp4",
                        "https://v19-webapp-prime.tiktok.com/v_hevc_1.mp4",
                        "https://www.tiktok.com/aweme/v1/play/?file_id=abc",
                    ],
                },
            },
            {
                "GearName": "normal_540_0",
                "CodecType": "h264",
                "Bitrate": 900000,
                "PlayAddr": {
                    "Width": 576,
                    "Height": 1024,
                    "DataSize": 1500000,
                    "UrlList": ["https://v16-webapp-prime.tiktok.com/v_h264_0.mp4"],
                },
            },
        ]
    )

    res = parse_tiktok_html(_rehydration_html(payload), POST_ID, cookie_header=cookie_header)
    assert res is not None and not res.is_photo and res.duration == 15.0

    by_id = {f.format_id: f for f in res.formats}
    assert by_id["bytevc2_1080p_1-0"].vcodec == "bytevc2"
    assert is_unplayable_format(by_id["bytevc2_1080p_1-0"])
    assert by_id["adapt_lowest_1080_1-0"].vcodec == "hevc"
    assert by_id["adapt_lowest_1080_1-0"].filesize == 3200000
    assert by_id["adapt_lowest_1080_1-0"].tbr == 1800.0
    assert by_id["normal_540_0-0"].vcodec == "h264"

    # bytevc2 must be filtered out; primary mirror -0 must outrank -1 and -2 at equal resolution.
    choices = ranked_choices(res.formats)
    assert [c.selector for c in choices] == [
        "adapt_lowest_1080_1-0",
        "adapt_lowest_1080_1-1",
        "adapt_lowest_1080_1-2",
        "normal_540_0-0",
    ]
    assert select_format(res.formats).selector == "adapt_lowest_1080_1-0"

    # Primary CDN mirrors include Cookie + Referer + Firefox UA; `/aweme/v1/play/` omits Cookie.
    h0 = res.sources["adapt_lowest_1080_1-0"].headers
    assert h0["Cookie"] == cookie_header
    assert h0["Referer"] == TIKTOK_REFERER
    assert h0["User-Agent"] == TIKTOK_USER_AGENT
    h2 = res.sources["adapt_lowest_1080_1-2"].headers
    assert "Cookie" not in h2 and h2["Referer"] == TIKTOK_REFERER


def test_parse_tiktok_video_falls_back_to_play_addr_and_download_addr():
    payload = _video_payload(
        bitrate_info=[],
        play_addr="https://v16-webapp-prime.tiktok.com/play.mp4",
        download_addr="https://v16-webapp-prime.tiktok.com/download.mp4",
    )
    res = parse_tiktok_rehydration(payload, POST_ID, cookie_header="ttwid=1")
    assert res is not None and not res.is_photo
    choices = ranked_choices(res.formats)
    assert [c.selector for c in choices] == ["play-0", "download-10"]
    assert res.sources["play-0"].url == "https://v16-webapp-prime.tiktok.com/play.mp4"


# ===================================================================================== Photo-mode parsing
def test_parse_tiktok_photo_mode_preserves_order_and_rejects_missing_or_unsafe_url():
    payload = _photo_payload(
        [
            {
                "imageWidth": 1080,
                "imageHeight": 1440,
                "imageURL": {"urlList": ["https://p16-common-sign.tiktokcdn.com/img1~tplv.jpeg?x=1"]},
            },
            {
                "imageWidth": 720,
                "imageHeight": 960,
                "imageURL": {"urlList": ["https://p16-common-sign.tiktokcdn.com/img2~tplv.webp?x=2"]},
            },
        ]
    )
    res = parse_tiktok_rehydration(payload, POST_ID)
    assert res is not None and res.is_photo and len(res.photos) == 2
    assert [(p.num, p.source_item_id, p.extension, p.width, p.height) for p in res.photos] == [
        (1, f"{POST_ID}:1", "jpeg", 1080, 1440),
        (2, f"{POST_ID}:2", "webp", 720, 960),
    ]

    # Missing URL on ANY image must reject the entire photo post (fall back to gallery-dl).
    missing_url = _photo_payload(
        [
            {"imageWidth": 1080, "imageHeight": 1440, "imageURL": {"urlList": ["https://p16.tiktokcdn.com/1.jpeg"]}},
            {"imageWidth": 1080, "imageHeight": 1440, "imageURL": {"urlList": []}},
        ]
    )
    assert parse_tiktok_rehydration(missing_url, POST_ID) is None

    # Non-HTTPS URL on any image is rejected in production mode.
    http_url = _photo_payload(
        [{"imageWidth": 1080, "imageHeight": 1440, "imageURL": {"urlList": ["http://p16.tiktokcdn.com/1.jpeg"]}}]
    )
    assert parse_tiktok_rehydration(http_url, POST_ID, allow_http=False) is None


# ===================================================================================== Safety & identity checks
@pytest.mark.parametrize(
    "payload",
    [
        _video_payload(post_id="7399999999999999999", play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(post_id="", play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(status_code=10204, play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(status_code=10222, play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(status_code=True, play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(private_item=True, play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(secret=True, play_addr="https://v16.tiktok.com/v.mp4"),
        _video_payload(private_account=True, play_addr="https://v16.tiktok.com/v.mp4"),
    ],
)
def test_parse_tiktok_rejects_mismatched_private_secret_or_nonzero_status(payload):
    assert parse_tiktok_rehydration(payload, POST_ID) is None


def test_parse_tiktok_html_rejects_challenge_and_login_pages():
    waf_html = '<!DOCTYPE html><html><script id="slardar-config" type="application/json">{"slardarClient":"SlardarWAF"}</script></html>'
    assert parse_tiktok_html(waf_html, POST_ID) is None
    assert parse_tiktok_html("<html><body>Log in to TikTok</body></html>", POST_ID) is None


def test_tiktok_redirect_policy_enforces_https_and_exact_tiktok_domain():
    assert _tiktok_redirect_allowed("https://www.tiktok.com/@u/video/123", allow_http=False)
    assert _tiktok_redirect_allowed("https://m.tiktok.com/v/123.html", allow_http=False)
    assert not _tiktok_redirect_allowed("http://www.tiktok.com/@u/video/123", allow_http=False)
    assert not _tiktok_redirect_allowed("https://evil.com/@u/video/123", allow_http=False)
    assert not _tiktok_redirect_allowed("https://nottiktok.com/@u/video/123", allow_http=False)
    assert not _tiktok_redirect_allowed("https://127.0.0.1/@u/video/123", allow_http=False)
    assert not _tiktok_redirect_allowed("https://www.tiktok.com:8443/@u/video/123", allow_http=False)


async def test_discover_tiktok_redirects_max_2_hops_and_blocks_off_domain():
    async def handler(request: web.Request) -> web.Response:
        if request.path == f"/@i/video/{POST_ID}":
            return web.Response(
                status=302,
                headers={"Location": f"/@hop1/video/{POST_ID}", "Set-Cookie": "ttwid=hop1; Path=/"},
            )
        if request.path == f"/@hop1/video/{POST_ID}":
            return web.Response(
                status=301,
                headers={"Location": f"/@hop2/video/{POST_ID}", "Set-Cookie": "tt_chain_token=hop2; Path=/"},
            )
        if request.path == f"/@hop2/video/{POST_ID}":
            body = _rehydration_html(_video_payload(play_addr=f"{srv.base_url}/ok.mp4")).encode("utf-8")
            stream = web.StreamResponse(status=200, headers={"Content-Type": "text/html"})
            await stream.prepare(request)
            # Split across multiple chunks to guarantee discover_tiktok reads until EOF, not 1st chunk only.
            mid = len(body) // 2
            await stream.write(body[:mid])
            await stream.write(body[mid:])
            await stream.write_eof()
            return stream
        if request.path == "/@i/video/7300000000000000001":
            return web.Response(status=302, headers={"Location": "https://evil.com/steal"})
        if request.path.startswith("/@i/video/7300000000000000002"):
            return web.Response(status=302, headers={"Location": "/@too1"})
        if request.path == "/@too1":
            return web.Response(status=302, headers={"Location": "/@too2"})
        if request.path == "/@too2":
            return web.Response(status=302, headers={"Location": "/@too3"})
        return web.Response(status=404)

    srv = await _LoopbackServer(handler).start()
    fast = _loopback_fast_api(srv.base_url)
    try:
        # 2 hops succeed and accumulate Set-Cookie headers across redirects.
        res = await fast.discover_tiktok(POST_ID, Deadline(10))
        assert res is not None and res.sources["play-0"].headers["Cookie"] == "ttwid=hop1; tt_chain_token=hop2"
        # Off-domain redirect rejected.
        assert await fast.discover_tiktok("7300000000000000001", Deadline(10)) is None
        # >2 redirect hops rejected.
        assert await fast.discover_tiktok("7300000000000000002", Deadline(10)) is None
    finally:
        await fast.aclose()
        await srv.stop()


async def test_discover_tiktok_blocks_private_dns_via_pinned_resolver():
    async def private_dns(_host: str) -> list[str]:
        return ["127.0.0.1"]

    fast = FastApiClient(dns=private_dns)
    try:
        with pytest.raises(BotError) as exc:
            await fast.discover_tiktok(POST_ID, Deadline(10))
        assert exc.value.code is ErrorCode.INVALID_URL and exc.value.reason == "unsafe_destination"
    finally:
        await fast.aclose()


# ===================================================================================== TikTokAdapter integration
async def test_tiktok_adapter_fast_video_downloads_directly_and_falls_back_on_403(tmp_path):
    video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"A" * 4096
    seen_video_headers: list[dict[str, str]] = []
    video_status = {"code": 200}

    async def handler(request: web.Request) -> web.Response:
        if request.path == f"/@i/video/{POST_ID}":
            payload = _video_payload(
                bitrate_info=[
                    {
                        "GearName": "custom_fast_720p",
                        "CodecType": "h264",
                        "Bitrate": 1200000,
                        "PlayAddr": {
                            "Width": 720,
                            "Height": 1280,
                            "DataSize": len(video_bytes),
                            "UrlList": [f"{srv.base_url}/clip.mp4"],
                        },
                    }
                ]
            )
            return web.Response(
                status=200,
                text=_rehydration_html(payload),
                content_type="text/html",
                headers={"Set-Cookie": "tt_chain_token=chain123; Path=/; Domain=.tiktok.com; HttpOnly"},
            )
        if request.path == "/clip.mp4":
            seen_video_headers.append(dict(request.headers))
            if video_status["code"] != 200:
                return web.Response(status=video_status["code"], text="forbidden")
            return web.Response(status=200, body=video_bytes, headers={"Content-Type": "video/mp4"})
        return web.Response(status=404)

    srv = await _LoopbackServer(handler).start()
    fast = _loopback_fast_api(srv.base_url)
    dl = _loopback_downloader()
    fake = FakeToolRunner()
    ctx = ExtractionContext(
        ytdlp=YtDlpRunner(fake),
        gallerydl=GalleryDlRunner(fake),
        downloader=dl,
        fast_api=fast,
    )
    adapter = TikTokAdapter(ctx)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        # 1. Fast-path video succeeds with ZERO yt-dlp or gallery-dl calls.
        req1 = _make_request(TT_VIDEO_URL)
        m1 = await adapter.discover(req1, ws, Deadline(15))
        assert m1.platform is Platform.TIKTOK and m1.post_type is PostType.VIDEO and m1.count_verified
        cached1 = adapter.cache.peek(req1.job_id)
        assert cached1 is not None and cached1.source == "fast_tiktok" and not cached1.direct_telegram_ok
        out1 = await adapter.fetch(req1, m1, ws, Deadline(15))
        assert len(out1.items) == 1 and out1.items[0].path.read_bytes() == video_bytes
        assert seen_video_headers[0].get("Cookie") == "tt_chain_token=chain123"
        assert seen_video_headers[0].get("Referer") == TIKTOK_REFERER
        assert fake.calls_to("yt_dlp") == [] and fake.calls_to("gallery_dl") == []

        # 2. CDN returns 403 -> falls back cleanly to _discover_ytdlp + _fetch_ytdlp, NEVER calling yt-dlp with custom_fast_720p-0.
        video_status["code"] = 403
        fallback_file = tmp_path / "fallback.mp4"
        fallback_file.write_bytes(video_bytes)
        fake.yt_info(TT_VIDEO_URL, "ytdlp_tiktok_video")
        fake.yt_file(TT_VIDEO_URL, fallback_file)

        req2 = _make_request(TT_VIDEO_URL)
        m2 = await adapter.discover(req2, ws, Deadline(15))
        out2 = await adapter.fetch(req2, m2, ws, Deadline(15))
        assert len(out2.items) == 1 and out2.items[0].source_format == "bytevc1_720p_1"
        yt_calls = fake.calls_to("yt_dlp")
        assert len(yt_calls) == 2  # discover (-J) + download (-f bytevc1_720p_1)
        assert all("custom_fast_720p-0" not in " ".join(c) for c in yt_calls)
    finally:
        await fast.aclose()
        await dl.aclose()
        await srv.stop()


async def test_tiktok_adapter_fast_photo_downloads_directly_and_falls_back_to_gallerydl(tmp_path):
    p1_bytes, p2_bytes = b"\xff\xd8\xff\xe0PHOTO1", b"\xff\xd8\xff\xe0PHOTO2"
    photo_status = {"code": 200}

    async def handler(request: web.Request) -> web.Response:
        if request.path == f"/@i/video/{POST_ID}":
            payload = _photo_payload(
                [
                    {
                        "imageWidth": 1080,
                        "imageHeight": 1920,
                        "imageURL": {"urlList": [f"{srv.base_url}/img1.jpeg"]},
                    },
                    {
                        "imageWidth": 1080,
                        "imageHeight": 1440,
                        "imageURL": {"urlList": [f"{srv.base_url}/img2.jpeg"]},
                    },
                ]
            )
            return web.Response(status=200, text=_rehydration_html(payload), content_type="text/html")
        if request.path in {"/img1.jpeg", "/img2.jpeg"}:
            if photo_status["code"] != 200:
                return web.Response(status=photo_status["code"], text="expired")
            data = p1_bytes if request.path == "/img1.jpeg" else p2_bytes
            return web.Response(status=200, body=data, headers={"Content-Type": "image/jpeg"})
        return web.Response(status=404)

    srv = await _LoopbackServer(handler).start()
    fast = _loopback_fast_api(srv.base_url)
    dl = _loopback_downloader()
    fake = FakeToolRunner()
    ctx = ExtractionContext(
        ytdlp=YtDlpRunner(fake),
        gallerydl=GalleryDlRunner(fake),
        downloader=dl,
        fast_api=fast,
    )
    adapter = TikTokAdapter(ctx)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        # 1. Fast-path photo post succeeds directly with ZERO gallery-dl calls & direct_telegram_ok=True.
        req1 = _make_request(TT_PHOTO_URL)
        m1 = await adapter.discover(req1, ws, Deadline(15))
        assert m1.post_type is PostType.CAROUSEL and m1.expected_count == 2 and m1.count_verified
        cached1 = adapter.cache.peek(req1.job_id)
        assert cached1 is not None and cached1.source == "fast_tiktok" and cached1.direct_telegram_ok is True
        out1 = await adapter.fetch(req1, m1, ws, Deadline(15))
        assert [i.path.read_bytes() for i in out1.items] == [p1_bytes, p2_bytes]
        assert out1.omissions == [] and fake.calls_to("gallery_dl") == []

        # 2. Direct photo download 403 -> falls back cleanly to _discover_gallerydl + _fetch_gallerydl.
        photo_status["code"] = 403
        fb_jpg = tmp_path / "fb.jpg"
        fb_jpg.write_bytes(p1_bytes)
        fake.gd_dump(TT_PHOTO_URL, "gd_tiktok_photo_1")
        fake.gd_files(TT_PHOTO_URL, {1: fb_jpg})

        req2 = _make_request(TT_PHOTO_URL)
        m2 = await adapter.discover(req2, ws, Deadline(15))
        out2 = await adapter.fetch(req2, m2, ws, Deadline(15))
        assert len(out2.items) == 1 and out2.items[0].path.read_bytes() == p1_bytes
        assert len(fake.calls_to("gallery_dl")) == 2  # fallback discover + download
    finally:
        await fast.aclose()
        await dl.aclose()
        await srv.stop()


async def test_tiktok_adapter_challenge_falls_back_to_ytdlp_error_classification_and_gallerydl_sets_direct_ok(
    tmp_path: Path,
):
    async def waf_handler(_request: web.Request) -> web.Response:
        return web.Response(status=200, text="<html>SlardarWAF challenge</html>", content_type="text/html")

    srv = await _LoopbackServer(waf_handler).start()
    fast = _loopback_fast_api(srv.base_url)
    fake = FakeToolRunner()
    ctx = ExtractionContext(ytdlp=YtDlpRunner(fake), gallerydl=GalleryDlRunner(fake), fast_api=fast)
    adapter = TikTokAdapter(ctx)
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        # Challenge returns None from fast-path -> yt-dlp classifies ACCESS_RESTRICTED without bypass.
        fake.yt_fails(TT_VIDEO_URL, "ERROR: [TikTok] 7300000000000000000: Video unavailable. This video is private")
        with pytest.raises(BotError) as exc:
            await adapter.discover(_make_request(TT_VIDEO_URL), ws, Deadline(15))
        assert exc.value.code is ErrorCode.ACCESS_RESTRICTED

        # Photo fallback via gallery-dl marks cookie-free TikTok photos direct_telegram_ok=True.
        fake.gd_dump(TT_PHOTO_URL, fixture("gd_tiktok_photo_1"))
        req_photo = _make_request(TT_PHOTO_URL)
        m_photo = await adapter.discover(req_photo, ws, Deadline(15))
        assert m_photo.access is Access.PERMITTED_PUBLIC and m_photo.post_type is PostType.PHOTO
        cached = adapter.cache.peek(req_photo.job_id)
        assert cached is not None and cached.source == "gallerydl" and cached.direct_telegram_ok is True
    finally:
        await fast.aclose()
        await srv.stop()
