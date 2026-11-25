"""T03.4-T03.10, T03.12, T06.2: contract tests for every content type P01-P09 and the abuse cases.

Adapters + backend + finalize run for real; only the external tools are replaced by `FakeToolRunner`
playing back SYNTHETIC outputs (tests/fixtures). Media files are real (FFmpeg-generated) and are really
probed, so ordering is verified by the actual dimensions of the files that arrive.

What this proves: our mapping/policy is right for the shapes we expect. What it does NOT prove: that the
platforms still return those shapes. That is the job of `scripts/feasibility_probe.py live` (not yet run).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.contracts import (
    Access,
    ExtractionRequest,
    ExtractionResult,
    ItemKind,
    PostManifest,
    PostType,
    Processing,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.url_validator import validate_url
from app.extractors.local import LocalExtractionBackend
from tests.conftest import requires_ffmpeg
from tests.fake_tools import FakeToolRunner, fixture

pytestmark = requires_ffmpeg

TT_VIDEO = "https://www.tiktok.com/@someuser/video/7300000000000000000"
TT_PHOTO = "https://www.tiktok.com/@someuser/photo/7300000000000000000"
IG_POST = "https://www.instagram.com/p/ABCdef12345/"
IG_REEL = "https://www.instagram.com/reel/ABCdef12345/"
STORY_ID = "3000000000000000001"
IG_STORY = f"https://www.instagram.com/stories/storyuser/{STORY_ID}/"
IG_INFO = "https://www.instagram.com/storyuser/info/"
X_URL = "https://x.com/someuser/status/1700000000000000000"


@dataclass
class World:
    backend: LocalExtractionBackend
    fake: FakeToolRunner
    media: object
    ws: Path
    cookies: Path

    def request(self, url: str) -> ExtractionRequest:
        v = validate_url(url)
        return ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=url,
            extraction_url=v.extraction_url,
            platform=v.platform,
            post_hint=v.post_hint,
        )

    async def discover(self, url: str) -> tuple[ExtractionRequest, PostManifest]:
        req = self.request(url)
        return req, await self.backend.discover(req, self.ws, Deadline(60))

    async def run(self, url: str) -> tuple[PostManifest, ExtractionResult]:
        req, manifest = await self.discover(url)
        return manifest, await self.backend.fetch(req, manifest, self.ws, Deadline(60))


@pytest.fixture
def world(tmp_path, media, make_settings):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    fake = FakeToolRunner()
    backend = LocalExtractionBackend(make_settings(instagram_cookies_file=cookies), runner=fake)
    ws = tmp_path / "ws"
    ws.mkdir()
    return World(backend, fake, media, ws, cookies)


def dims(result: ExtractionResult) -> list[tuple[int | None, int | None]]:
    return [(a.width, a.height) for a in result.assets]


# ===================================================================================== P01
async def test_p01_tiktok_video_picks_the_highest_resolution_source(world):
    world.fake.yt_info(TT_VIDEO, "ytdlp_tiktok_video")
    world.fake.yt_file(TT_VIDEO, world.media.mp4_h264_aac)
    manifest, result = await world.run(TT_VIDEO)
    assert (manifest.post_type, manifest.access, manifest.expected_count) == (
        PostType.VIDEO,
        Access.PERMITTED_PUBLIC,
        1,
    )
    assert len(manifest.items[0].candidate_formats) == 3
    (asset,) = result.assets
    assert asset.kind is ItemKind.VIDEO and asset.video_codec == "h264" and asset.container == "mp4"
    assert asset.source_format == "bytevc1_720p_1", "720x1280 beats 576x1024: never silently downscale"
    assert asset.audio_codec == "aac" and asset.processing is Processing.UNCHANGED
    assert result.completeness == "complete" and result.omissions == []
    (download,) = [c for c in world.fake.calls_to("yt_dlp") if "-f" in c]
    assert download[download.index("-f") + 1] == "bytevc1_720p_1"


async def test_p01_a_mismatched_post_id_is_refused(world):
    payload = {
        **fixture("ytdlp_tiktok_video"),
        "id": "7399999999999999999",
    }
    world.fake.yt_info(TT_VIDEO, payload)
    with pytest.raises(BotError) as exc:
        await world.discover(TT_VIDEO)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED


# ===================================================================================== P02
async def test_p02_tiktok_photo_post_returns_the_source_photos_in_order_without_the_audio_track(world):
    world.fake.gd_dump(TT_PHOTO, "gd_tiktok_photo_3_with_audio")
    m = world.media
    world.fake.gd_files(TT_PHOTO, {1: m.jpg, 2: m.png, 3: m.tall_jpg})
    manifest, result = await world.run(TT_PHOTO)
    assert manifest.post_type is PostType.CAROUSEL and [i.position for i in manifest.items] == [0, 1, 2]
    assert all(i.kind is ItemKind.PHOTO for i in manifest.items), "the audio item must be excluded"
    assert [a.position for a in result.assets] == [0, 1, 2]
    assert dims(result) == [(320, 240), (200, 100), (40, 1000)], "files must map to items by identity/order"
    assert result.expected_count == 3 and result.completeness == "complete" and result.omissions == []
    gd_args = world.fake.calls_to("gallery_dl")[0]
    assert "audio=false" in gd_args and "covers=false" in gd_args  # asked not to emit the audio at all


async def test_p02_a_single_tiktok_photo_is_a_photo_not_a_carousel(world):
    world.fake.gd_dump(TT_PHOTO, "gd_tiktok_photo_1")
    world.fake.gd_files(TT_PHOTO, {1: world.media.jpg})
    manifest, result = await world.run(TT_PHOTO)
    assert manifest.post_type is PostType.PHOTO and len(result.assets) == 1 and result.completeness == "complete"


async def test_p02_post_declaring_more_images_than_listed_is_partial_with_the_missing_position(world):
    world.fake.gd_dump(TT_PHOTO, "gd_tiktok_photo_declares_4_lists_3")
    m = world.media
    world.fake.gd_files(TT_PHOTO, {1: m.jpg, 2: m.png, 3: m.jpg})
    manifest, result = await world.run(TT_PHOTO)
    assert manifest.expected_count == 4 and len(manifest.items) == 3
    assert result.completeness == "partial"
    assert [(o.position, o.code) for o in result.omissions] == [(3, ErrorCode.EXTRACTOR_CHANGED)]


async def test_p02_unidentified_or_different_post_is_refused(world):
    payload = fixture("gd_tiktok_photo_1")
    payload[0][1]["id"] = "7388888888888888888"
    world.fake.gd_dump(TT_PHOTO, payload)
    with pytest.raises(BotError) as exc:
        await world.discover(TT_PHOTO)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED
    payload[0][1].pop("id")  # identity must be PROVEN, not assumed
    with pytest.raises(BotError):
        await world.discover(TT_PHOTO)


async def test_p02_an_audio_only_result_is_never_returned_as_media(world):
    audio_only = [
        fixture("gd_tiktok_photo_1")[0],
        [3, "https://x.invalid/a.mp3", {"id": "7300000000000000000", "type": "audio", "num": 0, "extension": "mp3"}],
    ]
    world.fake.gd_dump(TT_PHOTO, audio_only)
    with pytest.raises(BotError) as exc:
        await world.discover(TT_PHOTO)
    assert exc.value.reason == "no_media"


async def test_tiktok_video_url_that_is_really_a_photo_post_falls_back_to_gallerydl(world):
    world.fake.yt_fails(TT_VIDEO, "ERROR: [TikTok] 7300000000000000000: There is no video in this post")
    world.fake.gd_dump(TT_VIDEO, "gd_tiktok_photo_1")
    world.fake.gd_files(TT_VIDEO, {1: world.media.jpg})
    manifest, result = await world.run(TT_VIDEO)
    assert manifest.post_type is PostType.PHOTO and len(result.assets) == 1
    assert world.fake.urls_requested("yt_dlp") and world.fake.urls_requested("gallery_dl")


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("ERROR: [TikTok] 1: Video unavailable. This video is private", ErrorCode.ACCESS_RESTRICTED),
        ("ERROR: [TikTok] 1: HTTP Error 429: Too Many Requests", ErrorCode.RATE_LIMITED),
        ("ERROR: [TikTok] 1: Please complete the CAPTCHA", ErrorCode.RATE_LIMITED),
        ("ERROR: [TikTok] 1: Video not found", ErrorCode.NOT_FOUND_OR_EXPIRED),
    ],
)
async def test_no_fallback_extractor_is_tried_after_access_rate_or_challenge_errors(world, stderr, code):
    world.fake.yt_fails(TT_VIDEO, stderr)
    world.fake.gd_dump(TT_VIDEO, "gd_tiktok_photo_1")  # would succeed - and must NOT be consulted
    with pytest.raises(BotError) as exc:
        await world.discover(TT_VIDEO)
    assert exc.value.code is code
    assert world.fake.urls_requested("gallery_dl") == [], "a second extractor must not be used to get around a block"


# ===================================================================================== P03 / P04
async def test_p03_instagram_single_photo(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_photo")
    world.fake.gd_files(IG_POST, {1: world.media.jpg})
    manifest, result = await world.run(IG_POST)
    assert manifest.post_type is PostType.PHOTO and manifest.access is Access.PERMITTED_PUBLIC
    assert result.assets[0].kind is ItemKind.PHOTO and result.completeness == "complete"
    assert result.assets[0].sha256 and result.assets[0].size_bytes == world.media.jpg.stat().st_size


async def test_p04_instagram_reel_is_merged_from_the_best_video_and_audio_tracks(world):
    world.fake.yt_info(IG_REEL, "ytdlp_instagram_reel")
    world.fake.yt_file(IG_REEL, world.media.mp4_h264_aac)
    manifest, result = await world.run(IG_REEL)
    assert manifest.post_type is PostType.VIDEO and manifest.items[0].source_item_id == "ABCdef12345"
    (asset,) = result.assets
    assert asset.processing is Processing.MERGED and asset.source_format == "dash-v-high"
    (download,) = [c for c in world.fake.calls_to("yt_dlp") if "-f" in c]
    assert download[download.index("-f") + 1] == "dash-v-high+dash-a"
    assert (
        download[download.index("--merge-output-format") + 1] == "mp4/mkv"
    )  # container choice only, never a re-encode


async def test_p04_instagram_video_post_via_gallerydl(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_video_post")
    world.fake.gd_files(IG_POST, {1: world.media.mp4_h264_aac})
    manifest, result = await world.run(IG_POST)
    assert manifest.post_type is PostType.VIDEO
    assert result.assets[0].kind is ItemKind.VIDEO and result.assets[0].audio_codec == "aac"


async def test_p04_a_reel_that_resolves_to_a_photo_is_a_preview_stand_in_and_is_refused(world):
    world.fake.yt_fails(IG_REEL, "ERROR: Unsupported URL")
    world.fake.gd_dump(IG_REEL, "gd_instagram_photo")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_REEL)
    assert exc.value.reason == "preview_only"


async def test_reel_falls_back_to_gallerydl_when_ytdlp_cannot_handle_it(world):
    world.fake.yt_fails(IG_REEL, "ERROR: Unsupported URL")
    world.fake.gd_dump(IG_REEL, "gd_instagram_video_post")
    world.fake.gd_files(IG_REEL, {1: world.media.mp4_h264_aac})
    manifest, result = await world.run(IG_REEL)
    assert manifest.post_type is PostType.VIDEO and len(result.assets) == 1


# ===================================================================================== P05
async def test_p05_mixed_carousel_keeps_every_item_in_source_order_and_type(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    m = world.media
    world.fake.gd_files(IG_POST, {3: m.tall_jpg, 1: m.jpg, 2: m.mp4_h264_aac})  # "arrival" order scrambled on purpose
    manifest, result = await world.run(IG_POST)
    assert manifest.post_type is PostType.CAROUSEL
    assert [(i.position, i.kind) for i in manifest.items] == [
        (0, ItemKind.PHOTO),
        (1, ItemKind.VIDEO),
        (2, ItemKind.PHOTO),
    ]
    assert [(a.position, a.kind) for a in result.assets] == [
        (0, ItemKind.PHOTO),
        (1, ItemKind.VIDEO),
        (2, ItemKind.PHOTO),
    ]
    assert dims(result) == [(320, 240), (160, 120), (40, 1000)]  # item 2 really is the video, in place
    assert result.completeness == "complete"


async def test_p05_carousel_with_a_silently_skipped_item_is_partial_and_names_the_position(world):
    world.fake.gd_dump(
        IG_POST, "gd_instagram_carousel_item2_skipped", stderr="[instagram][warning] Missing media in post ABCdef12345"
    )
    world.fake.gd_files(IG_POST, {1: world.media.jpg, 3: world.media.png})
    manifest, result = await world.run(IG_POST)
    assert manifest.skipped_media_warning and manifest.expected_count == 3 and not manifest.count_verified
    assert [a.position for a in result.assets] == [0, 2]
    assert [o.position for o in result.omissions] == [1]  # "Item 2 could not be retrieved"
    assert result.completeness == "partial"


async def test_p05_a_numbering_gap_alone_is_enough_to_call_it_partial(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_item2_skipped")  # no warning at all
    world.fake.gd_files(IG_POST, {1: world.media.jpg, 3: world.media.png})
    _, result = await world.run(IG_POST)
    assert result.completeness == "partial" and [o.position for o in result.omissions] == [1]


async def test_p05_a_carousel_beyond_ten_items_is_preserved_whole(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_12")
    world.fake.gd_files(IG_POST, {n: world.media.jpg for n in range(1, 13)})
    manifest, result = await world.run(IG_POST)
    assert len(manifest.items) == 12 and [a.position for a in result.assets] == list(range(12))
    assert result.completeness == "complete"  # the planner (Phase 4) chunks it into 10 + 2


async def test_a_missing_downloaded_file_is_an_explicit_omission_never_silent(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    world.fake.gd_files(IG_POST, {1: world.media.jpg, 3: world.media.png})  # item 2 (the video) never arrives
    _, result = await world.run(IG_POST)
    assert [a.position for a in result.assets] == [0, 2]
    assert [(o.position, o.source_item_id) for o in result.omissions] == [(1, "3100000000000000002")]
    assert result.completeness == "partial"


async def test_a_file_that_is_not_what_the_item_claims_is_dropped_not_substituted(world, tmp_path):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    world.fake.gd_files(
        IG_POST, {1: world.media.jpg, 2: world.media.jpg, 3: world.media.png}
    )  # a PHOTO where a VIDEO belongs
    _, result = await world.run(IG_POST)
    assert [a.position for a in result.assets] == [0, 2]
    assert [(o.position, o.code) for o in result.omissions] == [(1, ErrorCode.MEDIA_INVALID)]


async def test_an_error_page_saved_as_an_image_is_rejected_by_validation(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    world.fake.gd_files(IG_POST, {1: world.media.broken_jpg, 2: world.media.mp4_h264_aac, 3: world.media.png})
    _, result = await world.run(IG_POST)
    assert [a.position for a in result.assets] == [1, 2] and result.omissions[0].code is ErrorCode.MEDIA_INVALID


async def test_every_file_invalid_means_the_job_fails_with_media_invalid(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_photo")
    world.fake.gd_files(IG_POST, {1: world.media.broken_jpg})
    with pytest.raises(BotError) as exc:
        await world.run(IG_POST)
    assert exc.value.code is ErrorCode.MEDIA_INVALID


async def test_item_limit_is_enforced_at_discovery_before_any_download(tmp_path, media, make_settings):
    fake = FakeToolRunner()
    backend = LocalExtractionBackend(make_settings(max_items_per_job=10), runner=fake)
    ws = tmp_path / "w"
    ws.mkdir()
    fake.gd_dump(IG_POST, "gd_instagram_carousel_12")
    w = World(backend, fake, media, ws, tmp_path / "c")
    with pytest.raises(BotError) as exc:
        await w.discover(IG_POST)
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED
    assert not [c for c in fake.calls_to("gallery_dl") if "--directory" in c], "nothing may be downloaded"


# ===================================================================================== P06 / P07 (Stories)
def story_world(world, *, info="gd_user_info_public", story="gd_instagram_story_photo", files=None):
    world.fake.gd_dump(IG_INFO, info)
    world.fake.gd_dump(IG_STORY, story)
    world.fake.gd_files(IG_STORY, files if files is not None else {1: world.media.jpg})


async def test_p06_public_photo_story_exact_id(world):
    story_world(world)
    manifest, result = await world.run(IG_STORY)
    assert (
        manifest.post_type is PostType.STORY
        and manifest.post_id == STORY_ID
        and manifest.access is Access.PERMITTED_PUBLIC
    )
    assert len(result.assets) == 1 and result.assets[0].kind is ItemKind.PHOTO and result.completeness == "complete"


async def test_p07_public_video_story(world):
    story_world(world, story="gd_instagram_story_video", files={1: world.media.mp4_h264_aac})
    manifest, result = await world.run(IG_STORY)
    assert result.assets[0].kind is ItemKind.VIDEO and manifest.items[0].source_item_id == STORY_ID


async def test_story_visibility_is_checked_before_the_story_itself_is_requested(world):
    story_world(world)
    await world.discover(IG_STORY)
    urls = world.fake.urls_requested("gallery_dl")
    assert urls == [IG_INFO, IG_STORY], "profile visibility first, then exactly the requested story"


@pytest.mark.parametrize(
    ("info", "reason"),
    [("gd_user_info_private", "private_account"), ("gd_user_info_unknown", "visibility_unknown")],
)
async def test_story_of_a_private_or_unverifiable_account_is_refused_and_never_requested(world, info, reason):
    story_world(world, info=info)
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.ACCESS_RESTRICTED and exc.value.reason == reason
    assert IG_STORY not in world.fake.urls_requested(), "fail closed: the Story itself must never be fetched"


async def test_a_visibility_value_that_is_not_exactly_false_is_not_public(world):
    for weird in (None, 0, "false", "", [], {}):
        world.fake.gd_dump(IG_INFO, [[2, {"_synthetic": True, "username": "storyuser", "is_private": weird}]])
        world.fake.gd_dump(IG_STORY, "gd_instagram_story_photo")
        with pytest.raises(BotError) as exc:
            await world.discover(IG_STORY)
        assert exc.value.code is ErrorCode.ACCESS_RESTRICTED, weird


async def test_profile_lookup_for_a_different_account_is_refused(world):
    world.fake.gd_dump(IG_INFO, [[2, {"_synthetic": True, "username": "someoneelse", "is_private": False}]])
    world.fake.gd_dump(IG_STORY, "gd_instagram_story_photo")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED


async def test_story_selection_is_exact_when_the_whole_reel_comes_back(world):
    story_world(world, story="gd_instagram_story_whole_reel")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED  # three stories returned: never guess which was meant


async def test_story_with_a_different_id_is_treated_as_expired_or_unavailable(world):
    story_world(world, story="gd_instagram_story_wrong_id")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.NOT_FOUND_OR_EXPIRED


async def test_close_friends_story_is_never_delivered(world):
    story_world(world, story="gd_instagram_story_close_friends")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.ACCESS_RESTRICTED and exc.value.reason == "restricted_audience"


async def test_highlight_shaped_result_is_rejected(world):
    payload = fixture("gd_instagram_story_photo")
    payload[0][1]["type"] = "highlight"
    story_world(world, story=payload)
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST


async def test_expired_story_is_reported_as_unavailable(world):
    world.fake.gd_dump(IG_INFO, "gd_user_info_public")
    world.fake.gd_dump(IG_STORY, "gd_error_not_found", rc=4)
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.NOT_FOUND_OR_EXPIRED


async def test_story_without_a_configured_session_asks_the_owner_to_configure_one(world):
    world.fake.gd_dump(IG_INFO, "gd_error_auth_required", rc=16)
    with pytest.raises(BotError) as exc:
        await world.discover(IG_STORY)
    assert exc.value.code is ErrorCode.AUTH_REQUIRED and exc.value.reason == "story"
    assert not exc.value.fallback_allowed


def assert_private_session_copy(given: Path, world: World) -> None:
    assert given != world.cookies and given.is_relative_to(world.ws)
    assert given.read_text() == world.cookies.read_text()


async def test_session_cookies_are_used_for_stories_only(world):
    story_world(world)
    await world.discover(IG_STORY)
    for call in world.fake.calls_to("gallery_dl"):
        assert "--cookies" in call
        # The tools only ever see a PRIVATE COPY inside the job workspace (they write cookies back on exit);
        # the operator's file is never passed, and gallery-dl is told not to export the session anywhere.
        assert_private_session_copy(Path(call[call.index("--cookies") + 1]), world)
        assert call[call.index("-o", call.index("--cookies")) + 1] == "cookies-update=false"
    world.fake.calls.clear()
    world.fake.gd_dump(IG_POST, "gd_instagram_photo")
    world.fake.yt_info(IG_REEL, "ytdlp_instagram_reel")
    await world.discover(IG_POST)
    await world.discover(IG_REEL)
    assert world.fake.calls and all("--cookies" not in c for c in world.fake.calls), "posts/reels never use the session"


async def test_posts_that_are_not_public_are_refused(world):
    world.fake.gd_dump(IG_POST, "gd_instagram_photo", stderr="[instagram][warning] someone's posts are private")
    with pytest.raises(BotError) as exc:
        await world.discover(IG_POST)
    assert exc.value.code is ErrorCode.ACCESS_RESTRICTED


async def test_subscriber_only_post_items_are_refused(world):
    payload = fixture("gd_instagram_photo")
    payload[1][2]["subscription"] = True
    world.fake.gd_dump(IG_POST, payload)
    with pytest.raises(BotError) as exc:
        await world.discover(IG_POST)
    assert exc.value.code is ErrorCode.ACCESS_RESTRICTED


# ===================================================================================== P08 / P09 (X)
async def test_p08_x_multi_photo_in_source_order(world):
    world.fake.gd_dump(X_URL, "gd_twitter_4_photos")
    m = world.media
    world.fake.gd_files(X_URL, {1: m.jpg, 2: m.png, 3: m.tall_jpg, 4: m.jpg})
    manifest, result = await world.run(X_URL)
    assert manifest.post_type is PostType.CAROUSEL and manifest.expected_count == 4
    assert dims(result) == [(320, 240), (200, 100), (40, 1000), (320, 240)] and result.completeness == "complete"
    args = world.fake.calls_to("gallery_dl")[0]
    for opt in (
        "quoted=false",
        "retweets=false",
        "cards=false",
        "text-tweets=false",
        "articles=false",
        "ratelimit=abort",
    ):
        assert opt in args  # only the post's own attachments


async def test_p09_x_video_and_gif_are_videos(world):
    for fixture_name in ("gd_twitter_video", "gd_twitter_gif"):
        world.fake.gd_dump(X_URL, fixture_name)
        world.fake.gd_files(X_URL, {1: world.media.mp4_h264_aac})
        manifest, result = await world.run(X_URL)
        assert manifest.post_type is PostType.VIDEO and result.assets[0].kind is ItemKind.VIDEO


async def test_p09_x_mixed_media_keeps_positions(world):
    world.fake.gd_dump(X_URL, "gd_twitter_mixed")
    m = world.media
    world.fake.gd_files(X_URL, {1: m.jpg, 2: m.mp4_h264_aac, 3: m.png})
    _, result = await world.run(X_URL)
    assert [a.kind for a in result.assets] == [ItemKind.PHOTO, ItemKind.VIDEO, ItemKind.PHOTO]


async def test_x_media_belonging_to_a_quoted_post_is_rejected(world):
    world.fake.gd_dump(X_URL, "gd_twitter_foreign_media")
    with pytest.raises(BotError) as exc:
        await world.discover(X_URL)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED


async def test_x_retweet_media_is_rejected(world):
    payload = fixture("gd_twitter_video")
    payload[1][2]["retweet_id"] = 1699999999999999999
    world.fake.gd_dump(X_URL, payload)
    with pytest.raises(BotError):
        await world.discover(X_URL)


async def test_x_text_only_post_has_no_media(world):
    world.fake.gd_dump(X_URL, "gd_empty", stderr="[twitter][info] Requesting guest token\n")
    with pytest.raises(BotError) as exc:
        await world.discover(X_URL)
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST and exc.value.reason == "no_media"


@pytest.mark.parametrize(
    ("error", "rc", "code"),
    [
        ("AuthRequired", 16, ErrorCode.ACCESS_RESTRICTED),
        ("ChallengeError", 8, ErrorCode.RATE_LIMITED),
        ("NotFoundError", 4, ErrorCode.NOT_FOUND_OR_EXPIRED),
    ],
)
async def test_x_access_problems_are_reported_and_no_second_extractor_is_consulted(world, error, rc, code):
    world.fake.gd_dump(X_URL, [[-1, {"error": error, "message": "synthetic"}]], rc=rc)
    world.fake.yt_info(X_URL, "ytdlp_twitter_video")  # must stay untouched
    with pytest.raises(BotError) as exc:
        await world.discover(X_URL)
    assert exc.value.code is code and world.fake.urls_requested("yt_dlp") == []


async def test_x_single_video_gets_one_identity_checked_ytdlp_retry(world):
    world.fake.gd_dump(X_URL, "gd_twitter_video")
    world.fake.gd_files(X_URL, {})  # gallery-dl could not deliver the file
    # Real-world Twitter amplify videos: entry.id is amplify media ID, entry.display_id is tweet ID
    amplify_entry = {
        **fixture("ytdlp_twitter_video"),
        "id": "1964428072200900608",
        "display_id": "1700000000000000000",
    }
    world.fake.yt_info(X_URL, amplify_entry)
    world.fake.yt_file(X_URL, world.media.mp4_h264_aac)
    manifest, result = await world.run(X_URL)
    assert len(result.assets) == 1 and result.omissions == [] and result.assets[0].source_format == "http-2176"


async def test_the_ytdlp_retry_is_refused_if_it_returns_a_different_tweet(world):
    world.fake.gd_dump(X_URL, "gd_twitter_video")
    world.fake.gd_files(X_URL, {})
    wrong = {
        **fixture("ytdlp_twitter_video"),
        "id": "1111111111111111111",
    }
    world.fake.yt_info(X_URL, wrong)
    world.fake.yt_file(X_URL, world.media.mp4_h264_aac)
    with pytest.raises(BotError) as exc:  # the only item is missing and the substitute was refused
        await world.run(X_URL)
    assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED
    downloads = [c for c in world.fake.calls_to("yt_dlp") if "-f" in c]
    assert downloads == [], "a different tweet's video must never even be downloaded"


async def test_multi_item_posts_never_use_the_ytdlp_guesswork_fallback(world):
    world.fake.gd_dump(X_URL, "gd_twitter_mixed")
    world.fake.gd_files(X_URL, {1: world.media.jpg, 3: world.media.png})  # the video (2) failed
    world.fake.yt_info(X_URL, "ytdlp_twitter_video")
    world.fake.yt_file(X_URL, world.media.mp4_h264_aac)
    _, result = await world.run(X_URL)
    assert world.fake.urls_requested("yt_dlp") == [], (
        "matching yt-dlp entries to gallery-dl items by guesswork is not allowed"
    )
    assert [o.position for o in result.omissions] == [1] and result.completeness == "partial"


async def test_x_uses_no_session_cookies(world):
    world.fake.gd_dump(X_URL, "gd_twitter_video")
    await world.discover(X_URL)
    assert all("--cookies" not in c for c in world.fake.calls)


# ===================================================================================== cross-cutting safety
async def test_all_tool_invocations_follow_the_safety_contract(world):
    story_world(world)
    world.fake.gd_dump(TT_PHOTO, "gd_tiktok_photo_1")
    world.fake.gd_files(TT_PHOTO, {1: world.media.jpg})
    world.fake.yt_info(TT_VIDEO, "ytdlp_tiktok_video")
    world.fake.yt_file(TT_VIDEO, world.media.mp4_h264_aac)
    for url in (IG_STORY, TT_PHOTO, TT_VIDEO):
        await world.run(url)
    assert world.fake.calls
    for argv in world.fake.calls:
        if "gallery_dl" not in argv and "yt_dlp" not in argv:
            continue
        assert argv[-2] == "--" and argv[-1].startswith("https://"), "the URL is data, never an option"
        assert ("--config-ignore" in argv) != ("--ignore-config" in argv), "no user/system config may be read"
        if "yt_dlp" in argv:
            allowed = argv[argv.index("--use-extractors") + 1]
            assert "generic" not in allowed.split(","), "yt-dlp's generic extractor must stay off"
            assert "--no-playlist" in argv
    for env in world.fake.envs:
        assert "BOT_TOKEN" not in env and "WORKER_SECRET" not in env
        assert str(world.ws) in env["HOME"], "tool HOME is sandboxed inside the job workspace"


async def test_result_contract_is_uniform_across_platforms(world):
    cases = {
        TT_PHOTO: ("gd_tiktok_photo_3_with_audio", {1: world.media.jpg, 2: world.media.png, 3: world.media.jpg}),
        IG_POST: ("gd_instagram_carousel_mixed", {1: world.media.jpg, 2: world.media.mp4_h264_aac, 3: world.media.png}),
        X_URL: ("gd_twitter_mixed", {1: world.media.jpg, 2: world.media.mp4_h264_aac, 3: world.media.png}),
    }
    for url, (name, files) in cases.items():
        world.fake.gd_dump(url, name)
        world.fake.gd_files(url, files)
        _, result = await world.run(url)
        positions = [a.position for a in result.assets]
        assert positions == sorted(set(positions))
        for a in result.assets:
            assert a.local_path.is_absolute() and a.local_path.is_relative_to(world.ws.resolve())
            assert a.mime_type.startswith(("image/", "video/")) and a.size_bytes > 0 and a.width and a.height
            assert (a.kind is ItemKind.VIDEO) == (a.duration_seconds is not None)


async def test_result_is_unverified_when_the_source_cannot_prove_completeness(tmp_path, world):
    from app.extractors.base import FetchedItem, FetchOutcome
    from app.extractors.finalize import finalize_outcome

    (tmp_path / "a.jpg").write_bytes(world.media.jpg.read_bytes())
    outcome = FetchOutcome(items=[FetchedItem("a", 0, ItemKind.PHOTO, tmp_path / "a.jpg")], expected_count=None)
    manifest = PostManifest(
        platform="x", post_id="1", post_type=PostType.PHOTO, access=Access.PERMITTED_PUBLIC, items=[]
    )
    result = await finalize_outcome(outcome, manifest, tmp_path, max_total_bytes=10**9, max_items=30)
    assert result.completeness == "unverified"
    outcome.expected_count, outcome.count_verified = 1, True
    assert (
        await finalize_outcome(outcome, manifest, tmp_path, max_total_bytes=10**9, max_items=30)
    ).completeness == "complete"


async def test_tiktok_skips_bytevc2_and_transcodes_hevc_in_media_mode_only(world):
    from app.core.contracts import DeliveryMode

    payload = fixture("ytdlp_tiktok_video")
    payload = {
        **payload,
        "formats": [
            {
                "format_id": "bytevc2_1080p_1",
                "ext": "mp4",
                "vcodec": "bytevc2",
                "acodec": "aac",
                "width": 1080,
                "height": 1920,
                "tbr": 3000.0,
                "format_note": "Playback video (UNPLAYABLE)",
            },
            *payload["formats"],
        ],
    }
    world.fake.yt_info(TT_VIDEO, payload)
    world.fake.yt_file(TT_VIDEO, world.media.mp4_hevc_aac)
    manifest, result = await world.run(TT_VIDEO)
    assert manifest.post_type is PostType.VIDEO
    (asset,) = result.assets
    assert asset.source_format == "bytevc1_720p_1"
    assert asset.processing is Processing.TRANSCODED
    assert (asset.container, asset.video_codec, asset.audio_codec, asset.pix_fmt) == ("mp4", "h264", "aac", "yuv420p")

    # Original mode preserves raw HEVC bytes without transcoding.
    orig_req = world.request(TT_VIDEO).model_copy(update={"delivery_mode": DeliveryMode.ORIGINAL})
    orig_manifest = await world.backend.discover(orig_req, world.ws, Deadline(60))
    orig_res = await world.backend.fetch(orig_req, orig_manifest, world.ws, Deadline(60))
    assert orig_res.assets[0].video_codec == "hevc" and orig_res.assets[0].processing is Processing.UNCHANGED


async def test_instagram_non_native_reel_and_video_post_transcode_to_inline_mp4(world):
    # Reel via yt-dlp returning non-native VP9/Opus WebM
    world.fake.yt_info(IG_REEL, "ytdlp_instagram_reel")
    world.fake.yt_file(IG_REEL, world.media.webm_vp9_opus)
    _, reel_res = await world.run(IG_REEL)
    (reel_asset,) = reel_res.assets
    assert reel_asset.processing is Processing.TRANSCODED
    assert (reel_asset.container, reel_asset.video_codec, reel_asset.audio_codec, reel_asset.pix_fmt) == (
        "mp4",
        "h264",
        "aac",
        "yuv420p",
    )

    # /p/ video post via gallery-dl returning HEVC MP4
    world.fake.gd_dump(IG_POST, "gd_instagram_video_post")
    world.fake.gd_files(IG_POST, {1: world.media.mp4_hevc_aac})
    _, post_res = await world.run(IG_POST)
    (post_asset,) = post_res.assets
    assert post_asset.processing is Processing.TRANSCODED
    assert (post_asset.container, post_asset.video_codec, post_asset.audio_codec, post_asset.pix_fmt) == (
        "mp4",
        "h264",
        "aac",
        "yuv420p",
    )
