"""FFprobe-based media validation (T04.1, FR-04, architecture section 9).

Probing is purely LOCAL: `-protocol_whitelist file` stops a malicious playlist/HLS/concat file
from making ffprobe reach out to the network, and the path is always an absolute file inside a
job workspace. (`-nostdin` is an ffmpeg-only flag - ffprobe aborts on it - so stdin is closed
by the process runner instead.)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.contracts import ItemKind
from app.core.errors import BotError, ErrorCode
from app.services.subprocess_runner import ProcessRunner, SubprocessRunner, sanitized_env

_IMAGE_FORMATS = {
    "image2": "jpeg",
    "jpeg_pipe": "jpeg",
    "png_pipe": "png",
    "webp_pipe": "webp",
    "gif": "gif",
    "bmp_pipe": "bmp",
    "tiff_pipe": "tiff",
    "avif": "avif",
    "heif": "heif",
    "apng": "png",
}
_MIME = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "avif": "image/avif",
    "heif": "image/heif",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "flv": "video/x-flv",
    "mpegts": "video/mp2t",
    "avi": "video/x-msvideo",
}
HASH_VIDEO_LIMIT = 50_000_000  # hashing a 2 GB video costs seconds; photos (the hash use-case) always hashed


@dataclass(frozen=True)
class ProbeInfo:
    kind: ItemKind
    container: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    video_codec: str | None
    audio_codec: str | None
    pix_fmt: str | None
    has_audio: bool
    sha256: str | None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return round(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                return 0
    try:
        return int(stream.get("tags", {}).get("rotate", 0)) % 360
    except (TypeError, ValueError):
        return 0


def _container(format_name: str, ext: str, brand: str | None) -> str:
    names = set(format_name.split(","))
    if names & set(_IMAGE_FORMATS):
        return next(_IMAGE_FORMATS[n] for n in format_name.split(",") if n in _IMAGE_FORMATS)
    if "mov" in names or "mp4" in names:
        return "mov" if (brand or "").strip().lower() == "qt" or ext == ".mov" else "mp4"
    if "matroska" in names or "webm" in names:
        return "webm" if ext == ".webm" else "mkv"
    if "flv" in names:
        return "flv"
    if "mpegts" in names:
        return "mpegts"
    if "avi" in names:
        return "avi"
    return "other"


def parse_probe(data: dict, path: Path, size: int) -> ProbeInfo:
    """Turn ffprobe JSON into a `ProbeInfo`; raise MEDIA_INVALID for anything we must not send."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    container = _container(
        str(fmt.get("format_name", "")), path.suffix.lower(), (fmt.get("tags") or {}).get("major_brand")
    )

    if container in {v for v in _IMAGE_FORMATS.values()}:
        kind = ItemKind.PHOTO
    elif video is not None:
        kind = ItemKind.VIDEO
    else:
        raise BotError(ErrorCode.MEDIA_INVALID, "no video stream (audio-only or unrecognised file)")
    if video is None:
        raise BotError(ErrorCode.MEDIA_INVALID, "no decodable image/video stream")

    width, height = video.get("width"), video.get("height")
    if not width or not height:
        raise BotError(ErrorCode.MEDIA_INVALID, "missing dimensions")
    if _rotation(video) in {90, 270}:
        width, height = height, width  # display orientation, so aspect ratio is preserved
    duration = None
    if kind is ItemKind.VIDEO:
        raw = fmt.get("duration") or video.get("duration")
        try:
            duration = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None and duration <= 0:
            raise BotError(ErrorCode.MEDIA_INVALID, "zero-length video")
    return ProbeInfo(
        kind=kind,
        container=container,
        mime_type=_MIME.get(container, "application/octet-stream"),
        size_bytes=size,
        width=int(width),
        height=int(height),
        duration_seconds=duration,
        video_codec=(video.get("codec_name") if kind is ItemKind.VIDEO else None),
        audio_codec=(audio.get("codec_name") if audio else None),
        pix_fmt=video.get("pix_fmt"),
        has_audio=audio is not None,
        sha256=None,
    )


async def probe_file(
    path: Path,
    *,
    runner: ProcessRunner | None = None,
    ffprobe: str | None = None,
    timeout: float = 30.0,
) -> ProbeInfo:
    """Probe one local file. Never raises anything but BotError(MEDIA_INVALID/DOWNLOAD_TIMEOUT)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        raise BotError(ErrorCode.MEDIA_INVALID, "file missing") from None
    if size == 0:
        raise BotError(ErrorCode.MEDIA_INVALID, "empty file")
    exe = ffprobe or shutil.which("ffprobe")
    if not exe:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "ffprobe is not installed")
    result = await (runner or SubprocessRunner()).run(
        [
            exe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        cwd=path.parent,
        env=sanitized_env(path.parent),
        timeout=timeout,
        max_stdout=2_000_000,
    )
    if result.timed_out:
        raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "ffprobe timed out")
    if result.returncode != 0:
        raise BotError(ErrorCode.MEDIA_INVALID, f"ffprobe rejected the file: {result.stderr_text()[-200:]}")
    try:
        data = json.loads(result.stdout_text())
    except ValueError:
        raise BotError(ErrorCode.MEDIA_INVALID, "ffprobe output was not JSON") from None
    info = parse_probe(data, path, size)
    if info.kind is ItemKind.PHOTO or size <= HASH_VIDEO_LIMIT:
        digest = await asyncio.to_thread(_sha256, path)
        info = ProbeInfo(**{**info.__dict__, "sha256": digest})
    return info


def file_name_is_safe(name: str) -> bool:
    """Extractor-produced file names are only ever used inside the workspace, but reject the
    obviously hostile anyway (path separators, control characters, option-looking names)."""
    return (
        bool(name) and name == os.path.basename(name) and not name.startswith("-") and all(ord(c) >= 32 for c in name)
    )
