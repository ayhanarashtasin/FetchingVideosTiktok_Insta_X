"""T04.2 / AT15 / AT16: lossless stream-copy remux first, then auto-transcode fallback in Media mode."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset, Processing
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.media.prepare import prepare_assets, remux_to_mp4, transcode_to_mp4
from app.media.probe import probe_file
from app.media.quality import is_native_video
from app.services.subprocess_runner import ProcessResult, SubprocessRunner
from tests.conftest import FFMPEG, requires_ffmpeg, stream_hash

pytestmark = requires_ffmpeg


async def asset_from(path, position=0, dest_dir=None) -> MediaAsset:
    """Probe a synthetic file (copied into dest_dir so tests may delete/replace it) and describe it."""
    if dest_dir is not None:
        copy = dest_dir / path.name
        shutil.copyfile(path, copy)
        path = copy
    info = await probe_file(path)
    return MediaAsset(
        source_item_id=f"i{position}",
        position=position,
        local_path=path.resolve(),
        kind=info.kind,
        mime_type=info.mime_type,
        size_bytes=info.size_bytes,
        width=info.width,
        height=info.height,
        duration_seconds=info.duration_seconds,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        sha256=info.sha256,
        container=info.container,
        pix_fmt=info.pix_fmt,
    )


async def test_mkv_h264_aac_is_remuxed_to_mp4_without_touching_the_streams(media, tmp_path):  # AT15 / AT16
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    assert not is_native_video(
        container=src.container, video_codec=src.video_codec, audio_codec=src.audio_codec, pix_fmt=src.pix_fmt
    )
    video_hash, audio_hash = stream_hash(src.local_path, "0:v:0"), stream_hash(src.local_path, "0:a:0")
    out = await remux_to_mp4(src, runner=SubprocessRunner(), ffmpeg=FFMPEG)
    assert out is not None and out.processing is Processing.REMUXED
    assert out.container == "mp4" and out.local_path.suffix == ".mp4"
    # The compressed packets are bit-identical: proof there was no re-encode, and the audio survived.
    assert stream_hash(out.local_path, "0:v:0") == video_hash
    assert stream_hash(out.local_path, "0:a:0") == audio_hash
    assert (out.video_codec, out.audio_codec, out.width, out.height) == ("h264", "aac", 160, 120)
    assert is_native_video(
        container=out.container, video_codec=out.video_codec, audio_codec=out.audio_codec, pix_fmt=out.pix_fmt
    )
    assert not src.local_path.exists(), "the replaced original must not linger in the job directory"


async def test_prepare_remuxes_only_what_it_can_do_losslessly(media, tmp_path):
    """Lossless remux runs when codecs are already native; non-native codecs transcode to H.264/AAC MP4."""
    h264_mkv = await asset_from(media.mkv_h264_aac, 0, tmp_path)
    vp9_webm = await asset_from(media.webm_vp9_opus, 1, tmp_path)
    native = await asset_from(media.mp4_h264_aac, 2, tmp_path)
    photo = await asset_from(media.jpg, 3, tmp_path)
    before_native, before_photo = native.local_path.read_bytes(), photo.local_path.read_bytes()

    out = await prepare_assets([h264_mkv, vp9_webm, native, photo], mode=DeliveryMode.MEDIA)

    assert [a.processing for a in out] == [
        Processing.REMUXED,
        Processing.TRANSCODED,
        Processing.UNCHANGED,
        Processing.UNCHANGED,
    ]
    assert out[1].container == "mp4" and out[1].local_path.suffix == ".mp4"
    assert (out[1].video_codec, out[1].audio_codec, out[1].pix_fmt) == ("h264", "aac", "yuv420p")
    assert is_native_video(
        container=out[1].container,
        video_codec=out[1].video_codec,
        audio_codec=out[1].audio_codec,
        pix_fmt=out[1].pix_fmt,
    )
    assert not vp9_webm.local_path.exists(), "transcoded file replaces the original in the job dir"
    assert out[2].local_path.read_bytes() == before_native, "an already-native file is sent untouched"
    assert out[3].local_path.read_bytes() == before_photo and out[3].kind is ItemKind.PHOTO


async def test_original_mode_never_remuxes(media, tmp_path):
    """Original-files mode never remuxes or transcodes anything."""
    mkv = await asset_from(media.mkv_h264_aac, 0, tmp_path)
    webm = await asset_from(media.webm_vp9_opus, 1, tmp_path)
    hevc = await asset_from(media.mp4_hevc_aac, 2, tmp_path)
    out = await prepare_assets([mkv, webm, hevc], mode=DeliveryMode.ORIGINAL)
    assert out == [mkv, webm, hevc]
    assert all(a.processing is Processing.UNCHANGED and a.local_path.exists() for a in out)


async def test_hevc_is_native_only_when_enabled_and_never_re_encoded(media, tmp_path):
    """When NATIVE_HEVC=true, HEVC stays bit-identical (remux only if needed). When false, it transcodes."""
    hevc = await asset_from(media.mp4_hevc_aac, dest_dir=tmp_path)
    (kept,) = await prepare_assets([hevc], mode=DeliveryMode.MEDIA, native_hevc=True)
    assert kept is hevc and kept.processing is Processing.UNCHANGED and kept.video_codec == "hevc"

    mkv_hevc = tmp_path / "h.mkv"
    subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(media.mp4_hevc_aac), "-c", "copy", str(mkv_hevc)],
        check=True,
    )
    a2 = await asset_from(mkv_hevc, 1)
    (out,) = await prepare_assets([a2], mode=DeliveryMode.MEDIA, native_hevc=True)
    assert out.processing is Processing.REMUXED and out.video_codec == "hevc"
    assert stream_hash(out.local_path) == stream_hash(media.mp4_hevc_aac)

    (transcoded,) = await prepare_assets([hevc], mode=DeliveryMode.MEDIA, native_hevc=False)
    assert transcoded.processing is Processing.TRANSCODED
    assert (transcoded.container, transcoded.video_codec, transcoded.audio_codec, transcoded.pix_fmt) == (
        "mp4",
        "h264",
        "aac",
        "yuv420p",
    )


async def test_odd_dimensions_and_10bit_silent_video_transcode_to_native_mp4(tmp_path):
    odd_vp9 = tmp_path / "odd_silent.webm"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=161x121:rate=10", "-t", "0.5",
            "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-an",
            str(odd_vp9),
        ],
        check=True,
    )  # fmt: skip
    src_odd = await asset_from(odd_vp9, 0)
    assert (src_odd.width, src_odd.height) == (161, 121)

    (out_odd,) = await prepare_assets([src_odd], mode=DeliveryMode.MEDIA)
    assert out_odd.processing is Processing.TRANSCODED
    assert (out_odd.container, out_odd.video_codec, out_odd.audio_codec, out_odd.pix_fmt) == (
        "mp4",
        "h264",
        None,
        "yuv420p",
    )
    assert (out_odd.width, out_odd.height) == (160, 120)
    assert is_native_video(
        container=out_odd.container,
        video_codec=out_odd.video_codec,
        audio_codec=out_odd.audio_codec,
        pix_fmt=out_odd.pix_fmt,
    )

    ten_bit = tmp_path / "ten_bit.mkv"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast", "-an",
            str(ten_bit),
        ],
        check=True,
    )  # fmt: skip
    src_10 = await asset_from(ten_bit, 1)
    assert src_10.pix_fmt == "yuv420p10le"
    (out_10,) = await prepare_assets([src_10], mode=DeliveryMode.MEDIA)
    assert out_10.processing is Processing.TRANSCODED and out_10.pix_fmt == "yuv420p"


async def test_failed_remux_falls_back_to_transcode(media, tmp_path, monkeypatch):
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)

    async def fail_remux(*args, **kwargs):
        return None

    monkeypatch.setattr("app.media.prepare.remux_to_mp4", fail_remux)
    (out,) = await prepare_assets([src], mode=DeliveryMode.MEDIA)
    assert out.processing is Processing.TRANSCODED
    assert is_native_video(
        container=out.container, video_codec=out.video_codec, audio_codec=out.audio_codec, pix_fmt=out.pix_fmt
    )


async def test_a_failed_or_unverifiable_remux_keeps_the_original(media, tmp_path):
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)

    class Broken:
        async def run(self, args, **kw):
            return ProcessResult(returncode=1, stdout=b"", stderr=b"boom")

    assert await remux_to_mp4(src, runner=Broken(), ffmpeg="ffmpeg") is None
    assert await transcode_to_mp4(src, runner=Broken(), ffmpeg="ffmpeg") is None
    assert src.local_path.exists()
    assert not list(tmp_path.glob("rm_*")) and not list(tmp_path.glob("tc_*"))


async def test_remux_that_changes_the_streams_is_discarded(media, tmp_path):
    """If a (hypothetical) tool silently altered the codec, the verification must refuse the output."""
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    wrong = src.model_copy(update={"video_codec": "hevc"})  # claims a codec the output will not have
    assert await remux_to_mp4(wrong, runner=SubprocessRunner(), ffmpeg=FFMPEG) is None
    assert src.local_path.exists() and not list(tmp_path.glob("rm_*"))


async def test_remux_with_a_missing_ffmpeg_degrades_to_the_original(media, tmp_path):
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    (out,) = await prepare_assets([src], mode=DeliveryMode.MEDIA, ffmpeg="")
    assert out.processing is Processing.UNCHANGED


async def test_oversized_transcode_keeps_original_when_original_fit_under_cap(media, tmp_path, monkeypatch):
    from app.media import prepare as prepare_mod

    src = await asset_from(media.webm_vp9_opus, dest_dir=tmp_path)
    real_probe = prepare_mod.probe_file

    async def oversized_probe(path, **kw):
        info = await real_probe(path, **kw)
        return type(info)(**{**info.__dict__, "size_bytes": src.size_bytes + 500})

    monkeypatch.setattr(prepare_mod, "probe_file", oversized_probe)
    assert await transcode_to_mp4(src, runner=SubprocessRunner(), ffmpeg=FFMPEG, max_file_bytes=src.size_bytes) is None
    assert src.local_path.exists() and not list(tmp_path.glob("tc_*"))


async def test_large_local_bot_api_cap_does_not_overflow_x264_vbv(media, tmp_path):
    src = await asset_from(media.mp4_hevc_aac, dest_dir=tmp_path)
    out = await transcode_to_mp4(src, runner=SubprocessRunner(), ffmpeg=FFMPEG, max_file_bytes=1_900_000_000)
    assert out is not None and out.processing is Processing.TRANSCODED
    assert (out.container, out.video_codec, out.pix_fmt) == ("mp4", "h264", "yuv420p")


async def test_prepare_respects_expired_deadline(media, tmp_path):
    src = await asset_from(media.webm_vp9_opus, dest_dir=tmp_path)
    expired = Deadline(1.0, clock=lambda: 100.0)
    expired._end = 50.0  # force expired without sleeping
    with pytest.raises(BotError) as exc:
        await prepare_assets([src], mode=DeliveryMode.MEDIA, deadline=expired)
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT


@pytest.mark.parametrize("hostile", ["x; rm -rf", "-i evil", "a b"])
async def test_ffmpeg_only_ever_sees_paths_we_generate(media, tmp_path, hostile):
    """File names derive from numeric positions - no extractor-provided text reaches ffmpeg's argv."""
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    src = src.model_copy(update={"source_item_id": hostile})
    calls = []

    class Spy:
        async def run(self, args, **kw):
            calls.append(list(args))
            return await SubprocessRunner().run(args, **kw)

    await remux_to_mp4(src, runner=Spy(), ffmpeg=FFMPEG)
    assert calls and all(hostile not in a for a in calls[0])
    assert "-protocol_whitelist" in calls[0] and calls[0][calls[0].index("-protocol_whitelist") + 1] == "file"
    assert "-nostdin" in calls[0]

    calls.clear()
    hevc = await asset_from(media.mp4_hevc_aac, 1, tmp_path)
    hevc = hevc.model_copy(update={"source_item_id": hostile})
    await transcode_to_mp4(hevc, runner=Spy(), ffmpeg=FFMPEG)
    assert calls and all(hostile not in a for a in calls[0])
    assert "-protocol_whitelist" in calls[0] and calls[0][calls[0].index("-protocol_whitelist") + 1] == "file"
    assert "-nostdin" in calls[0]


async def test_remux_is_abandoned_and_the_original_kept_when_the_disk_headroom_is_gone(media, tmp_path):
    """T05.4: remux/transcode writes a second copy. When killed for low disk, partials are deleted and original stays."""
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    seen: dict = {}

    class LowDisk:
        async def run(self, args, **kw):
            seen.update(kw)
            name = "tc_000.mp4" if "libx264" in args else "rm_000.mp4"
            (src.local_path.parent / name).write_bytes(b"partial")
            return ProcessResult(returncode=-9, stdout=b"", stderr=b"", killed_for_budget=True)

    out = await remux_to_mp4(src, runner=LowDisk(), ffmpeg="ffmpeg", min_free_bytes=123_456)
    assert out is None and src.local_path.exists() and not list(tmp_path.glob("rm_*"))
    assert seen["min_free_bytes"] == 123_456 and seen["watch_dir"] == src.local_path.parent

    seen.clear()
    out2 = await transcode_to_mp4(src, runner=LowDisk(), ffmpeg="ffmpeg", min_free_bytes=654_321)
    assert out2 is None and src.local_path.exists() and not list(tmp_path.glob("tc_*"))
    assert seen["min_free_bytes"] == 654_321 and seen["watch_dir"] == src.local_path.parent
