"""Per-job private copy of the optional session (cookies) file.

Both extractors WRITE cookies back to the file they are given: yt-dlp saves its cookie jar when it
exits and gallery-dl rewrites the file through a temporary sibling (`cookies-update`, on by default).
The operator's session file may be read-only and must never change, and a
failed write-back would add error noise to stderr that our classifier reads. So every job gets its
own private copy inside its throw-away workspace; whatever the tools do to it disappears with the
workspace, and the original is only ever opened for reading.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.errors import BotError, ErrorCode

COOKIES_NAME = "session-cookies.txt"
_MAX_COOKIE_FILE_BYTES = 1_000_000  # a cookies.txt larger than this is not a cookies file


def private_cookie_copy(source: Path | None, home: Path) -> Path | None:
    """Copy `source` into `home` (0600, never through a symlink) and return the copy; None if unset."""
    if source is None:
        return None
    dest = home / COOKIES_NAME
    try:
        with open(source, "rb") as src:
            data = src.read(_MAX_COOKIE_FILE_BYTES + 1)
        if len(data) > _MAX_COOKIE_FILE_BYTES:
            raise OSError("session file is implausibly large")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(dest, flags, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
    except OSError as exc:
        # The path is operator configuration and the file content is a secret: report neither.
        raise BotError(
            ErrorCode.AUTH_REQUIRED, f"session file unavailable ({type(exc).__name__})", reason="session_unavailable"
        ) from None
    return dest
