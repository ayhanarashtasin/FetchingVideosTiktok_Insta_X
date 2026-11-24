"""T04.2 / AT15 / AT16: the only video transformation is a provably lossless stream-copy remux."""

from __future__ import annotations

import shutil

import pytest

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset, Processing
from app.media.prepare import prepare_assets, remux_to_mp4
from app.media.probe import probe_file
from app.media.quality import is_native_video
from app.services.subprocess_runner import SubprocessRunner
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
    h264_mkv = await asset_from(media.mkv_h264_aac, 0, tmp_path)
    vp9_webm = await asset_from(media.webm_vp9_opus, 1, tmp_path)
    native = await asset_from(media.mp4_h264_aac, 2, tmp_path)
    photo = await asset_from(media.jpg, 3, tmp_path)
    before_webm, before_native, before_photo = (
        vp9_webm.local_path.read_bytes(),
        native.local_path.read_bytes(),
        photo.local_path.read_bytes(),
    )

    out = await prepare_assets([h264_mkv, vp9_webm, native, photo], mode=DeliveryMode.MEDIA)

    assert [a.processing for a in out] == [
        Processing.REMUXED,
        Processing.UNCHANGED,
        Processing.UNCHANGED,
        Processing.UNCHANGED,
    ]
    assert out[1].container == "webm" and out[1].local_path.read_bytes() == before_webm, "VP9/Opus is never transcoded"
    assert out[2].local_path.read_bytes() == before_native, "an already-native file is sent untouched"
    assert out[3].local_path.read_bytes() == before_photo and out[3].kind is ItemKind.PHOTO


async def test_original_mode_never_remuxes(media, tmp_path):
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    (out,) = await prepare_assets([src], mode=DeliveryMode.ORIGINAL)
    assert out is src and out.container == "mkv" and out.local_path.exists()


async def test_hevc_is_native_only_when_enabled_and_never_re_encoded(media, tmp_path):
    hevc = await asset_from(media.mp4_hevc_aac, dest_dir=tmp_path)
    (a,) = await prepare_assets([hevc], mode=DeliveryMode.MEDIA, native_hevc=False)
    assert (
        a.container == "mp4" and a.video_codec == "hevc" and a.processing is Processing.UNCHANGED
    )  # stays; planner -> document
    mkv_hevc = tmp_path / "h.mkv"
    import subprocess

    subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(media.mp4_hevc_aac), "-c", "copy", str(mkv_hevc)],
        check=True,
    )
    a2 = await asset_from(mkv_hevc, 1)
    (out,) = await prepare_assets([a2], mode=DeliveryMode.MEDIA, native_hevc=True)
    assert out.processing is Processing.REMUXED and out.video_codec == "hevc"
    assert stream_hash(out.local_path) == stream_hash(media.mp4_hevc_aac)


async def test_a_failed_or_unverifiable_remux_keeps_the_original(media, tmp_path):
    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)

    class Broken:
        async def run(self, args, **kw):
            from app.services.subprocess_runner import ProcessResult

            return ProcessResult(returncode=1, stdout=b"", stderr=b"boom")

    assert await remux_to_mp4(src, runner=Broken(), ffmpeg="ffmpeg") is None
    assert src.local_path.exists()
    assert not list(tmp_path.glob("rm_*")), "no partial output may be left behind"


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


async def test_remux_is_abandoned_and_the_original_kept_when_the_disk_headroom_is_gone(media, tmp_path):
    """T05.4: the remux writes a second copy of the video. If the runner reports it was killed for lack of
    disk space the partial output is deleted and the ORIGINAL is kept (deterministic stub; the real watcher is
    covered in test_subprocess_runner)."""
    from app.services.subprocess_runner import ProcessResult

    src = await asset_from(media.mkv_h264_aac, dest_dir=tmp_path)
    seen: dict = {}

    class LowDisk:
        async def run(self, args, **kw):
            seen.update(kw)
            (src.local_path.parent / "rm_000.mp4").write_bytes(b"partial")  # what ffmpeg had written so far
            return ProcessResult(returncode=-9, stdout=b"", stderr=b"", killed_for_budget=True)

    out = await remux_to_mp4(src, runner=LowDisk(), ffmpeg="ffmpeg", min_free_bytes=123_456)
    assert out is None and src.local_path.exists() and not list(tmp_path.glob("rm_*"))
    assert seen["min_free_bytes"] == 123_456 and seen["watch_dir"] == src.local_path.parent  # the guard was armed
