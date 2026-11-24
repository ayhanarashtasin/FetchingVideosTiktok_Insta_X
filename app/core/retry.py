"""Bounded retry with exponential backoff and jitter (T05.5, FR-16).

Only failures that declare themselves safely retryable (`BotError.auto_retryable`) are retried;
a server-provided `retry_after` is honoured (never undercut) and a request to wait longer than
`max_retry_after` is surfaced to the user instead of being slept through. Cancellation is never
swallowed.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from app.core.errors import BotError


def backoff_delay(
    attempt: int,
    *,
    base: float,
    cap: float,
    rng: Callable[[], float] = random.random,
    retry_after: float | None = None,
) -> float:
    """Delay before retry number `attempt + 1` (attempt is zero-based)."""
    exponential = min(cap, base * (2**attempt))
    jittered = exponential * (0.5 + rng() / 2)  # between 50% and 100% of the exponential step
    if retry_after is not None:
        return max(retry_after, jittered)  # never retry sooner than the server asked
    return jittered


def default_is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, BotError) and exc.auto_retryable


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    max_retry_after: float = 60.0,
    is_retryable: Callable[[BaseException], bool] = default_is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable(exc):
                raise
            retry_after = exc.retry_after if isinstance(exc, BotError) else None
            if retry_after is not None and retry_after > max_retry_after:
                raise  # the server wants a long pause: report it, do not sleep through it
            delay = backoff_delay(attempt, base=base_delay, cap=max_delay, rng=rng, retry_after=retry_after)
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            await sleep(delay)
