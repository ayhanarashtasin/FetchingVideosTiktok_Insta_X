"""Structured JSON logging with mandatory redaction (T02.1, T05.7, PRD section 7).

Nothing sensitive may reach a log line: full URLs/query strings, bot tokens, session cookies,
authorization headers and long opaque secrets are replaced before any handler formats the record,
including records emitted by third-party libraries (aiogram, aiohttp, yt-dlp output we forward).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HEADER = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization|x-worker-secret)\b\s*[:=]\s*[^\r\n]+"
)
_URL = re.compile(r"(?i)\b(?:https?|ftp|file|socks5?)://[^\s<>\"'`]+")
_TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_KV = re.compile(
    r"(?i)\b(sessionid|sessionid_ss|auth_token|sid_tt|ds_user_id|csrftoken|ct0|mstoken|ttwid|igsh)\s*[=:]\s*[^\s;,&\"']+"
)
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-+/]{40,}={0,2}")

MAX_REDACTED_LENGTH = 2000


def redact(text: object, *, max_len: int = MAX_REDACTED_LENGTH) -> str:
    """Remove secrets and URLs from `text` and truncate it."""
    s = str(text)
    s = _HEADER.sub(lambda m: f"{m.group(1)}: <redacted>", s)
    s = _URL.sub("<url>", s)
    s = _TOKEN.sub("<token>", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", s)
    s = _COOKIE_KV.sub(lambda m: f"{m.group(1)}=<redacted>", s)
    s = _LONG_OPAQUE.sub("<secret>", s)
    if len(s) > max_len:
        s = s[:max_len] + f"...<truncated {len(s) - max_len} chars>"
    return s


class RedactingFilter(logging.Filter):
    """Rewrites every record's message and structured fields before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            record.exc_text = redact(trace, max_len=6000)
            record.exc_info = None  # the formatter must use the redacted text only
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", *, log_file: Path | None = None, json_output: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        )
    formatter: logging.Formatter = (
        JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    root.setLevel(level)
    # Library chatter that can include request details stays at WARNING.
    for noisy in ("aiohttp.access", "aiogram.client.session.aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Emit a structured event: `log_event(log, INFO, "job finished", job_id=..., state=...)`."""
    logger.log(level, msg, extra={"fields": fields})
