"""Process entry point (T02.1): startup, long polling, housekeeping and graceful shutdown.

    uv run python -m app.main

Order of operations is deliberate:
  validate config -> logging -> DB migrate -> startup recovery (interrupt dead jobs, sweep orphan
  workspaces, purge expired metadata) -> start workers -> poll.  On shutdown: stop polling,
  interrupt running jobs (they report it and clean up), THEN close the Telegram session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.types import ReplyParameters

from app.bot import callbacks, handlers, messages
from app.bot.auth import AuthMiddleware
from app.bot.context import AppContext
from app.bot.keyboards import retry_keyboard
from app.bot.middleware import ErrorBoundaryMiddleware
from app.bot.notifier import TelegramNotifier
from app.config import Settings
from app.core.contracts import JobRecord
from app.core.errors import ConfigError
from app.core.job_queue import JobQueue
from app.core.url_validator import parse_hosts_config
from app.core.worker import JobRunner
from app.logging_setup import log_event, setup_logging
from app.services.cleanup import WorkspaceManager
from app.services.health import Heartbeat, write_heartbeat
from app.storage.db import Database, PurgeResult

log = logging.getLogger("app.main")

HOUSEKEEPING_INTERVAL = 30.0
POLL_WATCHDOG_SECONDS = 300.0


class TrackingSession(AiohttpSession):
    """Remembers when `getUpdates` last succeeded so the health check can detect stalled polling."""

    last_poll_ok: float | None = None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod,
        timeout: int | None = None,
    ):  # type: ignore[override]
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, GetUpdates):
            self.last_poll_ok = time.monotonic()
        return result


def build_bot(settings: Settings) -> Bot:
    api = (
        TelegramAPIServer.from_base(settings.local_bot_api_url.rstrip("/"), is_local=True)
        if settings.bot_api_mode == "local"
        else None
    )
    session = TrackingSession(api=api) if api else TrackingSession()
    return Bot(
        token=settings.resolved_token(),
        session=session,
        # Disable link previews: replies contain the original URL and a social-media card under
        # every result would be noise.
        default=DefaultBotProperties(link_preview_is_disabled=True),
    )


def build_dispatcher(settings: Settings, ctx: AppContext) -> Dispatcher:
    dp = Dispatcher()
    # Order matters: authorization first (nothing happens for strangers), then the error boundary.
    dp.update.outer_middleware(AuthMiddleware(settings.allowed_user_ids, private_only=settings.private_chat_only))
    dp.update.outer_middleware(ErrorBoundaryMiddleware())
    dp["ctx"] = ctx
    dp.include_router(callbacks.create_router())
    dp.include_router(handlers.create_router())
    return dp


@dataclass
class RecoveryReport:
    interrupted: list[JobRecord]
    removed_workspaces: list[str]
    purged: PurgeResult


def recover_on_startup(db: Database, workspaces: WorkspaceManager) -> RecoveryReport:
    """Make the world consistent after a (possibly crashed) previous run (T05.6, FR-13)."""
    interrupted = db.interrupt_nonterminal()
    removed = workspaces.sweep(startup=True)
    purged = db.purge_expired()
    return RecoveryReport(interrupted, removed, purged)


async def notify_interrupted(bot: Bot, jobs: list[JobRecord]) -> None:
    """Tell the owner about jobs that died with the previous process. Best effort."""
    for job in jobs:
        text = messages.final_text(
            messages.FinalReport(  # delivered_count is whatever Telegram confirmed before the crash
                state=job.state, original_url=job.original_url, delivered=job.delivered_count
            )
        )
        if not text:
            continue
        try:
            await bot.send_message(
                job.chat_id,
                text,
                reply_markup=retry_keyboard(job.id),
                reply_parameters=ReplyParameters(message_id=job.origin_message_id, allow_sending_without_reply=True),
            )
        except Exception as exc:
            log_event(log, logging.WARNING, "could not notify about interrupted job", error_type=type(exc).__name__)


async def housekeeping(
    settings: Settings, db: Database, workspaces: WorkspaceManager, extractor, session: TrackingSession, started: float
) -> None:
    """Periodic: heartbeat, expiry purge, orphan sweep, disk/worker/poll watchdogs."""
    while True:
        try:
            purged = db.purge_expired()
            swept = workspaces.sweep()
            if purged.jobs or swept:
                log_event(log, logging.INFO, "housekeeping", purged_jobs=purged.jobs, swept_workspaces=len(swept))
            worker_ok = await extractor.health()
            free = workspaces.free_bytes()
            poll_age = None if session.last_poll_ok is None else time.monotonic() - session.last_poll_ok
            write_heartbeat(
                settings.heartbeat_file,
                Heartbeat(
                    ts=time.time(),
                    poll_age=poll_age,
                    db_ok=db.check_writable(),
                    worker_ok=worker_ok,
                    disk_free=free,
                    started=started,
                ),
            )
            if free < settings.min_free_disk_bytes:
                log_event(log, logging.WARNING, "low disk space", free_mb=free // 1_000_000)
            if poll_age is not None and poll_age > POLL_WATCHDOG_SECONDS:
                # Polling is wedged. Exit non-zero so the restart policy brings a clean process up.
                log_event(log, logging.CRITICAL, "polling stalled; exiting for restart", poll_age=round(poll_age))
                os._exit(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(log, logging.ERROR, "housekeeping error", error_type=type(exc).__name__)
        await asyncio.sleep(min(HOUSEKEEPING_INTERVAL, float(settings.heartbeat_interval_seconds)))


async def amain() -> None:
    settings = Settings()
    setup_logging(settings.log_level)
    try:
        settings.validate_for_startup()
    except ConfigError as exc:
        log_event(log, logging.CRITICAL, "refusing to start: invalid configuration", problems=str(exc))
        raise SystemExit(2) from exc
    if not settings.allowed_user_ids:
        log_event(log, logging.WARNING, "ALLOWED_USER_IDS is empty: every request will be rejected")
    if settings.worker_mode == "inprocess":
        log_event(log, logging.WARNING, "extractor runs IN-PROCESS: development mode, not production-isolated")

    # Imported here so that `import app.main` works for tests that only need the helpers above.
    from app.extractors.factory import build_extraction_backend
    from app.services.telegram_sender import TelegramDeliverer

    db = Database(settings.db_path)
    db.migrate()
    workspaces = WorkspaceManager(
        settings.work_dir,
        orphan_ttl=timedelta(minutes=settings.orphan_ttl_minutes),
        shared_group=settings.worker_mode == "remote",
    )
    report = recover_on_startup(db, workspaces)
    log_event(
        log,
        logging.INFO,
        "startup recovery",
        interrupted=len(report.interrupted),
        removed_workspaces=len(report.removed_workspaces),
        purged_jobs=report.purged.jobs,
    )

    bot = build_bot(settings)
    session = bot.session
    assert isinstance(session, TrackingSession)
    extractor = build_extraction_backend(settings)
    notifier = TelegramNotifier(bot, db, min_interval=settings.status_edit_min_interval_seconds)
    deliverer = TelegramDeliverer(bot, db, settings, workspaces)
    queue = JobQueue(
        db,
        max_queue_size=settings.max_queue_size,
        max_active_per_user=settings.max_active_jobs_per_user,
        max_concurrent=settings.max_concurrent_jobs,
    )
    runner = JobRunner(
        settings=settings, db=db, extractor=extractor, deliverer=deliverer, notifier=notifier, workspaces=workspaces
    )
    ctx = AppContext(
        settings=settings,
        db=db,
        queue=queue,
        notifier=notifier,
        extra_hosts=parse_hosts_config(settings.extra_allowed_hosts),
    )
    dp = build_dispatcher(settings, ctx)

    started = time.time()
    hk = asyncio.create_task(housekeeping(settings, db, workspaces, extractor, session, started), name="housekeeping")
    queue.start(runner)
    try:
        await notify_interrupted(bot, report.interrupted)
        log_event(
            log,
            logging.INFO,
            "bot started",
            mode=settings.bot_api_mode,
            worker=settings.worker_mode,
            env=settings.app_env,
        )
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"], handle_signals=True)
    finally:
        log_event(log, logging.INFO, "shutting down")
        hk.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hk
        leftover = await queue.stop()  # running jobs report INTERRUPTED + clean up before the session closes
        await notify_interrupted(bot, [db.get_job(h.job.id) or h.job for h in leftover])
        await extractor.aclose()
        await bot.session.close()
        db.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
