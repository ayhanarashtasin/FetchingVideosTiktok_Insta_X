"""Lossless media preparation (T04.2, FR-05, FR-07, architecture section 9).

The ONLY transformation this project ever applies to a video is a stream-copy remux into MP4 so
that an otherwise Telegram-compatible stream (H.264/AAC in MKV/TS/FLV...) can be sent natively.
`-c copy` never touches the compressed data; after the remux the result is re-probed and, if it is not
provably the same streams, the output is discarded and the ORIGINAL file is kept (it will then go out
as a document). There is no transcode path, no scaling, no watermark handling.

FFmpeg is told `-protocol_whitelist file` and `-nostdin`, takes only local absolute paths we generate,
and never receives a URL.
"""

from __future__ import annotations

import logging
import shutil

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset, Processing
from app.media.probe import probe_file
from app.media.quality import can_remux_to_mp4, is_native_video
from app.services.subprocess_runner import ProcessRunner, SubprocessRunner, sanitized_env

log = logging.getLogger(__name__)
DURATION_TOLERANCE = 0.5  # seconds: container rounding only; a bigger gap means something was cut


async def remux_to_mp4(
    asset: MediaAsset, *, runner: ProcessRunner, ffmpeg: str, timeout: float = 120.0, min_free_bytes: int = 0
) -> MediaAsset | None:
    """Stream-copy `asset` into an MP4. Returns the new asset, or None if it cannot be done losslessly."""
    src = asset.local_path
    dest = src.with_name(f"rm_{asset.position:03d}.mp4")
    args = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file", "-i", str(src),
        "-map", "0:v", "-map", "0:a?",  # all video + audio streams; never data/subtitle/attachment streams
        "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dest),
    ]  # fmt: skip
    res = await runner.run(
        args,
        cwd=src.parent,
        env=sanitized_env(src.parent),
        timeout=timeout,
        max_stderr=20_000,
        watch_dir=src.parent,  # the output is a second copy of the video: stop if the disk is running out
        min_free_bytes=min_free_bytes,
    )
    if res.returncode != 0 or res.timed_out or res.killed_for_budget or not dest.exists():
        dest.unlink(missing_ok=True)
        return None
    try:
        probed = await probe_file(dest, runner=runner)
    except Exception:
        dest.unlink(missing_ok=True)
        return None
    lossless = (
        probed.kind is ItemKind.VIDEO
        and probed.video_codec == asset.video_codec
        and probed.audio_codec == asset.audio_codec  # audio retained, bit-identical codec
        and (probed.width, probed.height) == (asset.width, asset.height)
        and (
            asset.duration_seconds is None
            or probed.duration_seconds is None
            or abs(probed.duration_seconds - asset.duration_seconds) <= DURATION_TOLERANCE
        )
    )
    if not lossless:
        dest.unlink(missing_ok=True)
        return None
    src.unlink(missing_ok=True)  # the remuxed file replaces it; keep the job directory small
    return asset.model_copy(
        update={
            "local_path": dest.resolve(),
            "size_bytes": probed.size_bytes,
            "container": probed.container,
            "mime_type": probed.mime_type,
            "sha256": probed.sha256,
            "processing": Processing.REMUXED,
        }
    )


async def prepare_assets(
    assets: list[MediaAsset],
    *,
    mode: DeliveryMode,
    native_hevc: bool = False,
    runner: ProcessRunner | None = None,
    ffmpeg: str | None = None,
    min_free_bytes: int = 0,
) -> list[MediaAsset]:
    """Apply the lossless remux where (and only where) it changes native playability.

    Original-files mode never remuxes: it delivers exactly what was downloaded (or merged by yt-dlp's
    stream copy). Photos are never touched.
    """
    if mode is DeliveryMode.ORIGINAL:
        return assets
    # `ffmpeg=None` -> look it up on PATH; an explicit empty string means "FFmpeg is unavailable".
    exe = shutil.which("ffmpeg") if ffmpeg is None else ffmpeg
    run = runner or SubprocessRunner()
    out: list[MediaAsset] = []
    for asset in assets:
        wants_remux = (
            bool(exe)
            and asset.kind is ItemKind.VIDEO
            and not is_native_video(
                container=asset.container, video_codec=asset.video_codec, audio_codec=asset.audio_codec,
                pix_fmt=asset.pix_fmt, native_hevc=native_hevc,
            )
            and can_remux_to_mp4(
                video_codec=asset.video_codec, audio_codec=asset.audio_codec, pix_fmt=asset.pix_fmt,
                native_hevc=native_hevc,
            )
        )  # fmt: skip
        if wants_remux:
            assert exe is not None
            remuxed = await remux_to_mp4(asset, runner=run, ffmpeg=exe, min_free_bytes=min_free_bytes)
            if remuxed is not None:
                out.append(remuxed)
                continue
            log.info("lossless remux not possible; keeping the original file (will be sent as a document)")
        out.append(asset)
    return out
