"""Authorization (T02.2, FR-01, FR-17).

Outer middleware checks the sender and chat before handlers run. Public access accepts any
human user in their own private chat. The default requires a configured numeric user ID.
Messages and callbacks that fail these checks are dropped silently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Update

from app.logging_setup import log_event

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    user_id: int
    is_bot: bool
    chat_id: int
    chat_type: str


def extract_actor(update: Update) -> Actor | None:
    """Who sent this update and from which chat; None for update types we never serve."""
    if update.message is not None:
        m = update.message
        if m.from_user is None:
            return None
        return Actor(m.from_user.id, bool(m.from_user.is_bot), m.chat.id, str(m.chat.type))
    if update.callback_query is not None:
        cq = update.callback_query
        if cq.message is None:  # inline-message callbacks have no chat: not something we serve
            return None
        return Actor(cq.from_user.id, bool(cq.from_user.is_bot), cq.message.chat.id, str(cq.message.chat.type))
    return None


class AuthMiddleware(BaseMiddleware):
    def __init__(
        self, allowed_user_ids: Iterable[int], *, private_only: bool = True, public_access: bool = False
    ) -> None:
        self._allowed = frozenset(allowed_user_ids)
        self._public_access = public_access
        self._private_only = private_only or public_access
        self._warned: set[int] = set()

    def is_authorized(self, actor: Actor) -> bool:
        if actor.is_bot or actor.user_id <= 0:
            return False
        if not self._public_access and actor.user_id not in self._allowed:
            return False
        if self._private_only:
            # In a private chat the chat id equals the user id; checking both stops a callback
            # or message that claims one user but lives in another chat.
            return actor.chat_type == "private" and actor.chat_id == actor.user_id
        return True

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        actor = extract_actor(event)
        if actor is None or not self.is_authorized(actor):
            uid = actor.user_id if actor else None
            if uid is not None and uid not in self._warned and len(self._warned) < 1000:
                self._warned.add(uid)
                log_event(
                    log,
                    logging.WARNING,
                    "rejected update from unauthorized sender",
                    user_id=uid,
                    chat_type=actor.chat_type if actor else None,
                )
            return None  # dropped: no handler, no reply, no network activity
        data["owner_id"] = actor.user_id
        return await handler(event, data)
