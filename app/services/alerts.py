"""Optional operational alerts (T05.10, P1): repeated extractor failures and low disk space.

Alerts go to the configured owner(s) only, are rate-limited (one per episode / cooldown) and NEVER contain a
source URL, a media file or any user content - only counters, thresholds and generic guidance.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from aiogram import Bot

from app.logging_setup import log_event

log = logging.getLogger(__name__)


class AlertMonitor:
    def __init__(
        self,
        bot: Bot,
        owners: Iterable[int],
        *,
        failure_threshold: int,
        min_free_bytes: int,
        cooldown_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._owners = list(owners)
        self._threshold = failure_threshold
        self._min_free = min_free_bytes
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failure_alerted = False
        self._last_disk_alert: float | None = None

    async def check(self, *, consecutive_failures: int, free_bytes: int) -> list[str]:
        """Evaluate current conditions; returns the alert texts that were sent this call."""
        sent: list[str] = []
        if consecutive_failures >= self._threshold:
            if not self._failure_alerted:
                self._failure_alerted = True
                sent.append(
                    f"Alert: {consecutive_failures} extraction jobs in a row have failed. A platform or one of the "
                    "downloader tools may have changed. Check the logs and consider updating the extractors "
                    "(see ops/troubleshooting.md)."
                )
        else:
            self._failure_alerted = False  # recovered: a future streak will alert again

        now = self._clock()
        if free_bytes < self._min_free and (
            self._last_disk_alert is None or now - self._last_disk_alert >= self._cooldown
        ):
            self._last_disk_alert = now
            sent.append(f"Alert: low disk space on the bot host ({free_bytes // 1_000_000} MB free).")

        for text in sent:
            for owner in self._owners:
                try:
                    await self._bot.send_message(owner, text)
                except Exception as exc:
                    log_event(log, logging.WARNING, "could not deliver alert", error_type=type(exc).__name__)
        return sent
