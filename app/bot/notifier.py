"""Telegram implementation of the Notifier port: one status message per job, edited coarsely
(throttled), then replaced by the final outcome or removed on full success (FR-10, T02.8)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyParameters

from app.bot import messages
from app.bot.keyboards import retry_keyboard
from app.bot.messages import FinalReport
from app.core.contracts import JobRecord
from app.logging_setup import log_event
from app.storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class _Status:
    message_id: int | None = None
    last_text: str | None = None
    last_edit: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        *,
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._db = db
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._states: dict[str, _Status] = {}

    def _state(self, job_id: str) -> _Status:
        return self._states.setdefault(job_id, _Status())

    # ------------------------------------------------------------------ Notifier port
    async def ack(self, job: JobRecord) -> None:
        await self.status(job, messages.FETCHING)

    async def status(self, job: JobRecord, text: str) -> None:
        st = self._state(job.id)
        async with st.lock:  # ack() and the runner's first update can race: serialise per job
            if st.message_id is not None and st.last_text == text:
                return
            try:
                if st.message_id is None:
                    st.message_id = await self._send(job, text)
                    self._db.set_status_message(job.id, st.message_id)
                else:
                    wait = self._min_interval - (self._clock() - st.last_edit)
                    if wait > 0:
                        await self._sleep(wait)
                    await self._edit(job, st.message_id, text, None)
                st.last_text = text
                st.last_edit = self._clock()
            except TelegramAPIError as exc:  # a status update must never fail the job
                log_event(log, logging.DEBUG, "status update failed", job_id=job.id, error_type=type(exc).__name__)

    async def finish(self, job: JobRecord, report: FinalReport) -> None:
        st = self._state(job.id)
        text = messages.final_text(report)
        keyboard = retry_keyboard(job.id) if report.retry_offered else None
        async with st.lock:
            try:
                if text is None:
                    if st.message_id is not None:
                        with contextlib.suppress(TelegramAPIError):
                            await self._bot.delete_message(job.chat_id, st.message_id)
                    return
                if st.message_id is not None:
                    try:
                        await self._edit(job, st.message_id, text, keyboard)
                        return
                    except TelegramAPIError:
                        pass  # fall through: send a fresh message instead
                await self._send(job, text, keyboard)
            except TelegramAPIError as exc:  # the notifier must never raise into the job
                log_event(log, logging.WARNING, "could not deliver final status", job_id=job.id,
                          error_type=type(exc).__name__)
            finally:
                self._states.pop(job.id, None)

    # ------------------------------------------------------------------ low level
    async def _send(self, job: JobRecord, text: str, keyboard: InlineKeyboardMarkup | None = None) -> int:
        for attempt in (0, 1):
            try:
                reply_to = ReplyParameters(message_id=job.origin_message_id, allow_sending_without_reply=True)
                msg = await self._bot.send_message(job.chat_id, text, reply_markup=keyboard, reply_parameters=reply_to)
                return msg.message_id
            except TelegramRetryAfter as exc:
                if attempt == 1:
                    raise
                await self._sleep(min(float(exc.retry_after), 10.0))
        raise AssertionError("unreachable")

    async def _edit(self, job: JobRecord, message_id: int, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
        try:
            await self._bot.edit_message_text(text, chat_id=job.chat_id, message_id=message_id, reply_markup=keyboard)
        except TelegramRetryAfter as exc:
            await self._sleep(min(float(exc.retry_after), 10.0))
            await self._bot.edit_message_text(text, chat_id=job.chat_id, message_id=message_id, reply_markup=keyboard)
