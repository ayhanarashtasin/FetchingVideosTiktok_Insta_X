"""Source quality selection and Telegram playback-compatibility policy (T04.2, FR-05, PRD section 6).

`Highest quality` = the best representation the platform exposes, ranked by resolution first,
then (at equal resolution) codec compatibility, then bitrate. Never upscaled, never silently
downscaled; compatibility only breaks ties, it never trades resolution away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.contracts import FormatDescriptor
from app.core.errors import BotError, ErrorCode

# yt-dlp selector syntax uses + / , [ ] * ( ) etc.; an id containing them could alter the selector.
_SAFE_FORMAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
FALLBACK_SELECTOR = "bv*+ba/b"

# Telegram's native player: H.264 (+ HEVC where the client allows) with AAC/MP3 in MP4/MOV.
NATIVE_AUDIO_CODECS = frozenset({"aac", "mp3"})
NATIVE_CONTAINERS = frozenset({"mp4", "mov"})


def native_video_codecs(*, native_hevc: bool = False) -> frozenset[str]:
    return frozenset({"h264", "hevc"}) if native_hevc else frozenset({"h264"})


def _codec_family(codec: str | None) -> str:
    c = (codec or "").lower()
    if c.startswith(("avc", "h264")):
        return "h264"
    if c.startswith(("hvc", "hev", "h265", "hevc")):
        return "hevc"
    if c.startswith("vp9") or c.startswith("vp09"):
        return "vp9"
    if c.startswith(("av01", "av1")):
        return "av1"
    return c or "unknown"


def _audio_family(codec: str | None) -> str:
    c = (codec or "").lower()
    if c.startswith(("mp4a", "aac")):
        return "aac"
    if c.startswith("opus"):
        return "opus"
    if c.startswith(("mp3", "mp4a.6b")):
        return "mp3"
    return c or "unknown"


def _pixels(f: FormatDescriptor) -> int:
    return (f.width or 0) * (f.height or 0) or (f.height or 0)


def _video_key(f: FormatDescriptor, index: int) -> tuple[int, int, float, int]:
    fam = _codec_family(f.vcodec)
    compat = {"h264": 2, "unknown": 1, "hevc": 1}.get(fam, 0)
    return (_pixels(f), compat, f.tbr or 0.0, index)  # index: the extractor's own order breaks ties


def _audio_key(f: FormatDescriptor, index: int) -> tuple[int, float, int]:
    compat = {"aac": 2, "mp3": 1}.get(_audio_family(f.acodec), 0)
    return (compat, f.tbr or 0.0, index)


@dataclass(frozen=True)
class FormatChoice:
    selector: str
    video: FormatDescriptor | None
    audio: FormatDescriptor | None
    merge: bool  # True when separate video+audio tracks must be stream-copied together


def _safe(f: FormatDescriptor) -> bool:
    return bool(_SAFE_FORMAT_ID.fullmatch(f.format_id))


def select_format(formats: list[FormatDescriptor]) -> FormatChoice:
    """Pick the best source representation. Raises EXTRACTOR_CHANGED if nothing playable exists."""
    return ranked_choices(formats)[0]


def ranked_choices(formats: list[FormatDescriptor]) -> list[FormatChoice]:
    """All playable representations ranked strictly best-to-worst (resolution, codec compat, bitrate).

    Element 0 is identical to `select_format(formats)`. Subsequent entries let the worker fall back
    to the highest source variant that fits under Telegram's upload cap without re-encoding.
    """
    indexed = list(enumerate(formats))
    muxed = [(i, f) for i, f in indexed if f.has_video and f.has_audio and (f.vcodec or "") != "none"]
    video_only = [(i, f) for i, f in indexed if f.has_video and not f.has_audio]
    audio_only = [(i, f) for i, f in indexed if f.has_audio and not f.has_video]

    best_audio = max(audio_only, key=lambda p: _audio_key(p[1], p[0]), default=None)

    # Build candidates with their sort key. At equal resolution, a muxed stream stays ahead of a
    # separate-track merge unless the video-only track has >15% higher bitrate (matching select_format).
    muxed_by_pixels: dict[int, float] = {}
    for _i, f in muxed:
        px = _pixels(f)
        muxed_by_pixels[px] = max(muxed_by_pixels.get(px, 0.0), f.tbr or 0.0)

    candidates: list[tuple[tuple[int, int, int, float, int], FormatChoice]] = []
    for i, m in muxed:
        vk = _video_key(m, i)
        # priority=1 beats equal-resolution merge unless merge clears the 1.15x bitrate bar (priority=2)
        key = (vk[0], 1, vk[1], vk[2], vk[3])
        choice = FormatChoice(m.format_id if _safe(m) else FALLBACK_SELECTOR, m, None, merge=False)
        candidates.append((key, choice))

    for i, v in video_only:
        vk = _video_key(v, i)
        if best_audio is not None:
            a = best_audio[1]
            m_tbr = muxed_by_pixels.get(vk[0], 0.0)
            v_tbr = v.tbr or 0.0
            beats_muxed = (vk[0] not in muxed_by_pixels) or (m_tbr > 0 and v_tbr > m_tbr * 1.15)
            prio = 2 if beats_muxed else 0
            key = (vk[0], prio, vk[1], vk[2], vk[3])
            selector = f"{v.format_id}+{a.format_id}" if (_safe(v) and _safe(a)) else FALLBACK_SELECTOR
            candidates.append((key, FormatChoice(selector, v, a, merge=True)))
        else:
            key = (vk[0], 1, vk[1], vk[2], vk[3])
            choice = FormatChoice(v.format_id if _safe(v) else FALLBACK_SELECTOR, v, None, merge=False)
            candidates.append((key, choice))

    if not candidates:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "no playable video format offered", reason="no_video")

    candidates.sort(key=lambda item: item[0], reverse=True)
    # Deduplicate identical selectors while preserving best-to-worst order.
    seen: set[str] = set()
    ordered: list[FormatChoice] = []
    for _k, choice in candidates:
        sig = f"{choice.selector}:{choice.video.format_id if choice.video else ''}"
        if sig not in seen:
            seen.add(sig)
            ordered.append(choice)
    return ordered


def exact_choice_bytes(choice: FormatChoice) -> int | None:
    """Exact byte size when reported by the extractor; None when unknown."""
    if choice.video is None or choice.video.filesize is None:
        return None
    if choice.merge and choice.audio is not None:
        if choice.audio.filesize is None:
            return None
        return choice.video.filesize + choice.audio.filesize
    return choice.video.filesize


def estimated_choice_bytes(choice: FormatChoice, duration_seconds: float | None = None) -> int | None:
    """Known filesize, or bitrate * duration estimate when exact filesize is unavailable."""
    exact = exact_choice_bytes(choice)
    if exact is not None:
        return exact
    if not duration_seconds or duration_seconds <= 0 or choice.video is None or not choice.video.tbr:
        return None
    kbps = choice.video.tbr + (choice.audio.tbr or 0.0 if choice.merge and choice.audio else 0.0)
    return int((kbps * 1000.0 / 8.0) * duration_seconds * 1.03)


def resolution_label(fmt: FormatDescriptor | None) -> str | None:
    if fmt is None:
        return None
    if fmt.width and fmt.height:
        return f"{min(fmt.width, fmt.height)}p"
    if fmt.height:
        return f"{fmt.height}p"
    return None


def format_quality_note(chosen: FormatChoice, top: FormatChoice | None, max_bytes: int) -> str:
    """Short user-facing caption note explaining why a smaller source variant was sent."""
    cap_mb = max(1, max_bytes // 1_000_000)
    chosen_res = resolution_label(chosen.video)
    top_res = resolution_label(top.video) if top is not None else None
    if chosen_res and top_res and chosen_res != top_res:
        return f"Sent {chosen_res} ({top_res} source exceeded {cap_mb} MB Telegram limit)"
    if chosen_res and top_res and chosen_res == top_res:
        return f"Sent lower-bitrate {chosen_res} (original exceeded {cap_mb} MB Telegram limit)"
    if chosen_res:
        return f"Sent {chosen_res} (original exceeded {cap_mb} MB Telegram limit)"
    return f"Sent smaller variant (original exceeded {cap_mb} MB Telegram limit)"


def is_native_video(
    *,
    container: str | None,
    video_codec: str | None,
    audio_codec: str | None,
    pix_fmt: str | None,
    native_hevc: bool = False,
) -> bool:
    """Can this file be sent with `sendVideo` and be expected to play in Telegram's own player?"""
    if (container or "") not in NATIVE_CONTAINERS:
        return False
    if _codec_family(video_codec) not in native_video_codecs(native_hevc=native_hevc):
        return False
    if audio_codec and _audio_family(audio_codec) not in NATIVE_AUDIO_CODECS:
        return False
    # 4:4:4 / 10-bit H.264 plays on few phones; yuv420p (8-bit) is the safe baseline.
    return not (pix_fmt and pix_fmt not in {"yuv420p", "yuvj420p"})


def can_remux_to_mp4(
    *, video_codec: str | None, audio_codec: str | None, pix_fmt: str | None, native_hevc: bool = False
) -> bool:
    """Would a stream-copy into MP4 yield a natively playable file? (VP9/AV1/Opus would not.)"""
    return is_native_video(
        container="mp4", video_codec=video_codec, audio_codec=audio_codec, pix_fmt=pix_fmt, native_hevc=native_hevc
    )
