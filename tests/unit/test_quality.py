"""Unit tests for codec-priority format ranking in `app.media.quality`."""

from __future__ import annotations

from app.core.contracts import FormatDescriptor
from app.media.quality import ranked_choices, select_format


def _fmt(
    fid: str,
    *,
    v: bool = True,
    a: bool = True,
    w: int | None = None,
    h: int | None = None,
    vc: str | None = "avc1.64001f",
    ac: str | None = "mp4a.40.2",
    tbr: float | None = None,
    ext: str = "mp4",
) -> FormatDescriptor:
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


def test_equal_res_muxed_h264_beats_higher_bitrate_vp9_and_av1_dash():
    formats = [
        _fmt("prog_h264", w=720, h=1280, vc="avc1.64001f", tbr=1200.0),
        _fmt("dash_vp9", a=False, w=720, h=1280, vc="vp09.00.31.08", tbr=3500.0),
        _fmt("dash_av1", a=False, w=720, h=1280, vc="av01.0.08M.08", tbr=4000.0),
        _fmt("dash_a", v=False, ac="mp4a.40.2", tbr=128.0, ext="m4a"),
    ]
    ordered = ranked_choices(formats)
    assert ordered[0].selector == "prog_h264" and not ordered[0].merge
    assert [c.selector for c in ordered] == ["prog_h264", "dash_av1+dash_a", "dash_vp9+dash_a"]


def test_equal_res_dash_h264_beats_dash_vp9_regardless_of_bitrate():
    formats = [
        _fmt("dash_h264", a=False, w=1080, h=1920, vc="avc1.640028", tbr=1800.0),
        _fmt("dash_vp9", a=False, w=1080, h=1920, vc="vp09.00.41.08", tbr=3200.0),
        _fmt("dash_a", v=False, ac="mp4a.40.2", tbr=128.0, ext="m4a"),
    ]
    choice = select_format(formats)
    assert choice.merge and choice.selector == "dash_h264+dash_a"


def test_equal_res_h264_dash_still_needs_1_15x_bitrate_to_beat_muxed_h264():
    audio = _fmt("dash_a", v=False, ac="mp4a.40.2", tbr=128.0, ext="m4a")
    muxed = _fmt("prog_h264", w=720, h=1280, vc="avc1.64001f", tbr=2000.0)
    dash_close = _fmt("dash_close", a=False, w=720, h=1280, vc="avc1.64001f", tbr=2200.0)
    dash_high = _fmt("dash_high", a=False, w=720, h=1280, vc="avc1.64001f", tbr=2400.0)
    # Unrelated higher-bitrate muxed VP9 must not skew H.264's 1.15x bitrate comparison.
    muxed_vp9 = _fmt("prog_vp9", w=720, h=1280, vc="vp09.00", tbr=5000.0)

    assert select_format([muxed, dash_close, audio]).selector == "prog_h264"
    assert select_format([muxed, muxed_vp9, dash_high, audio]).selector == "dash_high+dash_a"


def test_higher_resolution_vp9_still_beats_lower_resolution_h264():
    formats = [
        _fmt("prog_720", w=720, h=1280, vc="avc1.64001f", tbr=2500.0),
        _fmt("dash_1080_vp9", a=False, w=1080, h=1920, vc="vp09.00.41.08", tbr=2000.0),
        _fmt("dash_a", v=False, ac="mp4a.40.2", tbr=128.0, ext="m4a"),
    ]
    choice = select_format(formats)
    assert choice.merge and choice.selector == "dash_1080_vp9+dash_a"
