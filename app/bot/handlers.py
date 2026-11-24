"""Message handlers (T02.3): /start, /help, /settings, /cancel and the URL handler.

Handlers never touch the network and never download anything: they validate, reserve a queue
slot and acknowledge. All real work happens in the job runner, so incoming updates are never
blocked by a slow platform.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.bot import messages
from app.bot.context import AppContext
from app.bot.keyboards import settings_keyboard
from app.bot.messages import FinalReport
from app.core.contracts import JobState
from app.core.job_queue import CancelKind, SubmitStatus
from app.core.url_validator import parse_message_url
from app.logging_setup import log_event

log = logging.getLogger(__name__)


def extract_text_and_entity_urls(message: Message) -> tuple[str, list[str]]:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    # Hyperlinked text ("tap here") hides the URL in the entity; plain URLs are found in the text.
    return text, [e.url for e in entities if e.type == "text_link" and e.url]


async def cmd_start(message: Message) -> None:
    await message.answer(messages.START_TEXT)


async def cmd_help(message: Message) -> None:
    await message.answer(messages.HELP_TEXT)


async def cmd_settings(message: Message, ctx: AppContext, owner_id: int) -> None:
    mode = ctx.db.get_delivery_mode(owner_id, ctx.settings.default_delivery_mode)
    await message.answer(messages.settings_text(mode), reply_markup=settings_keyboard(mode))


async def cmd_cancel(message: Message, ctx: AppContext, owner_id: int) -> None:
    result = ctx.queue.cancel_for_owner(owner_id)
    if result.kind is CancelKind.NOTHING or result.handle is None:
        await message.answer(messages.NOTHING_TO_CANCEL)
    elif result.kind is CancelKind.QUEUED:
        job = ctx.db.get_job(result.handle.job.id) or result.handle.job
        await ctx.notifier.finish(job, FinalReport(state=JobState.CANCELLED, original_url=job.original_url))
    else:  # RUNNING: the runner reports the final outcome (incl. anything already delivered)
        await message.answer(messages.CANCELLING)


async def on_text(message: Message, ctx: AppContext, owner_id: int) -> None:
    text, entity_urls = extract_text_and_entity_urls(message)
    # Raises BotError (INVALID_URL / UNSUPPORTED_POST) -> ErrorBoundaryMiddleware replies.
    validated = parse_message_url(text, entity_urls, extra_hosts=ctx.extra_hosts)
    mode = ctx.db.get_delivery_mode(owner_id, ctx.settings.default_delivery_mode)
    result = ctx.enqueue(
        owner_id=owner_id,
        chat_id=message.chat.id,
        origin_message_id=message.message_id,
        validated=validated,
        mode=mode,
    )
    if result.status is SubmitStatus.ACCEPTED and result.handle is not None:
        await ctx.notifier.ack(result.handle.job)  # prompt acknowledgement, before any download
        log_event(
            log,
            logging.INFO,
            "job accepted",
            job_id=result.handle.job.id,
            platform=validated.platform.value,
            mode=mode.value,
        )
    elif result.status is SubmitStatus.DUPLICATE:
        log_event(log, logging.INFO, "duplicate telegram event ignored", message_id=message.message_id)
    elif result.status is SubmitStatus.USER_BUSY:
        await message.answer(messages.ALREADY_RUNNING)
    else:
        await message.answer(messages.QUEUE_FULL)


async def on_other(message: Message) -> None:
    """Photos, stickers, voice notes, ... : answer politely, never process."""
    await message.answer(messages.UNSUPPORTED_MESSAGE_TYPE)


def create_router() -> Router:
    """A fresh router per dispatcher (an aiogram Router can only be attached once)."""
    r = Router(name="user-messages")
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_settings, Command("settings"))
    r.message.register(cmd_cancel, Command("cancel"))
    r.message.register(on_text, F.text | F.caption)
    r.message.register(on_other)  # catch-all: must stay last
    return r
