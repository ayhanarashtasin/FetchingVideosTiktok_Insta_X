"""T02.7 / T02.8: bounded queue, atomic reservation, cancellation, concurrency, shutdown (AT19, AT22)."""

from __future__ import annotations

import asyncio

import pytest

from app.core.contracts import JobState
from app.core.job_queue import CancelKind, JobHandle, JobQueue, SubmitStatus
from app.storage.db import Database
from tests.helpers import make_job


@pytest.fixture
def db():
    d = Database(":memory:")
    d.migrate()
    yield d
    d.close()


def queue(db, **kw):
    params = dict(max_queue_size=10, max_active_per_user=1, max_concurrent=2)
    params.update(kw)
    return JobQueue(db, **params)


def submit(q, db, **kw):
    job, req, val = make_job(db, **kw)
    return q.submit(job, req, val)


def test_accepts_then_rejects_second_job_for_same_user(db):
    q = queue(db)
    assert submit(q, db, owner_id=1, message_id=1).status is SubmitStatus.ACCEPTED
    assert submit(q, db, owner_id=1, message_id=2).status is SubmitStatus.USER_BUSY
    assert submit(q, db, owner_id=2, message_id=1).status is SubmitStatus.ACCEPTED
    assert q.waiting_count == 2


def test_duplicate_event_does_not_create_a_second_job(db):  # AT22
    q = queue(db)
    job, req, val = make_job(db, owner_id=1, message_id=7)
    assert q.submit(job, req, val).status is SubmitStatus.ACCEPTED
    assert q.submit(job, req, val).status is SubmitStatus.DUPLICATE
    assert q.waiting_count == 1
    assert len(db.active_jobs()) == 1


def test_queue_size_is_bounded(db):
    q = queue(db, max_queue_size=3, max_active_per_user=99)
    results = [submit(q, db, owner_id=1, message_id=i).status for i in range(5)]
    assert results[:3] == [SubmitStatus.ACCEPTED] * 3
    assert results[3:] == [SubmitStatus.QUEUE_FULL] * 2
    assert len(db.active_jobs()) == 3  # a rejected submission leaves NOTHING behind


async def test_concurrent_submissions_for_one_user_accept_exactly_one(db):
    q = queue(db)

    async def one(i):
        await asyncio.sleep(0)
        return submit(q, db, owner_id=5, message_id=i).status

    results = await asyncio.gather(*(one(i) for i in range(20)))
    assert results.count(SubmitStatus.ACCEPTED) == 1
    assert results.count(SubmitStatus.USER_BUSY) == 19


def test_cancel_queued_job_frees_capacity_and_is_never_run(db):
    q = queue(db, max_queue_size=1)
    res = submit(q, db, owner_id=1, message_id=1)
    assert submit(q, db, owner_id=2, message_id=1).status is SubmitStatus.QUEUE_FULL
    cancelled = q.cancel_for_owner(1)
    assert cancelled.kind is CancelKind.QUEUED and cancelled.handle is res.handle
    assert db.get_job(res.handle.job.id).state is JobState.CANCELLED
    assert q.waiting_count == 0
    assert submit(q, db, owner_id=2, message_id=1).status is SubmitStatus.ACCEPTED  # slot is free again


def test_cancel_with_nothing_active(db):
    assert queue(db).cancel_for_owner(42).kind is CancelKind.NOTHING


async def test_next_job_skips_cancelled_entries(db):
    q = queue(db, max_active_per_user=5)
    a = submit(q, db, owner_id=1, message_id=1).handle
    b = submit(q, db, owner_id=2, message_id=1).handle
    q.cancel_for_owner(1)
    got = await asyncio.wait_for(q.next_job(), 1)
    assert got is b and a.cancel_event.is_set()


class Probe:
    """Runner stub that records concurrency and can be released."""

    def __init__(self, db):
        self.db = db
        self.running = 0
        self.max_running = 0
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def run(self, handle: JobHandle) -> None:
        self.started.append(handle.job.id)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(handle.job.id)
            raise
        finally:
            self.running -= 1


async def test_global_concurrency_cap(db):
    q = queue(db, max_concurrent=2)
    probe = Probe(db)
    for owner in (1, 2, 3, 4):
        submit(q, db, owner_id=owner, message_id=1)
    q.start(probe)
    await asyncio.sleep(0.05)
    assert probe.running == 2 and q.running_count == 2 and q.waiting_count == 2
    probe.release.set()
    await asyncio.sleep(0.05)
    assert probe.max_running == 2  # never exceeded
    assert len(probe.started) == 4
    await q.stop()


async def test_cancel_running_job_cancels_its_task_and_worker_keeps_serving(db):
    q = queue(db, max_concurrent=1)
    probe = Probe(db)
    first = submit(q, db, owner_id=1, message_id=1).handle
    second = submit(q, db, owner_id=2, message_id=1).handle
    q.start(probe)
    await asyncio.sleep(0.05)
    assert probe.started == [first.job.id]
    res = q.cancel_for_owner(1)
    assert res.kind is CancelKind.RUNNING and first.cancel_event.is_set()
    await asyncio.sleep(0.05)
    assert probe.cancelled == [first.job.id]
    assert probe.started == [first.job.id, second.job.id]  # the same worker moved on
    probe.release.set()
    await q.stop()


async def test_runner_exception_does_not_kill_the_worker(db, caplog):
    q = queue(db, max_concurrent=1)
    ran = []

    class Boom:
        async def run(self, handle):
            ran.append(handle.job.id)
            if len(ran) == 1:
                raise RuntimeError("bug in runner")

    submit(q, db, owner_id=1, message_id=1)
    submit(q, db, owner_id=2, message_id=1)
    q.start(Boom())
    await asyncio.sleep(0.1)
    assert len(ran) == 2
    await q.stop()


async def test_stop_interrupts_waiting_jobs_and_cancels_running_ones(db):
    q = queue(db, max_concurrent=1)
    probe = Probe(db)
    running = submit(q, db, owner_id=1, message_id=1).handle
    waiting = submit(q, db, owner_id=2, message_id=1).handle
    q.start(probe)
    await asyncio.sleep(0.05)
    leftover = await asyncio.wait_for(q.stop(), 2)
    assert [h.job.id for h in leftover] == [waiting.job.id]
    assert db.get_job(waiting.job.id).state is JobState.INTERRUPTED
    assert probe.cancelled == [running.job.id]  # runner got CancelledError with cancel_event UNSET -> "shutdown"
    assert not running.cancel_event.is_set()


async def test_stop_does_not_hang_when_runner_swallows_cancellation(db):
    q = queue(db, max_concurrent=1)

    class Stubborn:
        async def run(self, handle):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return  # swallows it (as a runner handling a user-cancel might)

    submit(q, db, owner_id=1, message_id=1)
    q.start(Stubborn())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(q.stop(), 2)  # must terminate
