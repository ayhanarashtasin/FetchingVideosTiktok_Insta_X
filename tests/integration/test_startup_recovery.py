"""T05.6 / T05.7 / T06.3: startup recovery - interrupted jobs, orphan workspaces, expired metadata (AT21, AT24)."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

from app.bot.keyboards import parse_job_callback
from app.core.contracts import JobState
from app.main import notify_interrupted, recover_on_startup
from app.services.cleanup import WorkspaceManager
from app.storage.db import Database
from tests.fakes import make_bot
from tests.helpers import Clock, make_job


def setup(tmp_path):
    clock = Clock()
    db = Database(":memory:", clock=clock)
    db.migrate()
    ws = WorkspaceManager(tmp_path / "work", orphan_ttl=timedelta(minutes=30))
    return clock, db, ws


def test_recovery_interrupts_dead_jobs_removes_orphans_and_purges_expired(tmp_path):
    clock, db, ws = setup(tmp_path)

    # a job that was mid-download when the previous process died, plus its half-written files
    running, _, _ = make_job(db, owner_id=1)
    db.reserve_job(running, max_active_per_user=1)
    db.transition(running.id, JobState.RUNNING)
    orphan = ws.base / running.id
    orphan.mkdir()
    (orphan / "partial.part").write_bytes(b"x" * 100)
    # a queued job that never got a worker
    queued, _, _ = make_job(db, owner_id=2)
    db.reserve_job(queued, max_active_per_user=1)
    # an old finished job whose TTL has passed, and a recent one that must stay
    old, _, _ = make_job(db, owner_id=3, ttl_hours=1)
    db.reserve_job(old, max_active_per_user=1)
    db.transition(old.id, JobState.CANCELLED)
    clock.advance(hours=2)
    recent, _, _ = make_job(db, owner_id=4)
    db.reserve_job(recent, max_active_per_user=1)
    db.transition(recent.id, JobState.CANCELLED)
    # a stray directory that is not ours must be left alone
    foreign = ws.base / "keep-me"
    foreign.mkdir()

    # simulate the new process: a fresh manager with nothing active
    ws2 = WorkspaceManager(tmp_path / "work")
    report = recover_on_startup(db, ws2)

    assert {j.id for j in report.interrupted} == {running.id, queued.id}
    assert db.get_job(running.id).state is JobState.INTERRUPTED
    assert db.get_job(queued.id).state is JobState.INTERRUPTED
    assert report.removed_workspaces == [running.id] and not orphan.exists()
    assert foreign.exists()
    assert report.purged.jobs == 1 and db.get_job(old.id) is None  # expired URL purged
    assert db.get_job(recent.id) is not None
    assert db.active_jobs() == []


def test_recovery_is_idempotent(tmp_path):
    _, db, ws = setup(tmp_path)
    recover_on_startup(db, ws)
    report = recover_on_startup(db, ws)
    assert report.interrupted == [] and report.removed_workspaces == [] and report.purged.jobs == 0


def test_housekeeping_sweep_removes_stale_orphans_but_not_fresh_ones(tmp_path):
    clock = {"t": 10_000.0}
    ws = WorkspaceManager(tmp_path / "w", orphan_ttl=timedelta(minutes=30), clock=lambda: clock["t"])
    stale, fresh = uuid.uuid4().hex, uuid.uuid4().hex
    for j in (stale, fresh):
        (ws.base / j).mkdir()
    os.utime(ws.base / stale, (clock["t"] - 3600, clock["t"] - 3600))
    os.utime(ws.base / fresh, (clock["t"] - 10, clock["t"] - 10))
    assert ws.sweep() == [stale] and (ws.base / fresh).exists()


async def test_owner_is_told_about_interrupted_jobs_with_a_retry_button(tmp_path):
    _, db, ws = setup(tmp_path)
    job, _, _ = make_job(db, owner_id=111, chat_id=111, message_id=42)
    db.reserve_job(job, max_active_per_user=1)
    db.record_delivery(job.id, 0, [0, 1], "sent", [5, 6])  # two items made it before the crash
    report = recover_on_startup(db, ws)
    bot, session = make_bot()
    await notify_interrupted(bot, report.interrupted)
    (msg,) = session.of("SendMessage")
    assert msg.chat_id == 111 and "restarted" in msg.text
    assert msg.reply_parameters.message_id == 42
    (button,) = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert parse_job_callback(button.callback_data, "retry:") == job.id


async def test_notification_failures_never_block_startup(tmp_path):
    _, db, ws = setup(tmp_path)
    job, _, _ = make_job(db)
    db.reserve_job(job, max_active_per_user=1)
    report = recover_on_startup(db, ws)
    bot, session = make_bot()
    session.fail = lambda method: RuntimeError("telegram unreachable")
    await notify_interrupted(bot, report.interrupted)  # must not raise
