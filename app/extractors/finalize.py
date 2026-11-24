"""Turn an adapter's raw downloads into verified `MediaAsset`s (T04.1, T03.10, T03.11).

Every file is probed with ffprobe and must be what the source said it was: a photo item must be an
image, a video item must contain a video stream. Files that fail are recorded as omissions (never
sent, never silently swapped); only if NOTHING usable remains does the job fail.
"""

from __future__ import annotations

from pathlib import Path

from app.core.contracts import (
    ExtractionResult,
    ItemFailure,
    MediaAsset,
    PostManifest,
    derive_completeness,
)
from app.core.errors import BotError, ErrorCode
from app.extractors.base import FetchOutcome
from app.media.probe import probe_file
from app.services.subprocess_runner import ProcessRunner


async def finalize_outcome(
    outcome: FetchOutcome,
    manifest: PostManifest,
    workspace: Path,
    *,
    runner: ProcessRunner | None = None,
    max_total_bytes: int,
    max_items: int,
) -> ExtractionResult:
    if len(outcome.items) > max_items:
        raise BotError(ErrorCode.LIMIT_EXCEEDED, "too many items")
    omissions: list[ItemFailure] = list(outcome.omissions)
    assets: list[MediaAsset] = []
    total = 0
    for item in sorted(outcome.items, key=lambda i: i.position):
        try:
            info = await probe_file(item.path, runner=runner)
        except BotError as err:
            if err.code is ErrorCode.DOWNLOAD_TIMEOUT:
                raise  # the probe itself timed out: retry the attempt, do not blame the media
            omissions.append(
                ItemFailure(
                    position=item.position,
                    source_item_id=item.source_item_id,
                    code=ErrorCode.MEDIA_INVALID,
                    message="downloaded file failed validation",
                )
            )
            continue
        if info.kind is not item.kind:
            omissions.append(
                ItemFailure(
                    position=item.position,
                    source_item_id=item.source_item_id,
                    code=ErrorCode.MEDIA_INVALID,
                    message="file type does not match the source item",
                )
            )
            continue
        total += info.size_bytes
        if total > max_total_bytes:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "temporary byte budget exceeded")
        assets.append(
            MediaAsset(
                source_item_id=item.source_item_id,
                position=item.position,
                local_path=item.path.resolve(),
                kind=info.kind,
                mime_type=info.mime_type,
                size_bytes=info.size_bytes,
                width=info.width,
                height=info.height,
                duration_seconds=info.duration_seconds,
                video_codec=info.video_codec,
                audio_codec=info.audio_codec,
                sha256=info.sha256,
                source_format=item.source_format,
                processing=item.processing,
                container=info.container,
                pix_fmt=info.pix_fmt,
            )
        )
    if not assets:
        code = omissions[0].code if omissions else ErrorCode.EXTRACTOR_CHANGED
        raise BotError(code, "no usable media was retrieved")
    completeness = derive_completeness(
        expected_count=outcome.expected_count,
        delivered_or_extracted=len(assets),
        omissions=len(omissions),
        count_verified=outcome.count_verified,
        skipped_media_warning=outcome.skipped_media_warning,
    )
    return ExtractionResult(
        assets=assets, expected_count=outcome.expected_count, omissions=omissions, completeness=completeness
    )
