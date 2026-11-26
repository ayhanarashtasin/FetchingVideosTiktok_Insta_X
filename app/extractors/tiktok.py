"""TikTok adapter (T03.4 video + short links, T03.5 photo mode).

* Fast path: single-GET TikTok web rehydration JSON (`FastApiClient.discover_tiktok`) for both
  public video posts and photo-mode posts, with clean fallback to `yt-dlp` / `gallery-dl`.
* Video (`/video/<id>`): yt-dlp - best format, merged losslessly if needed.
* Photo mode (`/photo/<id>`): gallery-dl, returning the SOURCE photos in order. The audio track and
  covers that gallery-dl can emit are switched off and filtered again by file type, so a slideshow
  is never fabricated and an audio-only item is never sent as if it were a photo.
* Fallback between the two happens only when the target turns out to be of the other kind
  (`no_video` / unsupported); never for access, auth, rate-limit or challenge errors.
* Every path verifies that the extractor returned the REQUESTED post id.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, cast

from app.core.contracts import (
    Access,
    ExtractionRequest,
    ItemDescriptor,
    ItemKind,
    Platform,
    PostManifest,
    PostType,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.base import (
    Discovered,
    DiscoveryCache,
    ExtractionContext,
    FetchedItem,
    FetchOutcome,
    build_gd_items,
    check_item_limit,
    fetch_ytdlp_video,
    gaps_in_numbering,
    manifest_items,
    map_downloads,
    try_direct_gallerydl,
)
from app.media.quality import select_format

_POST_ID = re.compile(r"/(?:video|photo)/(\d{6,25})(?:/|$)")
GD_OPTIONS = {"audio": "false", "covers": "false", "subtitles": "false"}
_FALLBACK_CODES = frozenset({ErrorCode.EXTRACTOR_CHANGED, ErrorCode.UNSUPPORTED_POST})


class _FastVideoFallback(Exception):
    """Raised when fast-path direct video download cannot complete and must fall back to yt-dlp."""


class _NoSubprocessYtDlp:
    """Prevents `fetch_ytdlp_video` from invoking `yt-dlp -f` with a fast-path synthetic format_id."""

    async def download(self, *_args: object, **_kwargs: object) -> list[Path]:
        raise _FastVideoFallback()


def post_id_of(url: str) -> str:
    m = _POST_ID.search(url)
    if not m:
        raise BotError(ErrorCode.UNSUPPORTED_POST, "no post id in tiktok url")
    return m.group(1)


class TikTokAdapter:
    platform = Platform.TIKTOK

    def __init__(self, ctx: ExtractionContext, cache: DiscoveryCache | None = None) -> None:
        self.ctx = ctx
        self.cache = cache or DiscoveryCache()

    # ------------------------------------------------------------------ discover
    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        post_id = post_id_of(request.extraction_url)
        if self.ctx.fast_api is not None:
            fast_found = await self._discover_fast_tiktok(post_id, deadline)
            if fast_found is not None:
                self.cache.put(request.job_id, fast_found)
                return fast_found.manifest
        if request.post_hint == "photo":
            found = await self._discover_gallerydl(request, post_id, workspace, deadline)
        else:
            try:
                found = await self._discover_ytdlp(request, post_id, workspace, deadline)
            except BotError as err:
                if not (err.fallback_allowed and err.code in _FALLBACK_CODES):
                    raise
                # The "video" URL may actually be a photo post; same post id, different extractor.
                found = await self._discover_gallerydl(request, post_id, workspace, deadline)
        self.cache.put(request.job_id, found)
        return found.manifest

    async def _discover_fast_tiktok(self, post_id: str, deadline: Deadline) -> Discovered | None:
        assert self.ctx.fast_api is not None
        fast = await self.ctx.fast_api.discover_tiktok(post_id, deadline)
        if fast is None:
            return None
        if fast.is_photo:
            check_item_limit(len(fast.photos), self.ctx.max_items)
            manifest = PostManifest(
                platform=Platform.TIKTOK,
                post_id=post_id,
                post_type=PostType.PHOTO if len(fast.photos) == 1 else PostType.CAROUSEL,
                access=Access.PERMITTED_PUBLIC,
                expected_count=len(fast.photos),
                count_verified=True,
                items=[
                    ItemDescriptor(
                        source_item_id=p.source_item_id,
                        position=p.num - 1,
                        kind=ItemKind.PHOTO,
                        width=p.width,
                        height=p.height,
                    )
                    for p in fast.photos
                ],
            )
            return Discovered(
                manifest,
                "fast_tiktok",
                nums={p.num - 1: p.num for p in fast.photos},
                urls={p.num - 1: (p.url, p.extension) for p in fast.photos},
                direct_telegram_ok=True,  # TikTok photo CDN (p16/p19) verified HTTP 200/206 without Referer/Cookie
                hint="photo",
            )

        try:
            choice = select_format(fast.formats)
        except BotError:
            return None  # e.g. only unplayable bytevc2 variants -> let yt-dlp classify/extract
        best = choice.video
        manifest = PostManifest(
            platform=Platform.TIKTOK,
            post_id=post_id,
            post_type=PostType.VIDEO,
            access=Access.PERMITTED_PUBLIC,
            expected_count=1,
            count_verified=True,
            items=[
                ItemDescriptor(
                    source_item_id=post_id,
                    position=0,
                    kind=ItemKind.VIDEO,
                    width=best.width if best else fast.width,
                    height=best.height if best else fast.height,
                    candidate_formats=fast.formats,
                )
            ],
        )
        return Discovered(
            manifest,
            "fast_tiktok",
            nums={0: 1},
            yt_info_json=None,
            yt_sources=fast.sources,
            yt_duration=fast.duration,
            direct_telegram_ok=False,  # TikTok video CDN requires session cookies / Referer
            hint="video",
        )

    async def _discover_ytdlp(
        self, request: ExtractionRequest, post_id: str, workspace: Path, deadline: Deadline
    ) -> Discovered:
        info = await self.ctx.ytdlp.discover(request.extraction_url, workspace, deadline, hint="video")
        if len(info.entries) != 1:
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, f"expected one entry, got {len(info.entries)}")
        entry = info.entries[0]
        if entry.id != post_id and entry.display_id != post_id:
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "yt-dlp returned a different post than requested")
        choice = select_format(entry.formats)  # raises EXTRACTOR_CHANGED/no_video when nothing is playable
        best = choice.video
        manifest = PostManifest(
            platform=Platform.TIKTOK,
            post_id=post_id,
            post_type=PostType.VIDEO,
            access=Access.PERMITTED_PUBLIC,  # served without any session
            expected_count=1,
            count_verified=True,  # a TikTok video post is exactly one video
            items=[
                ItemDescriptor(
                    source_item_id=post_id,
                    position=0,
                    kind=ItemKind.VIDEO,
                    width=best.width if best else entry.width,
                    height=best.height if best else entry.height,
                    candidate_formats=entry.formats,
                )
            ],
        )
        return Discovered(
            manifest,
            "ytdlp",
            nums={0: 1},
            yt_info_json=info.info_json,
            yt_sources=entry.sources,
            yt_duration=entry.duration,
            hint="video",
        )

    async def _discover_gallerydl(
        self, request: ExtractionRequest, post_id: str, workspace: Path, deadline: Deadline
    ) -> Discovered:
        dump = await self.ctx.gallerydl.discover(
            request.extraction_url, workspace, deadline, options=GD_OPTIONS, hint="photo", session_supported=False
        )
        directory = dump.directories[0] if dump.directories else {}
        if str(directory.get("id") or "") != post_id:
            # A missing id counts as a mismatch: identity must be PROVEN, not assumed.
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "gallery-dl returned a different or unidentified post")
        items = build_gd_items(dump.files, identity=lambda meta, i: f"{post_id}:{meta.get('num') or i + 1}")
        if not items:
            raise BotError(ErrorCode.UNSUPPORTED_POST, "post has no photos or videos", reason="no_media")
        check_item_limit(len(items), self.ctx.max_items)

        nums = [i.num for i in items]
        gaps = gaps_in_numbering(nums)
        image_post = directory.get("imagePost")
        images = image_post.get("images") if isinstance(image_post, dict) else None
        declared = len(images) if isinstance(images, list) else None
        photos = [i for i in items if i.kind is ItemKind.PHOTO]
        if photos:
            expected = max(max(nums), declared or 0)
            # `declared` comes from the post's own image list, i.e. independent of what we extracted.
            # It is a trustworthy count even when it DISAGREES with what was listed: then the result is
            # PARTIAL (we know items are missing), not merely unverified.
            verified = declared is not None and not gaps and len(photos) == len(items)
        else:  # a video post reached through gallery-dl: one video, by definition of the post type
            expected, verified = max(nums), len(items) == 1 and not gaps
        warned = dump.warnings.skipped_media
        manifest = PostManifest(
            platform=Platform.TIKTOK,
            post_id=post_id,
            post_type=(PostType.VIDEO if not photos else (PostType.PHOTO if len(items) == 1 else PostType.CAROUSEL)),
            access=Access.PERMITTED_PUBLIC,
            expected_count=expected,
            count_verified=verified and not warned,
            skipped_media_warning=warned,
            items=manifest_items(items),
        )
        all_direct_photos = (
            bool(photos) and len(photos) == len(items) and all(i.url and i.scheme != "ytdl" for i in items)
        )
        return Discovered(
            manifest,
            "gallerydl",
            nums={i.num - 1: i.num for i in items},
            schemes={i.num - 1: i.scheme for i in items},
            urls={i.num - 1: (i.url, i.extension) for i in items if i.url},
            direct_telegram_ok=all_direct_photos,
            hint="photo",
        )

    # ------------------------------------------------------------------ fetch
    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        found = self.cache.take(request.job_id)
        if found is None:  # worker restarted between discover and fetch: ask again
            await self.discover(request, workspace, deadline)
            found = self.cache.take(request.job_id)
            assert found is not None
        if found.source == "fast_tiktok":
            return await self._fetch_fast_tiktok(request, found, workspace, deadline)
        if found.source == "ytdlp":
            return await self._fetch_ytdlp(request, found, workspace, deadline)
        return await self._fetch_gallerydl(request, found, workspace, deadline)

    async def _fetch_fast_tiktok(
        self, request: ExtractionRequest, found: Discovered, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        post_id = found.manifest.post_id
        if found.hint == "photo":
            direct_files = await try_direct_gallerydl(
                self.ctx, found, workspace, deadline, extra_headers={"Referer": "https://www.tiktok.com/"}
            )
            if direct_files is not None:
                items, omissions = map_downloads(found, direct_files)
                manifest = found.manifest
                return FetchOutcome(
                    items=items,
                    omissions=omissions,
                    expected_count=manifest.expected_count,
                    count_verified=manifest.count_verified,
                    skipped_media_warning=manifest.skipped_media_warning,
                )
            fallback = await self._discover_gallerydl(request, post_id, workspace, deadline)
            return await self._fetch_gallerydl(request, fallback, workspace, deadline)

        fast_ctx = dataclasses.replace(self.ctx, ytdlp=cast(Any, _NoSubprocessYtDlp()))
        try:
            return await self._fetch_ytdlp(request, found, workspace, deadline, ctx=fast_ctx)
        except _FastVideoFallback:
            fallback = await self._discover_ytdlp(request, post_id, workspace, deadline)
            return await self._fetch_ytdlp(request, fallback, workspace, deadline)

    async def _fetch_ytdlp(
        self,
        request: ExtractionRequest,
        found: Discovered,
        workspace: Path,
        deadline: Deadline,
        *,
        ctx: ExtractionContext | None = None,
    ) -> FetchOutcome:
        item = found.manifest.items[0]
        path, choice, processing, note = await fetch_ytdlp_video(
            ctx or self.ctx,
            request.extraction_url,
            item.candidate_formats,
            found.yt_sources,
            found.yt_info_json,
            workspace,
            deadline,
            mode=request.delivery_mode,
            duration=found.yt_duration,
            hint="video",
        )
        fetched = FetchedItem(
            source_item_id=item.source_item_id,
            position=0,
            kind=ItemKind.VIDEO,
            path=path,
            source_format=choice.video.format_id if choice.video else None,
            processing=processing,
            quality_note=note,
        )
        return FetchOutcome(items=[fetched], expected_count=1, count_verified=True)

    async def _fetch_gallerydl(
        self, request: ExtractionRequest, found: Discovered, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        direct_files = await try_direct_gallerydl(
            self.ctx, found, workspace, deadline, extra_headers={"Referer": "https://www.tiktok.com/"}
        )
        if direct_files is not None:
            items, omissions = map_downloads(found, direct_files)
            manifest = found.manifest
            return FetchOutcome(
                items=items,
                omissions=omissions,
                expected_count=manifest.expected_count,
                count_verified=manifest.count_verified,
                skipped_media_warning=manifest.skipped_media_warning,
            )
        res = await self.ctx.gallerydl.download(
            request.extraction_url, workspace, deadline, options=GD_OPTIONS, hint="photo", session_supported=False
        )
        items, omissions = map_downloads(found, res.files)
        manifest = found.manifest
        return FetchOutcome(
            items=items,
            omissions=omissions,
            expected_count=manifest.expected_count,
            count_verified=manifest.count_verified and not res.warnings.skipped_media,
            skipped_media_warning=manifest.skipped_media_warning or res.warnings.skipped_media,
        )
