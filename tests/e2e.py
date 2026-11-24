"""Runs the REAL `app.main.run` (dispatcher, middleware, queue, runner, notifier, deliverer, housekeeping,
shutdown) against the fake Telegram HTTP server. The only substitute is the external-tool layer:
`LocalExtractionBackend` runs the real adapters/finalize/ffprobe/remux over a `FakeToolRunner` that plays
back SYNTHETIC extractor output and copies real synthetic media."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.extractors.local import LocalExtractionBackend
from app.main import run
from tests.conftest import fake_bot_token
from tests.fake_telegram import FakeTelegramServer
from tests.fake_tools import FakeToolRunner

OWNER = 111
ACTIVE = {"QUEUED", "RUNNING", "PREPARING", "UPLOADING"}


@dataclass
class E2E:
    server: FakeTelegramServer
    settings: Settings
    tools: FakeToolRunner
    backend: LocalExtractionBackend
    task: asyncio.Task[None]

    async def say(self, text: str, *, message_id: int, user_id: int = OWNER, chat_type: str = "private") -> None:
        self.server.push(FakeTelegramServer.message_update(user_id, text, message_id, chat_type=chat_type))

    async def tap(self, data: str, *, cq_id: str, user_id: int = OWNER) -> None:
        self.server.push(FakeTelegramServer.callback_update(user_id, data, cq_id))

    def jobs(self) -> list[dict[str, Any]]:
        """Rows of the app's own SQLite file (read-only view; WAL permits concurrent readers)."""
        path = self.settings.db_path
        if not path.exists():
            return []
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY created_at")]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def active_jobs(self) -> list[dict[str, Any]]:
        return [j for j in self.jobs() if j["state"] in ACTIVE]

    async def idle(self, timeout: float = 10.0) -> None:
        """Wait until at least one job exists and none is queued or running."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.jobs() or self.active_jobs():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"bot did not finish; jobs={[(j['state'], j['error_code']) for j in self.jobs()]}")
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.15)  # let the final status edit/delete reach the server

    async def stop(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        await self.server.stop()


async def start_e2e(tmp_path: Path, *, before_start: Callable[[Settings], None] | None = None, **overrides: Any) -> E2E:
    server = FakeTelegramServer()
    base = await server.start()
    params: dict[str, Any] = {
        "bot_token": fake_bot_token(),
        "allowed_user_ids": [OWNER],
        "bot_api_mode": "local",
        "local_bot_api_url": base,
        "data_dir": tmp_path / "data",
        "work_dir": tmp_path / "work",
        "polling_timeout_seconds": 1,
        "heartbeat_interval_seconds": 1,
        "status_edit_min_interval_seconds": 0,
        "retry_base_delay_seconds": 0.01,
        "retry_max_delay_seconds": 0.05,
    }
    params.update(overrides)
    settings = Settings(_env_file=None, **params)
    if before_start is not None:
        before_start(settings)  # seed state a previous process would have left behind
    tools = FakeToolRunner()
    backend = LocalExtractionBackend(settings, runner=tools)
    task = asyncio.create_task(run(settings, extractor=backend, handle_signals=False), name="e2e-app")
    e2e = E2E(server, settings, tools, backend, task)
    await server.wait_for(lambda: server.polls >= 1 or task.done(), timeout=8)
    if task.done():
        task.result()  # surface a startup crash
    return e2e
