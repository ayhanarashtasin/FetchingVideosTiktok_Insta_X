"""T05.5: bounded retry/backoff with jitter, Retry-After handling; Deadline helper."""

from __future__ import annotations

import asyncio

import pytest

from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.retry import backoff_delay, retry_async


# --- backoff -----------------------------------------------------------------------------------
def test_backoff_grows_exponentially_with_bounded_jitter():
    lo = [backoff_delay(i, base=2, cap=60, rng=lambda: 0.0) for i in range(5)]
    hi = [backoff_delay(i, base=2, cap=60, rng=lambda: 1.0) for i in range(5)]
    assert hi == [2, 4, 8, 16, 32]  # upper envelope: base * 2**n
    assert lo == [1, 2, 4, 8, 16]  # jitter never goes below 50 %
    assert backoff_delay(10, base=2, cap=60, rng=lambda: 1.0) == 60  # capped


def test_retry_after_is_never_undercut():
    assert backoff_delay(0, base=2, cap=60, rng=lambda: 0.0, retry_after=30) == 30
    assert backoff_delay(5, base=2, cap=60, rng=lambda: 1.0, retry_after=1) == 60  # backoff larger than hint


# --- retry_async ---------------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.sleeps: list[float] = []

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def flaky(failures: int, exc: Exception):
    state = {"calls": 0}

    async def fn():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise exc
        return "ok"

    return fn, state


async def test_retries_a_transient_failure_then_succeeds():
    rec = Recorder()
    fn, state = flaky(2, BotError(ErrorCode.DOWNLOAD_TIMEOUT))
    got = await retry_async(fn, max_retries=2, base_delay=1, max_delay=10, sleep=rec.sleep, rng=lambda: 1.0)
    assert got == "ok" and state["calls"] == 3
    assert rec.sleeps == [1, 2]


async def test_stops_after_max_retries_and_raises_the_last_error():
    rec = Recorder()
    fn, state = flaky(99, BotError(ErrorCode.DOWNLOAD_TIMEOUT))
    with pytest.raises(BotError) as exc:
        await retry_async(fn, max_retries=2, base_delay=1, max_delay=10, sleep=rec.sleep)
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT
    assert state["calls"] == 3 and len(rec.sleeps) == 2  # bounded: 1 try + 2 retries


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.NOT_FOUND_OR_EXPIRED,
        ErrorCode.ACCESS_RESTRICTED,
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.INVALID_URL,
        ErrorCode.FILE_TOO_LARGE,
        ErrorCode.MEDIA_INVALID,
        ErrorCode.EXTRACTOR_CHANGED,
        ErrorCode.DELIVERY_UNKNOWN,
        ErrorCode.CANCELLED,
    ],
)
async def test_permanent_or_unsafe_errors_are_never_retried(code):
    rec = Recorder()
    fn, state = flaky(99, BotError(code))
    with pytest.raises(BotError):
        await retry_async(fn, max_retries=5, base_delay=1, max_delay=10, sleep=rec.sleep)
    assert state["calls"] == 1 and rec.sleeps == []


async def test_unexpected_exceptions_are_not_retried():
    fn, state = flaky(99, RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await retry_async(fn, max_retries=5, base_delay=0.01, max_delay=0.1)
    assert state["calls"] == 1


async def test_upload_failure_retried_only_when_declared_safe():
    rec = Recorder()
    fn, _ = flaky(1, BotError(ErrorCode.UPLOAD_FAILED, safe_to_retry=True))
    assert await retry_async(fn, max_retries=2, base_delay=1, max_delay=5, sleep=rec.sleep) == "ok"
    fn2, state2 = flaky(1, BotError(ErrorCode.UPLOAD_FAILED, safe_to_retry=False))
    with pytest.raises(BotError):
        await retry_async(fn2, max_retries=2, base_delay=1, max_delay=5, sleep=rec.sleep)
    assert state2["calls"] == 1


async def test_rate_limit_waits_at_least_retry_after():
    rec = Recorder()
    fn, _ = flaky(1, BotError(ErrorCode.RATE_LIMITED, retry_after=7))
    await retry_async(fn, max_retries=2, base_delay=1, max_delay=5, sleep=rec.sleep, rng=lambda: 0.0)
    assert rec.sleeps == [7]


async def test_rate_limit_asking_for_a_long_pause_is_surfaced_not_slept():
    rec = Recorder()
    fn, state = flaky(5, BotError(ErrorCode.RATE_LIMITED, retry_after=3600))
    with pytest.raises(BotError) as exc:
        await retry_async(fn, max_retries=3, base_delay=1, max_delay=5, sleep=rec.sleep, max_retry_after=60)
    assert exc.value.code is ErrorCode.RATE_LIMITED and exc.value.retry_after == 3600
    assert state["calls"] == 1 and rec.sleeps == []


async def test_cancellation_is_never_swallowed():
    async def fn():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(fn, max_retries=3, base_delay=0.01, max_delay=0.1)


async def test_on_retry_callback_sees_attempt_delay_and_error():
    seen = []
    fn, _ = flaky(1, BotError(ErrorCode.DOWNLOAD_TIMEOUT))
    await retry_async(
        fn,
        max_retries=2,
        base_delay=2,
        max_delay=9,
        rng=lambda: 1.0,
        sleep=Recorder().sleep,
        on_retry=lambda a, d, e: seen.append((a, d, e.code)),
    )
    assert seen == [(1, 2, ErrorCode.DOWNLOAD_TIMEOUT)]


# --- Deadline ---------------------------------------------------------------------------------------
def test_deadline_counts_down_and_raises_when_expired():
    t = [100.0]
    d = Deadline(10, clock=lambda: t[0])
    assert d.remaining() == 10 and not d.expired()
    assert d.cap(30) == 10 and d.cap(3) == 3
    t[0] += 9.5
    assert 0.4 < d.remaining() < 0.6
    t[0] += 1
    assert d.expired()
    with pytest.raises(BotError) as exc:
        d.check()
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT
    with pytest.raises(BotError):
        d.cap(5)


def test_error_flags_follow_the_error_contract():
    assert BotError(ErrorCode.DOWNLOAD_TIMEOUT).auto_retryable
    assert not BotError(ErrorCode.DELIVERY_UNKNOWN).auto_retryable  # owner decides
    assert BotError(ErrorCode.DELIVERY_UNKNOWN).owner_retryable
    assert not BotError(ErrorCode.ACCESS_RESTRICTED).fallback_allowed  # never evade a control
    assert not BotError(ErrorCode.RATE_LIMITED).fallback_allowed
    assert not BotError(ErrorCode.AUTH_REQUIRED).fallback_allowed
    assert BotError(ErrorCode.EXTRACTOR_CHANGED).fallback_allowed
    assert not BotError(ErrorCode.NOT_FOUND_OR_EXPIRED).owner_retryable
    wire = BotError(ErrorCode.RATE_LIMITED, "secret detail", retry_after=12, position=3).to_wire()
    assert "secret detail" not in str(wire)  # internal detail never crosses the worker boundary
    back = BotError.from_wire(wire)
    assert (back.code, back.retry_after, back.position) == (ErrorCode.RATE_LIMITED, 12, 3)
