"""Bounded in-memory job queue with atomic reservation (T02.7, T02.8, FR-14).

Rules (PRD section 8): one active job per user, `max_concurrent` globally, at most
`max_queue_size` jobs waiting. `submit()` is fully synchronous (no `await` between the capacity
checks and the insert), so under asyncio it is atomic with respect to other handlers; the SQLite
reservation inside it is additionally a single `BEGIN IMMEDIATE` transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.contracts import ExtractionRequest, JobRecord, JobState
from app.core.errors import IllegalTransitionError
from app.core.url_validator import ValidatedUrl
from app.storage.db import Database, ReserveOutcome

log = logging.getLogger(__name__)


@dataclass(eq=False)
class JobHandle:
    job: JobRecord
    request: ExtractionRequest
    validated: ValidatedUrl
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class SubmitStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    USER_BUSY = "user_busy"
    QUEUE_FULL = "queue_full"


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    handle: JobHandle | None = None


class CancelKind(StrEnum):
    NOTHING = "nothing"
    QUEUED = "queued"
    RUNNING = "running"


@dataclass(frozen=True)
class CancelResult:
    kind: CancelKind
    handle: JobHandle | None = None


class Runner(Protocol):
    """Anything with `async run(handle)`; implemented by `app.core.worker.JobRunner`."""

    async def run(self, handle: JobHandle) -> None: ...


class JobQueue:
    def __init__(
        self,
        db: Database,
        *,
        max_queue_size: int,
        max_active_per_user: int,
        max_concurrent: int,
    ) -> None:
        self._db = db
        self._max_queue = max_queue_size
        self._max_per_user = max_active_per_user
        self._max_concurrent = max_concurrent
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._waiting: OrderedDict[str, JobHandle] = OrderedDict()
        self._running: dict[str, JobHandle] = {}
        self._workers: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ introspection
    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    @property
    def running_count(self) -> int:
        return len(self._running)

    def active_for(self, owner_id: int) -> JobHandle | None:
        for pool in (self._running, self._waiting):
            for handle in pool.values():
                if handle.job.owner_id == owner_id:
                    return handle
        return None

    def handle_for(self, job_id: str) -> JobHandle | None:
        return self._running.get(job_id) or self._waiting.get(job_id)

    # ------------------------------------------------------------------ submit / cancel
    def submit(self, job: JobRecord, request: ExtractionRequest, validated: ValidatedUrl) -> SubmitResult:
        # NOTE: no awaits in this method - the checks and the insert must not interleave.
        if len(self._waiting) >= self._max_queue:
            return SubmitResult(SubmitStatus.QUEUE_FULL)
        outcome = self._db.reserve_job(job, max_active_per_user=self._max_per_user)
        if outcome is ReserveOutcome.DUPLICATE:
            return SubmitResult(SubmitStatus.DUPLICATE)
        if outcome is ReserveOutcome.USER_BUSY:
            return SubmitResult(SubmitStatus.USER_BUSY)
        handle = JobHandle(job=job, request=request, validated=validated)
        self._waiting[job.id] = handle
        self._ready.put_nowait(job.id)
        return SubmitResult(SubmitStatus.ACCEPTED, handle)

    def cancel_for_owner(self, owner_id: int) -> CancelResult:
        handle = self.active_for(owner_id)
        if handle is None:
            return CancelResult(CancelKind.NOTHING)
        return self._cancel(handle)

    def _cancel(self, handle: JobHandle) -> CancelResult:
        job_id = handle.job.id
        if job_id in self._waiting:
            del self._waiting[job_id]
            handle.cancel_event.set()
            try:
                self._db.transition(job_id, JobState.CANCELLED)
            except IllegalTransitionError:  # raced with a state change; treat as running
                return CancelResult(CancelKind.NOTHING)
            return CancelResult(CancelKind.QUEUED, handle)
        if job_id in self._running:
            handle.cancel_event.set()
            if handle.task is not None:
                handle.task.cancel()
            return CancelResult(CancelKind.RUNNING, handle)
        return CancelResult(CancelKind.NOTHING)

    # ------------------------------------------------------------------ worker side
    async def next_job(self) -> JobHandle:
        """Wait for the next job that has not been cancelled while queued."""
        while True:
            job_id = await self._ready.get()
            handle = self._waiting.pop(job_id, None)
            if handle is None:
                continue  # cancelled while waiting
            self._running[job_id] = handle
            return handle

    def finish(self, handle: JobHandle) -> None:
        self._running.pop(handle.job.id, None)
        self._waiting.pop(handle.job.id, None)

    async def _worker_loop(self, runner: Runner) -> None:
        me = asyncio.current_task()
        assert me is not None
        while True:
            handle = await self.next_job()
            handle.task = asyncio.create_task(runner.run(handle), name=f"job-{handle.job.id[:8]}")
            try:
                await handle.task
            except asyncio.CancelledError:
                if me.cancelling():
                    raise  # this WORKER is being shut down
                # otherwise only the job was cancelled (user /cancel): keep serving the queue
            except Exception:
                log.exception("job runner crashed", extra={"fields": {"job_id": handle.job.id}})
            finally:
                self.finish(handle)
            if me.cancelling():
                # A runner may swallow the cancellation it was handed (user-cancel racing with
                # shutdown). Without this check the loop would block on the queue and stop() hang.
                raise asyncio.CancelledError

    def start(self, runner: Runner) -> None:
        for i in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop(runner), name=f"worker-{i}"))

    async def stop(self) -> list[JobHandle]:
        """Cancel workers (running jobs are interrupted) and mark still-waiting jobs interrupted."""
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w
        self._workers.clear()
        leftover = list(self._waiting.values())
        for handle in leftover:
            with contextlib.suppress(IllegalTransitionError, KeyError):
                self._db.transition(handle.job.id, JobState.INTERRUPTED, error_code="INTERRUPTED")
        self._waiting.clear()
        return leftover
