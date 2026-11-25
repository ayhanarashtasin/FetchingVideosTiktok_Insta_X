"""TelegramNotifier (status message lifecycle) and main-module helpers."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates

from app.bot import messages
from app.bot.messages import FinalReport
from app.bot.notifier import TelegramNotifier
from app.core.contracts import JobState
from app.main import TrackingSession, build_dispatcher, warn_missing_media_tools
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
    n, session, job, db, _ = setup()
    await n.ack(job)
    await n.status(job, messages.SENDING)
    await n.status(job, messages.SENDING)
    (edit,) = session.of("EditMessageText")
    assert edit.text == messages.SENDING and edit.message_id == db.get_job(job.id).status_message_id


async def test_edits_are_throttled_to_avoid_chat_spam():
    t = {"now": 100.0}
    n, session, job, _, sleeps = setup(min_interval=2.0, clock=lambda: t["now"])
    await n.ack(job)
    await n.status(job, messages.SENDING)
    assert sleeps == [] and session.of("EditMessageText") == []
    t["now"] += 5
    await n.status(job, messages.SENDING)
    assert sleeps == [] and len(session.of("EditMessageText")) == 1


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


def test_warn_missing_media_tools_logs_cross_platform_hints(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.main")
    missing = warn_missing_media_tools(which=lambda name: "/usr/bin/ffprobe" if name == "ffprobe" else None)
    assert missing == ["ffmpeg"]
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "winget install Gyan.FFmpeg" in msg and "sudo apt install ffmpeg" in msg
    assert caplog.records[0].fields["missing"] == "ffmpeg"


def test_warn_missing_media_tools_is_silent_when_present(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.main")
    assert warn_missing_media_tools(which=lambda name: f"/usr/bin/{name}") == []
    assert caplog.records == []


# --- fatal startup errors never reach the default traceback printer --------------------------------------------------
@pytest.fixture
def startup_env(monkeypatch, tmp_path):
    from tests.conftest import fake_bot_token

    monkeypatch.setenv("BOT_TOKEN", fake_bot_token())
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("APP_ENV", "development")


async def test_a_rejected_token_exits_with_a_clear_message_and_code_3(startup_env, monkeypatch, capsys):
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetMe

    import app.main as main_mod

    async def rejected(settings, **kw):
        raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")

    monkeypatch.setattr(main_mod, "run", rejected)
    with pytest.raises(SystemExit) as exc:
        await main_mod.amain()
    err = capsys.readouterr().err  # setup_logging owns the root handlers, so the JSON line lands on stderr
    assert exc.value.code == 3
    assert "rejected the bot token" in err and "Traceback" not in err


async def test_any_other_fatal_error_is_logged_redacted_and_exits_1(startup_env, monkeypatch, capsys):
    import io
    import logging

    import app.main as main_mod
    from tests.conftest import fake_bot_token

    token = fake_bot_token()

    async def explode(settings, **kw):
        # A transport error that embeds the request URL - which contains the bot token.
        raise RuntimeError(f"cannot reach https://api.telegram.org/bot{token}/getMe")

    monkeypatch.setattr(main_mod, "run", explode)
    sink = io.StringIO()
    real_setup = main_mod.setup_logging

    def capture_setup(level, **kw):
        real_setup(level, **kw)
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.getLogger().handlers[0].formatter)
        handler.addFilter(logging.getLogger().handlers[0].filters[0])
        logging.getLogger().addHandler(handler)

    monkeypatch.setattr(main_mod, "setup_logging", capture_setup)
    with pytest.raises(SystemExit) as exc:
        await main_mod.amain()
    logging.getLogger().handlers.clear()
    out = sink.getvalue() + capsys.readouterr().err
    assert exc.value.code == 1
    assert "fatal error" in out and token not in out, "the token must not survive into any log output"
