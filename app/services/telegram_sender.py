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
    DeliveryMode,
    DeliveryOutcome,
    ExtractionRequest,
    ExtractionResult,
    ItemKind,
    JobRecord,
)
from app.core.errors import BotError, ErrorCode
from app.extractors.base import DirectUrlItem
from app.logging_setup import log_event
from app.media.delivery_plan import (
    CAPTION_LIMIT,
    GROUP_MAX,
    Caps,
    DeliveryPlan,
    PlannedItem,
    SendAs,
    Step,
    caption_for,
    display_url,
    original_command_line,
    plan_delivery,
)
from app.media.probe import HASH_VIDEO_LIMIT
from app.services.cleanup import WorkspaceManager
from app.storage.db import CachedMediaItem, CachedPost, Database

log = logging.getLogger(__name__)
MAX_FLOOD_RETRIES = 2
MAX_FLOOD_WAIT = 60.0
RECENT_FILE_LIMIT = 128
DELIVERED_CACHE_LIMIT = 64

type FileKey = tuple[int, int, SendAs, str | None, str | None]


def _extract_file_id(message: Message, send_as: SendAs) -> str | None:
    media = getattr(message, send_as.value, None)
    if isinstance(media, list):
        media = media[-1] if media else None
    fid = getattr(media, "file_id", None)
    return str(fid) if fid else None


def _res_tag(width: int | None, height: int | None) -> str | None:
    if width and height:
        return f"{min(width, height)}p"
    if height:
        return f"{height}p"
    return None


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
    cached_items: list[CachedMediaItem] = field(default_factory=list)


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
        self._delivered_cache: OrderedDict[str, list[CachedMediaItem]] = OrderedDict()

    def take_confirmed_cache_items(self, job_id: str) -> list[CachedMediaItem] | None:
        return self._delivered_cache.pop(job_id, None)

    def _remember_delivered(self, job_id: str, items: list[CachedMediaItem], expected_total: int) -> None:
        if len(items) == expected_total and expected_total > 0 and all(i.file_id for i in items):
            ordered = sorted(items, key=lambda i: i.position)
            self._delivered_cache[job_id] = ordered
            self._delivered_cache.move_to_end(job_id)
            while len(self._delivered_cache) > DELIVERED_CACHE_LIMIT:
                self._delivered_cache.popitem(last=False)

    # ------------------------------------------------------------------ public
    async def deliver(
        self, job: JobRecord, request: ExtractionRequest, result: ExtractionResult, workspace: Path
    ) -> DeliveryOutcome:
        caps = Caps(
            photo_bytes=self._s.photo_cap_bytes, file_bytes=self._s.file_cap_bytes, native_hevc=self._s.native_hevc
        )
        plan = plan_delivery(result.assets, mode=job.delivery_mode, caps=caps, url=job.original_url, job_id=job.id)
        acc = _Accumulator()
        captured: list[CachedMediaItem] = []
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
                captured += res.cached_items
            elif res.outcome == "failed":
                for pos in step.positions:
                    acc.failed[pos] = res.code or ErrorCode.UPLOAD_FAILED
            else:
                acc.unknown += step.positions
            stop = stop or res.abort

        if plan.link_message and acc.confirmed:
            await self._send_link(job, plan, acc)
        outcome = self._outcome(acc, len(result.assets))
        if outcome.completeness is Completeness.COMPLETE:
            self._remember_delivered(job.id, captured, len(result.assets))
        return outcome

    async def deliver_cached(self, job: JobRecord, cached: CachedPost) -> DeliveryOutcome | None:
        """Deliver a previously cached post using Telegram file_ids directly.

        Returns `None` if batch 0 is definitely rejected (`TelegramBadRequest`) before anything is sent,
        so the caller can evict the stale cache entry and fall back to fresh extraction seamlessly.
        """
        items = sorted(cached.items, key=lambda i: i.position)
        if not items:
            return None
        # Chunk into contiguous same-family runs (media vs document), max 10 per group.
        runs: list[list[CachedMediaItem]] = []
        for it in items:
            fam = "document" if it.send_as == SendAs.DOCUMENT.value else "media"
            if runs and ("document" if runs[-1][0].send_as == SendAs.DOCUMENT.value else "media") == fam:
                runs[-1].append(it)
            else:
                runs.append([it])
        batches = [run[i : i + GROUP_MAX] for run in runs for i in range(0, len(run), GROUP_MAX)]
        has_photo = any(i.kind is ItemKind.PHOTO for i in items)
        button_allowed = job.delivery_mode is DeliveryMode.MEDIA and has_photo

        captions: list[str | None] = [
            caption_for(job.original_url, [r for i in b if (r := _res_tag(i.width, i.height))]) for b in batches
        ]
        embedded_command = False
        if len(batches) == 1 and len(batches[0]) > 1 and button_allowed and captions[0] is not None:
            with_cmd = f"{captions[0]}\n{original_command_line(job.id)}"
            if len(with_cmd) <= CAPTION_LIMIT:
                captions[0] = with_cmd
                embedded_command = True
        all_res = list(dict.fromkeys(r for i in items if (r := _res_tag(i.width, i.height))))
        needs_link = (
            len(batches) > 1
            or any(c is None for c in captions)
            or (any(len(b) > 1 for b in batches) and button_allowed and not embedded_command)
        )
        clean = display_url(job.original_url)
        link_text = f"{clean}\n{'/'.join(all_res)}" if all_res else clean
        button_on_media = not needs_link and button_allowed and not embedded_command
        button_on_link = needs_link and button_allowed

        acc = _Accumulator()
        stop = False
        for batch_index, (batch, cap) in enumerate(zip(batches, captions, strict=True)):
            positions = [i.position for i in batch]
            if stop:
                for pos in positions:
                    acc.failed.setdefault(pos, ErrorCode.UPLOAD_FAILED)
                continue
            kb = original_keyboard(job.id) if (button_on_media and len(batches) == 1) else None
            try:
                sent = await self._send_ref_batch(
                    job,
                    [(SendAs(i.send_as), i.file_id, i.width, i.height, i.duration_seconds) for i in batch],
                    cap,
                    kb,
                    first=batch_index == 0,
                )
            except asyncio.CancelledError:
                self._db.record_delivery(job.id, batch_index, positions, "unknown", [], ErrorCode.CANCELLED)
                raise
            except TelegramBadRequest:
                if batch_index == 0 and not acc.confirmed:
                    bad_fids = {i.file_id for i in batch}
                    for k in [k for k, (fid, _exp) in self._file_ids.items() if fid in bad_fids]:
                        self._file_ids.pop(k, None)
                    return None  # stale file_id, nothing was sent -> caller evicts & extracts fresh
                res = StepResult("failed", code=ErrorCode.UPLOAD_FAILED)
            except Exception as exc:
                res = classify_send_error(exc)
            else:
                res = StepResult("sent", message_ids=[m.message_id for m in sent])
            self._db.record_delivery(job.id, batch_index, positions, res.outcome, res.message_ids, res.code)
            if res.outcome == "sent":
                acc.confirmed += positions
                acc.message_ids += res.message_ids
            elif res.outcome == "failed":
                for pos in positions:
                    acc.failed[pos] = res.code or ErrorCode.UPLOAD_FAILED
            else:
                acc.unknown += positions
            stop = stop or res.abort

        if needs_link and acc.confirmed:
            await self._send_link_text(job, link_text, button_on_link, acc)
        return self._outcome(acc, len(items))

    async def deliver_direct_urls(self, job: JobRecord, items: list[DirectUrlItem]) -> DeliveryOutcome | None:
        """Let Telegram fetch public cookie-free CDN URLs directly for single-batch Media-mode posts.

        Atomic: only runs on 1..10 items (one Telegram API call). On `TelegramBadRequest` (e.g. URL >20 MB,
        CDN blocked from Telegram DC, or bad codec), nothing was sent and `None` is returned so the caller
        falls back seamlessly to local download + probe + multipart upload.
        """
        if job.delivery_mode is not DeliveryMode.MEDIA or not (1 <= len(items) <= GROUP_MAX):
            return None
        ordered = sorted(items, key=lambda i: i.position)
        if any(it.exceeds_url_cap() for it in ordered):
            return None  # Telegram refuses URL fetches over its 5 MB photo / 20 MB video ceiling

        has_photo = any(i.kind is ItemKind.PHOTO for i in ordered)
        button_allowed = has_photo
        resolutions = [r for i in ordered if (r := _res_tag(i.width, i.height))]
        cap = caption_for(job.original_url, resolutions)
        embedded_command = False
        if len(ordered) > 1 and button_allowed and cap is not None:
            with_cmd = f"{cap}\n{original_command_line(job.id)}"
            if len(with_cmd) <= CAPTION_LIMIT:
                cap = with_cmd
                embedded_command = True
        all_res = list(dict.fromkeys(resolutions))
        needs_link = cap is None or (len(ordered) > 1 and button_allowed and not embedded_command)
        clean = display_url(job.original_url)
        link_text = f"{clean}\n{'/'.join(all_res)}" if all_res else clean
        button_on_media = not needs_link and button_allowed and not embedded_command
        button_on_link = needs_link and button_allowed
        kb = original_keyboard(job.id) if button_on_media else None
        positions = [i.position for i in ordered]
        specs = [
            (
                SendAs.PHOTO if i.kind is ItemKind.PHOTO else SendAs.VIDEO,
                i.url,
                i.width,
                i.height,
                i.duration,
            )
            for i in ordered
        ]
        try:
            sent = await self._send_ref_batch(job, specs, cap, kb, first=True)
        except asyncio.CancelledError:
            self._db.record_delivery(job.id, 0, positions, "unknown", [], ErrorCode.CANCELLED)
            raise
        except TelegramBadRequest:
            return None  # Telegram definitely did not send; caller falls back to local download+upload
        except Exception as exc:
            res = classify_send_error(exc)
            self._db.record_delivery(job.id, 0, positions, res.outcome, [], res.code)
            acc = _Accumulator()
            if res.outcome == "failed":
                for pos in positions:
                    acc.failed[pos] = res.code or ErrorCode.UPLOAD_FAILED
            else:
                acc.unknown += positions
            return self._outcome(acc, len(ordered))

        msg_ids = [m.message_id for m in sent]
        self._db.record_delivery(job.id, 0, positions, "sent", msg_ids, None)
        acc = _Accumulator(confirmed=list(positions), message_ids=list(msg_ids))
        if len(sent) == len(ordered):
            captured: list[CachedMediaItem] = []
            for it, (send_as, _url, w, h, dur), msg in zip(ordered, specs, sent, strict=True):
                fid = _extract_file_id(msg, send_as)
                if fid:
                    # Prefer dimensions/duration Telegram itself probed on the fetched video/photo.
                    media_obj = getattr(msg, send_as.value, None)
                    if isinstance(media_obj, list):
                        media_obj = media_obj[-1] if media_obj else None
                    captured.append(
                        CachedMediaItem(
                            position=it.position,
                            kind=it.kind,
                            send_as=send_as.value,
                            file_id=fid,
                            width=getattr(media_obj, "width", None) or w,
                            height=getattr(media_obj, "height", None) or h,
                            duration_seconds=getattr(media_obj, "duration", None) or dur,
                        )
                    )
            self._remember_delivered(job.id, captured, len(ordered))
        if needs_link:
            await self._send_link_text(job, link_text, button_on_link, acc)
        return self._outcome(acc, len(ordered))

    async def _send_ref_batch(
        self,
        job: JobRecord,
        specs: Sequence[tuple[SendAs, str, int | None, int | None, float | None]],
        caption: str | None,
        keyboard: InlineKeyboardMarkup | None,
        *,
        first: bool,
    ) -> list[Message]:
        """Send one batch of string media references (Telegram `file_id`s or public HTTPS URLs)."""
        reply = ReplyParameters(message_id=job.origin_message_id, allow_sending_without_reply=True) if first else None
        timeout = self._s.upload_timeout_seconds
        common: dict[str, Any] = {"chat_id": job.chat_id, "request_timeout": timeout}
        if reply is not None:
            common["reply_parameters"] = reply

        async def call() -> list[Message]:
            if len(specs) == 1:
                send_as, ref, width, height, duration = specs[0]
                single = {**common, "caption": caption, "reply_markup": keyboard}
                if send_as is SendAs.PHOTO:
                    msg = await self._bot.send_photo(photo=ref, **single)
                elif send_as is SendAs.VIDEO:
                    msg = await self._bot.send_video(
                        video=ref,
                        width=width,
                        height=height,
                        duration=round(duration) if duration else None,
                        supports_streaming=True,
                        **single,
                    )
                else:
                    msg = await self._bot.send_document(document=ref, disable_content_type_detection=True, **single)
                return [msg]
            media: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
            for idx, (send_as, ref, width, height, duration) in enumerate(specs):
                cap = caption if idx == 0 else None
                if send_as is SendAs.PHOTO:
                    media.append(InputMediaPhoto(media=ref, caption=cap))
                elif send_as is SendAs.VIDEO:
                    media.append(
                        InputMediaVideo(
                            media=ref,
                            caption=cap,
                            width=width,
                            height=height,
                            duration=round(duration) if duration else None,
                            supports_streaming=True,
                        )
                    )
                else:
                    media.append(InputMediaDocument(media=ref, caption=cap, disable_content_type_detection=True))
            return await self._bot.send_media_group(media=media, **common)

        return await self._with_flood_wait(call)

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

        cached_items: list[CachedMediaItem] = []
        if len(sent) == len(items):
            expires = time.monotonic() + self._s.job_metadata_ttl_hours * 3600
            for key, item, source, message in zip(keys, items, sources, sent, strict=True):
                fid = _extract_file_id(message, item.send_as)
                if fid:
                    cached_items.append(
                        CachedMediaItem(
                            position=item.asset.position,
                            kind=item.asset.kind,
                            send_as=item.send_as.value,
                            file_id=fid,
                            width=item.asset.width,
                            height=item.asset.height,
                            duration_seconds=item.asset.duration_seconds,
                        )
                    )
                if isinstance(source, str) or source.read_sha256 is None or source.read_sha256 != key[-1]:
                    continue
                if fid:
                    self._file_ids[key] = (fid, expires)
                    self._file_ids.move_to_end(key)
                    if len(self._file_ids) > RECENT_FILE_LIMIT:
                        self._file_ids.popitem(last=False)

        return StepResult("sent", message_ids=[msg.message_id for msg in sent], cached_items=cached_items)

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
        assert plan.link_message is not None
        await self._send_link_text(job, plan.link_message, plan.button_on_link, acc)

    async def _send_link_text(self, job: JobRecord, link_text: str, with_button: bool, acc: _Accumulator) -> None:
        """The separate source-link message under an album. The media is already delivered, so a failure
        here is logged and never changes the outcome."""
        keyboard = original_keyboard(job.id) if with_button else None
        try:
            msg = await self._bot.send_message(
                job.chat_id, link_text, reply_markup=keyboard, request_timeout=self._s.upload_timeout_seconds
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
