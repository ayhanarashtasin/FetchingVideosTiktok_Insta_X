"""Map extractor failures to typed `BotError`s (T03.2, T03.10, T05.5).

Extractor output is untrusted text. It is only ever matched against fixed patterns; the raw text
is kept (redacted, truncated) in `BotError.detail` for logs and NEVER shown to the user.

Rules of thumb encoded here:
* access denial / auth / rate limit / challenge  -> no fallback to a second extractor (that would
  be circumventing a platform control);
* transient network trouble                      -> DOWNLOAD_TIMEOUT (bounded automatic retry);
* "this post has no video" style messages        -> EXTRACTOR_CHANGED with reason `no_video`,
  which is the one case where a fallback extractor is legitimate.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import BotError, ErrorCode
from app.logging_setup import redact

_I = re.IGNORECASE

# (pattern, code, reason) - first match wins. Order matters: auth/private before "unavailable".
_TEXT_RULES: tuple[tuple[re.Pattern[str], ErrorCode, str | None], ...] = (
    (re.compile(r"larger than max-?filesize|file is larger than", _I), ErrorCode.LIMIT_EXCEEDED, "filesize"),
    (
        re.compile(
            r"captcha|challenge|verify you are human|are you a robot|unusual traffic|confirm you.re not a bot", _I
        ),
        ErrorCode.RATE_LIMITED,
        "challenge",
    ),
    (
        re.compile(r"\blog[ -]?in\b|sign[ -]?in|logged.?in|--cookies|account credentials|authentication", _I),
        ErrorCode.AUTH_REQUIRED,
        None,
    ),
    (
        re.compile(
            r"private|protected|members.only|not authori[sz]ed|forbidden|http error 40[13]|\b40[13]\b|age.restricted|"
            r"geo.?restrict|not available in your country|subscribers? only|fan.club|paywall",
            _I,
        ),
        ErrorCode.ACCESS_RESTRICTED,
        None,
    ),
    (re.compile(r"(?<!\d)429(?!\d)|too many requests|rate.?limit", _I), ErrorCode.RATE_LIMITED, None),
    (re.compile(r"unsupported url|no suitable extractor|no extractor", _I), ErrorCode.UNSUPPORTED_POST, None),
    # Server-side trouble must be recognised BEFORE the "unavailable" wording below ("503 Service Unavailable").
    (
        re.compile(
            r"http error 5\d\d|\b5\d\d\b (?:server|service|bad gateway|gateway)|service unavailable|bad gateway|"
            r"gateway time-?out|server error",
            _I,
        ),
        ErrorCode.DOWNLOAD_TIMEOUT,
        "network",
    ),
    (
        re.compile(
            r"(?<!\d)404(?!\d)|(?<!\d)410(?!\d)|not found|unavailable|removed|deleted|does not exist|"
            r"no longer available|expired|could not be found",
            _I,
        ),
        ErrorCode.NOT_FOUND_OR_EXPIRED,
        None,
    ),
    (
        re.compile(
            r"no video (?:formats )?(?:found|in this post)|there is no video|requested format is not available|"
            r"no video could be found|image post|photo post|slideshow",
            _I,
        ),
        ErrorCode.EXTRACTOR_CHANGED,
        "no_video",
    ),
    (
        re.compile(
            r"timed? ?out|connection (?:reset|aborted|refused|error)|temporary failure|name or service not known|"
            r"http error 5\d\d|\b5\d\d\b server|remote end closed|incomplete ?read|ssl|network is unreachable|"
            r"errno 11[01]|failed to (?:establish|resolve)",
            _I,
        ),
        ErrorCode.DOWNLOAD_TIMEOUT,
        "network",
    ),
)


def _detail(text: str) -> str:
    return redact(text.strip()[-400:], max_len=400)


def classify_text(text: str, *, session_supported: bool = False, hint: str | None = None) -> BotError:
    """Classify free-form extractor text. `hint` is the post kind ('story', 'reel', ...)."""
    for pattern, code, reason in _TEXT_RULES:
        if pattern.search(text):
            if code is ErrorCode.AUTH_REQUIRED:
                if not session_supported:
                    # Platforms where this project never uses a session: content that needs a login is,
                    # for our purposes, not permitted public content.
                    return BotError(ErrorCode.ACCESS_RESTRICTED, _detail(text), reason="login_wall")
                return BotError(code, _detail(text), reason="story" if hint == "story" else None)
            return BotError(code, _detail(text), reason=reason)
    return BotError(ErrorCode.EXTRACTOR_CHANGED, _detail(text) or "unclassified extractor failure")


def classify_ytdlp(
    stderr: str, returncode: int | None, *, session_supported: bool = False, hint: str | None = None
) -> BotError:
    return classify_text(stderr, session_supported=session_supported, hint=hint)


_HTTP_STATUS = re.compile(r"'(\d{3})\s")
_GD_STRUCTURED = {
    "AuthRequired": ErrorCode.AUTH_REQUIRED,
    "AuthenticationError": ErrorCode.AUTH_REQUIRED,
    "AuthorizationError": ErrorCode.ACCESS_RESTRICTED,
    "NotFoundError": ErrorCode.NOT_FOUND_OR_EXPIRED,
    "NoExtractorError": ErrorCode.UNSUPPORTED_POST,
}


@dataclass(frozen=True)
class GdError:
    error: str
    message: str


def classify_gallerydl(
    errors: Sequence[GdError],
    returncode: int | None,
    stderr: str,
    *,
    session_supported: bool = False,
    hint: str | None = None,
) -> BotError:
    """Classify a gallery-dl failure from its structured `[-1, {...}]` entries, else exit bits, else text."""
    for e in errors:
        name, msg = e.error, e.message
        if name == "ChallengeError":
            return BotError(ErrorCode.RATE_LIMITED, _detail(msg), reason="challenge")
        if name in {"AuthRequired", "AuthenticationError"}:
            if not session_supported:
                return BotError(ErrorCode.ACCESS_RESTRICTED, _detail(msg), reason="login_wall")
            return BotError(ErrorCode.AUTH_REQUIRED, _detail(msg), reason="story" if hint == "story" else None)
        if name in _GD_STRUCTURED:
            return BotError(_GD_STRUCTURED[name], _detail(msg))
        if name == "HttpError":
            m = _HTTP_STATUS.search(msg)
            status = int(m.group(1)) if m else 0
            if status == 429:
                return BotError(ErrorCode.RATE_LIMITED, _detail(msg))
            if status in {401, 403}:
                return BotError(ErrorCode.ACCESS_RESTRICTED, _detail(msg))
            if status in {404, 410}:
                return BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, _detail(msg))
            return BotError(ErrorCode.DOWNLOAD_TIMEOUT, _detail(msg), reason="network")
        # AbortExtraction and friends carry a human message: reuse the text rules
        return classify_text(f"{name}: {msg}", session_supported=session_supported, hint=hint)
    if returncode:
        if returncode & 64:
            return BotError(ErrorCode.UNSUPPORTED_POST, _detail(stderr))
        if returncode & 8:
            return BotError(ErrorCode.RATE_LIMITED, _detail(stderr), reason="challenge")
        if returncode & 16:
            return classify_text(stderr or "authorization", session_supported=session_supported, hint=hint)
    return classify_text(stderr, session_supported=session_supported, hint=hint)


@dataclass(frozen=True)
class GdWarnings:
    """Signals found in gallery-dl's stderr that the structured output does not carry."""

    skipped_media: bool  # an item was silently dropped (cannot prove the post is complete)
    private_hint: bool  # the extractor warned that the account's posts are private


_SKIPPED = re.compile(
    r"missing media in post|media unavailable|skipping (?:post|tweet)|failed to extract (?:post|media)", _I
)
_PRIVATE = re.compile(r"posts are private|is private|protected", _I)


def scan_gallerydl_warnings(stderr: str) -> GdWarnings:
    return GdWarnings(skipped_media=bool(_SKIPPED.search(stderr)), private_hint=bool(_PRIVATE.search(stderr)))
