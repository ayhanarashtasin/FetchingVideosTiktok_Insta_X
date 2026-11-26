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
    DeliveryMode,
    DeliveryOutcome,
    ExtractionResult,
    JobState,
    PostManifest,
    PostType,
)
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode, IllegalTransitionError
from app.core.job_queue import JobHandle
from app.core.ports import Deliverer, ExtractionBackend, Notifier
from app.core.retry import retry_async
from app.core.url_validator import UrlKind, ValidatedUrl
from app.logging_setup import log_event, redact
from app.services.cleanup import WorkspaceManager
from app.storage.db import CachedPost, Database

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RunContext:
    """Mutable per-run scratch so a cancellation can still see what had been achieved."""

    result: ExtractionResult | None = None
    outcome: DeliveryOutcome | None = None
    error: BotError | None = None
    state: JobState | None = None
    resolve_s: float | None = None
    discover_s: float | None = None
    fetch_s: float | None = None
    extraction_s: float | None = None
    telegram_s: float | None = None
    cache_hit: bool = False
    direct_url: bool = False
    resolved_post_id: str | None = None
    resolved_post_hint: str | None = None
    expected_count: int | None = None
    status_tasks: list[asyncio.Task[None]] = dataclasses.field(default_factory=list)


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


def _decide_direct_state(total_items: int, outcome: DeliveryOutcome) -> JobState:
    delivered = len(outcome.confirmed_positions)
    if outcome.unknown_positions:
        return JobState.DELIVERY_UNKNOWN
    if delivered == 0:
        return JobState.FAILED
    if outcome.failed_positions or delivered < total_items:
        return JobState.PARTIAL
    return JobState.COMPLETED


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

    def _fire_status(self, ctx: RunContext, job, text: str) -> None:
        """Send a progress update without blocking extraction or upload on Telegram RTT."""
        ctx.status_tasks.append(asyncio.create_task(self.notifier.status(job, text), name=f"status-{job.id[:8]}"))

    def _advance_to_uploading(self, job_id: str, expected_count: int | None) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        if job.state is JobState.RUNNING:
            self.db.transition(job_id, JobState.PREPARING, expected_count=expected_count)
            self.db.transition(job_id, JobState.UPLOADING)
        elif job.state is JobState.PREPARING:
            self.db.transition(job_id, JobState.UPLOADING, expected_count=expected_count)

    # ------------------------------------------------------------------ pipeline
    async def _pipeline(self, handle: JobHandle, workspace: Path, ctx: RunContext) -> None:
        job_id = handle.job.id
        job = self.db.get_job(job_id) or handle.job
        self._fire_status(ctx, job, messages.FETCHING)

        validated = handle.validated
        if validated.kind is UrlKind.SHORT_LINK:
            validated = await self._resolve_short_link(handle, validated, ctx)
            self._raise_if_cancelled(handle)
        ctx.resolved_post_id = validated.post_id
        ctx.resolved_post_hint = validated.post_hint

        # 1. Pre-extraction Telegram file_id cache hit (public non-Story posts only).
        deliver_cached = getattr(self.deliverer, "deliver_cached", None)
        if (
            self.settings.media_cache_ttl_seconds > 0
            and deliver_cached is not None
            and validated.post_id
            and validated.post_hint != "story"
        ):
            cached = self.db.get_media_cache(job.platform, validated.post_id, job.delivery_mode)
            if cached is not None:
                self._advance_to_uploading(job_id, cached.expected_count)
                self._fire_status(ctx, job, messages.SENDING)
                t0 = time.monotonic()
                outcome = await deliver_cached(self.db.get_job(job_id) or job, cached)
                if outcome is not None:
                    ctx.telegram_s = round(time.monotonic() - t0, 1)
                    ctx.cache_hit = True
                    ctx.outcome = outcome
                    ctx.expected_count = cached.expected_count
                    ctx.state = _decide_direct_state(len(cached.items), outcome)
                    if ctx.state is JobState.FAILED:
                        code = next(iter(outcome.failure_codes.values()), ErrorCode.UPLOAD_FAILED)
                        ctx.error = BotError(code)
                    return
                # Stale file_id rejected cleanly before any send: evict and extract fresh.
                self.db.delete_media_cache(job.platform, validated.post_id, job.delivery_mode)

        start = time.monotonic()
        try:
            direct_outcome = await self._extract(handle, validated, workspace, ctx)
        finally:
            ctx.extraction_s = round(time.monotonic() - start, 1)
        self._raise_if_cancelled(handle)

        if direct_outcome is not None:
            ctx.outcome = direct_outcome
            ctx.direct_url = True
            total_items = ctx.expected_count or len(direct_outcome.confirmed_positions)
            ctx.state = _decide_direct_state(total_items, direct_outcome)
            if ctx.state is JobState.FAILED:
                code = next(iter(direct_outcome.failure_codes.values()), ErrorCode.UPLOAD_FAILED)
                ctx.error = BotError(code)
            elif ctx.state is JobState.COMPLETED:
                self._store_media_cache(job, ctx, expected=ctx.expected_count)
            return

        assert ctx.result is not None
        ctx.expected_count = ctx.result.expected_count
        cur = self.db.get_job(job_id)
        if cur is None or cur.state is JobState.RUNNING:
            self.db.transition(job_id, JobState.PREPARING, expected_count=ctx.result.expected_count)
        ctx.result = self._validate_assets(workspace, ctx.result)

        cur = self.db.get_job(job_id)
        if cur is None or cur.state is JobState.PREPARING:
            self.db.transition(job_id, JobState.UPLOADING)
        self._fire_status(ctx, job, messages.SENDING)
        start = time.monotonic()
        try:
            ctx.outcome = await self.deliverer.deliver(
                self.db.get_job(job_id) or job, handle.request, ctx.result, workspace
            )
        finally:
            ctx.telegram_s = round(time.monotonic() - start, 1)
        ctx.state = decide_final_state(ctx.result, ctx.outcome)
        if ctx.state is JobState.FAILED:
            code = next(iter(ctx.outcome.failure_codes.values()), ErrorCode.UPLOAD_FAILED)
            ctx.error = BotError(code)
        elif ctx.state is JobState.COMPLETED and ctx.result.completeness == "complete":
            self._store_media_cache(job, ctx, expected=ctx.result.expected_count)

    def _store_media_cache(self, job, ctx: RunContext, *, expected: int | None) -> None:
        if self.settings.media_cache_ttl_seconds <= 0 or not ctx.resolved_post_id or ctx.resolved_post_hint == "story":
            return
        take_fn = getattr(self.deliverer, "take_confirmed_cache_items", None)
        if take_fn is None:
            return
        items = take_fn(job.id)
        if not items:
            return
        self.db.put_media_cache(
            CachedPost(
                platform=job.platform,
                post_id=ctx.resolved_post_id,
                delivery_mode=job.delivery_mode,
                expected_count=expected,
                items=items,
            ),
            ttl_seconds=self.settings.media_cache_ttl_seconds,
        )

    @staticmethod
    def _raise_if_cancelled(handle: JobHandle) -> None:
        if handle.cancel_event.is_set():
            raise BotError(ErrorCode.CANCELLED)

    # ------------------------------------------------------------------ extraction
    async def _resolve_short_link(self, handle: JobHandle, validated: ValidatedUrl, ctx: RunContext) -> ValidatedUrl:
        s = self.settings

        async def attempt() -> ValidatedUrl:
            deadline = Deadline(s.extraction_timeout_seconds)
            t0 = time.monotonic()
            try:
                async with asyncio.timeout(s.extraction_timeout_seconds + 5):
                    return await self.extractor.resolve(validated, deadline)
            except TimeoutError:
                raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "short link resolution timed out") from None
            finally:
                ctx.resolve_s = round(time.monotonic() - t0, 1)

        return await retry_async(
            attempt,
            max_retries=s.retry_max_attempts,
            base_delay=s.retry_base_delay_seconds,
            max_delay=s.retry_max_delay_seconds,
            sleep=self._sleep,
        )

    async def _extract(
        self, handle: JobHandle, validated: ValidatedUrl, workspace: Path, ctx: RunContext
    ) -> DeliveryOutcome | None:
        s = self.settings
        job_id = handle.job.id
        direct_rejected = False
        direct_candidates_fn = getattr(self.extractor, "direct_url_candidates", None)
        drop_discovery_fn = getattr(self.extractor, "drop_cached_discovery", None)
        deliver_direct_fn = getattr(self.deliverer, "deliver_direct_urls", None)

        async def attempt() -> tuple[ExtractionResult | None, DeliveryOutcome | None]:
            nonlocal direct_rejected
            self.workspaces.clear(workspace)  # a retry must not see the previous attempt's partials
            deadline = Deadline(s.extraction_timeout_seconds)
            try:
                async with asyncio.timeout(s.extraction_timeout_seconds + 5):
                    request = handle.request.model_copy(
                        update={
                            "extraction_url": validated.extraction_url,
                            "resolved_url": validated.extraction_url,
                            "post_hint": validated.post_hint,
                        }
                    )
                    t0 = time.monotonic()
                    try:
                        manifest = await self.extractor.discover(request, workspace, deadline)
                    finally:
                        ctx.discover_s = round(time.monotonic() - t0, 1)
                    self._check_manifest(manifest)
                    ctx.resolved_post_id = ctx.resolved_post_id or manifest.post_id
                    if manifest.post_type is PostType.STORY:
                        ctx.resolved_post_hint = "story"

                    # Optional fast path: let Telegram fetch public cookie-free CDN URLs directly in Media mode.
                    if (
                        not direct_rejected
                        and s.direct_telegram_url
                        and request.delivery_mode is DeliveryMode.MEDIA
                        and direct_candidates_fn is not None
                        and deliver_direct_fn is not None
                    ):
                        candidates = await direct_candidates_fn(request.job_id, manifest, deadline)
                        if candidates:
                            self._raise_if_cancelled(handle)
                            self._advance_to_uploading(job_id, manifest.expected_count)
                            cur_job = self.db.get_job(job_id) or handle.job
                            self._fire_status(ctx, cur_job, messages.SENDING)
                            tg0 = time.monotonic()
                            outcome = await deliver_direct_fn(cur_job, candidates)
                            if outcome is not None:
                                ctx.telegram_s = round(time.monotonic() - tg0, 1)
                                ctx.expected_count = manifest.expected_count
                                if drop_discovery_fn is not None:
                                    drop_discovery_fn(request.job_id, manifest.platform)
                                return None, outcome
                            direct_rejected = True

                    t0 = time.monotonic()
                    try:
                        res = await self.extractor.fetch(request, manifest, workspace, deadline)
                        return res, None
                    finally:
                        ctx.fetch_s = round(time.monotonic() - t0, 1)
            except TimeoutError:
                raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "hard extraction timeout") from None

        def on_retry(n: int, delay: float, exc: BaseException) -> None:
            log_event(
                log,
                logging.WARNING,
                "retrying extraction",
                job_id=job_id,
                attempt=n,
                delay=round(delay, 1),
                error=getattr(getattr(exc, "code", None), "value", type(exc).__name__),
            )

        try:
            result, direct_outcome = await retry_async(
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
        ctx.result = result
        return direct_outcome

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
            cache_hit=ctx.cache_hit,
            direct_url=ctx.direct_url,
            resolve_s=ctx.resolve_s,
            discover_s=ctx.discover_s,
            fetch_s=ctx.fetch_s,
            extraction_s=ctx.extraction_s,
            telegram_s=ctx.telegram_s,
            media_bytes=sum(asset.size_bytes for asset in ctx.result.assets) if ctx.result is not None else None,
            duration_s=round(time.monotonic() - started, 1),
        )
        if ctx.status_tasks:
            await asyncio.gather(*ctx.status_tasks, return_exceptions=True)
            ctx.status_tasks.clear()
        try:
            await self.notifier.finish(job, report)
        except Exception as exc:
            log_event(log, logging.WARNING, "could not send final status", job_id=job_id, error_type=type(exc).__name__)
