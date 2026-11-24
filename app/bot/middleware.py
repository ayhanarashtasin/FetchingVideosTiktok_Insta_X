"""Error boundary middleware (T02.1): typed errors become readable messages; anything else is
logged (redacted) and answered generically. A handler crash can never take the dispatcher down."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update

from app.bot import messages
from app.core.errors import BotError
from app.logging_setup import log_event, redact

log = logging.getLogger(__name__)


def _chat_id(update: Update) -> int | None:
    if update.message is not None:
        return update.message.chat.id
    if update.callback_query is not None and update.callback_query.message is not None:
        return update.callback_query.message.chat.id
    return None


class ErrorBoundaryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot | None = data.get("bot")
        try:
            return await handler(event, data)
        except BotError as err:
            await self._reply(bot, event, messages.error_text(err))
            log_event(log, logging.INFO, "request rejected", code=err.code.value, reason=err.reason)
        except TelegramAPIError as exc:
            log_event(
                log, logging.WARNING, "telegram api error in handler", error_type=type(exc).__name__, error=redact(exc)
            )
        except Exception as exc:
            log_event(
                log, logging.ERROR, "unhandled error in handler", error_type=type(exc).__name__, error=redact(exc)
            )
            await self._reply(bot, event, messages.INTERNAL_ERROR)
        return None

    @staticmethod
    async def _reply(bot: Bot | None, event: Update, text: str) -> None:
        chat_id = _chat_id(event)
        if bot is None or chat_id is None:
            return
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:
            log_event(log, logging.WARNING, "could not deliver error reply", error_type=type(exc).__name__)
