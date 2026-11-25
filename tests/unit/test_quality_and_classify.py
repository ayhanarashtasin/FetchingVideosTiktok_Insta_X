"""T03.2 / T04.2 / T06.1: format ranking, native-compatibility policy, extractor error classification."""

from __future__ import annotations

import pytest

from app.core.contracts import FormatDescriptor
from app.core.errors import BotError, ErrorCode
from app.extractors.classify import (
    GdError,
    classify_gallerydl,
    classify_text,
    classify_ytdlp,
    scan_gallerydl_warnings,
)
from app.media.quality import FALLBACK_SELECTOR, can_remux_to_mp4, is_native_video, select_format


def fmt(fid, *, v=True, a=True, w=None, h=None, vc="avc1.64001f", ac="mp4a.40.2", tbr=None, ext="mp4"):
    return FormatDescriptor(
        format_id=fid,
        has_video=v,
        has_audio=a,
        width=w,
        height=h,
        vcodec=vc if v else "none",
        acodec=ac if a else "none",
        tbr=tbr,
        ext=ext,
    )


# --- format ranking --------------------------------------------------------------------------------
def test_highest_resolution_wins_never_a_downscale():
    c = select_format([fmt("360", w=640, h=360), fmt("1080", w=1920, h=1080), fmt("720", w=1280, h=720)])
    assert c.selector == "1080" and not c.merge


def test_separate_streams_are_merged_when_they_beat_the_muxed_format():
    formats = [
        fmt("m720", w=1280, h=720, tbr=1500),
        fmt("v1080", a=False, w=1920, h=1080, tbr=4000),
        fmt("a_hi", v=False, ac="mp4a.40.2", tbr=128),
        fmt("a_lo", v=False, ac="mp4a.40.5", tbr=48),
    ]
    c = select_format(formats)
    assert c.merge and c.selector == "v1080+a_hi"
    assert c.video.format_id == "v1080" and c.audio.format_id == "a_hi"  # best audio, AAC preferred


def test_muxed_is_preferred_at_equal_resolution_to_avoid_needless_merging():
    formats = [
        fmt("m1080", w=1920, h=1080, tbr=3000),
        fmt("v1080", a=False, w=1920, h=1080, tbr=3100),
        fmt("a", v=False, tbr=128),
    ]
    assert not select_format(formats).merge


def test_clearly_better_bitrate_at_equal_resolution_justifies_a_merge():
    formats = [
        fmt("m1080", w=1920, h=1080, tbr=1000),
        fmt("v1080", a=False, w=1920, h=1080, tbr=5000),
        fmt("a", v=False, tbr=128),
    ]
    assert select_format(formats).merge


def test_codec_compatibility_only_breaks_resolution_ties():
    h264 = fmt("h264_720", w=1280, h=720, vc="avc1", tbr=1200)
    vp9 = fmt("vp9_720", w=1280, h=720, vc="vp09.00", tbr=1400)
    assert select_format([vp9, h264]).selector == "h264_720"  # same pixels: the compatible codec wins
    big_vp9 = fmt("vp9_1080", w=1920, h=1080, vc="vp09.00")
    assert select_format([h264, big_vp9]).selector == "vp9_1080"  # but compatibility never costs resolution


def test_extractor_order_breaks_remaining_ties():
    a, b = fmt("first", w=1280, h=720, tbr=1000), fmt("second", w=1280, h=720, tbr=1000)
    assert select_format([a, b]).selector == "second"  # yt-dlp lists worst->best


def test_source_without_audio_is_kept():
    c = select_format([fmt("v", a=False, w=1280, h=720)])
    assert c.selector == "v" and c.audio is None and not c.merge


def test_unknown_codecs_are_treated_as_muxed():  # TikTok often omits acodec
    f = FormatDescriptor(
        format_id="h264_540p", has_video=True, has_audio=True, width=576, height=1024, vcodec=None, acodec=None
    )
    assert select_format([f]).selector == "h264_540p"


@pytest.mark.parametrize("bad_id", ["1080+hack", "a/b", "x,y", "v[height>0]", "-f", "a b", "", "$(id)", "a;b"])
def test_hostile_format_ids_cannot_alter_the_selector(bad_id):
    c = select_format([FormatDescriptor(format_id=bad_id, has_video=True, has_audio=True, width=10, height=10)])
    assert c.selector == FALLBACK_SELECTOR


def test_nothing_playable_is_an_error():
    with pytest.raises(BotError) as exc:
        select_format([fmt("a_only", v=False)])
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED and exc.value.reason == "no_video"
    with pytest.raises(BotError):
        select_format([])


def test_unplayable_bytevc2_formats_are_filtered_out():
    vvc_1080 = FormatDescriptor(
        format_id="bytevc2_1080p",
        has_video=True,
        has_audio=True,
        width=1080,
        height=1920,
        vcodec="bytevc2",
        acodec="aac",
        tbr=2500.0,
        ext="mp4",
        note="(UNPLAYABLE)",
    )
    hevc_720 = fmt("bytevc1_720p", w=720, h=1280, vc="h265", tbr=1500.0)
    h264_540 = fmt("h264_540p", w=576, h=1024, vc="h264", tbr=900.0)
    assert select_format([vvc_1080, hevc_720, h264_540]).selector == "bytevc1_720p"

    flagged_only = FormatDescriptor(
        format_id="weird_1080",
        has_video=True,
        has_audio=True,
        width=1080,
        height=1920,
        vcodec="h264",
        note="UNPLAYABLE",
    )
    with pytest.raises(BotError) as exc:
        select_format([vvc_1080, flagged_only])
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED and exc.value.reason == "no_video"


# --- native playback policy ------------------------------------------------------------------------------
def test_native_video_policy():
    ok = dict(container="mp4", video_codec="h264", audio_codec="aac", pix_fmt="yuv420p")
    assert is_native_video(**ok)
    assert is_native_video(**{**ok, "audio_codec": None})  # silent video is fine
    assert is_native_video(**{**ok, "container": "mov"})
    assert not is_native_video(**{**ok, "container": "mkv"})
    assert not is_native_video(**{**ok, "container": "webm"})
    assert not is_native_video(**{**ok, "video_codec": "vp9"})
    assert not is_native_video(**{**ok, "video_codec": "av1"})
    assert not is_native_video(**{**ok, "audio_codec": "opus"})
    assert not is_native_video(**{**ok, "pix_fmt": "yuv444p"})
    assert not is_native_video(**{**ok, "pix_fmt": "yuv420p10le"})


def test_hevc_is_native_only_when_enabled():
    hevc = dict(container="mp4", video_codec="hevc", audio_codec="aac", pix_fmt="yuv420p")
    assert not is_native_video(**hevc)
    assert is_native_video(**hevc, native_hevc=True)


def test_remux_candidates():
    assert can_remux_to_mp4(video_codec="h264", audio_codec="aac", pix_fmt="yuv420p")  # e.g. MKV/TS/FLV source
    assert not can_remux_to_mp4(video_codec="vp9", audio_codec="opus", pix_fmt="yuv420p")


# --- yt-dlp error text -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "code", "reason"),
    [
        (
            "ERROR: [Instagram] X: Requested content is not available, rate-limit reached or login required. Use --cookies",
            ErrorCode.AUTH_REQUIRED,
            None,
        ),
        ("ERROR: [twitter] 1: HTTP Error 429: Too Many Requests", ErrorCode.RATE_LIMITED, None),
        ("ERROR: [TikTok] 1: Video unavailable. This video is private", ErrorCode.ACCESS_RESTRICTED, None),
        ("ERROR: [TikTok] 1: Video not found", ErrorCode.NOT_FOUND_OR_EXPIRED, None),
        ("ERROR: [twitter] 1: HTTP Error 404: Not Found", ErrorCode.NOT_FOUND_OR_EXPIRED, None),
        ("ERROR: Unsupported URL: https://example.com/x", ErrorCode.UNSUPPORTED_POST, None),
        ("ERROR: No suitable extractor found for URL https://example.com", ErrorCode.UNSUPPORTED_POST, None),
        ("File is larger than max-filesize (5000 bytes > 1000 bytes). Aborting.", ErrorCode.LIMIT_EXCEEDED, "filesize"),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", ErrorCode.DOWNLOAD_TIMEOUT, "network"),
        (
            "ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable",
            ErrorCode.DOWNLOAD_TIMEOUT,
            "network",
        ),
        ("ERROR: [TikTok] 1: There is no video in this post", ErrorCode.EXTRACTOR_CHANGED, "no_video"),
        ("ERROR: [TikTok] 1: Your IP address is blocked from accessing this post", ErrorCode.RATE_LIMITED, None),
        ("Please complete the CAPTCHA to continue", ErrorCode.RATE_LIMITED, "challenge"),
        ("failed to solve the challenge", ErrorCode.RATE_LIMITED, "challenge"),
        ("something totally unexpected", ErrorCode.EXTRACTOR_CHANGED, None),
    ],
)
def test_ytdlp_text_classification(text, code, reason):
    err = classify_ytdlp(text, 1, session_supported=True)
    assert (err.code, err.reason) == (code, reason)


def test_gallerydl_info_log_noise_does_not_trigger_challenge_or_hide_real_error():
    # Solving JavaScript challenge is normal gallery-dl info and must not trigger RATE_LIMITED
    mixed = "[tiktok][info] Solving JavaScript challenge\n[tiktok][error] 123: Requested post not available"
    err = classify_text(mixed)
    assert err.code is ErrorCode.NOT_FOUND_OR_EXPIRED

    info_only = "[tiktok][info] Solving JavaScript challenge"
    err2 = classify_text(info_only)
    assert err2.code is ErrorCode.EXTRACTOR_CHANGED and err2.reason is None


def test_a_login_wall_on_a_platform_without_session_support_is_restricted_not_auth_required():
    err = classify_text("ERROR: [twitter] login required", session_supported=False)
    assert err.code is ErrorCode.ACCESS_RESTRICTED and err.reason == "login_wall"


def test_story_hint_selects_the_story_specific_message():
    assert classify_text("login required", session_supported=True, hint="story").reason == "story"
    assert classify_text("login required", session_supported=True, hint="reel").reason is None


def test_classification_never_exposes_urls_in_detail():
    err = classify_text("ERROR: HTTP Error 404 for https://www.instagram.com/p/SECRET/?igsh=abc")
    assert "instagram.com" not in (err.detail or "") and "SECRET" not in (err.detail or "")


@pytest.mark.parametrize(
    "code",
    [ErrorCode.ACCESS_RESTRICTED, ErrorCode.AUTH_REQUIRED, ErrorCode.RATE_LIMITED, ErrorCode.NOT_FOUND_OR_EXPIRED],
)
def test_access_style_errors_forbid_fallback(code):
    assert not BotError(code).fallback_allowed


# --- gallery-dl structured errors ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "message", "session", "code", "reason"),
    [
        ("AuthRequired", "login needed to access this story", True, ErrorCode.AUTH_REQUIRED, None),
        ("AuthRequired", "login needed", False, ErrorCode.ACCESS_RESTRICTED, "login_wall"),
        ("AuthorizationError", "Insufficient privileges", True, ErrorCode.ACCESS_RESTRICTED, None),
        ("NotFoundError", "Requested story could not be found", True, ErrorCode.NOT_FOUND_OR_EXPIRED, None),
        ("ChallengeError", "Cloudflare challenge (403 Forbidden)", True, ErrorCode.RATE_LIMITED, "challenge"),
        ("HttpError", "'429 Too Many Requests' for 'https://x'", True, ErrorCode.RATE_LIMITED, None),
        ("HttpError", "'403 Forbidden' for 'https://x'", True, ErrorCode.ACCESS_RESTRICTED, None),
        ("HttpError", "'404 Not Found' for 'https://x'", True, ErrorCode.NOT_FOUND_OR_EXPIRED, None),
        ("HttpError", "'502 Bad Gateway' for 'https://x'", True, ErrorCode.DOWNLOAD_TIMEOUT, "network"),
        ("HttpError", "HTTP request failed", True, ErrorCode.DOWNLOAD_TIMEOUT, "network"),
        ("NoExtractorError", "Unsupported URL", True, ErrorCode.UNSUPPORTED_POST, None),
        (
            "AbortExtraction",
            "HTTP redirect to login page ('https://tiktok.com/login')",
            False,
            ErrorCode.ACCESS_RESTRICTED,
            "login_wall",
        ),
    ],
)
def test_gallerydl_structured_classification(name, message, session, code, reason):
    err = classify_gallerydl(
        [GdError(name, message)], 1, "", session_supported=session, hint="story" if name == "AuthRequired" else None
    )
    assert err.code is code
    if reason is not None:
        assert err.reason == reason


def test_gallerydl_exit_bits_are_the_fallback():
    assert classify_gallerydl([], 64, "Unsupported URL").code is ErrorCode.UNSUPPORTED_POST
    assert classify_gallerydl([], 8, "").code is ErrorCode.RATE_LIMITED
    assert (
        classify_gallerydl([], 4, "[tiktok][error] Failed to extract post (KeyError: 'x')").code
        is ErrorCode.EXTRACTOR_CHANGED
    )


def test_warning_scan_detects_silently_dropped_media():
    assert scan_gallerydl_warnings("[instagram][warning] Missing media in post ABC").skipped_media
    assert scan_gallerydl_warnings("[twitter][warning] Media unavailable (1700, 'Deleted')").skipped_media
    assert scan_gallerydl_warnings("[instagram][warning] someone's posts are private").private_hint
    quiet = scan_gallerydl_warnings("[instagram][info] fetching")
    assert not quiet.skipped_media and not quiet.private_hint


@pytest.mark.parametrize(
    "noisy",
    [
        "Unable to connect to proxy: Failed to establish a new connection: [WinError 10061] port=4040",
        "request to host:4290 failed: connection refused",
        "worker 14100 exited while reading socket 24045",
    ],
)
def test_status_codes_are_not_matched_inside_unrelated_numbers(noisy):
    """Regression: bare `404` / `410` / `429` once matched inside ports and error numbers."""
    err = classify_text(noisy)
    assert err.code is not ErrorCode.NOT_FOUND_OR_EXPIRED
    assert err.code is not ErrorCode.RATE_LIMITED
