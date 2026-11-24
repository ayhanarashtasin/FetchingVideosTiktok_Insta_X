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
    indexed = list(enumerate(formats))
    muxed = [(i, f) for i, f in indexed if f.has_video and f.has_audio and (f.vcodec or "") != "none"]
    video_only = [(i, f) for i, f in indexed if f.has_video and not f.has_audio]
    audio_only = [(i, f) for i, f in indexed if f.has_audio and not f.has_video]

    best_muxed = max(muxed, key=lambda p: _video_key(p[1], p[0]), default=None)
    best_video = max(video_only, key=lambda p: _video_key(p[1], p[0]), default=None)
    best_audio = max(audio_only, key=lambda p: _audio_key(p[1], p[0]), default=None)

    use_merge = False
    if best_video is not None and best_audio is not None:
        if best_muxed is None:
            use_merge = True
        else:
            vm, mm = _video_key(best_video[1], best_video[0]), _video_key(best_muxed[1], best_muxed[0])
            if vm[0] > mm[0]:  # strictly more pixels: worth a merge
                use_merge = True
            elif vm[0] == mm[0]:
                v_tbr, m_tbr = best_video[1].tbr or 0.0, best_muxed[1].tbr or 0.0
                use_merge = v_tbr > m_tbr * 1.15 and m_tbr > 0  # clearly better bitrate at equal resolution

    if use_merge and best_video and best_audio:
        v, a = best_video[1], best_audio[1]
        if _safe(v) and _safe(a):
            return FormatChoice(f"{v.format_id}+{a.format_id}", v, a, merge=True)
        return FormatChoice(FALLBACK_SELECTOR, v, a, merge=True)
    if best_muxed is not None:
        m = best_muxed[1]
        return FormatChoice(m.format_id if _safe(m) else FALLBACK_SELECTOR, m, None, merge=False)
    if best_video is not None:  # a source with no audio track at all: keep it as is
        v = best_video[1]
        return FormatChoice(v.format_id if _safe(v) else FALLBACK_SELECTOR, v, None, merge=False)
    raise BotError(ErrorCode.EXTRACTOR_CHANGED, "no playable video format offered", reason="no_video")


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
