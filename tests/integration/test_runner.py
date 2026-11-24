"""T02.8 / T04.8 / T04.9 / T06.3: the job state machine end to end on the REAL wiring
(dispatcher -> queue -> runner -> DB -> workspace) with in-memory extractor/Telegram fakes.

Covers AT18 (partial / unknown), AT19 (cancel), AT20 (bounded retry), AT21 (interruption).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.bot import messages
from app.core.contracts import Access, DeliveryOutcome, JobState
from app.core.errors import BotError, ErrorCode
from app.core.job_queue import JobHandle
from app.core.worker import decide_final_state
from tests.fakes import (
    OWNER,
    FakeDeliverer,
    FakeExtractor,
    asset_in,
    message_update,
)
from tests.harness import build_app
from tests.helpers import make_job

GOOD = "https://x.com/someuser/status/1700000000000"


def job_of(app):
    (job_id, *_) = app.notifier.acks
    return app.db.get_job(job_id)


async def run_one(app, text=GOOD, **kw):
    await app.feed(message_update(text, **kw))
    await app.drain()
    return job_of(app)


@pytest.fixture
async def app(tmp_path):
    a = await build_app(tmp_path)
    yield a
    await a.shutdown()


# --- success -----------------------------------------------------------------------------------------------
async def test_happy_path_completes_cleans_up_and_removes_the_status_message(app):
    job = await run_one(app)
    assert job.state is JobState.COMPLETED and job.delivered_count == 2 and job.expected_count == 2
    assert app.notifier.status_texts() == [messages.FETCHING, messages.PREPARING, messages.SENDING]
    ((_, report),) = app.notifier.finals
    assert report.state is JobState.COMPLETED and messages.final_text(report) is None  # status message removed
    assert not (app.settings.work_dir / job.id).exists(), "workspace must be gone after completion"
    assert app.deliverer.files_seen_at_delivery == [True, True]  # files existed when the sender ran


async def test_delivery_mode_and_original_url_reach_the_extractor(app):
    app.db.set_delivery_mode(OWNER, __import__("app.core.contracts", fromlist=["DeliveryMode"]).DeliveryMode.ORIGINAL)
    url = "https://twitter.com/someuser/status/1700000000000?s=20"
    await run_one(app, url)
    req = app.extractor.seen_requests[0]
    assert req.original_url == url and req.extraction_url == "https://x.com/someuser/status/1700000000000"
    assert req.delivery_mode.value == "original"


async def test_short_links_are_resolved_before_discovery(app):
    await run_one(app, "https://vm.tiktok.com/ZMabcdEfg/")
    assert app.extractor.resolve_calls == 1
    req = app.extractor.seen_requests[0]
    assert req.extraction_url == "https://www.tiktok.com/@resolved/video/7300000000000000000"
    assert req.original_url == "https://vm.tiktok.com/ZMabcdEfg/"  # attribution keeps what the user sent


# --- failures ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (BotError(ErrorCode.NOT_FOUND_OR_EXPIRED), "unavailable or may have expired"),
        (BotError(ErrorCode.ACCESS_RESTRICTED), "private or restricted"),
        (BotError(ErrorCode.AUTH_REQUIRED, reason="story"), "requires an authorized Instagram session"),
        (BotError(ErrorCode.EXTRACTOR_CHANGED), "could not be processed right now"),
    ],
)
async def test_extraction_errors_become_failed_with_a_clear_message_and_no_retry(tmp_path, error, expected_text):
    ext = FakeExtractor(plan=[error])
    a = await build_app(tmp_path, extractor=ext)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == error.code.value
        assert ext.attempts == 1, "permanent errors must not be retried"
        assert expected_text in messages.final_text(a.notifier.finals[0][1])
        assert not (a.settings.work_dir / job.id).exists()
    finally:
        await a.shutdown()


async def test_transient_failures_are_retried_with_backoff_then_succeed(tmp_path):  # AT20
    ext = FakeExtractor(plan=[BotError(ErrorCode.DOWNLOAD_TIMEOUT), BotError(ErrorCode.RATE_LIMITED, retry_after=3), 2])
    a = await build_app(tmp_path, extractor=ext)
    try:
        job = await run_one(a)
        assert job.state is JobState.COMPLETED and ext.attempts == 3
        assert len(a.sleeps) == 2 and a.sleeps[1] >= 3  # Retry-After honoured
    finally:
        await a.shutdown()


async def test_retries_are_bounded(tmp_path):
    ext = FakeExtractor(plan=[BotError(ErrorCode.DOWNLOAD_TIMEOUT)] * 10)
    a = await build_app(tmp_path, extractor=ext, retry_max_attempts=2)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "DOWNLOAD_TIMEOUT"
        assert ext.attempts == 3  # 1 try + 2 retries, never more
    finally:
        await a.shutdown()


async def test_a_long_platform_rate_limit_is_reported_not_slept_through(tmp_path):
    ext = FakeExtractor(plan=[BotError(ErrorCode.RATE_LIMITED, retry_after=7200)])
    a = await build_app(tmp_path, extractor=ext)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "RATE_LIMITED"
        assert ext.attempts == 1 and a.sleeps == []
        assert "about 120 min" in messages.final_text(a.notifier.finals[0][1])
    finally:
        await a.shutdown()


@pytest.mark.parametrize("access", [Access.RESTRICTED, Access.UNKNOWN])
async def test_only_proven_public_content_is_fetched_fail_closed(tmp_path, access):
    ext = FakeExtractor(access=access)
    a = await build_app(tmp_path, extractor=ext)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "ACCESS_RESTRICTED"
        assert ext.fetch_calls == 0, "restricted/unknown content must never be downloaded"
    finally:
        await a.shutdown()


async def test_item_limit_is_enforced_before_any_download(tmp_path):
    ext = FakeExtractor(items=31)
    a = await build_app(tmp_path, extractor=ext, max_items_per_job=30)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "LIMIT_EXCEEDED"
        assert ext.fetch_calls == 0
    finally:
        await a.shutdown()


async def test_unexpected_exception_fails_the_job_without_killing_the_worker(tmp_path):
    ext = FakeExtractor(plan=[RuntimeError("bug with https://secret.example/token?x=1"), 1])
    a = await build_app(tmp_path, extractor=ext, allowed_user_ids=[OWNER, 222])
    try:
        job = await run_one(a, message_id=1)
        assert job.state is JobState.FAILED and job.error_code == "INTERNAL_ERROR"
        assert "secret.example" not in messages.final_text(a.notifier.finals[0][1])
        # the same worker keeps serving the next job
        await a.feed(message_update(GOOD, user_id=222, message_id=2))
        await a.drain()
        assert a.queue.running_count == 0
        assert len(a.notifier.finals) == 2 and a.notifier.finals[1][1].state is JobState.COMPLETED
    finally:
        await a.shutdown()


# --- untrusted extractor output ------------------------------------------------------------------------------------
class RogueExtractor(FakeExtractor):
    """An extractor that lies about the files it produced."""

    def __init__(self, kind: str, **kw):
        super().__init__(**kw)
        self.kind = kind

    async def fetch(self, request, manifest, workspace, deadline):
        res = await super().fetch(request, manifest, workspace, deadline)
        a0 = res.assets[0]
        if self.kind == "outside":
            outside = workspace.parent / f"outside-{uuid.uuid4().hex}.jpg"
            outside.write_bytes(b"x" * 5)
            bad = a0.model_copy(update={"local_path": outside.resolve(), "size_bytes": 5})
        elif self.kind == "size-lie":
            bad = a0.model_copy(update={"size_bytes": a0.size_bytes + 100})
        elif self.kind == "empty":
            a0.local_path.write_bytes(b"")
            bad = a0.model_copy(update={"size_bytes": 0})
        else:  # traversal
            bad = a0.model_copy(update={"local_path": (workspace / ".." / "sneaky.jpg")})
        return res.model_copy(update={"assets": [bad, *res.assets[1:]]})


@pytest.mark.parametrize("kind", ["outside", "size-lie", "empty", "traversal"])
async def test_lying_or_hostile_extractor_output_is_rejected_before_upload(tmp_path, kind):
    a = await build_app(tmp_path, extractor=RogueExtractor(kind))
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "MEDIA_INVALID"
        assert a.deliverer.calls == [], "nothing may be uploaded when validation fails"
    finally:
        await a.shutdown()


async def test_temp_byte_budget_is_enforced(tmp_path):
    a = await build_app(tmp_path, max_temp_bytes_per_job=1_000_000)

    class Big(FakeExtractor):
        async def fetch(self, request, manifest, workspace, deadline):
            res = await super().fetch(request, manifest, workspace, deadline)
            big = asset_in(workspace, 0, data=b"x" * 1_500_000)
            return res.model_copy(update={"assets": [big]})

    a.runner.extractor = Big(items=1)
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "LIMIT_EXCEEDED"
    finally:
        await a.shutdown()


# --- honest outcome states -----------------------------------------------------------------------------------------------
async def test_missing_item_is_partial_never_complete(tmp_path):  # AT18
    ext = FakeExtractor(items=5, drop={2})
    a = await build_app(tmp_path, extractor=ext)
    try:
        job = await run_one(a)
        assert job.state is JobState.PARTIAL and job.delivered_count == 4
        text = messages.final_text(a.notifier.finals[0][1])
        assert text.startswith("Delivered 4 of 5 items. Item 3 could not be retrieved.")
        assert "Original link: " + GOOD in text
    finally:
        await a.shutdown()


async def test_unproven_source_completeness_is_unverified(tmp_path):
    a = await build_app(tmp_path, extractor=FakeExtractor(items=3, completeness="unverified"))
    try:
        job = await run_one(a)
        assert job.state is JobState.UNVERIFIED and job.delivered_count == 3
        assert "could not verify whether the source contains additional media" in messages.final_text(
            a.notifier.finals[0][1]
        )
    finally:
        await a.shutdown()


async def test_ambiguous_send_is_delivery_unknown_and_offers_retry_but_never_auto_replays(tmp_path):
    deliverer = FakeDeliverer(
        outcome=DeliveryOutcome(confirmed_positions=[0], unknown_positions=[1], completeness="partial")
    )
    a = await build_app(tmp_path, deliverer=deliverer)
    try:
        job = await run_one(a)
        assert job.state is JobState.DELIVERY_UNKNOWN
        ((_, report),) = a.notifier.finals
        assert report.retry_offered and "can't tell whether 1 item arrived" in messages.final_text(report)
        assert len(deliverer.calls) == 1, "an uncertain delivery must never be replayed automatically"
    finally:
        await a.shutdown()


async def test_total_delivery_failure_is_failed_with_the_real_reason(tmp_path):
    outcome = DeliveryOutcome(
        failed_positions=[0, 1], failure_codes={0: ErrorCode.FILE_TOO_LARGE, 1: ErrorCode.FILE_TOO_LARGE}
    )
    a = await build_app(tmp_path, deliverer=FakeDeliverer(outcome=outcome))
    try:
        job = await run_one(a)
        assert job.state is JobState.FAILED and job.error_code == "FILE_TOO_LARGE"
        assert "exceeds the configured Telegram upload limit. No quality was reduced." in messages.final_text(
            a.notifier.finals[0][1]
        )
    finally:
        await a.shutdown()


async def test_partial_delivery_failure_reports_which_items(tmp_path):
    outcome = DeliveryOutcome(
        confirmed_positions=[0], failed_positions=[1], failure_codes={1: ErrorCode.FILE_TOO_LARGE}
    )
    a = await build_app(tmp_path, deliverer=FakeDeliverer(outcome=outcome))
    try:
        job = await run_one(a)
        assert job.state is JobState.PARTIAL
        text = messages.final_text(a.notifier.finals[0][1])
        assert "Item 2 could not be sent." in text and "no quality was reduced" in text
    finally:
        await a.shutdown()


@pytest.mark.parametrize(
    ("delivered", "failed", "unknown", "omissions", "completeness", "expected"),
    [
        (2, 0, 0, 0, "complete", JobState.COMPLETED),
        (2, 0, 0, 0, "unverified", JobState.UNVERIFIED),
        (2, 0, 0, 1, "complete", JobState.PARTIAL),
        (2, 0, 0, 0, "partial", JobState.PARTIAL),
        (1, 1, 0, 0, "complete", JobState.PARTIAL),
        (1, 0, 0, 0, "complete", JobState.PARTIAL),  # planned 2, confirmed 1, nothing explained
        (1, 0, 1, 0, "complete", JobState.DELIVERY_UNKNOWN),
        (0, 0, 2, 0, "complete", JobState.DELIVERY_UNKNOWN),
        (0, 2, 0, 0, "complete", JobState.FAILED),
        (0, 0, 0, 0, "complete", JobState.FAILED),
    ],
)
def test_final_state_is_never_optimistic(tmp_path, delivered, failed, unknown, omissions, completeness, expected):
    from app.core.contracts import ExtractionResult, ItemFailure

    assets = [asset_in(tmp_path, i) for i in range(2)]
    result = ExtractionResult(
        assets=assets,
        expected_count=2,
        completeness=completeness,
        omissions=[ItemFailure(position=9, code=ErrorCode.EXTRACTOR_CHANGED, message="m")] * omissions,
    )
    outcome = DeliveryOutcome(
        confirmed_positions=list(range(delivered)),
        failed_positions=list(range(delivered, delivered + failed)),
        unknown_positions=list(range(delivered, delivered + unknown)),
    )
    assert decide_final_state(result, outcome) is expected


# --- cancellation / interruption (AT19, AT21) -----------------------------------------------------------------------------
async def test_cancel_during_extraction_stops_it_and_cleans_up(tmp_path):
    ext = FakeExtractor()
    ext.block = asyncio.Event()
    a = await build_app(tmp_path, extractor=ext)
    try:
        await a.feed(message_update(GOOD))
        await asyncio.wait_for(ext.entered_fetch.wait(), 2)
        (ws,) = list(a.settings.work_dir.iterdir())
        assert ws.is_dir()
        await a.feed(message_update("/cancel"))
        await a.drain()
        job = job_of(a)
        assert job.state is JobState.CANCELLED and ext.cancelled
        assert not ws.exists(), "cancelled job's files must be removed"
        assert a.deliverer.calls == []
        assert a.queue.running_count == 0
    finally:
        await a.shutdown()


async def test_cancel_during_upload_reports_what_was_already_confirmed(tmp_path):
    a = await build_app(tmp_path, deliverer=None)
    a.runner.deliverer = FakeDeliverer(a.db, hang_after_first_batch=True)
    try:
        await a.feed(message_update(GOOD))
        await asyncio.wait_for(a.runner.deliverer.entered.wait(), 2)
        await a.feed(message_update("/cancel"))
        await a.drain()
        job = job_of(a)
        assert job.state is JobState.CANCELLED and job.delivered_count == 1
        text = messages.final_text(a.notifier.finals[0][1])
        assert text == "Cancelled. 1 item had already been sent."
        assert not (a.settings.work_dir / job.id).exists()
    finally:
        await a.shutdown()


async def test_shutdown_interrupts_the_job_cleans_up_and_offers_retry(tmp_path):
    ext = FakeExtractor()
    ext.block = asyncio.Event()
    a = await build_app(tmp_path, extractor=ext)
    await a.feed(message_update(GOOD))
    await asyncio.wait_for(ext.entered_fetch.wait(), 2)
    (ws,) = list(a.settings.work_dir.iterdir())
    await asyncio.wait_for(a.queue.stop(), 3)
    job = job_of(a)
    assert job.state is JobState.INTERRUPTED and job.error_code == "INTERRUPTED"
    assert not ws.exists()
    ((_, report),) = a.notifier.finals
    assert report.state is JobState.INTERRUPTED and report.retry_offered
    a.db.close()


async def test_job_cancelled_before_it_starts_is_a_noop(tmp_path):
    a = await build_app(tmp_path, start_workers=False)
    try:
        job, req, val = make_job(a.db, owner_id=OWNER)
        a.db.reserve_job(job, max_active_per_user=1)
        a.db.transition(job.id, JobState.CANCELLED)
        handle = JobHandle(job=job, request=req, validated=val)
        await a.runner.run(handle)  # must return quietly
        assert a.extractor.discover_calls == 0 and a.notifier.finals == []
        assert not (a.settings.work_dir / job.id).exists()
    finally:
        await a.shutdown()


async def test_a_failing_final_notification_does_not_mask_the_outcome(tmp_path):
    a = await build_app(tmp_path)
    a.notifier.fail_finish = True
    try:
        job = await run_one(a)
        assert job.state is JobState.COMPLETED
    finally:
        await a.shutdown()


async def test_active_workspace_is_never_swept_while_its_job_runs(tmp_path):
    import os

    ext = FakeExtractor()
    ext.block = asyncio.Event()
    a = await build_app(tmp_path, extractor=ext)
    try:
        await a.feed(message_update(GOOD))
        await asyncio.wait_for(ext.entered_fetch.wait(), 2)
        (ws,) = list(a.settings.work_dir.iterdir())
        os.utime(ws, (1, 1))  # ancient mtime: would be an orphan if it were not active
        assert a.workspaces.sweep() == []
        assert ws.exists()
        ext.block.set()
        await a.drain()
    finally:
        await a.shutdown()


async def test_two_users_run_in_parallel_but_each_user_is_serial(tmp_path):
    ext = FakeExtractor()
    ext.block = asyncio.Event()
    a = await build_app(tmp_path, extractor=ext, allowed_user_ids=[OWNER, 222, 333], max_concurrent_jobs=2)
    try:
        for uid in (OWNER, 222, 333):
            await a.feed(message_update(GOOD, user_id=uid, message_id=1))
        await asyncio.sleep(0.1)
        assert a.queue.running_count == 2 and a.queue.waiting_count == 1  # global cap = 2
        ext.block.set()
        await a.drain()
        assert len(a.notifier.finals) == 3
    finally:
        await a.shutdown()
