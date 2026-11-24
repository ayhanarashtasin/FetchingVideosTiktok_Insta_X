"""Typed error contract (architecture section 14).

Every failure that can reach the user is a `BotError` carrying an `ErrorCode`. Internal detail
strings are for (redacted) logs only; user-facing text lives in `app.bot.messages`.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_POST = "UNSUPPORTED_POST"
    NOT_FOUND_OR_EXPIRED = "NOT_FOUND_OR_EXPIRED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    EXTRACTOR_CHANGED = "EXTRACTOR_CHANGED"
    DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    MEDIA_INVALID = "MEDIA_INVALID"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


# Codes for which an automatic, bounded retry may be attempted (FR-16). Everything else is
# never retried automatically. RATE_LIMITED and UPLOAD_FAILED are additionally conditional
# (see `BotError.auto_retryable`).
_AUTO_RETRY_CODES = frozenset(
    {ErrorCode.DOWNLOAD_TIMEOUT, ErrorCode.RATE_LIMITED, ErrorCode.UPLOAD_FAILED}
)

# Codes after which the owner may be offered a manual "Retry" button (T05.9).
_OWNER_RETRY_CODES = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.EXTRACTOR_CHANGED,
        ErrorCode.DOWNLOAD_TIMEOUT,
        ErrorCode.UPLOAD_FAILED,
        ErrorCode.DELIVERY_UNKNOWN,
        ErrorCode.INTERRUPTED,
    }
)

# Codes where falling back to a second extractor is forbidden because it would amount to
# evading a platform control (architecture section 6).
_NO_FALLBACK_CODES = frozenset(
    {
        ErrorCode.ACCESS_RESTRICTED,
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.NOT_FOUND_OR_EXPIRED,
        ErrorCode.UNAUTHORIZED,
        ErrorCode.CANCELLED,
    }
)


class BotError(Exception):
    """A typed, user-mappable failure.

    Attributes:
        code: machine-readable error code from the error contract.
        reason: optional sub-reason (e.g. ``multiple_urls``) used to pick a precise message.
        detail: internal diagnostic text. NEVER shown to users; always redact before logging.
        retry_after: seconds the server asked us to wait, when known.
        safe_to_retry: for UPLOAD_FAILED - True only for definite/transient-safe failures.
        position: item position the failure relates to, when item-specific.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        *,
        reason: str | None = None,
        retry_after: float | None = None,
        safe_to_retry: bool = False,
        position: int | None = None,
    ) -> None:
        super().__init__(f"{code.value}{': ' + detail if detail else ''}")
        self.code = code
        self.detail = detail
        self.reason = reason
        self.retry_after = retry_after
        self.safe_to_retry = safe_to_retry
        self.position = position

    @property
    def auto_retryable(self) -> bool:
        """Whether the orchestrator may retry this automatically (bounded, with backoff)."""
        if self.code not in _AUTO_RETRY_CODES:
            return False
        if self.code is ErrorCode.UPLOAD_FAILED:
            return self.safe_to_retry
        return True

    @property
    def owner_retryable(self) -> bool:
        return self.code in _OWNER_RETRY_CODES

    @property
    def fallback_allowed(self) -> bool:
        """Whether a secondary extractor may be tried (never for access/rate/challenge errors)."""
        return self.code not in _NO_FALLBACK_CODES

    def to_wire(self) -> dict[str, object]:
        """Serialisable form for the worker service boundary. `detail` is deliberately dropped."""
        return {
            "code": self.code.value,
            "reason": self.reason,
            "retry_after": self.retry_after,
            "position": self.position,
        }

    @classmethod
    def from_wire(cls, data: dict[str, object]) -> BotError:
        retry_after = data.get("retry_after")
        position = data.get("position")
        reason = data.get("reason")
        return cls(
            ErrorCode(str(data["code"])),
            reason=str(reason) if reason is not None else None,
            retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
            position=int(position) if isinstance(position, int) else None,
        )


class ConfigError(Exception):
    """Invalid or unsafe configuration; the process must refuse to start."""


class IllegalTransitionError(Exception):
    """A job state change that is not allowed by the lifecycle state machine."""
