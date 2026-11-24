"""Shared fixtures.

Synthetic media is generated on demand with FFmpeg's lavfi sources into a session temp dir:
nothing binary is committed and no third-party media is ever used (T00.7).
"""

from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import Settings

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available on PATH"
)


def fake_bot_token() -> str:
    # Built at runtime so that no token-shaped literal exists in the repository (secret scan).
    return "123456789" + ":" + "A" * 35


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: object) -> Settings:
        base: dict[str, object] = {
            "bot_token": fake_bot_token(),
            "allowed_user_ids": [111],
            "data_dir": tmp_path / "data",
            "work_dir": tmp_path / "work",
            "status_edit_min_interval_seconds": 0,
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)  # type: ignore[arg-type]

    return _make


def _ffmpeg(*args: str) -> None:
    assert FFMPEG is not None
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


@dataclass(frozen=True)
class SyntheticMedia:
    root: Path
    jpg: Path
    png: Path
    tall_jpg: Path  # aspect ratio > 20 -> not a valid native Telegram photo
    mp4_h264_aac: Path
    mp4_h264_silent: Path
    mkv_h264_aac: Path
    webm_vp9_opus: Path
    mp4_hevc_aac: Path
    video_only_mp4: Path  # separate streams for the merge test
    audio_only_m4a: Path
    broken_jpg: Path


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> SyntheticMedia:
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg/ffprobe not available on PATH")
    root = tmp_path_factory.mktemp("synthetic_media")
    src = "testsrc=size=160x120:rate=10"
    tone = "sine=frequency=440:sample_rate=44100"
    m = SyntheticMedia(
        root=root,
        jpg=root / "photo.jpg",
        png=root / "photo.png",
        tall_jpg=root / "tall.jpg",
        mp4_h264_aac=root / "v_h264_aac.mp4",
        mp4_h264_silent=root / "v_h264_silent.mp4",
        mkv_h264_aac=root / "v_h264_aac.mkv",
        webm_vp9_opus=root / "v_vp9_opus.webm",
        mp4_hevc_aac=root / "v_hevc_aac.mp4",
        video_only_mp4=root / "video_only.mp4",
        audio_only_m4a=root / "audio_only.m4a",
        broken_jpg=root / "broken.jpg",
    )
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=1", "-frames:v", "1", str(m.jpg))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=200x100:rate=1", "-frames:v", "1", str(m.png))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=40x1000:rate=1", "-frames:v", "1", str(m.tall_jpg))
    common_v = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    _ffmpeg("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", tone, "-t", "1", *common_v,
            "-c:a", "aac", "-shortest", str(m.mp4_h264_aac))
    _ffmpeg("-f", "lavfi", "-i", src, "-t", "1", *common_v, "-an", str(m.mp4_h264_silent))
    _ffmpeg("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", tone, "-t", "1", *common_v,
            "-c:a", "aac", "-shortest", str(m.mkv_h264_aac))
    _ffmpeg("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", tone, "-t", "1",
            "-c:v", "libvpx-vp9", "-b:v", "200k", "-deadline", "realtime", "-cpu-used", "8",
            "-c:a", "libopus", "-shortest", str(m.webm_vp9_opus))
    _ffmpeg("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", tone, "-t", "1",
            "-c:v", "libx265", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-tag:v", "hvc1", "-c:a", "aac", "-shortest", str(m.mp4_hevc_aac))
    _ffmpeg("-f", "lavfi", "-i", src, "-t", "1", *common_v, "-an", str(m.video_only_mp4))
    _ffmpeg("-f", "lavfi", "-i", tone, "-t", "1", "-c:a", "aac", str(m.audio_only_m4a))
    m.broken_jpg.write_bytes(b"<html><body>429 Too Many Requests</body></html>")
    return m


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture(scope="session")
def media_server(media: SyntheticMedia) -> Iterator[str]:
    """Loopback HTTP server over the synthetic media. Used ONLY to exercise real extractor
    binaries (gallery-dl `directlink`, yt-dlp `generic`) without touching any platform."""
    handler = functools.partial(_QuietHandler, directory=str(media.root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def stream_hash(path: Path, selector: str = "0:v:0") -> str:
    """SHA-256 of the *packets* of one stream (container-independent).

    Equal hashes before/after a remux prove the compressed stream was copied, not re-encoded
    (AT15/AT16).
    """
    assert FFMPEG is not None
    out = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-i", str(path), "-map", selector, "-c", "copy",
         "-f", "streamhash", "-hash", "sha256", "-"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    return out.rsplit("=", 1)[-1]
