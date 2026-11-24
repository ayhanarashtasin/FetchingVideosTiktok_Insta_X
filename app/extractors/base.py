"""Adapter contract and shared helpers (T03.1, T03.3, architecture sections 6-7).

A `PlatformAdapter` resolves ONE target post to an ordered manifest (`discover`) and materialises
only those items (`fetch`). Both steps treat extractor output as untrusted: the target identity is
verified, unrelated media (audio tracks, covers, other posts) is excluded, and anything that cannot
be proven is reported as an omission or as unverified completeness - never silently.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.core.contracts import (
    ExtractionRequest,
    ItemDescriptor,
    ItemFailure,
    ItemKind,
    Platform,
    PostManifest,
    Processing,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.ytdlp_runner import YtDlpRunner

PHOTO_EXT = frozenset({"jpg", "jpeg", "png", "webp", "avif", "heic"})
VIDEO_EXT = frozenset({"mp4", "m4v", "mov", "webm", "mkv"})
AUDIO_EXT = frozenset({"mp3", "m4a", "aac", "opus", "ogg", "wav", "flac"})


@dataclass
class ExtractionContext:
    ytdlp: YtDlpRunner
    gallerydl: GalleryDlRunner
    max_items: int = 30


@dataclass
class FetchedItem:
    source_item_id: str
    position: int
    kind: ItemKind
    path: Path
    source_format: str | None = None
    processing: Processing = Processing.UNCHANGED


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


async def ytdlp_single_video(
    ctx: ExtractionContext,
    url: str,
    workspace: Path,
    deadline: Deadline,
    *,
    id_matches: Callable[[str], bool],
    hint: str,
    session_supported: bool,
    use_cookies: bool = False,
) -> tuple[Path, str | None, Processing]:
    """Targeted yt-dlp fallback for a post whose ONLY item is one video.

    Reconciliation is trivial and therefore safe (one entry <-> one item): the entry must carry the
    requested post id. Multi-item posts never use this path - matching yt-dlp's entries to
    gallery-dl's items by guesswork could deliver the wrong media.
    """
    # Local import: media.quality does not depend on extractors, keep it that way.
    from app.media.quality import select_format

    info = await ctx.ytdlp.discover(
        url, workspace, deadline, hint=hint, session_supported=session_supported, use_cookies=use_cookies
    )
    if len(info.entries) != 1 or not id_matches(info.entries[0].id):
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "fallback did not return exactly the requested post")
    choice = select_format(info.entries[0].formats)
    paths = await ctx.ytdlp.download(
        url,
        workspace,
        deadline,
        selector=choice.selector,
        hint=hint,
        session_supported=session_supported,
        use_cookies=use_cookies,
        prefix="fb",
    )
    return (
        paths[0],
        choice.video.format_id if choice.video else None,
        Processing.MERGED if choice.merge else Processing.UNCHANGED,
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
) -> FetchOutcome:
    """Download a gallery-dl-discovered post and map the files back to manifest items by identity.

    If (and only if) the post's SOLE item is a video that gallery-dl could not deliver - either by
    failing outright or by returning nothing for it - one yt-dlp attempt is made, accepted only when it
    returns exactly the requested post (`ytdlp_id_matches`). Access/auth/rate/challenge errors never
    trigger it. Pass `ytdlp_id_matches=None` to disable the fallback (e.g. Stories).
    """
    manifest = found.manifest
    only = manifest.items[0] if len(manifest.items) == 1 else None
    fallback_possible = ytdlp_id_matches is not None and only is not None and only.kind is ItemKind.VIDEO

    items: list[FetchedItem] = []
    omissions: list[ItemFailure] = []
    skipped_warning = False
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
    if omissions and fallback_possible and only is not None and ytdlp_id_matches is not None:
        try:
            path, fmt, processing = await ytdlp_single_video(
                ctx,
                url,
                workspace,
                deadline,
                id_matches=ytdlp_id_matches,
                hint=hint,
                session_supported=session_supported,
                use_cookies=use_cookies,
            )
            items = [FetchedItem(only.source_item_id, only.position, ItemKind.VIDEO, path, fmt, processing)]
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
