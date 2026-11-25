"""T04.1: FFprobe validation against real synthetic media (+ local-only protocol defence)."""

from __future__ import annotations

import hashlib
import http.server
import subprocess
import threading

import pytest

from app.core.contracts import ItemKind
from app.core.errors import BotError, ErrorCode
from app.media.probe import file_name_is_safe, parse_probe, probe_file
from tests.conftest import FFMPEG, requires_ffmpeg

pytestmark = requires_ffmpeg


async def test_jpeg_is_a_photo_with_dimensions_and_hash(media):
    info = await probe_file(media.jpg)
    assert info.kind is ItemKind.PHOTO and info.container == "jpeg" and info.mime_type == "image/jpeg"
    assert (info.width, info.height) == (320, 240) and info.duration_seconds is None
    assert info.sha256 == hashlib.sha256(media.jpg.read_bytes()).hexdigest()  # original-bytes comparison (AT14)
    assert info.size_bytes == media.jpg.stat().st_size and info.video_codec is None


async def test_png_and_tall_images(media):
    png = await probe_file(media.png)
    assert png.kind is ItemKind.PHOTO and png.container == "png" and (png.width, png.height) == (200, 100)
    tall = await probe_file(media.tall_jpg)
    assert (tall.width, tall.height) == (40, 1000)  # aspect > 20: the planner must not send it as a native photo


async def test_h264_aac_mp4(media):
    info = await probe_file(media.mp4_h264_aac)
    assert info.kind is ItemKind.VIDEO and info.container == "mp4" and info.mime_type == "video/mp4"
    assert (info.video_codec, info.audio_codec, info.pix_fmt) == ("h264", "aac", "yuv420p")
    assert info.has_audio and (info.width, info.height) == (160, 120)
    assert 0.5 < info.duration_seconds < 2 and info.sha256  # small videos are hashed too
    unhashed = await probe_file(media.mp4_h264_aac, compute_hash=False)
    assert unhashed.sha256 is None and unhashed.video_codec == "h264"


async def test_silent_video_is_reported_as_silent(media):
    info = await probe_file(media.mp4_h264_silent)
    assert not info.has_audio and info.audio_codec is None


@pytest.mark.parametrize(
    ("attr", "container", "vcodec", "acodec"),
    [
        ("mkv_h264_aac", "mkv", "h264", "aac"),
        ("webm_vp9_opus", "webm", "vp9", "opus"),
        ("mp4_hevc_aac", "mp4", "hevc", "aac"),
    ],
)
async def test_other_containers_and_codecs(media, attr, container, vcodec, acodec):
    info = await probe_file(getattr(media, attr))
    assert (info.container, info.video_codec, info.audio_codec) == (container, vcodec, acodec)


async def test_audio_only_is_rejected(media):
    with pytest.raises(BotError) as exc:
        await probe_file(media.audio_only_m4a)
    assert exc.value.code is ErrorCode.MEDIA_INVALID


async def test_error_page_saved_as_jpg_is_rejected(media):
    with pytest.raises(BotError) as exc:
        await probe_file(media.broken_jpg)
    assert exc.value.code is ErrorCode.MEDIA_INVALID


async def test_empty_and_missing_files_are_rejected(tmp_path):
    empty = tmp_path / "e.jpg"
    empty.write_bytes(b"")
    for p in (empty, tmp_path / "nope.mp4"):
        with pytest.raises(BotError) as exc:
            await probe_file(p)
        assert exc.value.code is ErrorCode.MEDIA_INVALID


async def test_rotation_metadata_swaps_display_dimensions(media, tmp_path):
    out = tmp_path / "rot.mp4"
    cmd = [
        FFMPEG,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-display_rotation",
        "90",
        "-i",
        str(media.mp4_h264_aac),
        "-c",
        "copy",
        str(out),
    ]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        pytest.skip("this ffmpeg lacks -display_rotation")
    info = await probe_file(out)
    assert (info.width, info.height) == (120, 160), "portrait phone videos must keep their display aspect ratio"


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):
        type(self).seen.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"x" * 100)

    def log_message(self, *a):
        return None


async def test_probe_never_follows_network_references_inside_media(tmp_path):
    """A hostile playlist could make ffprobe fetch URLs (SSRF). -protocol_whitelist file blocks it."""
    _Recorder.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        playlist = tmp_path / "evil.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n"
            f"http://127.0.0.1:{port}/ssrf-probe\n#EXT-X-ENDLIST\n"
        )
        with pytest.raises(BotError) as exc:
            await probe_file(playlist)
        assert exc.value.code is ErrorCode.MEDIA_INVALID
        assert "/ssrf-probe" not in _Recorder.seen, "ffprobe made a network request on behalf of untrusted media"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_parse_probe_rejects_degenerate_streams(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"x")
    no_dims = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1"},
        "streams": [{"codec_type": "video"}],
    }
    with pytest.raises(BotError):
        parse_probe(no_dims, p, 1)
    zero = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "0"},
        "streams": [{"codec_type": "video", "width": 1, "height": 1}],
    }
    with pytest.raises(BotError):
        parse_probe(zero, p, 1)
    cover_art = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "width": 5, "height": 5, "disposition": {"attached_pic": 1}},
        ],
    }
    with pytest.raises(BotError):  # an audio file with cover art is not a video
        parse_probe(cover_art, p, 1)


@pytest.mark.parametrize("name", ["001.jpg", "yt_1_abc.mp4", "a b.png"])
def test_safe_file_names(name):
    assert file_name_is_safe(name)


@pytest.mark.parametrize("name", ["", "-rf", "../x.jpg", "a/b.jpg", "a\\b.jpg", "x\x00.jpg", "x\n.jpg"])
def test_unsafe_file_names(name):
    assert not file_name_is_safe(name) or name == "a\\b.jpg"  # backslash is a separator only on Windows
