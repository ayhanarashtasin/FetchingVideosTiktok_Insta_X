"""Instagram adapter (T03.6 photo/Reel/video, T03.7 carousels, T03.8 individual public Stories).

Policy that matters:

* Posts, Reels and carousels use NO session: whatever is served to an anonymous client is public by
  construction. If Instagram demands a login, the result is a typed AUTH_REQUIRED, not a retry with
  credentials.
* Stories need an ordinary owner-authorised session (mounted read-only, worker only). Because a
  session can also see non-public content, a Story is fetched only after PROVING it is public:
    1. the account is public: `/info` must report `is_private` as exactly False (missing = unknown =
       refuse; True = refuse);
    2. gallery-dl returned exactly ONE item and its id equals the Story id in the link (it fetches the
       whole reel internally; we do not trust it to have filtered);
    3. the item carries no Close-Friends / subscriber audience marker.
  Highlights, Story trays and other users' Stories are never requested.
* Carousels keep source order by gallery-dl's own item numbers; a silently skipped item (a gap in the
  numbering or a `Missing media` warning) makes the result `partial` instead of complete.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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
    PHOTO_EXT,
    Discovered,
    DiscoveryCache,
    ExtractionContext,
    FetchedItem,
    FetchOutcome,
    build_gd_items,
    check_item_limit,
    fetch_via_gallerydl,
    gaps_in_numbering,
    manifest_items,
)
from app.media.quality import select_format

_POST_CODE = re.compile(r"/(?:p|reel|reels|tv)/(?P<code>[A-Za-z0-9_\-]+)/?$")
_STORY = re.compile(r"/stories/(?P<user>[A-Za-z0-9._]+)/(?P<id>\d+)/?$")
GD_OPTIONS = {"audio": "false", "covers": "false", "previews": "false"}
_FALLBACK_CODES = frozenset({ErrorCode.EXTRACTOR_CHANGED, ErrorCode.UNSUPPORTED_POST})
_PUBLIC_AUDIENCES = frozenset({"", "public", "default", "everyone"})


def instagram_kind(meta: Any) -> ItemKind | None:
    """Photo/video from a gallery-dl Instagram file record. `video_url` is the authoritative video flag."""
    if meta.get("audio_url") and not meta.get("video_url") and not meta.get("display_url"):
        return None
    if meta.get("video_url"):
        return ItemKind.VIDEO
    if str(meta.get("extension") or "").lower() in PHOTO_EXT:
        return ItemKind.PHOTO
    return None


def restricted_marker(meta: Any) -> bool:
    """Close-Friends / subscriber content must never be delivered, whatever our session can see."""
    audience = str(meta.get("audience") or "").lower()
    return audience not in _PUBLIC_AUDIENCES or bool(meta.get("subscription"))


class InstagramAdapter:
    platform = Platform.INSTAGRAM

    def __init__(self, ctx: ExtractionContext, cache: DiscoveryCache | None = None) -> None:
        self.ctx = ctx
        self.cache = cache or DiscoveryCache()

    # ------------------------------------------------------------------ discover
    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        hint = request.post_hint
        if hint == "story":
            found = await self._discover_story(request, workspace, deadline)
        else:
            m = _POST_CODE.search(request.extraction_url)
            if not m:
                raise BotError(ErrorCode.UNSUPPORTED_POST, "not an instagram post url")
            code = m.group("code")
            if hint == "reel":
                try:
                    found = await self._discover_reel(request, code, workspace, deadline)
                except BotError as err:
                    if not (err.fallback_allowed and err.code in _FALLBACK_CODES):
                        raise
                    found = await self._discover_post(request, code, workspace, deadline, expect_video=True)
            else:
                found = await self._discover_post(request, code, workspace, deadline, expect_video=False)
        self.cache.put(request.job_id, found)
        return found.manifest

    async def _discover_reel(
        self, request: ExtractionRequest, code: str, workspace: Path, deadline: Deadline
    ) -> Discovered:
        info = await self.ctx.ytdlp.discover(
            request.extraction_url, workspace, deadline, hint="reel", session_supported=True
        )
        if len(info.entries) != 1 or info.entries[0].id != code:
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "yt-dlp did not return exactly the requested reel")
        entry = info.entries[0]
        choice = select_format(entry.formats)
        manifest = PostManifest(
            platform=Platform.INSTAGRAM,
            post_id=code,
            post_type=PostType.VIDEO,
            access=Access.PERMITTED_PUBLIC,
            expected_count=1,
            count_verified=True,
            items=[
                ItemDescriptor(
                    source_item_id=code,
                    position=0,
                    kind=ItemKind.VIDEO,
                    width=choice.video.width if choice.video else entry.width,
                    height=choice.video.height if choice.video else entry.height,
                    candidate_formats=entry.formats,
                )
            ],
        )
        return Discovered(manifest, "ytdlp", nums={0: 1}, hint="reel")

    async def _discover_post(
        self, request: ExtractionRequest, code: str, workspace: Path, deadline: Deadline, *, expect_video: bool
    ) -> Discovered:
        dump = await self.ctx.gallerydl.discover(
            request.extraction_url, workspace, deadline, options=GD_OPTIONS, hint="post", session_supported=True
        )
        directory = dump.directories[0] if dump.directories else {}
        if str(directory.get("post_shortcode") or "") != code:
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "gallery-dl returned a different or unidentified post")
        if dump.warnings.private_hint or any(restricted_marker(f) for f in dump.files):
            raise BotError(ErrorCode.ACCESS_RESTRICTED, "post is not public", reason="not_public")
        items = build_gd_items(
            dump.files, identity=lambda m, i: str(m.get("media_id") or f"{code}:{i + 1}"), kind_of=instagram_kind
        )
        if not items:
            raise BotError(ErrorCode.UNSUPPORTED_POST, "post has no photos or videos", reason="no_media")
        if expect_video and not any(i.kind is ItemKind.VIDEO for i in items):
            # A /reel/ link must yield a video; a photo here is a preview image standing in for it.
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "reel resolved to a photo only", reason="preview_only")
        check_item_limit(len(items), self.ctx.max_items)
        nums = [i.num for i in items]
        gaps = gaps_in_numbering(nums)
        declared = directory.get("count")
        warned = dump.warnings.skipped_media
        manifest = PostManifest(
            platform=Platform.INSTAGRAM,
            post_id=code,
            post_type=(
                PostType.CAROUSEL
                if len(items) > 1
                else (PostType.VIDEO if items[0].kind is ItemKind.VIDEO else PostType.PHOTO)
            ),
            access=Access.PERMITTED_PUBLIC,  # fetched anonymously (no session was supplied)
            expected_count=max(max(nums), declared if isinstance(declared, int) else 0),
            # gallery-dl's `count` is computed AFTER it drops items, so it proves nothing by itself;
            # completeness rests on "no numbering gap and no skip warning".
            count_verified=not gaps and not warned,
            skipped_media_warning=warned,
            items=manifest_items(items),
        )
        return Discovered(
            manifest,
            "gallerydl",
            nums={i.num - 1: i.num for i in items},
            schemes={i.num - 1: i.scheme for i in items},
            hint="post",
        )

    async def _discover_story(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> Discovered:
        m = _STORY.search(request.extraction_url)
        if not m:
            raise BotError(ErrorCode.UNSUPPORTED_POST, "not an individual story url", reason="story_id_required")
        user, story_id = m.group("user"), m.group("id")

        # 1. Prove the account is public BEFORE looking at any of its Stories.
        info = await self.ctx.gallerydl.discover(
            f"https://www.instagram.com/{user}/info/",
            workspace,
            deadline,
            options={},
            use_cookies=True,
            hint="story",
            session_supported=True,
            allow_empty=True,
        )
        profile = info.directories[0] if info.directories else {}
        if str(profile.get("username") or "").lower() != user.lower():
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "profile lookup returned a different account")
        is_private = profile.get("is_private")
        if is_private is True:
            raise BotError(ErrorCode.ACCESS_RESTRICTED, "account is private", reason="private_account")
        if is_private is not False:
            raise BotError(
                ErrorCode.ACCESS_RESTRICTED, "cannot verify the account is public", reason="visibility_unknown"
            )

        # 2. Fetch metadata for exactly the requested Story and check identity ourselves.
        dump = await self.ctx.gallerydl.discover(
            request.extraction_url,
            workspace,
            deadline,
            options=GD_OPTIONS,
            use_cookies=True,
            hint="story",
            session_supported=True,
        )
        directory = dump.directories[0] if dump.directories else {}
        if str(directory.get("type") or "") != "story":
            raise BotError(
                ErrorCode.UNSUPPORTED_POST,
                "not a story (highlights are not supported)",
                reason="highlights_unsupported",
            )
        items = build_gd_items(
            dump.files, identity=lambda meta, i: str(meta.get("media_id") or ""), kind_of=instagram_kind
        )
        if len(items) != 1:
            # gallery-dl fetches the user's whole reel; if it did not narrow it to our Story we must
            # not guess which item was meant.
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "story lookup did not return exactly one item")
        item = items[0]
        if item.source_item_id != story_id:
            raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "requested story id is not in the returned reel")
        if any(restricted_marker(f) for f in dump.files) or dump.warnings.private_hint:
            raise BotError(ErrorCode.ACCESS_RESTRICTED, "story audience is not public", reason="restricted_audience")

        manifest = PostManifest(
            platform=Platform.INSTAGRAM,
            post_id=story_id,
            post_type=PostType.STORY,
            access=Access.PERMITTED_PUBLIC,  # verified above: public account, public audience, exact id
            expected_count=1,
            count_verified=True,
            items=manifest_items(items),
        )
        return Discovered(
            manifest,
            "gallerydl",
            nums={item.num - 1: item.num},
            schemes={item.num - 1: item.scheme},
            use_cookies=True,
            hint="story",
        )

    # ------------------------------------------------------------------ fetch
    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        found = self.cache.take(request.job_id)
        if found is None:
            await self.discover(request, workspace, deadline)
            found = self.cache.take(request.job_id)
            assert found is not None
        if found.source == "ytdlp":
            return await self._fetch_ytdlp(request, found, workspace, deadline)
        return await self._fetch_gallerydl(request, found, workspace, deadline)

    async def _fetch_ytdlp(
        self, request: ExtractionRequest, found: Discovered, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        item = found.manifest.items[0]
        choice = select_format(item.candidate_formats)
        paths = await self.ctx.ytdlp.download(
            request.extraction_url, workspace, deadline, selector=choice.selector, hint="reel", session_supported=True
        )
        fetched = FetchedItem(
            item.source_item_id,
            0,
            ItemKind.VIDEO,
            paths[0],
            source_format=choice.video.format_id if choice.video else None,
            processing=Processing.MERGED if choice.merge else Processing.UNCHANGED,
        )
        return FetchOutcome(items=[fetched], expected_count=1, count_verified=True)

    async def _fetch_gallerydl(
        self, request: ExtractionRequest, found: Discovered, workspace: Path, deadline: Deadline
    ) -> FetchOutcome:
        match = _POST_CODE.search(request.extraction_url)
        shortcode = match.group("code") if match else None
        return await fetch_via_gallerydl(
            self.ctx,
            found,
            request.extraction_url,
            workspace,
            deadline,
            options=GD_OPTIONS,
            use_cookies=found.use_cookies,
            hint=found.hint or "post",
            session_supported=True,
            # Stories have no yt-dlp fallback: its story extractor returns the whole reel, so matching
            # the right entry would be guesswork.
            ytdlp_id_matches=None if (found.hint == "story" or shortcode is None) else (lambda i: i == shortcode),
        )
