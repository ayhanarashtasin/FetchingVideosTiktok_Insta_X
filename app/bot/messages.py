"""All user-facing text. Plain text only (no parse_mode), so untrusted strings can never inject
markup. The error wording follows PRD section 9."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.contracts import DeliveryMode, JobState
from app.core.errors import BotError, ErrorCode

FETCHING = "Fetching media..."
SENDING = "Sending..."

START_TEXT = (
    "Send me a link to a TikTok, Instagram or X (Twitter) post and I will send the photos or videos "
    "back here with the original link underneath.\n\n"
    "Supported:\n"
    "- TikTok videos and photo posts\n"
    "- Instagram photos, Reels, carousels and individual public Stories\n"
    "- X / Twitter posts with photos or videos\n\n"
    "Examples:\n"
    "https://www.tiktok.com/@user/video/123...\n"
    "https://www.instagram.com/p/abc123/\n"
    "https://x.com/user/status/123...\n\n"
    "One link per message. Use /help for details and /settings to choose how files are sent.\n\n"
    "Please only download content you are allowed to save. Private or restricted posts are not "
    "supported and are never bypassed."
)

HELP_TEXT = (
    "How it works\n"
    "Paste one post link. I reply with the media; the original link is shown below it.\n\n"
    "Links\n"
    "- Send a link to a single post, Reel, video or photo post.\n"
    "- Stories: send the link to ONE specific Story (instagram.com/stories/<user>/<id>). Stories "
    "expire after 24 hours, and Highlights are not supported.\n"
    "- Profiles, hashtags and other pages are not supported.\n\n"
    "Quality\n"
    "- Media mode (default): photos and videos you can view right in the chat. Telegram may compress photos.\n"
    "- Original files mode: photos are sent as uncompressed files (videos stay playable). Choose it in /settings, or "
    "use 'Original files' (button or /orig_... link) on a photo result "
    "(the source is fetched again, so it can fail if the post is gone).\n"
    "- I never reduce the resolution. If a file is too big for Telegram I tell you instead.\n\n"
    "Commands\n"
    "/settings - choose Media or Original files\n"
    "/cancel - stop the current download\n"
    "/help - this message\n\n"
    "Common messages\n"
    "- 'private or restricted': I only fetch public posts.\n"
    "- 'unavailable or may have expired': deleted posts and expired Stories cannot be fetched.\n"
    "- 'platform is limiting requests': wait a while and try again."
)

UNSUPPORTED_MESSAGE_TYPE = "Please send a TikTok, Instagram, or X post link as text."
ALREADY_RUNNING = "A download is already in progress. Send /cancel to stop it, or wait until it finishes."
QUEUE_FULL = "I'm busy with other requests right now. Please try again in a moment."
NOTHING_TO_CANCEL = "There is no active download to cancel. (Media that was already sent stays in the chat.)"
CANCELLED_QUEUED = "Cancelled. Nothing was downloaded."
CANCELLING = "Cancelling..."
ACTION_EXPIRED = "This action has expired. Please send the link again."
ACTION_NOT_ALLOWED = "This action isn't available."
NOT_RETRYABLE = "This request can't be retried automatically. Please send the link again."
INTERNAL_ERROR = "Something went wrong on my side. Please try again later."
RETRY_BUTTON = "Retry"
ORIGINAL_BUTTON = "Original files"
SETTINGS_SAVED = "Saved."


def settings_text(mode: DeliveryMode) -> str:
    current = (
        "Media (viewable in chat; photos may be compressed)"
        if mode is DeliveryMode.MEDIA
        else ("Original files (photos sent as uncompressed files)")
    )
    return f"Delivery mode for new links:\n{current}\n\nChoose:"


def original_link_line(url: str) -> str:
    return f"Original link: {url}"


# --------------------------------------------------------------------------------------
# Errors (PRD section 9)
# --------------------------------------------------------------------------------------
_REASON_TEXT = {
    ("INVALID_URL", "multiple_urls"): "Please send one link at a time.",
    ("UNSUPPORTED_POST", "story_id_required"): (
        "Story links must point to one specific Story. Open the Story, copy its link and send that."
    ),
    ("UNSUPPORTED_POST", "highlights_unsupported"): (
        "Highlights are not supported. Please send a link to an individual Story."
    ),
    ("UNSUPPORTED_POST", "unresolved"): (
        "I couldn't resolve that short link to one specific post. Please send the direct link to the post."
    ),
    ("AUTH_REQUIRED", "story"): (
        "This public Story requires an authorized Instagram session. The bot owner needs to configure it."
    ),
}

_CODE_TEXT = {
    ErrorCode.INVALID_URL: "Please send a TikTok, Instagram, or X post link.",
    ErrorCode.UNSUPPORTED_POST: "This link is not a supported individual post or Story.",
    ErrorCode.NOT_FOUND_OR_EXPIRED: "This post or Story is unavailable or may have expired.",
    ErrorCode.ACCESS_RESTRICTED: "This content is private or restricted. I can only retrieve permitted public media.",
    ErrorCode.AUTH_REQUIRED: (
        "This public Story requires an authorized Instagram session. The bot owner needs to configure it."
    ),
    ErrorCode.RATE_LIMITED: "The platform is limiting requests. Please try again later.",
    ErrorCode.EXTRACTOR_CHANGED: "The platform could not be processed right now. Please try again later.",
    ErrorCode.DOWNLOAD_TIMEOUT: "Fetching took too long and was stopped. Please try again later.",
    ErrorCode.LIMIT_EXCEEDED: "This post is larger than the configured limits (items or size). Nothing was sent.",
    ErrorCode.FILE_TOO_LARGE: "The source file exceeds the configured Telegram upload limit. No quality was reduced.",
    ErrorCode.MEDIA_INVALID: "The downloaded file was not valid media, so I did not send it.",
    ErrorCode.UPLOAD_FAILED: "Telegram rejected the upload. Please try again later.",
    ErrorCode.DELIVERY_UNKNOWN: "Sending was interrupted and I can't tell whether it arrived. I did not resend it.",
    ErrorCode.CANCELLED: "Cancelled.",
    ErrorCode.INTERRUPTED: "The bot restarted while this was running. Tap Retry to try again.",
    ErrorCode.UNAUTHORIZED: ACTION_NOT_ALLOWED,
    ErrorCode.INTERNAL_ERROR: INTERNAL_ERROR,
}


def error_text(err: BotError) -> str:
    """User-facing text for an error. Never includes `err.detail` (internal, may hold URLs)."""
    if err.reason:
        special = _REASON_TEXT.get((err.code.value, err.reason))
        if special:
            return special
    if err.code is ErrorCode.RATE_LIMITED and err.retry_after and err.retry_after >= 60:
        minutes = int(err.retry_after // 60)
        return f"The platform is limiting requests. Please try again in about {minutes} min."
    return _CODE_TEXT.get(err.code, INTERNAL_ERROR)


# --------------------------------------------------------------------------------------
# Final outcome reports
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FinalReport:
    state: JobState
    original_url: str
    delivered: int = 0
    expected: int | None = None
    omitted_positions: Sequence[int] = ()
    failed_positions: Sequence[int] = ()
    unknown_positions: Sequence[int] = ()
    failure_codes: Sequence[ErrorCode] = ()
    error: BotError | None = None
    retry_offered: bool = False


def _human_positions(positions: Sequence[int]) -> str:
    nums = sorted({p + 1 for p in positions})  # users count from 1
    if len(nums) == 1:
        return f"Item {nums[0]}"
    return "Items " + ", ".join(str(n) for n in nums)


def final_text(r: FinalReport) -> str | None:
    """Text for the final status message, or None when the status message should just be removed."""
    link = original_link_line(r.original_url)
    s = r.state
    if s is JobState.COMPLETED:
        return None
    if s is JobState.UNVERIFIED:
        return (
            f"Delivered {r.delivered} item{'s' if r.delivered != 1 else ''}. "
            f"I could not verify whether the source contains additional media.\n{link}"
        )
    if s is JobState.PARTIAL:
        if r.expected is not None:
            head = f"Delivered {r.delivered} of {r.expected} items."
        else:
            head = f"Delivered {r.delivered} item{'s' if r.delivered != 1 else ''}."
        omitted = sorted(set(r.omitted_positions))
        failed = sorted(set(r.failed_positions) - set(omitted))
        if omitted:
            head += f" {_human_positions(omitted)} could not be retrieved."
        if failed:
            head += f" {_human_positions(failed)} could not be sent."
        if ErrorCode.FILE_TOO_LARGE in r.failure_codes:
            head += " Some files exceed the Telegram upload limit; no quality was reduced."
        if not omitted and not failed:
            head += " I could not verify whether the source contains additional media."
        return f"{head}\n{link}"
    if s is JobState.DELIVERY_UNKNOWN:
        n = len(r.unknown_positions)
        confirmed = f" {r.delivered} item{'s' if r.delivered != 1 else ''} arrived for certain." if r.delivered else ""
        return (
            f"Sending was interrupted and I can't tell whether {n} item{'s' if n != 1 else ''} arrived. "
            f"I did not resend them.{confirmed} Check this chat, then tap Retry if something is missing.\n{link}"
        )
    if s is JobState.CANCELLED:
        tail = f" {r.delivered} item{'s' if r.delivered != 1 else ''} had already been sent." if r.delivered else ""
        return f"Cancelled.{tail}"
    if s is JobState.INTERRUPTED:
        tail = f" {r.delivered} item{'s' if r.delivered != 1 else ''} had already been sent." if r.delivered else ""
        return f"The bot restarted while this was running.{tail} Tap Retry to try again.\n{link}"
    # FAILED
    text = error_text(r.error) if r.error else INTERNAL_ERROR
    if r.delivered:
        text += f" {r.delivered} item{'s' if r.delivered != 1 else ''} had already been sent."
    return text
