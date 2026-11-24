"""T02.2 / T02.3 / T02.7: authorization before anything else, command handling, URL handler.

Uses the REAL dispatcher, middleware, queue and DB with a recording fake Telegram session, so
"zero network extraction for unauthorized requests" is asserted on the actual wiring (AT12, AT22, AT25).
"""

from __future__ import annotations

import pytest
from aiogram.types import MessageEntity

from app.bot import messages
from app.core.contracts import DeliveryMode, JobState
from tests.fakes import OWNER, FakeExtractor, callback_update, message_update
from tests.harness import build_app

GOOD = "https://x.com/someuser/status/1700000000000"


@pytest.fixture
async def app(tmp_path):
    a = await build_app(tmp_path, start_workers=False, allowed_user_ids=[OWNER, 222])
    yield a
    await a.shutdown()


# --- authorization (AT12) ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "update_kwargs",
    [
        dict(user_id=999),  # not on the allowlist
        dict(user_id=OWNER, chat_id=-100123, chat_type="supergroup"),  # allowed user, but in a group
        dict(user_id=OWNER, chat_id=-555, chat_type="group"),
        dict(user_id=OWNER, chat_id=777, chat_type="private"),  # claims private but chat != user id
        dict(user_id=OWNER, is_bot=True),
    ],
)
async def test_unauthorized_messages_cause_zero_work(app, update_kwargs):
    await app.feed(message_update(GOOD, **update_kwargs))
    assert app.session.calls == [], "no reply, no network activity of any kind"
    assert app.queue.waiting_count == 0 and app.db.active_jobs() == []
    assert app.extractor.discover_calls == 0


async def test_unauthorized_commands_are_ignored_too(app):
    for text in ("/start", "/help", "/settings", "/cancel"):
        await app.feed(message_update(text, user_id=999))
    assert app.session.calls == []


async def test_non_message_updates_are_dropped(app):
    from aiogram.types import Update

    await app.feed(Update(update_id=900))
    assert app.session.calls == []


async def test_unauthorized_callback_is_dropped_before_any_lookup(app):
    await app.feed(callback_update("orig:" + "a" * 32, user_id=999))
    await app.feed(callback_update("orig:" + "a" * 32, user_id=OWNER, chat_id=-1, chat_type="group"))
    assert app.session.calls == []  # not even an "answer"


# --- commands ---------------------------------------------------------------------------------------------
async def test_start_and_help(app):
    await app.feed(message_update("/start"))
    await app.feed(message_update("/help"))
    texts = app.session.texts()
    assert texts[0] == messages.START_TEXT and texts[1] == messages.HELP_TEXT
    assert "TikTok" in texts[0] and "Instagram" in texts[0] and "X" in texts[0]
    assert "one link" in texts[0].lower()


async def test_settings_shows_default_media_and_buttons(app):
    await app.feed(message_update("/settings"))
    (call,) = app.session.of("SendMessage")
    assert "Media" in call.text
    labels = [b.text for row in call.reply_markup.inline_keyboard for b in row]
    assert labels == ["✓ Media", "Original files"]


async def test_settings_choice_persists_and_applies_to_next_job(app):
    await app.feed(callback_update("set:original"))
    assert app.db.get_delivery_mode(OWNER, DeliveryMode.MEDIA) is DeliveryMode.ORIGINAL
    await app.feed(message_update(GOOD))
    (job,) = app.db.active_jobs()
    assert job.delivery_mode is DeliveryMode.ORIGINAL


async def test_settings_rejects_garbage_choice(app):
    await app.feed(callback_update("set:nonsense"))
    assert app.db.get_delivery_mode(OWNER, DeliveryMode.MEDIA) is DeliveryMode.MEDIA


async def test_settings_are_per_user(app):
    await app.feed(callback_update("set:original", user_id=222))
    assert app.db.get_delivery_mode(222, DeliveryMode.MEDIA) is DeliveryMode.ORIGINAL
    assert app.db.get_delivery_mode(OWNER, DeliveryMode.MEDIA) is DeliveryMode.MEDIA


# --- the URL handler -------------------------------------------------------------------------------------------
async def test_valid_url_creates_a_job_and_acknowledges_without_downloading(app):
    await app.feed(message_update(GOOD, message_id=10))
    (job,) = app.db.active_jobs()
    assert job.state is JobState.QUEUED and job.original_url == GOOD and job.origin_message_id == 10
    assert job.delivery_mode is DeliveryMode.MEDIA  # default is Media mode
    assert app.notifier.acks == [job.id]  # prompt acknowledgement
    assert app.extractor.discover_calls == 0, "handler must not touch the network/extractor"


async def test_exact_submitted_url_is_preserved(app):
    url = "https://twitter.com/someuser/status/1700000000000?s=20&t=abc"
    await app.feed(message_update(f"look: {url}"))
    (job,) = app.db.active_jobs()
    assert job.original_url == url  # not the normalised extraction URL


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello there", "Please send a TikTok, Instagram, or X post link."),
        ("https://evil.example/video.mp4", "Please send a TikTok, Instagram, or X post link."),
        ("https://www.instagram.com/someuser/", "This link is not a supported individual post or Story."),
        ("https://www.instagram.com/stories/someuser/", "Story links must point to one specific Story"),
        ("https://www.instagram.com/stories/highlights/17800000000000000/", "Highlights are not supported"),
    ],
)
async def test_invalid_or_unsupported_links_get_a_readable_reply(app, text, expected):
    await app.feed(message_update(text))
    assert any(expected in t for t in app.session.texts()), app.session.texts()
    assert app.db.active_jobs() == []


async def test_multiple_links_ask_for_one_at_a_time(app):  # AT25
    await app.feed(message_update(f"{GOOD} https://x.com/other/status/1700000000001"))
    assert app.session.texts() == ["Please send one link at a time."]
    assert app.db.active_jobs() == []


async def test_link_in_a_photo_caption_is_accepted(app):
    await app.feed(message_update(None, photo=True, caption=GOOD))
    assert len(app.db.active_jobs()) == 1


async def test_hidden_text_link_entity_is_used(app):
    entity = MessageEntity(type="text_link", offset=0, length=4, url=GOOD)
    await app.feed(message_update("tap!", entities=[entity]))
    (job,) = app.db.active_jobs()
    assert job.original_url == GOOD


async def test_photo_without_link_and_other_types_are_handled_gracefully(app):
    await app.feed(message_update(None, photo=True))
    assert app.session.texts() == [messages.UNSUPPORTED_MESSAGE_TYPE]
    assert app.db.active_jobs() == []


async def test_second_request_while_busy_is_refused(app):
    await app.feed(message_update(GOOD, message_id=1))
    await app.feed(message_update("https://x.com/b/status/1700000000009", message_id=2))
    assert messages.ALREADY_RUNNING in app.session.texts()
    assert len(app.db.active_jobs()) == 1


async def test_duplicate_telegram_update_makes_one_job_and_one_ack(app):  # AT22
    update = message_update(GOOD, message_id=55, update_id=4242)
    await app.feed(update)
    await app.feed(update)  # Telegram re-delivers the very same update
    assert len(app.db.active_jobs()) == 1 and len(app.notifier.acks) == 1
    assert app.session.calls == []  # and no "busy" noise for the duplicate


async def test_queue_full_is_reported(tmp_path):
    a = await build_app(
        tmp_path, start_workers=False, allowed_user_ids=[1, 2, 3], max_queue_size=2, max_active_jobs_per_user=1
    )
    try:
        for uid in (1, 2):
            await a.feed(message_update(GOOD, user_id=uid, message_id=1))
        await a.feed(message_update(GOOD, user_id=3, message_id=1))
        assert messages.QUEUE_FULL in a.session.texts()
        assert len(a.db.active_jobs()) == 2
    finally:
        await a.shutdown()


async def test_handler_crash_is_contained_and_reported(tmp_path):
    a = await build_app(tmp_path, start_workers=False)
    try:

        def boom(*args, **kwargs):
            raise RuntimeError("db exploded with https://secret.example/x")

        a.ctx.db.get_delivery_mode = boom  # type: ignore[method-assign]
        await a.feed(message_update(GOOD))
        assert messages.INTERNAL_ERROR in a.session.texts()
        assert all("secret.example" not in t for t in a.session.texts())  # internals never reach the user
    finally:
        await a.shutdown()


# --- /cancel ---------------------------------------------------------------------------------------------------------
async def test_cancel_with_nothing_active(app):
    await app.feed(message_update("/cancel"))
    assert app.session.texts() == [messages.NOTHING_TO_CANCEL]


async def test_cancel_a_queued_job_marks_it_cancelled_and_frees_the_slot(app):
    await app.feed(message_update(GOOD, message_id=1))
    (job,) = app.db.active_jobs()
    await app.feed(message_update("/cancel"))
    assert app.db.get_job(job.id).state is JobState.CANCELLED
    assert app.notifier.finals[0][1].state is JobState.CANCELLED
    await app.feed(message_update("https://x.com/b/status/1700000000009", message_id=2))  # slot is free again
    assert len(app.db.active_jobs()) == 1


async def test_cancel_a_running_job_says_cancelling(tmp_path):
    import asyncio

    ext = FakeExtractor()
    ext.block = asyncio.Event()
    a = await build_app(tmp_path, extractor=ext)
    try:
        await a.feed(message_update(GOOD))
        await asyncio.wait_for(ext.entered_fetch.wait(), 2)
        await a.feed(message_update("/cancel"))
        assert messages.CANCELLING in a.session.texts()
        await a.drain()
        (_, report), *_ = a.notifier.finals
        assert report.state is JobState.CANCELLED
    finally:
        await a.shutdown()
