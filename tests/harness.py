"""Builds the whole application graph (dispatcher + middleware + queue + runner + DB) around fakes,
so handler, callback and end-to-end orchestration tests exercise the REAL wiring."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.bot.context import AppContext
from app.bot.notifier import TelegramNotifier
from app.config import Settings
from app.core.job_queue import JobQueue
from app.core.worker import JobRunner
from app.main import build_dispatcher
from app.services.cleanup import WorkspaceManager
from app.storage.db import Database
from tests.fakes import FakeDeliverer, FakeExtractor, FakeSession, RecordingNotifier, make_bot, test_settings


@dataclass
class App:
    settings: Settings
    db: Database
    queue: JobQueue
    ctx: AppContext
    dp: Dispatcher
    bot: Bot
    session: FakeSession
    notifier: Any
    extractor: FakeExtractor
    deliverer: Any
    workspaces: WorkspaceManager
    runner: JobRunner
    sleeps: list[float]

    async def feed(self, update: Update) -> Any:
        return await self.dp.feed_update(self.bot, update)

    async def drain(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109
        """Wait until no job is queued or running."""
        async def idle() -> None:
            while self.queue.waiting_count or self.queue.running_count:  # noqa: ASYNC110 - polling a test counter
                await asyncio.sleep(0.01)

        await asyncio.wait_for(idle(), timeout)

    async def shutdown(self) -> None:
        await self.queue.stop()
        self.db.close()


async def build_app(
    tmp_path: Path,
    *,
    real_notifier: bool = False,
    extractor: FakeExtractor | None = None,
    deliverer: Any = None,
    start_workers: bool = True,
    **settings_overrides: Any,
) -> App:
    settings = test_settings(tmp_path, **settings_overrides)
    db = Database(":memory:")
    db.migrate()
    workspaces = WorkspaceManager(settings.work_dir, orphan_ttl=timedelta(minutes=30))
    queue = JobQueue(
        db, max_queue_size=settings.max_queue_size, max_active_per_user=settings.max_active_jobs_per_user,
        max_concurrent=settings.max_concurrent_jobs,
    )
    bot, session = make_bot()
    notifier: Any = TelegramNotifier(bot, db, min_interval=0) if real_notifier else RecordingNotifier()
    extractor = extractor or FakeExtractor()
    deliverer = deliverer or FakeDeliverer(db)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    runner = JobRunner(settings=settings, db=db, extractor=extractor, deliverer=deliverer, notifier=notifier,
                       workspaces=workspaces, sleep=fake_sleep)
    ctx = AppContext(settings=settings, db=db, queue=queue, notifier=notifier)
    dp = build_dispatcher(settings, ctx)
    if start_workers:
        queue.start(runner)
    return App(settings, db, queue, ctx, dp, bot, session, notifier, extractor, deliverer, workspaces, runner, sleeps)
