"""Adapter contract and shared helpers (T03.1, T03.3, architecture sections 6-7).

A `PlatformAdapter` resolves ONE target post to an ordered manifest (`discover`) and materialises
only those items (`fetch`). Both steps treat extractor output as untrusted: the target identity is
verified, unrelated media (audio tracks, covers, other posts) is excluded, and anything that cannot
be proven is reported as an omission or as unverified completeness - never silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.core.contracts import (
    DeliveryMode,
    ExtractionRequest,
    FormatDescriptor,
    ItemDescriptor,
    ItemFailure,
    ItemKind,
    Platform,
    PostManifest,
    Processing,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.direct_download import (
    DirectDownloadUnavailable,
    DirectFormatMissing,
    DirectMediaDownloader,
    DirectTarget,
    FileTooLargeDirect,
)
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.ytdlp_runner import YtDlpRunner, YtFormatSource
from app.services.subprocess_runner import ProcessRunner

PHOTO_EXT = frozenset({"jpg", "jpeg", "png", "webp", "avif", "heic"})
VIDEO_EXT = frozenset({"mp4", "m4v", "mov", "webm", "mkv"})
AUDIO_EXT = frozenset({"mp3", "m4a", "aac", "opus", "ogg", "wav", "flac"})
_SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,8}$")


@dataclass
class ExtractionContext:
    ytdlp: YtDlpRunner
    gallerydl: GalleryDlRunner
    max_items: int = 30
    downloader: DirectMediaDownloader | None = None
    file_cap_bytes: int | None = None
    runner: ProcessRunner | None = None
    native_hevc: bool = False
    min_free_bytes: int = 0


@dataclass
class FetchedItem:
    source_item_id: str
    position: int
    kind: ItemKind
    path: Path
    source_format: str | None = None
    processing: Processing = Processing.UNCHANGED
    quality_note: str | None = None


@dataclass
class FetchOutcome:
    items: list[FetchedItem]
    omissions: list[ItemFailure] = field(default_factory=list)
    expected_count: int | None = None
    count_verified: bool = False
    skipped_media_warning: bool = False


@dataclass
class Discovered:
    """What `discover` learned, kept (in the worker) so `fetch` does not have to re-ask."""

    manifest: PostManifest
    source: str  # "ytdlp" | "gallerydl"
    nums: dict[int, int] = field(default_factory=dict)  # position -> gallery-dl `num`
    schemes: dict[int, str] = field(default_factory=dict)  # position -> "ytdl" when gallery-dl merges via yt-dlp
    urls: dict[int, tuple[str, str]] = field(default_factory=dict)  # position -> (direct CDN url, ext); worker-only
    yt_info_json: Path | None = None
    yt_sources: dict[str, YtFormatSource] = field(default_factory=dict)
    yt_duration: float | None = None
    use_cookies: bool = False
    hint: str | None = None


class DiscoveryCache:
    """Small bounded cache keyed by job id. A miss only costs one extra discover call."""

    def __init__(self, capacity: int = 32) -> None:
        self._capacity = capacity
        self._data: OrderedDict[str, Discovered] = OrderedDict()

    def put(self, job_id: object, value: Discovered) -> None:
        self._data[str(job_id)] = value
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def take(self, job_id: object) -> Discovered | None:
        return self._data.pop(str(job_id), None)


class PlatformAdapter(Protocol):
    platform: Platform

    async def discover(self, request: ExtractionRequest, deadline: Deadline) -> PostManifest: ...

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> FetchOutcome: ...


# --------------------------------------------------------------------------------------
# Pure helpers over gallery-dl metadata
# --------------------------------------------------------------------------------------
def file_kind(meta: Mapping[str, Any]) -> ItemKind | None:
    """Photo/video classification of one gallery-dl file record; None = not in scope (audio, ...)."""
    ext = str(meta.get("extension") or "").lower()
    declared = str(meta.get("type") or "").lower()
    if declared in {"audio", "cover", "preview", "subtitle"} or ext in AUDIO_EXT:
        return None
    if ext in VIDEO_EXT or declared in {"video", "animated_gif"}:
        return ItemKind.VIDEO
    if ext in PHOTO_EXT or declared in {"image", "photo"}:
        return ItemKind.PHOTO
    return None


@dataclass(frozen=True)
class GdItem:
    num: int
    kind: ItemKind
    source_item_id: str
    width: int | None
    height: int | None
    scheme: str
    url: str = ""
    extension: str = ""


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value > 0 and value == int(value):
        return int(value)
    return None


def build_gd_items(
    files: list[dict[str, Any]],
    *,
    identity: Callable[[dict[str, Any], int], str],
    kind_of: Callable[[Mapping[str, Any]], ItemKind | None] = file_kind,
) -> list[GdItem]:
    """Ordered, de-duplicated, in-scope items from a gallery-dl dump.

    Items are ordered by the extractor's own `num` (source order), never by download time. A record
    without `num` is only accepted when it is the sole record (single-file extractors).
    """
    items: dict[int, GdItem] = {}
    for index, meta in enumerate(files):
        kind = kind_of(meta)
        if kind is None:
            continue
        num = _positive_int(meta.get("num"))
        if num is None:
            if len(files) != 1:
                raise BotError(ErrorCode.EXTRACTOR_CHANGED, "multi-item result without item numbers")
            num = 1
        if num in items:
            continue  # e.g. a preview image of a video that shares its num: the first record wins
        items[num] = GdItem(
            num=num,
            kind=kind,
            source_item_id=identity(meta, index),
            width=_positive_int(meta.get("width")),
            height=_positive_int(meta.get("height")),
            scheme=str(meta.get("_scheme") or ""),
            url=str(meta.get("_url") or ""),
            extension=str(meta.get("extension") or "").lower(),
        )
    return [items[n] for n in sorted(items)]


def gaps_in_numbering(nums: list[int]) -> list[int]:
    """`num` values missing from 1..max - each is a silently skipped item in the source order."""
    if not nums:
        return []
    present = set(nums)
    return [n for n in range(1, max(nums) + 1) if n not in present]


def manifest_items(items: list[GdItem]) -> list[ItemDescriptor]:
    return [
        ItemDescriptor(source_item_id=i.source_item_id, position=i.num - 1, kind=i.kind, width=i.width, height=i.height)
        for i in items
    ]


async def try_direct_gallerydl(
    ctx: ExtractionContext,
    found: Discovered,
    workspace: Path,
    deadline: Deadline,
    *,
    extra_headers: Mapping[str, str] | None = None,
    max_file_bytes: int | None = None,
) -> dict[int, Path] | None:
    """Download already-discovered direct CDN URLs concurrently, skipping a 2nd gallery-dl scrape.

    Returns `{num: local_path}` on full success, or `None` when any item requires gallery-dl
    (e.g. `ytdl:` DASH/HLS stream, missing extension, or a CDN rejecting direct fetch).
    `FileTooLargeDirect` propagates to the caller so single-video posts can switch to a smaller variant.
    """
    if ctx.downloader is None or not found.manifest.items:
        return None
    out_dir = workspace / "gd"
    targets: list[DirectTarget] = []
    headers = dict(extra_headers) if extra_headers else {}
    for item in found.manifest.items:
        num = found.nums.get(item.position)
        if num is None or found.schemes.get(item.position) == "ytdl":
            return None
        url, ext = found.urls.get(item.position, ("", ""))
        if not url or not _SAFE_EXT.fullmatch(ext):
            return None
        targets.append(DirectTarget(key=num, url=url, dest=out_dir / f"{num:03d}.{ext}", headers=headers))
    try:
        return await ctx.downloader.download_many(targets, workspace, deadline, max_file_bytes=max_file_bytes)
    except DirectDownloadUnavailable:
        return None


def _is_direct_source(src: YtFormatSource | None) -> bool:
    return (
        src is not None
        and (src.protocol or "https").lower() in {"https", "http"}
        and bool(src.ext)
        and bool(_SAFE_EXT.fullmatch(src.ext or ""))
    )


async def fetch_ytdlp_video(
    ctx: ExtractionContext,
    url: str,
    formats: list[FormatDescriptor],
    sources: Mapping[str, YtFormatSource],
    info_json: Path | None,
    workspace: Path,
    deadline: Deadline,
    *,
    mode: DeliveryMode = DeliveryMode.MEDIA,
    duration: float | None = None,
    forced_downgrade: bool = False,
    prefix: str = "yt",
    hint: str | None = None,
    session_supported: bool = False,
    use_cookies: bool = False,
) -> tuple[Path, Any, Processing, str | None]:
    """Fetch the highest source variant that fits under `ctx.file_cap_bytes`.

    Uses direct parallel byte-range downloads for progressive single-stream AND split video+audio
    variants (merging/transcoding in one FFmpeg pass without spawning a 2nd yt-dlp subprocess),
    falling back to `yt-dlp --load-info-json` whenever direct fetch is unavailable.
    """
    from app.media.prepare import merge_tracks_to_mp4
    from app.media.quality import (
        estimated_choice_bytes,
        exact_choice_bytes,
        format_quality_note,
        ranked_choices,
    )

    choices = ranked_choices(formats)
    top = choices[0]
    cap = ctx.file_cap_bytes
    skipped_oversized = forced_downgrade
    oversized_tiers: list[tuple[int, float]] = []  # (pixels, tbr) already proven > cap
    last_format_err: BotError | None = None

    def _pixels_of(c: Any) -> int:
        v = c.video
        return ((v.width or 0) * (v.height or 0) or (v.height or 0)) if v is not None else 0

    for idx, choice in enumerate(choices):
        has_smaller = idx < len(choices) - 1
        px = _pixels_of(choice)
        tbr = (choice.video.tbr or 0.0) if choice.video is not None else 0.0
        if cap is not None and has_smaller and any(px >= fp and tbr >= ft * 0.95 for fp, ft in oversized_tiers):
            skipped_oversized = True
            continue
        v_src = sources.get(choice.video.format_id) if choice.video is not None else None
        a_src = sources.get(choice.audio.format_id) if (choice.merge and choice.audio is not None) else None
        can_direct = ctx.downloader is not None and not choice.merge and _is_direct_source(v_src)
        can_direct_merge = (
            ctx.downloader is not None and choice.merge and _is_direct_source(v_src) and _is_direct_source(a_src)
        )

        if cap is not None:
            exact = exact_choice_bytes(choice)
            if exact is not None and exact > cap:
                skipped_oversized = True
                if px > 0:
                    oversized_tiers.append((px, tbr))
                continue
            # For merged / HLS / DASH streams we cannot inspect a single Content-Length header first,
            # so skip clearly oversized variants by bitrate*duration (keep the smallest as last resort).
            if not can_direct and has_smaller:
                est = estimated_choice_bytes(choice, duration)
                if est is not None and est > cap:
                    skipped_oversized = True
                    continue

        path: Path | None = None
        processing: Processing = Processing.MERGED if choice.merge else Processing.UNCHANGED
        if can_direct:
            assert ctx.downloader is not None and v_src is not None and v_src.ext is not None
            dest = workspace / f"{prefix}_1_direct.{v_src.ext.lower()}"
            try:
                downloaded = await ctx.downloader.download_many(
                    [DirectTarget(key=1, url=v_src.url, dest=dest, headers=v_src.headers)],
                    workspace,
                    deadline,
                    max_file_bytes=cap,
                )
                path = downloaded[1]
            except FileTooLargeDirect:
                skipped_oversized = True
                if px > 0:
                    oversized_tiers.append((px, tbr))
                continue
            except DirectFormatMissing:
                if has_smaller:
                    last_format_err = BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "format url returned 404/410")
                    continue
                path = None
            except DirectDownloadUnavailable:
                path = None
        elif can_direct_merge:
            assert (
                ctx.downloader is not None
                and v_src is not None
                and v_src.ext is not None
                and a_src is not None
                and a_src.ext is not None
                and choice.audio is not None
            )
            v_dest = workspace / f"{prefix}_1_v.{v_src.ext.lower()}"
            a_dest = workspace / f"{prefix}_1_a.{a_src.ext.lower()}"
            merged_dest = workspace / f"{prefix}_1_merged.mp4"
            downloaded_pair: dict[int, Path] | None = None
            try:
                downloaded_pair = await ctx.downloader.download_many(
                    [
                        DirectTarget(key=1, url=v_src.url, dest=v_dest, headers=v_src.headers),
                        DirectTarget(key=2, url=a_src.url, dest=a_dest, headers=a_src.headers),
                    ],
                    workspace,
                    deadline,
                    max_file_bytes=cap,
                )
            except FileTooLargeDirect:
                skipped_oversized = True
                if px > 0:
                    oversized_tiers.append((px, tbr))
                continue
            except DirectFormatMissing:
                if has_smaller:
                    last_format_err = BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "split format url returned 404/410")
                    continue
                downloaded_pair = None
            except DirectDownloadUnavailable:
                downloaded_pair = None
            if downloaded_pair is not None:
                v_path, a_path = downloaded_pair[1], downloaded_pair[2]
                try:
                    merged = await merge_tracks_to_mp4(
                        v_path,
                        a_path,
                        merged_dest,
                        mode=mode,
                        audio_codec=choice.audio.acodec,
                        native_hevc=ctx.native_hevc,
                        runner=ctx.runner,
                        timeout=max(1.0, deadline.remaining()),
                        min_free_bytes=ctx.min_free_bytes,
                        max_file_bytes=cap,
                    )
                    if merged is not None:
                        path, processing = merged
                finally:
                    with contextlib.suppress(OSError):
                        v_path.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        a_path.unlink(missing_ok=True)

        if path is None:
            try:
                paths = await ctx.ytdlp.download(
                    url,
                    workspace,
                    deadline,
                    selector=choice.selector,
                    info_json=info_json,
                    max_file_bytes=cap if has_smaller else None,
                    hint=hint,
                    session_supported=session_supported,
                    use_cookies=use_cookies,
                    prefix=prefix,
                )
                path = paths[0]
                processing = Processing.MERGED if choice.merge else Processing.UNCHANGED
            except BotError as err:
                if cap is not None and err.code is ErrorCode.LIMIT_EXCEEDED and err.reason == "filesize":
                    skipped_oversized = True
                    if px > 0:
                        oversized_tiers.append((px, tbr))
                    continue
                if has_smaller and (
                    err.code is ErrorCode.NOT_FOUND_OR_EXPIRED
                    or (err.code is ErrorCode.EXTRACTOR_CHANGED and err.reason == "no_video")
                ):
                    last_format_err = err
                    continue
                raise

        if cap is not None:
            actual_size = await asyncio.to_thread(os.path.getsize, path)
            if actual_size > cap:
                skipped_oversized = True
                if px > 0:
                    oversized_tiers.append((px, tbr))
                if has_smaller:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    continue
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                raise BotError(ErrorCode.FILE_TOO_LARGE, "all source video variants exceed upload limit")

        note = (
            format_quality_note(choice, None if (forced_downgrade and idx == 0) else top, cap)
            if (cap is not None and skipped_oversized)
            else None
        )
        return path, choice, processing, note

    if skipped_oversized and cap is not None:
        raise BotError(ErrorCode.FILE_TOO_LARGE, "all source video variants exceed upload limit")
    if last_format_err is not None:
        raise last_format_err
    raise BotError(ErrorCode.EXTRACTOR_CHANGED, "no playable video format could be downloaded")


async def ytdlp_single_video(
    ctx: ExtractionContext,
    url: str,
    workspace: Path,
    deadline: Deadline,
    *,
    id_matches: Callable[[str], bool],
    hint: str,
    session_supported: bool,
    mode: DeliveryMode = DeliveryMode.MEDIA,
    use_cookies: bool = False,
    forced_downgrade: bool = False,
) -> tuple[Path, str | None, Processing, str | None]:
    """Targeted yt-dlp fallback for a post whose ONLY item is one video.

    Reconciliation is trivial and therefore safe (one entry <-> one item): the entry must carry the
    requested post id. Multi-item posts never use this path - matching yt-dlp's entries to
    gallery-dl's items by guesswork could deliver the wrong media.
    """
    info = await ctx.ytdlp.discover(
        url, workspace, deadline, hint=hint, session_supported=session_supported, use_cookies=use_cookies
    )
    if len(info.entries) != 1:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "fallback did not return exactly the requested post")
    entry = info.entries[0]
    matched = id_matches(entry.id) or (bool(entry.display_id) and id_matches(entry.display_id))
    if not matched:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "fallback did not return exactly the requested post")
    path, choice, processing, note = await fetch_ytdlp_video(
        ctx,
        url,
        entry.formats,
        entry.sources,
        info.info_json,
        workspace,
        deadline,
        mode=mode,
        duration=entry.duration,
        forced_downgrade=forced_downgrade,
        prefix="fb",
        hint=hint,
        session_supported=session_supported,
        use_cookies=use_cookies,
    )
    return (
        path,
        choice.video.format_id if choice.video else None,
        processing,
        note,
    )


_FALLBACK_CODES = frozenset({ErrorCode.EXTRACTOR_CHANGED, ErrorCode.UNSUPPORTED_POST})


async def fetch_via_gallerydl(
    ctx: ExtractionContext,
    found: Discovered,
    url: str,
    workspace: Path,
    deadline: Deadline,
    *,
    options: Mapping[str, str],
    use_cookies: bool,
    hint: str,
    session_supported: bool,
    ytdlp_id_matches: Callable[[str], bool] | None,
    direct_headers: Mapping[str, str] | None = None,
    mode: DeliveryMode = DeliveryMode.MEDIA,
) -> FetchOutcome:
    """Download a gallery-dl-discovered post and map the files back to manifest items by identity.

    If (and only if) the post's SOLE item is a video that gallery-dl could not deliver - or whose
    top variant exceeds `ctx.file_cap_bytes` - one yt-dlp attempt is made, accepted only when it
    returns exactly the requested post (`ytdlp_id_matches`). Access/auth/rate/challenge errors never
    trigger it. Pass `ytdlp_id_matches=None` to disable the fallback (e.g. Stories).
    """
    manifest = found.manifest
    only = manifest.items[0] if len(manifest.items) == 1 else None
    fallback_possible = ytdlp_id_matches is not None and only is not None and only.kind is ItemKind.VIDEO

    items: list[FetchedItem] = []
    omissions: list[ItemFailure] = []
    skipped_warning = False
    oversized_single = False

    direct_files: dict[int, Path] | None = None
    try:
        direct_files = await try_direct_gallerydl(
            ctx,
            found,
            workspace,
            deadline,
            extra_headers=direct_headers,
            max_file_bytes=ctx.file_cap_bytes if fallback_possible else None,
        )
    except FileTooLargeDirect:
        oversized_single = True

    if oversized_single and fallback_possible and only is not None and ytdlp_id_matches is not None:
        try:
            path, fmt, processing, note = await ytdlp_single_video(
                ctx,
                url,
                workspace,
                deadline,
                id_matches=ytdlp_id_matches,
                hint=hint,
                session_supported=session_supported,
                mode=mode,
                use_cookies=use_cookies,
                forced_downgrade=True,
            )
            items = [FetchedItem(only.source_item_id, only.position, ItemKind.VIDEO, path, fmt, processing, note)]
            omissions = []
            return FetchOutcome(
                items=items,
                omissions=omissions,
                expected_count=manifest.expected_count,
                count_verified=manifest.count_verified,
                skipped_media_warning=manifest.skipped_media_warning,
            )
        except BotError as err:
            if not err.fallback_allowed or err.code is ErrorCode.FILE_TOO_LARGE:
                raise
            raise BotError(ErrorCode.FILE_TOO_LARGE, "source video exceeds upload limit") from None

    if direct_files is not None:
        items, omissions = map_downloads(found, direct_files)
    else:
        try:
            res = await ctx.gallerydl.download(
                url,
                workspace,
                deadline,
                options=options,
                use_cookies=use_cookies,
                hint=hint,
                session_supported=session_supported,
            )
            items, omissions = map_downloads(found, res.files)
            skipped_warning = res.warnings.skipped_media
        except BotError as err:
            if not (fallback_possible and err.fallback_allowed and err.code in _FALLBACK_CODES):
                raise
            assert only is not None
            omissions = [
                ItemFailure(
                    position=only.position,
                    source_item_id=only.source_item_id,
                    code=err.code,
                    message="gallery-dl could not deliver the video",
                )
            ]

    if (
        fallback_possible
        and ctx.file_cap_bytes is not None
        and len(items) == 1
        and not omissions
        and only is not None
        and ytdlp_id_matches is not None
    ):
        size = await asyncio.to_thread(os.path.getsize, items[0].path)
        if size > ctx.file_cap_bytes:
            oversized_path = items[0].path
            try:
                path, fmt, processing, note = await ytdlp_single_video(
                    ctx,
                    url,
                    workspace,
                    deadline,
                    id_matches=ytdlp_id_matches,
                    hint=hint,
                    session_supported=session_supported,
                    mode=mode,
                    use_cookies=use_cookies,
                    forced_downgrade=True,
                )
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(oversized_path.unlink, missing_ok=True)
                items = [FetchedItem(only.source_item_id, only.position, ItemKind.VIDEO, path, fmt, processing, note)]
            except BotError as err:
                if not err.fallback_allowed:
                    raise
                # Keep original oversized item so delivery_plan reports FILE_TOO_LARGE cleanly.
                if err.code is ErrorCode.FILE_TOO_LARGE:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(oversized_path.unlink, missing_ok=True)
                    raise

    if omissions and fallback_possible and only is not None and ytdlp_id_matches is not None:
        try:
            path, fmt, processing, note = await ytdlp_single_video(
                ctx,
                url,
                workspace,
                deadline,
                id_matches=ytdlp_id_matches,
                hint=hint,
                session_supported=session_supported,
                mode=mode,
                use_cookies=use_cookies,
            )
            items = [FetchedItem(only.source_item_id, only.position, ItemKind.VIDEO, path, fmt, processing, note)]
            omissions = []
        except BotError as err:
            if not err.fallback_allowed:
                raise  # e.g. rate limited / restricted while retrying: report it, do not paper over it
    return FetchOutcome(
        items=items,
        omissions=omissions,
        expected_count=manifest.expected_count,
        count_verified=manifest.count_verified and not skipped_warning,
        skipped_media_warning=manifest.skipped_media_warning or skipped_warning,
    )


def check_item_limit(count: int, limit: int) -> None:
    if count > limit:
        raise BotError(ErrorCode.LIMIT_EXCEEDED, f"{count} items exceeds the limit of {limit}")


def map_downloads(discovered: Discovered, files: Mapping[int, Path]) -> tuple[list[FetchedItem], list[ItemFailure]]:
    """Match downloaded `NNN.ext` files back to manifest items by identity (num), never by order of
    arrival. Items without a file become explicit omissions; surplus files are ignored."""
    items: list[FetchedItem] = []
    omissions: list[ItemFailure] = []
    manifest = discovered.manifest
    # Positions the source says exist (expected_count) but that the listing never returned - e.g. an
    # Instagram item gallery-dl silently skipped, or a TikTok post declaring more images than it listed.
    listed = {i.position for i in manifest.items}
    for position in range(manifest.expected_count or 0):
        if position not in listed:
            omissions.append(
                ItemFailure(
                    position=position,
                    code=ErrorCode.EXTRACTOR_CHANGED,
                    message="item is part of the post but was not returned by the extractor",
                )
            )
    for item in manifest.items:
        num = discovered.nums.get(item.position)
        path = files.get(num) if num is not None else None
        if path is None:
            omissions.append(
                ItemFailure(
                    position=item.position,
                    source_item_id=item.source_item_id,
                    code=ErrorCode.EXTRACTOR_CHANGED,
                    message="item was not downloaded",
                )
            )
            continue
        processing = Processing.MERGED if discovered.schemes.get(item.position) == "ytdl" else Processing.UNCHANGED
        items.append(FetchedItem(item.source_item_id, item.position, item.kind, path, processing=processing))
    omissions.sort(key=lambda o: (o.position is None, o.position))
    return items, omissions
