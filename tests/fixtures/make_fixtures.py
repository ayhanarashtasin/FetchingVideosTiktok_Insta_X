"""Generator for the SYNTHETIC extractor-output fixtures (T00.7, T03.12).

These files are hand-modelled on the field names and message shapes read from the installed
gallery_dl 1.32.21 / yt_dlp 2026.11.23 sources (see ops/feasibility.md section 3). They are NOT captures
of real platform responses: every record carries `_synthetic: true`, all ids/users are made up, and
they prove that OUR mapping handles the shapes we expect - not that upstream still emits them.
Only the live probe can establish the latter.

    uv run python tests/fixtures/make_fixtures.py            # (re)write tests/fixtures/extractor/*.json
    uv run pytest tests/contract                              # verifies the committed files are in sync

Message shapes (gallery_dl/extractor/message.py): [2, {dir}], [3, "url", {file}], [-1, {"error","message"}].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "extractor"

TT_ID = "7300000000000000000"
IG_CODE = "ABCdef12345"
STORY_ID = "3000000000000000001"
TW_ID = "1700000000000000000"


def gd(*messages: list[Any]) -> list[list[Any]]:
    return [list(m) for m in messages]


# ------------------------------------------------------------------------------------ TikTok
def tiktok_photo(n: int = 3, *, declared: int | None = None, with_audio: bool = True) -> list[list[Any]]:
    declared = n if declared is None else declared
    directory = {
        "_synthetic": True,
        "id": TT_ID,
        "user": "someuser",
        "post_type": "image",
        "desc": "synthetic photo post",
        "imagePost": {"images": [{"imageWidth": 1080, "imageHeight": 1920} for _ in range(declared)]},
    }
    msgs: list[list[Any]] = [[2, directory]]
    for i in range(1, n + 1):
        msgs.append(
            [
                3,
                f"https://p16.example.invalid/img{i}~tplv.jpeg",
                {
                    **directory,
                    "type": "image",
                    "num": i,
                    "width": 1080,
                    "height": 1920,
                    "extension": "jpeg",
                    "file_id": f"fid{i}",
                    "title": "synthetic photo post",
                },
            ]
        )
    if with_audio:  # what gallery-dl emits after the images unless audio=false (note num 0, type audio)
        msgs.append(
            [
                3,
                "https://sf16.example.invalid/music.mp3",
                {
                    **directory,
                    "type": "audio",
                    "num": 0,
                    "width": 0,
                    "height": 0,
                    "extension": "mp3",
                    "file_id": "music1",
                },
            ]
        )
    return msgs


def ytdlp_tiktok_video() -> dict[str, Any]:
    return {
        "_synthetic": True,
        "id": TT_ID,
        "extractor_key": "TikTok",
        "width": 720,
        "height": 1280,
        "duration": 12.5,
        "formats": [
            {
                "format_id": "download",
                "format_note": "Download video, watermarked",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 576,
                "height": 1024,
                "tbr": 700.0,
                "ext": "mp4",
            },
            {
                "format_id": "h264_540p_1",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 576,
                "height": 1024,
                "tbr": 900.0,
                "ext": "mp4",
            },
            {
                "format_id": "bytevc1_720p_1",
                "vcodec": "hevc",
                "acodec": "aac",
                "width": 720,
                "height": 1280,
                "tbr": 1100.0,
                "ext": "mp4",
            },
        ],
    }


# ---------------------------------------------------------------------------------- Instagram
def _ig_dir(count: int, **extra: Any) -> dict[str, Any]:
    return {
        "_synthetic": True,
        "post_id": "3000000000000000000",
        "post_shortcode": IG_CODE,
        "count": count,
        "username": "someuser",
        "owner_id": "42",
        "post_url": f"https://www.instagram.com/p/{IG_CODE}/",
        **extra,
    }


def _ig_file(directory: dict[str, Any], num: int, *, video: bool = False, **extra: Any) -> list[Any]:
    ext = "mp4" if video else "jpg"
    return [
        3,
        f"https://scontent.example.invalid/{num}.{ext}",
        {
            **directory,
            "num": num,
            "media_id": f"31000000000000000{num:02d}",
            "shortcode": f"{IG_CODE}{num}",
            "display_url": f"https://scontent.example.invalid/{num}.jpg",
            "video_url": f"https://scontent.example.invalid/{num}.mp4" if video else None,
            "width": 1080,
            "height": 1350,
            "extension": ext,
            **extra,
        },
    ]


def instagram_post(kinds: list[str], *, nums: list[int] | None = None, count: int | None = None) -> list[list[Any]]:
    nums = nums or list(range(1, len(kinds) + 1))
    directory = _ig_dir(len(kinds) if count is None else count)
    return [[2, directory], *[_ig_file(directory, n, video=(k == "video")) for n, k in zip(nums, kinds, strict=True)]]


def ytdlp_instagram_reel() -> dict[str, Any]:
    return {
        "_synthetic": True,
        "id": IG_CODE,
        "extractor_key": "Instagram",
        "width": 1080,
        "height": 1920,
        "duration": 8.0,
        "formats": [
            {
                "format_id": "dash-v-low",
                "vcodec": "avc1.4d401e",
                "acodec": "none",
                "width": 540,
                "height": 960,
                "tbr": 400.0,
                "ext": "mp4",
            },
            {
                "format_id": "dash-v-high",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "width": 1080,
                "height": 1920,
                "tbr": 2800.0,
                "ext": "mp4",
            },
            {"format_id": "dash-a", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 128.0, "ext": "m4a"},
        ],
    }


def user_info(*, is_private: Any, username: str = "storyuser") -> list[list[Any]]:
    profile: dict[str, Any] = {"_synthetic": True, "id": "99", "username": username, "full_name": "Synthetic"}
    if is_private != "ABSENT":
        profile["is_private"] = is_private
    return [[2, profile]]


def instagram_story(items: list[tuple[str, str]], *, audience: str | None = None) -> list[list[Any]]:
    """`items` = [(media_id, 'photo'|'video'), ...] as returned by gallery-dl for a story reel."""
    directory = {
        "_synthetic": True,
        "post_id": "99",
        "post_shortcode": "story99",
        "count": len(items),
        "username": "storyuser",
        "type": "story",
        "post_url": "https://www.instagram.com/stories/storyuser/",
        "user": {"username": "storyuser", "id": "99"},
    }
    msgs: list[list[Any]] = [[2, directory]]
    for num, (media_id, kind) in enumerate(items, 1):
        extra: dict[str, Any] = {"audience": audience} if audience else {}
        msgs.append(
            [
                3,
                f"https://scontent.example.invalid/story{num}.{'mp4' if kind == 'video' else 'jpg'}",
                {
                    **directory,
                    "num": num,
                    "media_id": media_id,
                    "shortcode": f"st{num}",
                    "display_url": f"https://scontent.example.invalid/story{num}.jpg",
                    "video_url": f"https://scontent.example.invalid/story{num}.mp4" if kind == "video" else None,
                    "width": 1080,
                    "height": 1920,
                    "extension": "mp4" if kind == "video" else "jpg",
                    **extra,
                },
            ]
        )
    return msgs


# ---------------------------------------------------------------------------------------- X
def twitter(kinds: list[str], *, tweet_id: str = TW_ID, foreign_at: int | None = None) -> list[list[Any]]:
    directory = {
        "_synthetic": True,
        "tweet_id": int(tweet_id),
        "retweet_id": 0,
        "quote_id": 0,
        "count": len(kinds),
        "user": {"name": "someuser"},
    }
    msgs: list[list[Any]] = [[2, directory]]
    for num, kind in enumerate(kinds, 1):
        video = kind in {"video", "animated_gif"}
        tid = int(tweet_id) + 1 if foreign_at == num else int(tweet_id)  # media of a different (quoted) tweet
        msgs.append(
            [
                3,
                f"https://pbs.example.invalid/{num}.{'mp4' if video else 'jpg'}",
                {
                    **directory,
                    "tweet_id": tid,
                    "num": num,
                    "type": kind,
                    "width": 1200,
                    "height": 800,
                    "extension": "mp4" if video else "jpg",
                    "source_id": 0,
                    **({"bitrate": 2176000, "duration": 6.0} if video else {}),
                },
            ]
        )
    return msgs


def ytdlp_twitter_video() -> dict[str, Any]:
    return {
        "_synthetic": True,
        "id": TW_ID,
        "extractor_key": "Twitter",
        "width": 1280,
        "height": 720,
        "duration": 6.0,
        "formats": [
            {
                "format_id": "http-832",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "width": 640,
                "height": 360,
                "tbr": 832.0,
                "ext": "mp4",
            },
            {
                "format_id": "http-2176",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "width": 1280,
                "height": 720,
                "tbr": 2176.0,
                "ext": "mp4",
            },
        ],
    }


# ------------------------------------------------------------------------------------ errors
def error(name: str, message: str) -> list[list[Any]]:
    return [[-1, {"error": name, "message": message}]]


FIXTURES: dict[str, Any] = {
    "gd_tiktok_photo_3_with_audio": tiktok_photo(3),
    "gd_tiktok_photo_declares_4_lists_3": tiktok_photo(3, declared=4, with_audio=False),
    "gd_tiktok_photo_1": tiktok_photo(1, with_audio=False),
    "ytdlp_tiktok_video": ytdlp_tiktok_video(),
    "gd_instagram_photo": instagram_post(["photo"]),
    "gd_instagram_video_post": instagram_post(["video"]),
    "gd_instagram_carousel_mixed": instagram_post(["photo", "video", "photo"]),
    "gd_instagram_carousel_item2_skipped": instagram_post(["photo", "photo"], nums=[1, 3], count=2),
    "gd_instagram_carousel_12": instagram_post(["photo"] * 12),
    "ytdlp_instagram_reel": ytdlp_instagram_reel(),
    "gd_user_info_public": user_info(is_private=False),
    "gd_user_info_private": user_info(is_private=True),
    "gd_user_info_unknown": user_info(is_private="ABSENT"),
    "gd_instagram_story_photo": instagram_story([(STORY_ID, "photo")]),
    "gd_instagram_story_video": instagram_story([(STORY_ID, "video")]),
    "gd_instagram_story_whole_reel": instagram_story(
        [("3000000000000000009", "photo"), (STORY_ID, "photo"), ("3000000000000000002", "video")]
    ),
    "gd_instagram_story_wrong_id": instagram_story([("3000000000000000777", "photo")]),
    "gd_instagram_story_close_friends": instagram_story([(STORY_ID, "photo")], audience="besties"),
    "gd_twitter_4_photos": twitter(["photo"] * 4),
    "gd_twitter_video": twitter(["video"]),
    "gd_twitter_gif": twitter(["animated_gif"]),
    "gd_twitter_mixed": twitter(["photo", "video", "photo"]),
    "gd_twitter_foreign_media": twitter(["photo", "photo"], foreign_at=2),
    "ytdlp_twitter_video": ytdlp_twitter_video(),
    "gd_error_auth_required": error("AuthRequired", "Account credentials needed to access this story"),
    "gd_error_not_found": error("NotFoundError", "Requested story could not be found"),
    "gd_empty": [],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in FIXTURES.items():
        (OUT / f"{name}.json").write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    print(f"wrote {len(FIXTURES)} fixtures to {OUT}")


if __name__ == "__main__":
    main()
