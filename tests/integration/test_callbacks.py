"""T04.7 / T05.9 / T06.4: 'Original files' and 'Retry' callbacks - ownership, TTL, idempotency (AT12, AT22, AT23)."""

from __future__ import annotations

import pytest

from app.bot import messages
from app.bot.callbacks import authorize_job_callback, retry_allowed
from app.bot.keyboards import original_data, parse_job_callback, retry_data
from app.core.contracts import DeliveryMode, JobState
from app.core.errors import BotError, ErrorCode
from tests.fakes import OWNER, FakeExtractor, callback_update, message_update
from tests.harness import build_app
from tests.helpers import Clock, make_job

GOOD = "https://x.com/someuser/status/1700000000000"
OTHER = 222


@pytest.fixture
async def app(tmp_path):
    a = await build_app(tmp_path, allowed_user_ids=[OWNER, OTHER])
    yield a
    await a.shutdown()


async def finished_job(app, *, owner=OWNER, message_id=7, plan=None):
    """Run a job to a terminal state and return its record."""
    if plan is not None:
        app.extractor.plan = list(plan)
    await app.feed(message_update(GOOD, user_id=owner, message_id=message_id))
    await app.drain()
    return app.db.get_job(app.notifier.acks[-1])


def alerts(app):
    return [c for c in app.session.of("AnswerCallbackQuery")]


# --- Original files ---------------------------------------------------------------------------------------
async def test_original_button_starts_an_original_mode_job_linked_to_its_parent(app):
    parent = await finished_job(app)
    assert parent.state is JobState.COMPLETED and parent.delivery_mode is DeliveryMode.MEDIA
    await app.feed(callback_update(original_data(parent.id), cq_id="cbq-1"))
    await app.drain()
    child = app.db.get_job(app.notifier.acks[-1])
    assert child.id != parent.id
    assert child.delivery_mode is DeliveryMode.ORIGINAL and child.parent_job_id == parent.id
    assert child.request_key == "cbq-1" and child.original_url == GOOD
    assert app.db.get_job(parent.id).state is JobState.COMPLETED  # the earlier result is untouched
    assert app.extractor.seen_requests[-1].delivery_mode is DeliveryMode.ORIGINAL  # re-fetches the source


async def test_duplicate_callback_event_does_not_start_a_second_job(app):  # AT22
    parent = await finished_job(app)
    cb = callback_update(original_data(parent.id), cq_id="same-id")
    await app.feed(cb)
    await app.drain()
    n_acks, n_attempts = len(app.notifier.acks), app.extractor.attempts
    await app.feed(cb)  # Telegram redelivers the identical callback
    await app.drain()
    assert len(app.notifier.acks) == n_acks and app.extractor.attempts == n_attempts


async def test_a_different_user_cannot_use_someone_elses_button(app):
    parent = await finished_job(app, owner=OWNER)
    await app.feed(callback_update(original_data(parent.id), user_id=OTHER))  # allowlisted, but not the owner
    assert len(app.notifier.acks) == 1, "no new job"
    (answer,) = alerts(app)
    assert answer.text == messages.ACTION_NOT_ALLOWED and answer.show_alert


async def test_button_pressed_from_another_chat_is_refused(app):
    parent = await finished_job(app)
    # same user id, but the callback's message lives in a different (private) chat id
    await app.feed(callback_update(original_data(parent.id), user_id=OWNER, chat_id=OWNER))
    assert len(app.notifier.acks) == 2  # sanity: same chat works
    stray = app.db.new_job_record(
        job_id="c" * 32,
        owner_id=OWNER,
        chat_id=OWNER + 5,
        origin_message_id=1,
        original_url=GOOD,
        platform=parent.platform,
        delivery_mode=DeliveryMode.MEDIA,
        ttl_hours=24,
    )
    app.db.reserve_job(stray, max_active_per_user=9)
    app.db.transition(stray.id, JobState.CANCELLED)
    with pytest.raises(BotError) as exc:
        authorize_job_callback(app.db, stray.id, user_id=OWNER, chat_id=OWNER)  # job belongs to a different chat
    assert exc.value.code is ErrorCode.UNAUTHORIZED


@pytest.mark.parametrize(
    "payload",
    [
        "orig:",
        "orig:xyz",
        "orig:" + "a" * 31,
        "orig:" + "A" * 32,
        "orig:" + "a" * 33,
        "orig:../../etc/passwd",
        "retry:",
        "retry:" + "g" * 32,
        "unknown:thing",
        "set:",
        "x" * 64,
    ],
)
async def test_malformed_callback_payloads_are_answered_and_ignored(app, payload):
    await app.feed(callback_update(payload))
    assert app.notifier.acks == [] and app.db.active_jobs() == []
    assert alerts(app), "the callback must still be answered so the client stops spinning"


async def test_unknown_job_id_is_reported_as_expired(app):
    await app.feed(callback_update(original_data("d" * 32)))
    assert alerts(app)[-1].text == messages.ACTION_EXPIRED and app.notifier.acks == []


async def test_expired_metadata_blocks_the_button_without_promising_retrieval(tmp_path):  # AT23 / AT24
    clock = Clock()
    a = await build_app(tmp_path, allowed_user_ids=[OWNER])
    try:
        job = await finished_job(a)
        later = clock.now.replace(year=2030)
        with pytest.raises(BotError) as exc:
            authorize_job_callback(a.db, job.id, user_id=OWNER, chat_id=OWNER, now=later)
        assert exc.value.reason == "callback_expired"
    finally:
        await a.shutdown()


async def test_busy_user_gets_a_clear_alert_instead_of_a_second_job(app):
    parent = await finished_job(app)
    app.extractor.block = __import__("asyncio").Event()
    await app.feed(message_update(GOOD, message_id=8))  # a new job is now running
    await __import__("asyncio").wait_for(app.extractor.entered_fetch.wait(), 2)
    await app.feed(callback_update(original_data(parent.id), cq_id="late"))
    assert alerts(app)[-1].text == messages.ALREADY_RUNNING
    app.extractor.block.set()
    await app.drain()


async def test_original_button_is_refused_for_failed_jobs(app):
    from app.core.errors import BotError as BE

    failed = await finished_job(app, plan=[BE(ErrorCode.NOT_FOUND_OR_EXPIRED)])
    assert failed.state is JobState.FAILED
    await app.feed(callback_update(original_data(failed.id)))
    assert alerts(app)[-1].text == messages.NOT_RETRYABLE


async def test_orig_command_starts_original_job_and_enforces_ownership_and_idempotency(app):
    parent = await finished_job(app)
    await app.feed(message_update(f"/orig_{parent.id}", message_id=80))
    await app.drain()
    child = app.db.get_job(app.notifier.acks[-1])
    assert child.delivery_mode is DeliveryMode.ORIGINAL and child.parent_job_id == parent.id
    assert child.request_key == "cmd:80"

    # Identical redelivered Telegram message is ignored without starting a third job.
    n_acks = len(app.notifier.acks)
    await app.feed(message_update(f"/orig_{parent.id}", message_id=80))
    await app.drain()
    assert len(app.notifier.acks) == n_acks

    # Another user cannot invoke the owner's /orig_ command.
    await app.feed(message_update(f"/orig_{parent.id}", user_id=OTHER, message_id=81))
    assert messages.ACTION_NOT_ALLOWED in app.session.texts()
    assert len(app.notifier.acks) == n_acks

    # Malformed or unknown /orig_ commands report expired rather than URL-invalid.
    await app.feed(message_update("/orig_notvalid", message_id=82))
    await app.feed(message_update(f"/orig_{'d' * 32}", message_id=83))
    assert app.session.texts()[-2:] == [messages.ACTION_EXPIRED, messages.ACTION_EXPIRED]


# --- Retry -----------------------------------------------------------------------------------------------------
async def test_retry_reruns_a_retryable_failure_with_the_same_mode(app):
    failed = await finished_job(app, plan=[BotError(ErrorCode.EXTRACTOR_CHANGED)])
    assert failed.state is JobState.FAILED and failed.error_code == "EXTRACTOR_CHANGED"
    await app.feed(callback_update(retry_data(failed.id), cq_id="r1"))
    await app.drain()
    again = app.db.get_job(app.notifier.acks[-1])
    assert again.id != failed.id and again.parent_job_id == failed.id
    assert again.state is JobState.COMPLETED and again.delivery_mode is failed.delivery_mode
    assert app.db.get_job(failed.id).state is JobState.FAILED  # history is not overwritten


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.NOT_FOUND_OR_EXPIRED,
        ErrorCode.ACCESS_RESTRICTED,
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.FILE_TOO_LARGE,
        ErrorCode.LIMIT_EXCEEDED,
    ],
)
async def test_retry_is_not_offered_for_permanent_failures(app, code):
    failed = await finished_job(app, plan=[BotError(code)])
    await app.feed(callback_update(retry_data(failed.id)))
    assert alerts(app)[-1].text == messages.NOT_RETRYABLE
    assert len(app.notifier.acks) == 1


async def test_retry_is_refused_for_a_successful_job(app):
    ok = await finished_job(app)
    await app.feed(callback_update(retry_data(ok.id)))
    assert alerts(app)[-1].text == messages.NOT_RETRYABLE


def test_retry_allowed_follows_the_error_contract(app):
    def job_with(state, code=None):
        j, _, _ = make_job(app.db, message_id=hash((state, code)) % 10_000)
        return j.model_copy(update={"state": state, "error_code": code})

    assert retry_allowed(job_with(JobState.INTERRUPTED))
    assert retry_allowed(job_with(JobState.DELIVERY_UNKNOWN))
    assert retry_allowed(job_with(JobState.FAILED, "RATE_LIMITED"))
    assert retry_allowed(job_with(JobState.FAILED, "DOWNLOAD_TIMEOUT"))
    assert not retry_allowed(job_with(JobState.FAILED, "NOT_FOUND_OR_EXPIRED"))
    assert not retry_allowed(job_with(JobState.FAILED, "not-a-code"))
    assert not retry_allowed(job_with(JobState.FAILED, None))
    assert not retry_allowed(job_with(JobState.COMPLETED))
    assert not retry_allowed(job_with(JobState.CANCELLED))


def test_callback_parsing():
    assert parse_job_callback("orig:" + "ab" * 16, "orig:") == "ab" * 16
    assert parse_job_callback("orig:" + "ab" * 16, "retry:") is None
    assert parse_job_callback(None, "orig:") is None
    assert len(original_data("a" * 32).encode()) < 64  # fits Telegram's callback_data limit


async def test_failed_job_final_message_carries_a_retry_button_when_retryable(tmp_path):
    a = await build_app(
        tmp_path, real_notifier=True, extractor=FakeExtractor(plan=[BotError(ErrorCode.EXTRACTOR_CHANGED)])
    )
    try:
        await a.feed(message_update(GOOD))
        await a.drain()
        edits = a.session.of("EditMessageText")
        final = edits[-1]
        assert "could not be processed right now" in final.text
        (button,) = [b for row in final.reply_markup.inline_keyboard for b in row]
        assert button.text == "Retry" and button.callback_data.startswith("retry:")
    finally:
        await a.shutdown()
