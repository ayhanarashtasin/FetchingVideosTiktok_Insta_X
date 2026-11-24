"""Job orchestrator / state machine (T02.8, T04.8, T04.9, FR-10..FR-15).

QUEUED -> RUNNING (resolve, discover, fetch) -> PREPARING (validate) -> UPLOADING (Telegram)
-> terminal state. Every path - success, failure, cancel, shutdown - goes through `_finish`, and
the job workspace is always removed in a `finally` block.

Invariants worth stating:
* "Completed" is only reported when Telegram confirmed every expected item AND the source's
  completeness was proven; otherwise the state is PARTIAL / UNVERIFIED / DELIVERY_UNKNOWN.
* An interrupted send is never replayed automatically.
* A user cancel (cancel_event set) ends the job quietly; a shutdown cancel (event unset) marks it
  INTERRUPTED and re-raises so the process can exit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path

from app.bot import messages
from app.bot.messages import FinalReport
from app.config import Settings
from app.core.contracts import (
    Access,
    DeliveryOutcome,
    ExtractionResult,
    JobState,
    PostManifest,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode, IllegalTransitionError
from app.core.job_queue import JobHandle
from app.core.ports import Deliverer, ExtractionBackend, Notifier
from app.core.retry import retry_async
from app.core.url_validator import UrlKind
from app.logging_setup import log_event, redact
from app.services.cleanup import WorkspaceManager
from app.storage.db import Database

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RunContext:
    """Mutable per-run scratch so a cancellation can still see what had been achieved."""

    result: ExtractionResult | None = None
    outcome: DeliveryOutcome | None = None
    error: BotError | None = None
    state: JobState | None = None


def decide_final_state(result: ExtractionResult, outcome: DeliveryOutcome) -> JobState:
    """Map extraction + delivery results to a terminal state. Never optimistic."""
    delivered = len(outcome.confirmed_positions)
    if outcome.unknown_positions:
        return JobState.DELIVERY_UNKNOWN
    if delivered == 0:
        return JobState.FAILED
    if outcome.failed_positions or result.omissions or result.completeness == "partial":
        return JobState.PARTIAL
    if delivered < len(result.assets):
        return JobState.PARTIAL  # something was planned but never confirmed
    return JobState.COMPLETED if result.completeness == "complete" else JobState.UNVERIFIED


class JobRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        extractor: ExtractionBackend,
        deliverer: Deliverer,
        notifier: Notifier,
        workspaces: WorkspaceManager,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.db = db
        self.extractor = extractor
        self.deliverer = deliverer
        self.notifier = notifier
        self.workspaces = workspaces
        self._sleep = sleep
        self.consecutive_extraction_failures = 0  # read by the optional alerting monitor

    # ------------------------------------------------------------------ public
    async def run(self, handle: JobHandle) -> None:
        job_id = handle.job.id
        ctx = RunContext()
        started = time.monotonic()
        shutdown = False
        workspace: Path | None = None
        try:
            try:
                self.db.transition(job_id, JobState.RUNNING)
            except IllegalTransitionError:
                return  # cancelled between dequeue and start: nothing to do, nothing to clean
            try:
                workspace = self.workspaces.create(job_id)
                await self._pipeline(handle, workspace, ctx)
            except asyncio.CancelledError:
                if handle.cancel_event.is_set():
                    ctx.state = JobState.CANCELLED
                else:
                    ctx.state = JobState.INTERRUPTED
                    shutdown = True
            except BotError as err:
                ctx.error = err
                ctx.state = JobState.CANCELLED if err.code is ErrorCode.CANCELLED else JobState.FAILED
            except Exception as exc:
                log_event(
                    log,
                    logging.ERROR,
                    "unexpected error in job",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    error=redact(exc),
                )
                ctx.error = BotError(ErrorCode.INTERNAL_ERROR, type(exc).__name__)
                ctx.state = JobState.FAILED
            await asyncio.shield(self._finish(handle, ctx, started))
        finally:
            if workspace is not None:
                self.workspaces.release(job_id)
        if shutdown:
            raise asyncio.CancelledError

    # ------------------------------------------------------------------ pipeline
    async def _pipeline(self, handle: JobHandle, workspace: Path, ctx: RunContext) -> None:
        job_id = handle.job.id
        job = self.db.get_job(job_id) or handle.job
        await self.notifier.status(job, messages.FETCHING)

        ctx.result = await self._extract(handle, workspace)
        self._raise_if_cancelled(handle)

        self.db.transition(job_id, JobState.PREPARING, expected_count=ctx.result.expected_count)
        await self.notifier.status(job, messages.PREPARING)
        ctx.result = self._validate_assets(workspace, ctx.result)

        self.db.transition(job_id, JobState.UPLOADING)
        await self.notifier.status(job, messages.SENDING)
        ctx.outcome = await self.deliverer.deliver(
            self.db.get_job(job_id) or job, handle.request, ctx.result, workspace
        )
        ctx.state = decide_final_state(ctx.result, ctx.outcome)
        if ctx.state is JobState.FAILED:
            code = next(iter(ctx.outcome.failure_codes.values()), ErrorCode.UPLOAD_FAILED)
            ctx.error = BotError(code)

    @staticmethod
    def _raise_if_cancelled(handle: JobHandle) -> None:
        if handle.cancel_event.is_set():
            raise BotError(ErrorCode.CANCELLED)

    # ------------------------------------------------------------------ extraction
    async def _extract(self, handle: JobHandle, workspace: Path) -> ExtractionResult:
        s = self.settings

        async def attempt() -> ExtractionResult:
            self.workspaces.clear(workspace)  # a retry must not see the previous attempt's partials
            deadline = Deadline(s.extraction_timeout_seconds)
            try:
                async with asyncio.timeout(s.extraction_timeout_seconds + 5):
                    validated = handle.validated
                    if validated.kind is UrlKind.SHORT_LINK:
                        validated = await self.extractor.resolve(validated, deadline)
                    request = handle.request.model_copy(
                        update={
                            "extraction_url": validated.extraction_url,
                            "resolved_url": validated.extraction_url,
                            "post_hint": validated.post_hint,
                        }
                    )
                    manifest = await self.extractor.discover(request, workspace, deadline)
                    self._check_manifest(manifest)
                    return await self.extractor.fetch(request, manifest, workspace, deadline)
            except TimeoutError:
                raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "hard extraction timeout") from None

        def on_retry(n: int, delay: float, exc: BaseException) -> None:
            log_event(
                log,
                logging.WARNING,
                "retrying extraction",
                job_id=handle.job.id,
                attempt=n,
                delay=round(delay, 1),
                error=getattr(getattr(exc, "code", None), "value", type(exc).__name__),
            )

        try:
            result = await retry_async(
                attempt,
                max_retries=s.retry_max_attempts,
                base_delay=s.retry_base_delay_seconds,
                max_delay=s.retry_max_delay_seconds,
                sleep=self._sleep,
                on_retry=on_retry,
            )
        except BotError as err:
            if err.code in {ErrorCode.EXTRACTOR_CHANGED, ErrorCode.DOWNLOAD_TIMEOUT}:
                self.consecutive_extraction_failures += 1
            raise
        self.consecutive_extraction_failures = 0
        return result

    def _check_manifest(self, manifest: PostManifest) -> None:
        # Fail closed: only content the adapter positively established as public may proceed.
        if manifest.access is not Access.PERMITTED_PUBLIC:
            raise BotError(ErrorCode.ACCESS_RESTRICTED, f"access={manifest.access.value}")
        limit = self.settings.max_items_per_job
        count = max(len(manifest.items), manifest.expected_count or 0)
        if count > limit:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, f"{count} items > limit {limit}")

    def _validate_assets(self, workspace: Path, result: ExtractionResult) -> ExtractionResult:
        """Re-check everything the (untrusted) extractor reported before it can be sent."""
        s = self.settings
        if len(result.assets) > s.max_items_per_job:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "too many items")
        checked, total = [], 0
        for asset in result.assets:
            real = self.workspaces.resolve_inside(workspace, asset.local_path)
            actual = real.stat().st_size
            if actual != asset.size_bytes or actual == 0:
                raise BotError(ErrorCode.MEDIA_INVALID, "reported size does not match file", position=asset.position)
            total += actual
            checked.append(asset.model_copy(update={"local_path": real}))
        if total > s.max_temp_bytes_per_job:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "temporary byte budget exceeded")
        return result.model_copy(update={"assets": checked})

    # ------------------------------------------------------------------ finishing
    async def _finish(self, handle: JobHandle, ctx: RunContext, started: float) -> None:
        job_id = handle.job.id
        state = ctx.state or JobState.FAILED
        error_code = ctx.error.code if ctx.error else (ErrorCode.INTERRUPTED if state is JobState.INTERRUPTED else None)
        try:
            job = self.db.transition(job_id, state, error_code=error_code)
        except IllegalTransitionError:
            job = self.db.get_job(job_id) or handle.job
        # Re-read counters: the deliverer persisted confirmed batches even if it never returned.
        job = self.db.get_job(job_id) or job

        outcome = ctx.outcome
        result = ctx.result
        report = FinalReport(
            state=job.state,
            original_url=job.original_url,
            delivered=job.delivered_count,
            expected=(result.expected_count if result and result.expected_count is not None else job.expected_count),
            omitted_positions=[o.position for o in (result.omissions if result else []) if o.position is not None],
            failed_positions=list(outcome.failed_positions) if outcome else [],
            unknown_positions=list(outcome.unknown_positions) if outcome else [],
            failure_codes=list(outcome.failure_codes.values()) if outcome else [],
            error=ctx.error,
            retry_offered=bool(
                job.state in {JobState.INTERRUPTED, JobState.DELIVERY_UNKNOWN}
                or (job.state is JobState.FAILED and ctx.error is not None and ctx.error.owner_retryable)
            ),
        )
        log_event(
            log,
            logging.INFO,
            "job finished",
            job_id=job_id,
            platform=job.platform.value,
            state=job.state.value,
            delivered=job.delivered_count,
            expected=report.expected,
            error_code=error_code.value if isinstance(error_code, ErrorCode) else error_code,
            duration_s=round(time.monotonic() - started, 1),
        )
        try:
            await self.notifier.finish(job, report)
        except Exception as exc:
            log_event(log, logging.WARNING, "could not send final status", job_id=job_id, error_type=type(exc).__name__)
