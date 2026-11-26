"""Media preparation (T04.2, FR-05, FR-07, architecture section 9).

Tries a lossless stream-copy remux into MP4 first whenever the source codecs are already
Telegram-compatible (H.264/AAC in MKV/TS/FLV...). In Media mode, if a video is still not natively
playable in Telegram (HEVC when `native_hevc=false`, 10-bit/non-yuv420p, VP9, AV1, Opus audio, or
a failed remux), falls back to transcoding to H.264 (`yuv420p`) + AAC `.mp4` (`+faststart`) so
Telegram plays it inline at full resolution. Original-files mode never remuxes or transcodes.

FFmpeg is told `-protocol_whitelist file` and `-nostdin`, takes only local absolute paths we generate,
and never receives a URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset, Processing
from app.core.deadline import Deadline
from app.logging_setup import log_event
from app.media.probe import ProbeInfo, probe_file
from app.media.quality import NATIVE_AUDIO_CODECS, _audio_family, can_remux_to_mp4, is_native_video
from app.services.subprocess_runner import ProcessRunner, SubprocessRunner, sanitized_env

log = logging.getLogger(__name__)
DURATION_TOLERANCE = 0.5  # seconds: container rounding only; a bigger gap means something was cut
REMUX_CONCURRENCY = 2
TRANSCODE_CRF = "25"  # ~25% smaller uploads than CRF 23 while keeping clean mobile/desktop quality
NVENC_CQ = "33"  # matches libx264 CRF 25 file size while encoding ~2-3x faster on NVIDIA GPUs
_HW_ENCODER_CACHE: dict[str, str | None] = {}


def _probe_hw_h264_sync(ffmpeg: str) -> str | None:
    """Return `'h264_nvenc'` if working on this host, else `None`. Result is cached per ffmpeg binary."""
    if ffmpeg in _HW_ENCODER_CACHE:
        return _HW_ENCODER_CACHE[ffmpeg]
    try:
        res = subprocess.run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:r=1", "-frames:v", "1",
                "-c:v", "h264_nvenc", "-preset", "p2", "-pix_fmt", "yuv420p", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )  # fmt: skip
        found = "h264_nvenc" if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        found = None
    _HW_ENCODER_CACHE[ffmpeg] = found
    return found


async def _hw_h264_encoder(ffmpeg: str, runner: ProcessRunner) -> str | None:
    if not isinstance(runner, SubprocessRunner):
        return None
    if ffmpeg in _HW_ENCODER_CACHE:
        return _HW_ENCODER_CACHE[ffmpeg]
    return await asyncio.to_thread(_probe_hw_h264_sync, ffmpeg)


def _transcode_encode_args(
    *,
    audio_codec: str | None,
    duration_seconds: float | None,
    max_file_bytes: int | None,
    hw_encoder: str | None = None,
) -> list[str]:
    """Shared H.264 (yuv420p) + AAC encoding flags for single-file transcode and split-track merge."""
    audio_fam = _audio_family(audio_codec) if audio_codec else None
    keep_audio = audio_fam in NATIVE_AUDIO_CODECS
    audio_args = ["-c:a", "copy"] if keep_audio else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    rate_args: list[str] = []
    if max_file_bytes and duration_seconds and duration_seconds > 0:
        audio_kbps = 192 if keep_audio else (128 if audio_fam else 0)
        total_kbps = int((max_file_bytes * 0.88 * 8) / (duration_seconds * 1000))
        video_max_kbps = max(150, total_kbps - audio_kbps)
        # Only cap VBV when needed (< 50 Mbps); huge caps (e.g. 1.9 GB on short clips) overflow x264's 32-bit VBV.
        if video_max_kbps < 50_000:
            rate_args = ["-maxrate", f"{video_max_kbps}k", "-bufsize", f"{video_max_kbps * 2}k"]
    if hw_encoder == "h264_nvenc":
        vcodec_args = [
            "-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", NVENC_CQ, "-b:v", "0",
            *rate_args, "-pix_fmt", "yuv420p",
        ]  # fmt: skip
    else:
        vcodec_args = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", TRANSCODE_CRF, *rate_args, "-pix_fmt", "yuv420p",
        ]  # fmt: skip
    return [
        "-vf", "scale=max(2\\,trunc(iw/2)*2):max(2\\,trunc(ih/2)*2)",
        *vcodec_args,
        *audio_args,
    ]  # fmt: skip


async def _run_ffmpeg_mp4(
    asset: MediaAsset,
    *,
    prefix: str,
    build_args: Callable[[Path, Path], list[str]],
    runner: ProcessRunner,
    timeout: float,
    min_free_bytes: int,
    valid_probe: Callable[[ProbeInfo], bool],
    processing: Processing,
    max_file_bytes: int | None = None,
) -> MediaAsset | None:
    src = asset.local_path
    dest = src.with_name(f"{prefix}_{asset.position:03d}.mp4")
    res = await runner.run(
        build_args(src, dest),
        cwd=src.parent,
        env=sanitized_env(src.parent),
        timeout=timeout,
        max_stderr=20_000,
        watch_dir=src.parent,  # second copy of the video: stop if the disk is running out
        min_free_bytes=min_free_bytes,
    )
    if res.returncode != 0 or res.timed_out or res.killed_for_budget or not dest.exists():
        log.warning(
            "ffmpeg %s failed (rc=%s, timeout=%s, budget=%s): %s",
            prefix,
            res.returncode,
            res.timed_out,
            res.killed_for_budget,
            res.stderr_text()[-200:].strip(),
        )
        dest.unlink(missing_ok=True)
        return None
    try:
        probed = await probe_file(dest, runner=runner)
    except Exception:
        dest.unlink(missing_ok=True)
        return None
    if not valid_probe(probed):
        dest.unlink(missing_ok=True)
        return None
    if max_file_bytes is not None and probed.size_bytes > max_file_bytes and asset.size_bytes <= max_file_bytes:
        dest.unlink(missing_ok=True)
        return None
    src.unlink(missing_ok=True)  # the prepared file replaces it; keep the job directory small
    return asset.model_copy(
        update={
            "local_path": dest.resolve(),
            "size_bytes": probed.size_bytes,
            "width": probed.width,
            "height": probed.height,
            "duration_seconds": probed.duration_seconds,
            "video_codec": probed.video_codec,
            "audio_codec": probed.audio_codec,
            "container": probed.container,
            "mime_type": probed.mime_type,
            "pix_fmt": probed.pix_fmt,
            "sha256": probed.sha256,
            "processing": processing,
        }
    )


async def remux_to_mp4(
    asset: MediaAsset, *, runner: ProcessRunner, ffmpeg: str, timeout: float = 120.0, min_free_bytes: int = 0
) -> MediaAsset | None:
    """Stream-copy `asset` into an MP4. Returns the new asset, or None if it cannot be done losslessly."""
    return await _run_ffmpeg_mp4(
        asset,
        prefix="rm",
        build_args=lambda src, dest: [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-i",
            str(src),
            "-map",
            "0:V",
            "-map",
            "0:a?",  # primary video (never attached pics) + optional audio
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(dest),
        ],
        runner=runner,
        timeout=timeout,
        min_free_bytes=min_free_bytes,
        processing=Processing.REMUXED,
        valid_probe=lambda probed: (
            probed.kind is ItemKind.VIDEO
            and probed.video_codec == asset.video_codec
            and probed.audio_codec == asset.audio_codec  # audio retained, bit-identical codec
            and (probed.width, probed.height) == (asset.width, asset.height)
            and (
                asset.duration_seconds is None
                or probed.duration_seconds is None
                or abs(probed.duration_seconds - asset.duration_seconds) <= DURATION_TOLERANCE
            )
        ),
    )


async def transcode_to_mp4(
    asset: MediaAsset,
    *,
    runner: ProcessRunner,
    ffmpeg: str,
    timeout: float = 120.0,
    min_free_bytes: int = 0,
    max_file_bytes: int | None = None,
) -> MediaAsset | None:
    """Transcode a non-native video to H.264 (`yuv420p`) + AAC `.mp4` (`+faststart`) for inline playback."""
    hw = await _hw_h264_encoder(ffmpeg, runner)
    encoders: tuple[str | None, ...] = (hw, None) if hw is not None else (None,)
    for enc in encoders:

        def _build_args(src: Path, dest: Path, _enc: str | None = enc) -> list[str]:
            encode_args = _transcode_encode_args(
                audio_codec=asset.audio_codec,
                duration_seconds=asset.duration_seconds,
                max_file_bytes=max_file_bytes,
                hw_encoder=_enc,
            )
            return [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file", "-i", str(src),
                "-map", "0:V", "-map", "0:a?",
                *encode_args,
                "-movflags", "+faststart", "-f", "mp4", str(dest),
            ]  # fmt: skip

        out = await _run_ffmpeg_mp4(
            asset,
            prefix="tc",
            build_args=_build_args,
            runner=runner,
            timeout=timeout,
            min_free_bytes=min_free_bytes,
            max_file_bytes=max_file_bytes,
            processing=Processing.TRANSCODED,
            valid_probe=lambda probed: (
                probed.kind is ItemKind.VIDEO
                and is_native_video(
                    container=probed.container,
                    video_codec=probed.video_codec,
                    audio_codec=probed.audio_codec,
                    pix_fmt=probed.pix_fmt,
                    native_hevc=False,
                )
            ),
        )
        if out is not None:
            return out
    return None


async def merge_tracks_to_mp4(
    video_path: Path,
    audio_path: Path,
    dest: Path,
    *,
    mode: DeliveryMode,
    audio_codec: str | None = None,
    native_hevc: bool = False,
    runner: ProcessRunner | None = None,
    ffmpeg: str | None = None,
    timeout: float = 120.0,
    min_free_bytes: int = 0,
    max_file_bytes: int | None = None,
) -> tuple[Path, Processing] | None:
    """Merge separate downloaded video + audio tracks in ONE FFmpeg pass.

    * `ORIGINAL` mode: stream-copy only into `.mp4` (falling back to `.mkv`).
    * `MEDIA` mode: stream-copy directly into `.mp4` when codecs are already Telegram-native,
      otherwise merge + transcode to H.264 (`yuv420p`) + AAC `.mp4` (`+faststart`) in one pass
      so we avoid an intermediate `.mkv` write and a second FFmpeg/FFprobe cycle.
    """
    exe = shutil.which("ffmpeg") if ffmpeg is None else ffmpeg
    if not exe:
        return None
    run = runner or SubprocessRunner()
    try:
        v_info = await probe_file(video_path, runner=run, compute_hash=False)
    except Exception:
        return None
    if v_info.kind is not ItemKind.VIDEO:
        return None

    try:
        audio_bytes = await asyncio.to_thread(os.path.getsize, audio_path)
    except OSError:
        audio_bytes = 0
    in_bytes = v_info.size_bytes + audio_bytes
    audio_fam = _audio_family(audio_codec) if audio_codec else None
    can_copy_mp4 = can_remux_to_mp4(
        video_codec=v_info.video_codec,
        audio_codec=audio_fam,
        pix_fmt=v_info.pix_fmt,
        native_hevc=native_hevc,
    ) and (audio_fam is None or audio_fam in NATIVE_AUDIO_CODECS)
    t0 = time.monotonic()

    async def _run_merge(
        out_path: Path, args: list[str], *, require_native: bool, native_hevc_ok: bool
    ) -> ProbeInfo | None:
        await asyncio.to_thread(out_path.unlink, missing_ok=True)
        res = await run.run(
            args,
            cwd=dest.parent,
            env=sanitized_env(dest.parent),
            timeout=timeout,
            max_stderr=20_000,
            watch_dir=dest.parent,
            min_free_bytes=min_free_bytes,
        )
        if (
            res.returncode != 0
            or res.timed_out
            or res.killed_for_budget
            or not await asyncio.to_thread(out_path.exists)
        ):
            log.warning(
                "ffmpeg merge failed (rc=%s, timeout=%s, budget=%s): %s",
                res.returncode,
                res.timed_out,
                res.killed_for_budget,
                res.stderr_text()[-200:].strip(),
            )
            await asyncio.to_thread(out_path.unlink, missing_ok=True)
            return None
        try:
            probed = await probe_file(out_path, runner=run, compute_hash=False)
        except Exception:
            probed = None
        if (
            probed is None
            or probed.kind is not ItemKind.VIDEO
            or not probed.has_audio
            or (
                require_native
                and not is_native_video(
                    container=probed.container,
                    video_codec=probed.video_codec,
                    audio_codec=probed.audio_codec,
                    pix_fmt=probed.pix_fmt,
                    native_hevc=native_hevc_ok,
                )
            )
        ):
            await asyncio.to_thread(out_path.unlink, missing_ok=True)
            return None
        return probed

    base_inputs = [
        exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file", "-i", str(video_path),
        "-protocol_whitelist", "file", "-i", str(audio_path),
        "-map", "0:V", "-map", "1:a:0",
    ]  # fmt: skip
    mp4_out = dest.with_suffix(".mp4")

    if mode is DeliveryMode.ORIGINAL:
        copy_mp4 = [*base_inputs, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(mp4_out)]
        probed = await _run_merge(mp4_out, copy_mp4, require_native=False, native_hevc_ok=True)
        if probed is not None:
            return mp4_out, Processing.MERGED
        mkv_out = dest.with_suffix(".mkv")
        copy_mkv = [*base_inputs, "-c", "copy", "-f", "matroska", str(mkv_out)]
        probed = await _run_merge(mkv_out, copy_mkv, require_native=False, native_hevc_ok=True)
        return (mkv_out, Processing.MERGED) if probed is not None else None

    if can_copy_mp4:
        copy_mp4 = [*base_inputs, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(mp4_out)]
        probed = await _run_merge(mp4_out, copy_mp4, require_native=True, native_hevc_ok=native_hevc)
        if probed is not None:
            log_event(
                log,
                logging.INFO,
                "media prepared",
                processing=Processing.MERGED.value,
                prepare_s=round(time.monotonic() - t0, 2),
                in_bytes=in_bytes,
                out_bytes=probed.size_bytes,
                vcodec=probed.video_codec,
                acodec=probed.audio_codec,
            )
            return mp4_out, Processing.MERGED

    hw = await _hw_h264_encoder(exe, run)
    encoders: tuple[str | None, ...] = (hw, None) if hw is not None else (None,)
    probed = None
    for enc in encoders:
        encode_args = _transcode_encode_args(
            audio_codec=audio_codec,
            duration_seconds=v_info.duration_seconds,
            max_file_bytes=max_file_bytes,
            hw_encoder=enc,
        )
        tc_args = [*base_inputs, *encode_args, "-movflags", "+faststart", "-f", "mp4", str(mp4_out)]
        probed = await _run_merge(mp4_out, tc_args, require_native=True, native_hevc_ok=False)
        if probed is not None:
            break
    if probed is None:
        return None
    log_event(
        log,
        logging.INFO,
        "media prepared",
        processing=Processing.TRANSCODED.value,
        prepare_s=round(time.monotonic() - t0, 2),
        in_bytes=in_bytes,
        out_bytes=probed.size_bytes,
        vcodec=probed.video_codec,
        acodec=probed.audio_codec,
    )
    return mp4_out, Processing.TRANSCODED


async def prepare_assets(
    assets: list[MediaAsset],
    *,
    mode: DeliveryMode,
    native_hevc: bool = False,
    runner: ProcessRunner | None = None,
    ffmpeg: str | None = None,
    min_free_bytes: int = 0,
    max_file_bytes: int | None = None,
    deadline: Deadline | None = None,
) -> list[MediaAsset]:
    """Ensure videos in Media mode are Telegram-playable (lossless remux first, else H.264/AAC transcode).

    Original-files mode never remuxes or transcodes: it delivers exactly what was downloaded (or merged
    by yt-dlp's stream copy). Photos are never touched.
    """
    if mode is DeliveryMode.ORIGINAL:
        return assets
    # `ffmpeg=None` -> look it up on PATH; an explicit empty string means "FFmpeg is unavailable".
    exe = shutil.which("ffmpeg") if ffmpeg is None else ffmpeg
    if not exe:
        return assets
    run = runner or SubprocessRunner()

    def _needs_prepare(asset: MediaAsset) -> bool:
        return asset.kind is ItemKind.VIDEO and not is_native_video(
            container=asset.container,
            video_codec=asset.video_codec,
            audio_codec=asset.audio_codec,
            pix_fmt=asset.pix_fmt,
            native_hevc=native_hevc,
        )

    targets = [i for i, a in enumerate(assets) if _needs_prepare(a)]
    if not targets:
        return assets

    if deadline is not None:
        deadline.check()
    step_timeout = max(1.0, deadline.remaining()) if deadline is not None else 120.0
    sem = asyncio.Semaphore(min(REMUX_CONCURRENCY, len(targets)))

    async def _one(asset: MediaAsset) -> MediaAsset:
        async with sem:
            t0 = time.monotonic()
            if can_remux_to_mp4(
                video_codec=asset.video_codec,
                audio_codec=asset.audio_codec,
                pix_fmt=asset.pix_fmt,
                native_hevc=native_hevc,
            ):
                remuxed = await remux_to_mp4(
                    asset, runner=run, ffmpeg=exe, timeout=step_timeout, min_free_bytes=min_free_bytes
                )
                if remuxed is not None and (
                    max_file_bytes is None or remuxed.size_bytes <= max_file_bytes or asset.size_bytes > max_file_bytes
                ):
                    log_event(
                        log,
                        logging.INFO,
                        "media prepared",
                        processing=remuxed.processing.value,
                        prepare_s=round(time.monotonic() - t0, 2),
                        in_bytes=asset.size_bytes,
                        out_bytes=remuxed.size_bytes,
                        vcodec=remuxed.video_codec,
                        acodec=remuxed.audio_codec,
                    )
                    return remuxed
                log.info("lossless remux not possible; falling back to H.264/AAC transcode")
            transcoded = await transcode_to_mp4(
                asset,
                runner=run,
                ffmpeg=exe,
                timeout=step_timeout,
                min_free_bytes=min_free_bytes,
                max_file_bytes=max_file_bytes,
            )
            if transcoded is not None:
                log_event(
                    log,
                    logging.INFO,
                    "media prepared",
                    processing=transcoded.processing.value,
                    prepare_s=round(time.monotonic() - t0, 2),
                    in_bytes=asset.size_bytes,
                    out_bytes=transcoded.size_bytes,
                    vcodec=transcoded.video_codec,
                    acodec=transcoded.audio_codec,
                )
                return transcoded
            log.info("auto-transcode failed; keeping the original file (will be sent as a document)")
            return asset

    out = list(assets)
    if len(targets) == 1:
        idx = targets[0]
        out[idx] = await _one(assets[idx])
    else:
        converted = await asyncio.gather(*(_one(assets[idx]) for idx in targets))
        for idx, asset in zip(targets, converted, strict=True):
            out[idx] = asset
    return out
