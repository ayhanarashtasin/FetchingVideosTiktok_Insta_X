"""Inline-button callbacks (T04.7, T05.9, FR-17): settings, 'Original files' and 'Retry'.

Callback payloads are untrusted. Each one is re-verified against the stored job: it must exist,
belong to the pressing user, live in the chat the button was pressed in, and be inside its TTL.
A duplicate delivery of the same callback is absorbed by the (chat, message, mode, callback-id)
unique key, so it can never start a second job or deliver anything twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot import messages
from app.bot.context import AppContext
from app.bot.keyboards import (
    ORIGINAL_PREFIX,
    RETRY_PREFIX,
    SETTINGS_PREFIX,
    parse_job_callback,
    settings_keyboard,
)
from app.core.contracts import DeliveryMode, JobRecord, JobState
from app.core.errors import BotError, ErrorCode
from app.core.job_queue import SubmitResult, SubmitStatus
from app.core.url_validator import validate_url
from app.logging_setup import log_event
from app.storage.db import Database, parse_iso, utcnow

log = logging.getLogger(__name__)

_ORIGINAL_ELIGIBLE = frozenset({JobState.COMPLETED, JobState.PARTIAL, JobState.UNVERIFIED})


def authorize_job_callback(
    db: Database, job_id: str, *, user_id: int, chat_id: int, now: datetime | None = None
) -> JobRecord:
    """Load the job behind a callback and prove the presser may use it. Raises BotError."""
    job = db.get_job(job_id)
    if job is None:
        raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "unknown or purged job", reason="callback_expired")
    if job.owner_id != user_id or job.chat_id != chat_id:
        raise BotError(ErrorCode.UNAUTHORIZED, "callback owner/chat mismatch")
    if parse_iso(job.expires_at) <= (now or utcnow()):
        raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "job metadata expired", reason="callback_expired")
    return job


def retry_allowed(job: JobRecord) -> bool:
    """Only failures that the error contract marks retryable (plus restarts / unknown sends)."""
    if job.state in {JobState.INTERRUPTED, JobState.DELIVERY_UNKNOWN}:
        return True
    if job.state is JobState.FAILED and job.error_code:
        try:
            return BotError(ErrorCode(job.error_code)).owner_retryable
        except ValueError:
            return False
    return False


async def _answer(cq: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await cq.answer(text, show_alert=alert)
    except Exception as exc:
        log_event(log, logging.DEBUG, "callback answer failed", error_type=type(exc).__name__)


# --------------------------------------------------------------------------------------
async def on_settings(cq: CallbackQuery, ctx: AppContext, owner_id: int) -> None:
    choice = (cq.data or "")[len(SETTINGS_PREFIX) :]
    try:
        mode = DeliveryMode(choice)
    except ValueError:
        await _answer(cq, messages.ACTION_NOT_ALLOWED)
        return
    ctx.db.set_delivery_mode(owner_id, mode)
    await _answer(cq, messages.SETTINGS_SAVED)
    if cq.message is not None and hasattr(cq.message, "edit_text"):
        try:
            await cq.message.edit_text(messages.settings_text(mode), reply_markup=settings_keyboard(mode))
        except Exception as exc:
            log_event(log, logging.DEBUG, "settings message not edited", error_type=type(exc).__name__)


async def on_original(cq: CallbackQuery, ctx: AppContext, owner_id: int) -> None:
    await _follow_up(cq, ctx, owner_id, prefix=ORIGINAL_PREFIX, original=True)


async def on_retry(cq: CallbackQuery, ctx: AppContext, owner_id: int) -> None:
    await _follow_up(cq, ctx, owner_id, prefix=RETRY_PREFIX, original=False)


async def on_unknown(cq: CallbackQuery) -> None:
    await _answer(cq, messages.ACTION_EXPIRED)


@dataclass(frozen=True)
class FollowUpOutcome:
    result: SubmitResult | None = None
    error_text: str | None = None
    alert: bool = False


def trigger_follow_up(
    ctx: AppContext,
    *,
    job_id: str | None,
    user_id: int,
    chat_id: int,
    request_key: str,
    original: bool,
) -> FollowUpOutcome:
    """Validate and enqueue a follow-up Original-files or Retry job for a button or /orig_ command."""
    if job_id is None:
        return FollowUpOutcome(error_text=messages.ACTION_EXPIRED)
    try:
        job = authorize_job_callback(ctx.db, job_id, user_id=user_id, chat_id=chat_id)
    except BotError as err:
        text = messages.ACTION_NOT_ALLOWED if err.code is ErrorCode.UNAUTHORIZED else messages.ACTION_EXPIRED
        return FollowUpOutcome(error_text=text, alert=err.code is ErrorCode.UNAUTHORIZED)

    eligible = (job.state in _ORIGINAL_ELIGIBLE) if original else retry_allowed(job)
    if not eligible:
        return FollowUpOutcome(error_text=messages.NOT_RETRYABLE, alert=True)
    try:
        validated = validate_url(job.original_url, extra_hosts=ctx.extra_hosts)  # never trust stored data blindly
    except BotError:
        return FollowUpOutcome(error_text=messages.ACTION_EXPIRED)

    mode = DeliveryMode.ORIGINAL if original else job.delivery_mode
    result = ctx.enqueue(
        owner_id=user_id,
        chat_id=job.chat_id,
        origin_message_id=job.origin_message_id,
        validated=validated,
        mode=mode,
        request_key=request_key,
        parent_job_id=job.id,
    )
    return FollowUpOutcome(result=result)


async def _follow_up(cq: CallbackQuery, ctx: AppContext, owner_id: int, *, prefix: str, original: bool) -> None:
    if cq.message is None:
        await _answer(cq, messages.ACTION_EXPIRED)
        return
    outcome = trigger_follow_up(
        ctx,
        job_id=parse_job_callback(cq.data, prefix),
        user_id=owner_id,
        chat_id=cq.message.chat.id,
        request_key=cq.id,  # fresh logical request id; a re-delivered callback becomes DUPLICATE
        original=original,
    )
    if outcome.error_text is not None or outcome.result is None:
        await _answer(cq, outcome.error_text or messages.ACTION_EXPIRED, alert=outcome.alert)
        return
    result = outcome.result
    if result.status is SubmitStatus.ACCEPTED and result.handle is not None:
        await _answer(cq, "Fetching original files..." if original else "Retrying...")
        await ctx.notifier.ack(result.handle.job)
    elif result.status is SubmitStatus.DUPLICATE:
        await _answer(cq)
    elif result.status is SubmitStatus.USER_BUSY:
        await _answer(cq, messages.ALREADY_RUNNING, alert=True)
    else:
        await _answer(cq, messages.QUEUE_FULL, alert=True)


def create_router() -> Router:
    r = Router(name="callbacks")
    r.callback_query.register(on_settings, F.data.startswith(SETTINGS_PREFIX))
    r.callback_query.register(on_original, F.data.startswith(ORIGINAL_PREFIX))
    r.callback_query.register(on_retry, F.data.startswith(RETRY_PREFIX))
    r.callback_query.register(on_unknown)  # catch-all: must stay last
    return r
