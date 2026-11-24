"""Inline keyboards and the compact callback-data protocol.

Callback data carries only an opaque 32-hex job id (or a fixed token) - never a URL - and is
always re-verified against the stored job (owner, chat, TTL) before anything happens.
"""

from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import messages
from app.core.contracts import DeliveryMode

ORIGINAL_PREFIX = "orig:"
RETRY_PREFIX = "retry:"
SETTINGS_PREFIX = "set:"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def original_data(job_id: str) -> str:
    return ORIGINAL_PREFIX + job_id


def retry_data(job_id: str) -> str:
    return RETRY_PREFIX + job_id


def parse_job_callback(data: str | None, prefix: str) -> str | None:
    """Return the job id from `prefix<32 hex>` or None if the payload is malformed."""
    if not data or not data.startswith(prefix):
        return None
    job_id = data[len(prefix):]
    return job_id if _JOB_ID.fullmatch(job_id) else None


def original_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=messages.ORIGINAL_BUTTON, callback_data=original_data(job_id))]]
    )


def retry_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=messages.RETRY_BUTTON, callback_data=retry_data(job_id))]]
    )


def settings_keyboard(current: DeliveryMode) -> InlineKeyboardMarkup:
    def label(mode: DeliveryMode, text: str) -> str:
        return ("✓ " if mode is current else "") + text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label(DeliveryMode.MEDIA, "Media"), callback_data=SETTINGS_PREFIX + "media")],
            [InlineKeyboardButton(text=label(DeliveryMode.ORIGINAL, "Original files"),
                                  callback_data=SETTINGS_PREFIX + "original")],
        ]
    )
