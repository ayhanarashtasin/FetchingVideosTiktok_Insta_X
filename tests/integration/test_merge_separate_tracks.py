"""AT15 / AT16: a source whose video and audio are SEPARATE tracks is merged by stream copy - no re-encode,
audio retained - and then (Media mode) remuxed to a natively playable MP4, still without re-encoding.

The source is a real local HLS master playlist with a separate audio rendition, downloaded by the REAL pinned
yt-dlp through the project's own runner and selection logic. Proof of "no re-encode" = identical DECODED frames
and samples before and after (impossible for any lossy re-encode).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset, Processing
from app.core.deadline import Deadline
from app.extractors.ytdlp_runner import YtDlpRunner
from app.media.prepare import prepare_assets
from app.media.probe import probe_file
from app.media.quality import is_native_video, select_format
from app.services.subprocess_runner import SubprocessRunner
from tests.conftest import decoded_md5, requires_ffmpeg, stream_hash

pytestmark = requires_ffmpeg


def asset_of(path: Path, info) -> MediaAsset:
    return MediaAsset(
        source_item_id="x", position=0, local_path=path.resolve(), kind=ItemKind.VIDEO, mime_type=info.mime_type,
        size_bytes=info.size_bytes, width=info.width, height=info.height, duration_seconds=info.duration_seconds,
        video_codec=info.video_codec, audio_codec=info.audio_codec, container=info.container, pix_fmt=info.pix_fmt,
        processing=Processing.MERGED,
    )  # fmt: skip


async def test_separate_video_and_audio_tracks_are_merged_losslessly_and_audio_is_kept(hls_dir, hls_server, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    url = f"{hls_server}/master.m3u8"
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))

    info = await y.discover(url, ws, Deadline(60))
    formats = info.entries[0].formats
    assert [f for f in formats if f.has_video and not f.has_audio] and [
        f for f in formats if f.has_audio and not f.has_video
    ]
    choice = select_format(formats)
    assert choice.merge and "+" in choice.selector, "the source really does need a merge"

    (merged,) = await y.download(url, ws, Deadline(120), selector=choice.selector)
    probed = await probe_file(merged)
    assert probed.kind is ItemKind.VIDEO and probed.has_audio, "original audio must be retained"
    assert (probed.video_codec, probed.audio_codec, probed.width, probed.height) == ("h264", "aac", 160, 120)

    # No re-encode: decoded frames / samples of the merged file == those of the two source renditions.
    assert decoded_md5(merged, "v") == decoded_md5(hls_dir / "stream_0.m3u8", "v")
    assert decoded_md5(merged, "a") == decoded_md5(hls_dir / "stream_1.m3u8", "a")


async def test_the_merged_result_is_then_remuxed_to_a_native_mp4_without_re_encoding(hls_dir, hls_server, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    url = f"{hls_server}/master.m3u8"
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    info = await y.discover(url, ws, Deadline(60))
    (merged,) = await y.download(url, ws, Deadline(120), selector=select_format(info.entries[0].formats).selector)
    asset = asset_of(merged, await probe_file(merged))
    # MKV -> MP4 keeps the packet framing (AVCC video, raw AAC), so the right proof for a REMUX is that the
    # compressed packets themselves are bit-identical. (Decoded audio is NOT compared here: AAC-in-MP4 carries
    # its encoder-delay "priming" samples in an edit list, so a decoder legitimately drops 1024 samples.)
    video_before, audio_before = stream_hash(merged, "0:v:0"), stream_hash(merged, "0:a:0")

    (prepared,) = await prepare_assets([asset], mode=DeliveryMode.MEDIA)

    assert prepared.container == "mp4" and prepared.processing is Processing.REMUXED
    assert is_native_video(
        container="mp4", video_codec=prepared.video_codec, audio_codec=prepared.audio_codec, pix_fmt=prepared.pix_fmt
    )
    assert (stream_hash(prepared.local_path, "0:v:0"), stream_hash(prepared.local_path, "0:a:0")) == (
        video_before,
        audio_before,
    )


@pytest.mark.parametrize("mode", [DeliveryMode.ORIGINAL])
async def test_original_mode_keeps_the_merged_file_exactly_as_downloaded(hls_dir, hls_server, tmp_path, mode):
    ws = tmp_path / "ws"
    ws.mkdir()
    url = f"{hls_server}/master.m3u8"
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    info = await y.discover(url, ws, Deadline(60))
    (merged,) = await y.download(url, ws, Deadline(120), selector=select_format(info.entries[0].formats).selector)
    asset = asset_of(merged, await probe_file(merged))
    before = merged.read_bytes()
    (out,) = await prepare_assets([asset], mode=mode)
    assert out is asset and merged.read_bytes() == before  # not remuxed, not touched
