"""Telegram delivery (T04.4, T04.5, T04.6, T04.8, T04.10).

Executes a `DeliveryPlan` against the Bot API and reports an honest `DeliveryOutcome`.

Principles
* Uploads read from a file DESCRIPTOR opened with O_NOFOLLOW and verified (regular file, expected size)
  after validation, so the untrusted worker cannot swap a path for a symlink to a bot secret in between.
* Outcomes are classified, never assumed. A definite Telegram rejection (400/403/413/429) means nothing
  was delivered; a network error / timeout / 5xx is AMBIGUOUS - the message may have been sent - and is
  recorded as `unknown`, after which no further batch is sent and nothing is replayed automatically.
* 429 (flood wait) is the one retryable case: Telegram guarantees it processed nothing, so waiting the
  requested time and repeating is safe. Bounded to two retries.
* If Telegram refuses a NATIVE photo/video (400), the same bytes are re-sent once as a DOCUMENT - a
  lossless fallback, never a re-encode.
* Each batch outcome is written to SQLite immediately, so even a crash mid-album leaves an accurate
  record of what was confirmed; a cancellation during an in-flight send is recorded as `unknown`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    InlineKeyboardMarkup,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)

from app.bot.keyboards import original_keyboard
from app.config import Settings
from app.core.contracts import (
    Completeness,
    DeliveryOutcome,
    ExtractionRequest,
    ExtractionResult,
    JobRecord,
)
from app.core.errors import BotError, ErrorCode
from app.logging_setup import log_event
from app.media.delivery_plan import Caps, DeliveryPlan, PlannedItem, SendAs, Step, plan_delivery
from app.media.probe import HASH_VIDEO_LIMIT
from app.services.cleanup import WorkspaceManager
from app.storage.db import Database

log = logging.getLogger(__name__)
MAX_FLOOD_RETRIES = 2
MAX_FLOOD_WAIT = 60.0
RECENT_FILE_LIMIT = 128

type FileKey = tuple[int, int, SendAs, str | None, str | None]


class FdInputFile(InputFile):
    """Streams an already-validated file descriptor. Re-readable (positional reads), so a flood-wait
    retry can send the same file again without reopening any path."""

    def __init__(self, fd: int, filename: str, size: int, chunk_size: int = 1 << 20) -> None:
        super().__init__(filename=filename, chunk_size=chunk_size)
        self._fd = fd
        self._size = size
        self.read_sha256: str | None = None

    @property
    def size(self) -> int:
        return self._size

    def _read_at(self, offset: int, n: int) -> bytes:
        if hasattr(os, "pread"):
            return os.pread(self._fd, n, offset)
        os.lseek(self._fd, offset, os.SEEK_SET)  # Windows: os.pread unavailable
        return os.read(self._fd, n)

    async def read(self, bot: Bot) -> AsyncGenerator[bytes, None]:
        self.read_sha256 = None
        digest = hashlib.sha256() if self._size <= HASH_VIDEO_LIMIT else None
        offset = 0
        while offset < self._size:
            chunk = await asyncio.to_thread(self._read_at, offset, min(self.chunk_size, self._size - offset))
            if not chunk:
                break
            offset += len(chunk)
            if digest is not None:
                digest.update(chunk)
            yield chunk
        if digest is not None and offset == self._size:
            self.read_sha256 = digest.hexdigest()


@dataclass
class StepResult:
    outcome: str  # "sent" | "failed" | "unknown"
    message_ids: list[int] = field(default_factory=list)
    code: ErrorCode | None = None
    abort: bool = False  # stop sending further batches


def classify_send_error(exc: BaseException) -> StepResult:
    """Map a Bot API exception to a definite failure or an ambiguous (unknown) outcome."""
    # NOTE: TelegramEntityTooLarge subclasses TelegramNetworkError, so it MUST be checked first: a 413
    # is a definite rejection, while a plain network error is ambiguous.
    if isinstance(exc, TelegramEntityTooLarge):
        return StepResult("failed", code=ErrorCode.FILE_TOO_LARGE)
    if isinstance(exc, TelegramRetryAfter):
        return StepResult("failed", code=ErrorCode.RATE_LIMITED, abort=True)
    if isinstance(exc, TelegramForbiddenError | TelegramUnauthorizedError):
        return StepResult("failed", code=ErrorCode.UPLOAD_FAILED, abort=True)  # blocked/revoked: stop
    if isinstance(exc, TelegramBadRequest):
        return StepResult("failed", code=ErrorCode.UPLOAD_FAILED)
    if isinstance(exc, TelegramNetworkError | TelegramServerError | TimeoutError | aiohttp.ClientError | OSError):
        return StepResult("unknown", code=ErrorCode.DELIVERY_UNKNOWN, abort=True)
    if isinstance(exc, TelegramAPIError):
        return StepResult("unknown", code=ErrorCode.DELIVERY_UNKNOWN, abort=True)  # unrecognised: be conservative
    raise exc


@dataclass
class _Accumulator:
    confirmed: list[int] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    failed: dict[int, ErrorCode] = field(default_factory=dict)
    unknown: list[int] = field(default_factory=list)


class TelegramDeliverer:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        workspaces: WorkspaceManager,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._db = db
        self._s = settings
        self._ws = workspaces
        self._sleep = sleep
        self._file_ids: OrderedDict[FileKey, tuple[str, float]] = OrderedDict()

    # ------------------------------------------------------------------ public
    async def deliver(
        self, job: JobRecord, request: ExtractionRequest, result: ExtractionResult, workspace: Path
    ) -> DeliveryOutcome:
        caps = Caps(
            photo_bytes=self._s.photo_cap_bytes, file_bytes=self._s.file_cap_bytes, native_hevc=self._s.native_hevc
        )
        plan = plan_delivery(result.assets, mode=job.delivery_mode, caps=caps, url=job.original_url, job_id=job.id)
        acc = _Accumulator()
        for r in plan.rejected:
            acc.failed[r.position] = r.code
            log_event(log, logging.INFO, "item not deliverable", job_id=job.id, position=r.position, code=r.code.value)

        stop = False
        for batch_index, step in enumerate(plan.steps):
            if stop:  # an earlier batch had an uncertain/blocked outcome: do not pile more sends on top
                for pos in step.positions:
                    acc.failed.setdefault(pos, ErrorCode.UPLOAD_FAILED)
                continue
            keyboard = original_keyboard(job.id) if (plan.button_on_media and len(plan.steps) == 1) else None
            res = await self._send_step(job, step, workspace, batch_index, keyboard=keyboard, first=batch_index == 0)
            self._db.record_delivery(job.id, batch_index, step.positions, res.outcome, res.message_ids, res.code)
            if res.outcome == "sent":
                acc.confirmed += step.positions
                acc.message_ids += res.message_ids
            elif res.outcome == "failed":
                for pos in step.positions:
                    acc.failed[pos] = res.code or ErrorCode.UPLOAD_FAILED
            else:
                acc.unknown += step.positions
            stop = stop or res.abort

        if plan.link_message and acc.confirmed:
            await self._send_link(job, plan, acc)
        return self._outcome(acc, len(result.assets))

    # ------------------------------------------------------------------ one batch
    async def _send_step(
        self,
        job: JobRecord,
        step: Step,
        workspace: Path,
        batch_index: int,
        *,
        keyboard: InlineKeyboardMarkup | None,
        first: bool,
    ) -> StepResult:
        fds: list[int] = []
        try:
            try:
                files = self._open_files(job, step.items, workspace, fds)
            except BotError as err:
                return StepResult("failed", code=err.code)
            try:
                return await self._send_files(job, step.items, step.caption, files, keyboard, first)
            except BotError:
                raise
            except TelegramBadRequest:
                if not any(i.send_as is not SendAs.DOCUMENT for i in step.items):
                    return StepResult("failed", code=ErrorCode.UPLOAD_FAILED)
                # Telegram refused the native photo/video: send the SAME bytes as documents instead.
                log_event(log, logging.INFO, "native send rejected; falling back to documents", job_id=job.id)
                as_docs = tuple(
                    PlannedItem(i.asset, SendAs.DOCUMENT, "telegram rejected the native upload") for i in step.items
                )
                try:
                    return await self._send_files(job, as_docs, step.caption, files, keyboard, first)
                except BotError:
                    raise
                except Exception as exc:
                    return classify_send_error(exc)
            except Exception as exc:
                return classify_send_error(exc)
        except asyncio.CancelledError:
            # Cancelled while a send may have been in flight: Telegram might still deliver it.
            self._db.record_delivery(job.id, batch_index, step.positions, "unknown", [], ErrorCode.CANCELLED)
            raise
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _open_files(
        self, job: JobRecord, items: tuple[PlannedItem, ...], workspace: Path, fds: list[int]
    ) -> list[FdInputFile]:
        files: list[FdInputFile] = []
        for item in items:
            a = item.asset
            fd = self._ws.open_validated(workspace, a.local_path, expected_size=a.size_bytes)
            fds.append(fd)
            name = f"{job.platform.value}_{a.position + 1:02d}{a.local_path.suffix.lower()}"
            files.append(FdInputFile(fd, name, a.size_bytes))
        return files

    async def _send_files(
        self,
        job: JobRecord,
        items: tuple[PlannedItem, ...],
        caption: str | None,
        files: list[FdInputFile],
        keyboard: InlineKeyboardMarkup | None,
        first: bool,
    ) -> StepResult:
        keys: list[FileKey] = []
        sources: list[FdInputFile | str] = []
        for item, file in zip(items, files, strict=True):
            key = (job.owner_id, job.chat_id, item.send_as, file.filename, item.asset.sha256)
            keys.append(key)
            cached = self._file_ids.get(key)
            if cached is not None and cached[1] > time.monotonic():
                # Verify the current bytes from the open descriptor before reusing Telegram's copy.
                async for _ in file.read(self._bot):
                    pass
                if file.read_sha256 == item.asset.sha256:
                    sources.append(cached[0])
                    continue
            sources.append(file)

        try:
            sent = await self._attempt(job, items, caption, sources, keyboard, first)
        except TelegramBadRequest:
            reused = [key for key, source in zip(keys, sources, strict=True) if isinstance(source, str)]
            if not reused:
                raise
            for key in reused:
                self._file_ids.pop(key, None)
            # A 400 is a definite rejection. Retry with the validated files before changing media type.
            sources = list(files)
            sent = await self._attempt(job, items, caption, sources, keyboard, first)

        if len(sent) == len(items):
            expires = time.monotonic() + self._s.job_metadata_ttl_hours * 3600
            for key, source, message in zip(keys, sources, sent, strict=True):
                if isinstance(source, str) or source.read_sha256 is None or source.read_sha256 != key[-1]:
                    continue
                media = getattr(message, key[2].value)
                if isinstance(media, list):
                    media = media[-1] if media else None
                if media is not None and media.file_id:
                    self._file_ids[key] = (media.file_id, expires)
                    self._file_ids.move_to_end(key)
                    if len(self._file_ids) > RECENT_FILE_LIMIT:
                        self._file_ids.popitem(last=False)

        return StepResult("sent", message_ids=[msg.message_id for msg in sent])

    async def _attempt(
        self,
        job: JobRecord,
        items: tuple[PlannedItem, ...],
        caption: str | None,
        files: Sequence[FdInputFile | str],
        keyboard: InlineKeyboardMarkup | None,
        first: bool,
    ) -> list[Message]:
        reply = ReplyParameters(message_id=job.origin_message_id, allow_sending_without_reply=True) if first else None
        timeout = self._s.upload_timeout_seconds
        common: dict[str, Any] = {"chat_id": job.chat_id, "request_timeout": timeout}
        if reply is not None:
            common["reply_parameters"] = reply

        async def call() -> list[Message]:
            if len(items) == 1:
                item, file = items[0], files[0]
                a = item.asset
                single = {**common, "caption": caption, "reply_markup": keyboard}
                if item.send_as is SendAs.PHOTO:
                    msg = await self._bot.send_photo(photo=file, **single)
                elif item.send_as is SendAs.VIDEO:
                    msg = await self._bot.send_video(
                        video=file, width=a.width, height=a.height,
                        duration=round(a.duration_seconds) if a.duration_seconds else None,
                        supports_streaming=True, **single,
                    )  # fmt: skip
                else:
                    msg = await self._bot.send_document(document=file, disable_content_type_detection=True, **single)
                return [msg]
            media: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
            for index, (item, file) in enumerate(zip(items, files, strict=True)):
                cap = caption if index == 0 else None  # a media group shows the first item's caption
                a = item.asset
                if item.send_as is SendAs.PHOTO:
                    media.append(InputMediaPhoto(media=file, caption=cap))
                elif item.send_as is SendAs.VIDEO:
                    media.append(
                        InputMediaVideo(
                            media=file, caption=cap, width=a.width, height=a.height,
                            duration=round(a.duration_seconds) if a.duration_seconds else None,
                            supports_streaming=True,
                        )
                    )  # fmt: skip
                else:
                    media.append(InputMediaDocument(media=file, caption=cap, disable_content_type_detection=True))
            return await self._bot.send_media_group(media=media, **common)

        return await self._with_flood_wait(call)

    async def _with_flood_wait(self, call: Callable[[], Awaitable[list[Message]]]) -> list[Message]:
        """Retry ONLY on 429: Telegram guarantees it processed nothing, so repeating cannot duplicate."""
        for attempt in range(MAX_FLOOD_RETRIES + 1):
            try:
                return await call()
            except TelegramRetryAfter as exc:
                if attempt == MAX_FLOOD_RETRIES or float(exc.retry_after) > MAX_FLOOD_WAIT:
                    raise
                await self._sleep(float(exc.retry_after) + 1)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ extras
    async def _send_link(self, job: JobRecord, plan: DeliveryPlan, acc: _Accumulator) -> None:
        """The separate source-link message under an album. The media is already delivered, so a failure
        here is logged and never changes the outcome."""
        assert plan.link_message is not None
        keyboard = original_keyboard(job.id) if plan.button_on_link else None
        try:
            msg = await self._bot.send_message(
                job.chat_id, plan.link_message, reply_markup=keyboard, request_timeout=self._s.upload_timeout_seconds
            )
            acc.message_ids.append(msg.message_id)
        except Exception as exc:
            log_event(log, logging.WARNING, "could not send the source-link message", job_id=job.id,
                      error_type=type(exc).__name__)  # fmt: skip

    @staticmethod
    def _outcome(acc: _Accumulator, total: int) -> DeliveryOutcome:
        if acc.confirmed and not acc.failed and not acc.unknown and len(acc.confirmed) == total:
            completeness = Completeness.COMPLETE
        elif acc.confirmed:
            completeness = Completeness.PARTIAL
        else:
            completeness = Completeness.FAILED
        return DeliveryOutcome(
            confirmed_positions=sorted(acc.confirmed),
            telegram_message_ids=acc.message_ids,
            failed_positions=sorted(acc.failed),
            unknown_positions=sorted(acc.unknown),
            completeness=completeness,
            failure_codes=dict(sorted(acc.failed.items())),
        )
