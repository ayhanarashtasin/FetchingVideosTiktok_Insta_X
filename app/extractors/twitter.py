"""X / Twitter adapter (T03.9).

Direct attachments of the linked post ONLY. gallery-dl is told explicitly to skip quoted posts,
retweets, link cards, text-only posts and Articles, and the adapter re-verifies identity: every file
must belong to the requested tweet id and must not be a retweet. Both x.com and twitter.com links
arrive here already normalised to x.com by the URL validator.

A single-video tweet whose video gallery-dl cannot deliver gets one yt-dlp retry, accepted only if
yt-dlp returns exactly that tweet. No X session is ever used: a login wall is reported, not bypassed.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.contracts import (
    Access,
    ExtractionRequest,
    ItemDescriptor,
    ItemKind,
    Platform,
    PostManifest,
    PostType,
    Processing,
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
    fetch_via_gallerydl,
    fetch_ytdlp_video,
    gaps_in_numbering,
    manifest_items,
    map_downloads,
)
from app.extractors.direct_download import (
    DirectDownloadUnavailable,
    DirectFormatMissing,
    DirectTarget,
    FileTooLargeDirect,
)
from app.media.quality import select_format

_STATUS = re.compile(r"/(?P<user>[A-Za-z0-9_]{1,15}|i(?:/web)?)/status/(?P<id>\d+)")
GD_OPTIONS = {
    "quoted": "false",
    "retweets": "false",
    "replies": "true",
    "cards": "false",
    "text-tweets": "false",
    "articles": "false",
    "conversations": "false",
    "expand": "false",
    "previews": "false",
    "twitpic": "false",
    "ratelimit": "abort",
}


def tweet_id_of(url: str) -> str:
    m = _STATUS.search(url)
    if not m:
        raise BotError(ErrorCode.UNSUPPORTED_POST, "no status id in x url")
    return m.group("id")


def _username_of(url: str) -> str | None:
    m = _STATUS.search(url)
    if not m:
        return None
    u = m.group("user")
    return None if u in {"i", "i/web"} else u


class TwitterAdapter:
    platform = Platform.X

    def __init__(self, ctx: ExtractionContext, cache: DiscoveryCache | None = None) -> None:
        self.ctx = ctx
        self.cache = cache or DiscoveryCache()

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        tweet_id = tweet_id_of(request.extraction_url)
        if self.ctx.fast_api is not None:
            fast = await self.ctx.fast_api.discover_x(tweet_id, _username_of(request.extraction_url), deadline)
            if fast is not None:
                if fast.not_found:
                    raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "post not found")
                if fast.text_only and not fast.items:
                    raise BotError(ErrorCode.UNSUPPORTED_POST, "post has no photos or videos", reason="no_media")
                if fast.items:
                    check_item_limit(len(fast.items), self.ctx.max_items)
                    manifest = PostManifest(
                        platform=Platform.X,
                        post_id=tweet_id,
                        post_type=(
                            PostType.CAROUSEL
                            if len(fast.items) > 1
                            else (PostType.VIDEO if fast.items[0].kind is ItemKind.VIDEO else PostType.PHOTO)
                        ),
                        access=Access.PERMITTED_PUBLIC,
                        expected_count=len(fast.items),
                        count_verified=True,
                        items=[
                            ItemDescriptor(
                                source_item_id=i.source_item_id,
                                position=i.num - 1,
                                kind=i.kind,
                                width=i.width,
                                height=i.height,
                            )
                            for i in fast.items
                        ],
                    )
                    self.cache.put(
                        request.job_id,
                        Discovered(
                            manifest,
                            "fast_api",
                            nums={i.num - 1: i.num for i in fast.items},
                            urls={i.num - 1: (i.url, i.extension) for i in fast.items},
                            variants={i.num - 1: i.variants for i in fast.items if i.variants},
                            durations={i.num - 1: i.duration for i in fast.items if i.duration},
                            direct_telegram_ok=True,
                            hint="tweet",
                        ),
                    )
                    return manifest

        try:
            dump = await self.ctx.gallerydl.discover(
                request.extraction_url,
                workspace,
                deadline,
                options=GD_OPTIONS,
                hint="tweet",
                session_supported=False,
                empty_means_no_media=True,
            )
        except BotError as err:
            if err.fallback_allowed and err.code is ErrorCode.EXTRACTOR_CHANGED:
                found = await self._discover_ytdlp_fallback(request, tweet_id, workspace, deadline)
                if found is not None:
                    self.cache.put(request.job_id, found)
                    return found.manifest
            raise
        for meta in dump.files:
            # Identity: every attachment must belong to THIS tweet (not a quoted/retweeted one).
            if str(meta.get("tweet_id") or "") != tweet_id or meta.get("retweet_id"):
                raise BotError(
                    ErrorCode.EXTRACTOR_CHANGED, "result contains media from a different post", reason="foreign_media"
                )
        items = build_gd_items(dump.files, identity=lambda m, i: f"{tweet_id}:{m.get('num') or i + 1}")
        if not items:
            raise BotError(ErrorCode.UNSUPPORTED_POST, "post has no photos or videos", reason="no_media")
        check_item_limit(len(items), self.ctx.max_items)
        nums = [i.num for i in items]
        gaps = gaps_in_numbering(nums)
        declared = dump.directories[0].get("count") if dump.directories else None
        warned = dump.warnings.skipped_media  # "Media unavailable": an attachment was dropped
        manifest = PostManifest(
            platform=Platform.X,
            post_id=tweet_id,
            post_type=(
                PostType.CAROUSEL
                if len(items) > 1
                else (PostType.VIDEO if items[0].kind is ItemKind.VIDEO else PostType.PHOTO)
            ),
            access=Access.PERMITTED_PUBLIC,  # anonymous access only
            expected_count=max(max(nums), declared if isinstance(declared, int) else 0),
            count_verified=not gaps and not warned,
            skipped_media_warning=warned,
            items=manifest_items(items),
        )
        all_direct = bool(items) and all(i.url and i.scheme != "ytdl" for i in items)
        self.cache.put(
            request.job_id,
            Discovered(
                manifest,
                "gallerydl",
                nums={i.num - 1: i.num for i in items},
                schemes={i.num - 1: i.scheme for i in items},
                urls={i.num - 1: (i.url, i.extension) for i in items if i.url},
                direct_telegram_ok=all_direct,
                hint="tweet",
            ),
        )
        return manifest

    async def _discover_ytdlp_fallback(
        self, request: ExtractionRequest, tweet_id: str, workspace: Path, deadline: Deadline
    ) -> Discovered | None:
        """Single-video yt-dlp fallback when gallery-dl fails with an unclassified extractor error."""
        try:
            info = await self.ctx.ytdlp.discover(
                request.extraction_url, workspace, deadline, hint="tweet", session_supported=False
            )
        except BotError:
            return None
        if len(info.entries) != 1:
            return None
        entry = info.entries[0]
        if not (
            entry.id == tweet_id
            or entry.id.startswith(f"{tweet_id}_")
            or (entry.display_id and (entry.display_id == tweet_id or entry.display_id.startswith(f"{tweet_id}_")))
        ):
            return None
        try:
            choice = select_format(entry.formats)
        except BotError:
            return None
        best = choice.video
        manifest = PostManifest(
            platform=Platform.X,
            post_id=tweet_id,
            post_type=PostType.VIDEO,
            access=Access.PERMITTED_PUBLIC,
            expected_count=1,
            count_verified=True,
            items=[
                ItemDescriptor(
                    source_item_id=f"{tweet_id}:1",
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
            hint="tweet",
        )

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        found = self.cache.take(request.job_id)
        if found is None:
            await self.discover(request, workspace, deadline)
            found = self.cache.take(request.job_id)
            assert found is not None
        tweet_id = found.manifest.post_id
        if found.source == "ytdlp":
            item = found.manifest.items[0]
            path, choice, processing, note = await fetch_ytdlp_video(
                self.ctx,
                request.extraction_url,
                item.candidate_formats,
                found.yt_sources,
                found.yt_info_json,
                workspace,
                deadline,
                mode=request.delivery_mode,
                duration=found.yt_duration,
                hint="tweet",
                session_supported=False,
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
        if found.source == "fast_api":
            fast_outcome = await self._fetch_fast_api(found, workspace, deadline)
            if fast_outcome is not None:
                return fast_outcome
        return await fetch_via_gallerydl(
            self.ctx,
            found,
            request.extraction_url,
            workspace,
            deadline,
            options=GD_OPTIONS,
            use_cookies=False,
            hint="tweet",
            session_supported=False,
            mode=request.delivery_mode,
            ytdlp_id_matches=lambda i: i == tweet_id or i.startswith(f"{tweet_id}_"),
        )

    async def _fetch_fast_api(self, found: Discovered, workspace: Path, deadline: Deadline) -> FetchOutcome | None:
        """Download fast-API-discovered items concurrently, downgrading single oversized videos cleanly."""
        if self.ctx.downloader is None or not found.manifest.items:
            return None
        manifest = found.manifest
        cap = self.ctx.file_cap_bytes
        out_dir = workspace / "gd"

        # Single video with multiple MP4 variants: downgrade >50 MB videos directly without a subprocess.
        if len(manifest.items) == 1 and manifest.items[0].kind is ItemKind.VIDEO:
            only = manifest.items[0]
            variants = found.variants.get(only.position) or ()
            if variants:
                skipped_oversized = False
                top_v = variants[0]
                for idx, v in enumerate(variants):
                    dest = out_dir / f"001.{v.ext}"
                    try:
                        res = await self.ctx.downloader.download_many(
                            [DirectTarget(key=1, url=v.url, dest=dest)],
                            workspace,
                            deadline,
                            max_file_bytes=cap,
                        )
                    except FileTooLargeDirect:
                        skipped_oversized = True
                        continue
                    except DirectFormatMissing:
                        if idx < len(variants) - 1:
                            continue
                        return None
                    except DirectDownloadUnavailable:
                        return None
                    note = None
                    if skipped_oversized and cap is not None:
                        cap_mb = max(1, cap // 1_000_000)
                        c_res = f"{min(v.width, v.height)}p" if (v.width and v.height) else None
                        t_res = f"{min(top_v.width, top_v.height)}p" if (top_v.width and top_v.height) else None
                        if c_res and t_res and c_res != t_res:
                            note = f"Sent {c_res} ({t_res} source exceeded {cap_mb} MB Telegram limit)"
                        elif c_res:
                            note = f"Sent lower-bitrate {c_res} (original exceeded {cap_mb} MB Telegram limit)"
                        else:
                            note = f"Sent smaller variant (original exceeded {cap_mb} MB Telegram limit)"
                    return FetchOutcome(
                        items=[
                            FetchedItem(
                                source_item_id=only.source_item_id,
                                position=only.position,
                                kind=ItemKind.VIDEO,
                                path=res[1],
                                processing=Processing.UNCHANGED,
                                quality_note=note,
                            )
                        ],
                        expected_count=1,
                        count_verified=True,
                    )
                if skipped_oversized and cap is not None:
                    raise BotError(ErrorCode.FILE_TOO_LARGE, "all source video variants exceed upload limit")
                return None

        targets: list[DirectTarget] = []
        for item in manifest.items:
            num = found.nums.get(item.position)
            url, ext = found.urls.get(item.position, ("", ""))
            if num is None or not url or not ext:
                return None
            targets.append(DirectTarget(key=num, url=url, dest=out_dir / f"{num:03d}.{ext}"))
        try:
            files = await self.ctx.downloader.download_many(targets, workspace, deadline)
        except (DirectDownloadUnavailable, FileTooLargeDirect):
            return None
        items, omissions = map_downloads(found, files)
        return FetchOutcome(
            items=items,
            omissions=omissions,
            expected_count=manifest.expected_count,
            count_verified=manifest.count_verified,
            skipped_media_warning=manifest.skipped_media_warning,
        )
