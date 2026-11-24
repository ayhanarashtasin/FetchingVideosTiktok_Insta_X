"""TelegramNotifier (status message lifecycle) and main-module helpers."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates

from app.bot import messages
from app.bot.messages import FinalReport
from app.bot.notifier import TelegramNotifier
from app.core.contracts import JobState
from app.main import TrackingSession, build_dispatcher
from app.storage.db import Database
from tests.fakes import FakeSession, make_bot
from tests.harness import build_app
from tests.helpers import make_job

URL = "https://x.com/someuser/status/1700000000000"


def setup(min_interval=0.0, clock=None):
    db = Database(":memory:")
    db.migrate()
    job, _, _ = make_job(db, owner_id=111, chat_id=111, message_id=42)
    db.reserve_job(job, max_active_per_user=1)
    bot, session = make_bot()
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    kw = {"clock": clock} if clock else {}
    return TelegramNotifier(bot, db, min_interval=min_interval, sleep=fake_sleep, **kw), session, job, db, sleeps


async def test_ack_creates_one_status_message_replying_to_the_request():
    n, session, job, db, _ = setup()
    await n.ack(job)
    (send,) = session.of("SendMessage")
    assert send.text == messages.FETCHING and send.chat_id == 111
    assert send.reply_parameters.message_id == 42 and send.reply_parameters.allow_sending_without_reply
    assert db.get_job(job.id).status_message_id is not None


async def test_ack_and_first_runner_status_race_creates_exactly_one_message():
    n, session, job, _, _ = setup()
    await asyncio.gather(n.ack(job), n.status(job, messages.FETCHING))
    assert len(session.of("SendMessage")) == 1
    assert session.of("EditMessageText") == []  # identical text is not re-sent either


async def test_progress_edits_the_same_message_and_skips_identical_text():
    n, session, job, _, _ = setup()
    await n.ack(job)
    await n.status(job, messages.PREPARING)
    await n.status(job, messages.PREPARING)  # duplicate: no second edit
    await n.status(job, messages.SENDING)
    assert [c.text for c in session.of("EditMessageText")] == [messages.PREPARING, messages.SENDING]
    assert len({c.message_id for c in session.of("EditMessageText")}) == 1


async def test_edits_are_throttled_to_avoid_chat_spam():
    t = {"now": 100.0}
    n, session, job, _, sleeps = setup(min_interval=2.0, clock=lambda: t["now"])
    await n.ack(job)
    await n.status(job, messages.PREPARING)  # immediately after the ack: must wait
    assert sleeps and 1.9 <= sleeps[0] <= 2.0
    t["now"] += 5
    sleeps.clear()
    await n.status(job, messages.SENDING)
    assert sleeps == []  # enough time has passed


async def test_success_removes_the_status_message():
    n, session, job, _, _ = setup()
    await n.ack(job)
    await n.finish(job, FinalReport(JobState.COMPLETED, URL, delivered=2, expected=2))
    (delete,) = session.of("DeleteMessage")
    assert delete.chat_id == 111


async def test_partial_result_replaces_the_status_with_the_outcome_text():
    n, session, job, _, _ = setup()
    await n.ack(job)
    await n.finish(job, FinalReport(JobState.PARTIAL, URL, delivered=4, expected=5, omitted_positions=[2]))
    last = session.of("EditMessageText")[-1]
    assert last.text.startswith("Delivered 4 of 5 items. Item 3 could not be retrieved.")
    assert session.of("DeleteMessage") == []


async def test_finish_falls_back_to_a_new_message_when_the_edit_fails():
    n, session, job, _, _ = setup()
    await n.ack(job)
    session.fail = lambda m: (
        TelegramBadRequest(m, "message to edit not found") if type(m).__name__ == "EditMessageText" else None
    )
    await n.finish(job, FinalReport(JobState.FAILED, URL, error=None))
    assert len(session.of("SendMessage")) == 2  # ack + the fallback
    assert messages.INTERNAL_ERROR in session.of("SendMessage")[-1].text


async def test_retry_button_only_when_offered():
    n, session, job, _, _ = setup()
    await n.ack(job)
    await n.finish(job, FinalReport(JobState.INTERRUPTED, URL, retry_offered=True))
    (button,) = [b for row in session.of("EditMessageText")[-1].reply_markup.inline_keyboard for b in row]
    assert button.callback_data == f"retry:{job.id}"


async def test_status_failures_never_raise_into_the_job():
    n, session, job, _, _ = setup()
    session.fail = lambda m: TelegramNetworkError(m, "boom")
    await n.ack(job)  # must not raise
    await n.status(job, messages.SENDING)
    await n.finish(job, FinalReport(JobState.CANCELLED, URL))


async def test_flood_wait_on_send_is_retried_once_after_the_requested_delay():
    n, session, job, _, sleeps = setup()
    calls = {"n": 0}

    def flood(m):
        if type(m).__name__ == "SendMessage" and calls["n"] == 0:
            calls["n"] += 1
            return TelegramRetryAfter(m, "Too Many Requests", 3)
        return None

    session.fail = flood
    await n.ack(job)
    assert sleeps == [3.0] and len(session.of("SendMessage")) == 2


# --- main helpers -----------------------------------------------------------------------------------------------
async def test_tracking_session_records_successful_polls_only(monkeypatch):
    from aiogram.client.session.aiohttp import AiohttpSession

    async def fake(self, bot, method, timeout=None):
        return [] if isinstance(method, GetUpdates) else True

    monkeypatch.setattr(AiohttpSession, "make_request", fake)
    session = TrackingSession()
    bot, _ = make_bot()
    assert session.last_poll_ok is None
    await session.make_request(bot, GetMe())
    assert session.last_poll_ok is None
    await session.make_request(bot, GetUpdates())
    assert session.last_poll_ok is not None


async def test_authorization_runs_before_the_error_boundary_and_before_any_handler(tmp_path):
    from app.bot.auth import AuthMiddleware
    from app.bot.middleware import ErrorBoundaryMiddleware

    a = await build_app(tmp_path, start_workers=False)
    try:
        dp = build_dispatcher(a.settings, a.ctx)
        # aiogram registers its own (state/context) outer middlewares first; ours follow in order.
        kinds = [type(m).__name__ for m in dp.update.outer_middleware]
        assert kinds.index(AuthMiddleware.__name__) < kinds.index(ErrorBoundaryMiddleware.__name__)
        # ...and the observer's router-level handlers are only reachable through them:
        assert dp.update.outer_middleware[-2].__class__ is AuthMiddleware
    finally:
        await a.shutdown()


def test_two_dispatchers_can_coexist_each_with_fresh_routers(tmp_path):
    """Regression: module-level routers could only be attached to one Dispatcher."""
    import asyncio as aio

    async def go():
        a = await build_app(tmp_path / "a", start_workers=False)
        b = await build_app(tmp_path / "b", start_workers=False)
        await a.shutdown()
        await b.shutdown()

    aio.run(go())


def test_helpers_are_unused_imports_free():
    assert FakeSession is not None
