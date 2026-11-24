"""T04.3 / T04.5 / T04.6 / T06.1: the delivery planner (AT11, AT14, AT16, AT17) - pure logic, no I/O."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset
from app.core.errors import ErrorCode
from app.media.delivery_plan import (
    CAPTION_LIMIT,
    Caps,
    SendAs,
    caption_for,
    chunk,
    decide,
    plan_delivery,
)

WORK = Path(__file__).resolve().parent / "work"  # absolute on every OS; never touched
URL = "https://www.instagram.com/p/ABCdef12345/?igsh=tracking"
CAPS = Caps(photo_bytes=9_500_000, file_bytes=49_000_000)
LOCAL_CAPS = Caps(photo_bytes=9_500_000, file_bytes=1_900_000_000)


def photo(pos, *, w=1080, h=1350, size=500_000, mime="image/jpeg"):
    return MediaAsset(
        source_item_id=f"p{pos}", position=pos, local_path=WORK / f"{pos}.jpg", kind=ItemKind.PHOTO,
        mime_type=mime, size_bytes=size, width=w, height=h, container="jpeg",
    )  # fmt: skip


def video(pos, *, vcodec="h264", acodec="aac", container="mp4", size=5_000_000, pix_fmt="yuv420p"):
    return MediaAsset(
        source_item_id=f"v{pos}", position=pos, local_path=WORK / f"{pos}.mp4", kind=ItemKind.VIDEO,
        mime_type="video/mp4", size_bytes=size, width=720, height=1280, duration_seconds=10.0, video_codec=vcodec,
        audio_codec=acodec, container=container, pix_fmt=pix_fmt,
    )  # fmt: skip


def sends(plan):
    return [[(i.asset.position, i.send_as.value) for i in s.items] for s in plan.steps]


# --- per-item decisions ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("asset", "expected", "why"),
    [
        (photo(0), SendAs.PHOTO, None),
        (photo(0, mime="image/png"), SendAs.PHOTO, None),
        (photo(0, mime="image/webp"), SendAs.PHOTO, None),
        (photo(0, mime="image/avif"), SendAs.DOCUMENT, "not accepted as a Telegram photo"),
        (photo(0, size=9_600_000), SendAs.DOCUMENT, "photo size limit"),
        (photo(0, w=6000, h=4001), SendAs.DOCUMENT, "dimensions exceed"),
        (photo(0, w=40, h=1000), SendAs.DOCUMENT, "aspect ratio"),  # 25:1
        (photo(0, w=1000, h=49), SendAs.DOCUMENT, "aspect ratio"),  # 20.4:1 exceeds the limit
        (photo(0, w=1000, h=50), SendAs.PHOTO, None),  # exactly 20:1 is still allowed
        (photo(0, w=5000, h=5000), SendAs.PHOTO, None),  # sum exactly 10000 is allowed
        (video(0), SendAs.VIDEO, None),
        (video(0, acodec=None), SendAs.VIDEO, None),  # silent video is fine
        (video(0, container="mov"), SendAs.VIDEO, None),
        (video(0, container="mkv"), SendAs.DOCUMENT, "not natively playable"),
        (video(0, container="webm", vcodec="vp9", acodec="opus"), SendAs.DOCUMENT, "not natively playable"),
        (video(0, vcodec="hevc"), SendAs.DOCUMENT, "hevc"),
        (video(0, vcodec="av1"), SendAs.DOCUMENT, "av1"),
        (video(0, pix_fmt="yuv444p"), SendAs.DOCUMENT, "not natively playable"),
    ],
)
def test_media_mode_native_or_document(asset, expected, why):
    item = decide(asset, DeliveryMode.MEDIA, CAPS)
    assert item.send_as is expected
    assert (why is None and item.reason is None) or why in item.reason


def test_hevc_goes_native_only_when_enabled():
    assert (
        decide(video(0, vcodec="hevc"), DeliveryMode.MEDIA, Caps(9_500_000, 49_000_000, native_hevc=True)).send_as
        is SendAs.VIDEO
    )


def test_original_mode_sends_everything_as_documents_unmodified():
    for a in (photo(0), photo(1, w=40, h=1000), video(2), video(3, container="mkv")):
        assert decide(a, DeliveryMode.ORIGINAL, CAPS).send_as is SendAs.DOCUMENT


# --- size limits: never shrink, report instead (AT17) ----------------------------------------------------------------
def test_oversized_files_are_rejected_not_reduced_on_the_hosted_profile():
    big_video, big_photo = video(0, size=60_000_000), photo(1, size=60_000_000)
    for asset in (big_video, big_photo):
        for mode in (DeliveryMode.MEDIA, DeliveryMode.ORIGINAL):
            r = decide(asset, mode, CAPS)
            assert r.code is ErrorCode.FILE_TOO_LARGE and r.position == asset.position


def test_the_local_profile_lifts_only_the_file_cap_not_the_photo_rules():
    assert decide(video(0, size=300_000_000), DeliveryMode.MEDIA, LOCAL_CAPS).send_as is SendAs.VIDEO
    assert (
        decide(photo(1, size=20_000_000), DeliveryMode.MEDIA, LOCAL_CAPS).send_as is SendAs.DOCUMENT
    )  # still > photo cap
    assert decide(video(2, size=2_100_000_000), DeliveryMode.ORIGINAL, LOCAL_CAPS).code is ErrorCode.FILE_TOO_LARGE


def test_boundary_sizes():
    assert decide(video(0, size=49_000_000), DeliveryMode.MEDIA, CAPS).send_as is SendAs.VIDEO
    assert decide(video(0, size=49_000_001), DeliveryMode.MEDIA, CAPS).code is ErrorCode.FILE_TOO_LARGE
    assert decide(photo(0, size=9_500_000), DeliveryMode.MEDIA, CAPS).send_as is SendAs.PHOTO
    assert decide(photo(0, size=9_500_001), DeliveryMode.MEDIA, CAPS).send_as is SendAs.DOCUMENT


# --- batching (AT11) -------------------------------------------------------------------------------------------------------
def test_eleven_photos_are_a_group_of_ten_then_a_single():  # the spec's own example
    plan = plan_delivery([photo(i) for i in range(11)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert [len(s.items) for s in plan.steps] == [10, 1]
    assert plan.steps[0].is_group and not plan.steps[1].is_group
    assert [p for s in plan.steps for p in s.positions] == list(range(11))


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, [1]),
        (2, [2]),
        (9, [9]),
        (10, [10]),
        (11, [10, 1]),
        (12, [10, 2]),
        (20, [10, 10]),
        (21, [10, 10, 1]),
        (30, [10, 10, 10]),
    ],
)
def test_batch_sizes_are_two_to_ten_with_a_lone_remainder_sent_alone(n, expected):
    plan = plan_delivery([photo(i) for i in range(n)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert [len(s.items) for s in plan.steps] == expected
    assert all(len(s.items) <= 10 for s in plan.steps)


def test_mixed_photo_and_video_albums_stay_together_in_order():
    plan = plan_delivery([photo(0), video(1), photo(2)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert sends(plan) == [[(0, "photo"), (1, "video"), (2, "photo")]]


def test_a_document_fallback_between_videos_splits_runs_without_moving_anything():
    # "deliver items 1-2, then item 3, then remaining media, in order"
    assets = [video(0), video(1), video(2, container="mkv"), video(3), video(4)]
    plan = plan_delivery(assets, mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert sends(plan) == [[(0, "video"), (1, "video")], [(2, "document")], [(3, "video"), (4, "video")]]


def test_documents_and_media_are_never_mixed_in_one_album():
    assets = [photo(0), photo(1, w=40, h=1000), photo(2), photo(3)]
    plan = plan_delivery(assets, mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert sends(plan) == [[(0, "photo")], [(1, "document")], [(2, "photo"), (3, "photo")]]
    for step in plan.steps:
        assert len({i.send_as is SendAs.DOCUMENT for i in step.items}) == 1


def test_all_original_files_form_document_groups_of_ten():
    plan = plan_delivery([photo(i) for i in range(12)], mode=DeliveryMode.ORIGINAL, caps=CAPS, url=URL)
    assert [len(s.items) for s in plan.steps] == [10, 2]
    assert all(i.send_as is SendAs.DOCUMENT for s in plan.steps for i in s.items)


def test_order_comes_from_source_position_not_input_order():
    plan = plan_delivery([photo(2), photo(0), photo(1)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert plan.steps[0].positions == [0, 1, 2]


def test_positions_with_gaps_keep_their_real_positions():
    plan = plan_delivery([photo(0), photo(3), photo(7)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert plan.steps[0].positions == [0, 3, 7]


def test_rejected_items_leave_the_rest_in_order_and_are_reported():
    plan = plan_delivery([photo(0), video(1, size=80_000_000), photo(2)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert sends(plan) == [[(0, "photo"), (2, "photo")]]
    assert [(r.position, r.code) for r in plan.rejected] == [(1, ErrorCode.FILE_TOO_LARGE)]


def test_nothing_deliverable_yields_an_empty_plan():
    plan = plan_delivery([video(0, size=80_000_000)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert plan.steps == () and plan.link_message is None and not plan.button_on_media and len(plan.rejected) == 1


# --- captions + source link + the Original-files button (FR-08, PRD section 4) ----------------------------------------------
def test_a_single_item_carries_the_caption_and_the_button_itself():
    plan = plan_delivery([photo(0)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert plan.steps[0].caption == f"Original link: {URL}"  # the EXACT submitted URL, tracking params and all
    assert plan.link_message is None and plan.button_on_media and not plan.button_on_link


def test_albums_get_a_separate_final_link_message_carrying_the_button():
    plan = plan_delivery([photo(0), photo(1), photo(2)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert plan.steps[0].caption == f"Original link: {URL}"  # first item of the batch still captioned
    assert plan.link_message == f"Original link: {URL}" and plan.button_on_link and not plan.button_on_media


def test_multi_step_plans_end_with_a_link_message_even_without_groups():
    plan = plan_delivery(
        [photo(0), photo(1, w=40, h=1000), video(2, container="mkv")], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL
    )
    assert len(plan.steps) == 2 and plan.link_message is not None and plan.button_on_link


def test_every_batch_of_a_long_album_has_a_caption_and_one_link_message_closes_it():
    plan = plan_delivery([photo(i) for i in range(21)], mode=DeliveryMode.MEDIA, caps=CAPS, url=URL)
    assert len(plan.steps) == 3 and all(s.caption for s in plan.steps)
    assert plan.link_message and plan.button_on_link


def test_original_mode_has_no_original_files_button():
    for assets in ([photo(0)], [photo(0), photo(1)]):
        plan = plan_delivery(assets, mode=DeliveryMode.ORIGINAL, caps=CAPS, url=URL)
        assert not plan.button_on_media and not plan.button_on_link


def test_a_long_url_is_never_truncated_a_separate_message_carries_it():
    long_url = "https://x.com/someuser/status/1700000000000?" + "a=1&" * 300
    assert len(f"Original link: {long_url}") > CAPTION_LIMIT and caption_for(long_url) is None
    plan = plan_delivery([photo(0)], mode=DeliveryMode.MEDIA, caps=CAPS, url=long_url)
    assert plan.steps[0].caption is None
    assert plan.link_message == f"Original link: {long_url}"  # full, untruncated
    assert plan.button_on_link and not plan.button_on_media


def test_caption_boundary():
    prefix = len("Original link: ")
    fits = "https://x.com/" + "a" * (CAPTION_LIMIT - prefix - len("https://x.com/"))
    assert len(f"Original link: {fits}") == CAPTION_LIMIT and caption_for(fits) is not None
    assert caption_for(fits + "a") is None


def test_chunk_never_reorders():
    items = [
        decide(a, DeliveryMode.MEDIA, CAPS)
        for a in [photo(i) if i % 3 else video(i, container="mkv") for i in range(25)]
    ]
    flat = [i.asset.position for batch in chunk(items) for i in batch]
    assert flat == list(range(25))
