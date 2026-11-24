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
    FetchOutcome,
    build_gd_items,
    check_item_limit,
    fetch_via_gallerydl,
    gaps_in_numbering,
    manifest_items,
)

_STATUS = re.compile(r"/status/(?P<id>\d+)")
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
}


def tweet_id_of(url: str) -> str:
    m = _STATUS.search(url)
    if not m:
        raise BotError(ErrorCode.UNSUPPORTED_POST, "no status id in x url")
    return m.group("id")


class TwitterAdapter:
    platform = Platform.X

    def __init__(self, ctx: ExtractionContext, cache: DiscoveryCache | None = None) -> None:
        self.ctx = ctx
        self.cache = cache or DiscoveryCache()

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        tweet_id = tweet_id_of(request.extraction_url)
        dump = await self.ctx.gallerydl.discover(
            request.extraction_url,
            workspace,
            deadline,
            options=GD_OPTIONS,
            hint="tweet",
            session_supported=False,
            empty_means_no_media=True,
        )
        for meta in dump.files:
            # Identity: every attachment must belong to THIS tweet (not a quoted/retweeted one).
            if str(meta.get("tweet_id") or "") != tweet_id or meta.get("retweet_id"):
                raise BotError(ErrorCode.EXTRACTOR_CHANGED, "result contains media from a different post")
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
        self.cache.put(
            request.job_id,
            Discovered(
                manifest,
                "gallerydl",
                nums={i.num - 1: i.num for i in items},
                schemes={i.num - 1: i.scheme for i in items},
                hint="tweet",
            ),
        )
        return manifest

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        found = self.cache.take(request.job_id)
        if found is None:
            await self.discover(request, workspace, deadline)
            found = self.cache.take(request.job_id)
            assert found is not None
        tweet_id = found.manifest.post_id
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
            ytdlp_id_matches=lambda i: i == tweet_id or i.startswith(f"{tweet_id}_"),
        )
