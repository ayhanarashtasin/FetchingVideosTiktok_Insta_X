"""A monotonic deadline shared by the steps of one extraction attempt."""

from __future__ import annotations

import time
from collections.abc import Callable

from app.core.errors import BotError, ErrorCode


class Deadline:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.total = float(seconds)
        self._end = clock() + self.total

    def remaining(self) -> float:
        return self._end - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "extraction deadline exceeded")

    def cap(self, seconds: float) -> float:
        """Clamp a per-step timeout to what is left; raises if nothing is left."""
        self.check()
        return max(0.1, min(float(seconds), self.remaining()))
