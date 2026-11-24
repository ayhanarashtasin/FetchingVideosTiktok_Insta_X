"""User-facing wording (PRD section 9), log redaction (T05.7) and health evaluation (T05.8)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from app.bot import messages
from app.bot.messages import FinalReport
from app.core.contracts import JobState
from app.core.errors import BotError, ErrorCode
from app.logging_setup import JsonFormatter, RedactingFilter, log_event, redact
from app.services.health import Heartbeat, evaluate_health, read_heartbeat, write_heartbeat
from tests.conftest import fake_bot_token

URL = "https://www.instagram.com/p/ABCdef12345/"


# --- PRD section 9: exact user-facing responses -----------------------------------------------------------
@pytest.mark.parametrize(
    ("err", "text"),
    [
        (BotError(ErrorCode.INVALID_URL), "Please send a TikTok, Instagram, or X post link."),
        (BotError(ErrorCode.UNSUPPORTED_POST), "This link is not a supported individual post or Story."),
        (BotError(ErrorCode.NOT_FOUND_OR_EXPIRED), "This post or Story is unavailable or may have expired."),
        (BotError(ErrorCode.ACCESS_RESTRICTED),
         "This content is private or restricted. I can only retrieve permitted public media."),
        (BotError(ErrorCode.AUTH_REQUIRED),
         "This public Story requires an authorized Instagram session. The bot owner needs to configure it."),
        (BotError(ErrorCode.RATE_LIMITED), "The platform is limiting requests. Please try again later."),
        (BotError(ErrorCode.FILE_TOO_LARGE),
         "The source file exceeds the configured Telegram upload limit. No quality was reduced."),
        (BotError(ErrorCode.EXTRACTOR_CHANGED), "The platform could not be processed right now. Please try again later."),
    ],
)
def test_error_texts_match_the_prd(err, text):
    assert messages.error_text(err) == text


def test_every_error_code_has_a_user_message_and_never_leaks_internal_detail():
    for code in ErrorCode:
        err = BotError(code, "internal detail with https://secret.example/path?token=abc")
        out = messages.error_text(err)
        assert out and "secret.example" not in out and "token=abc" not in out


def test_partial_album_message_matches_prd_example():
    r = FinalReport(JobState.PARTIAL, URL, delivered=4, expected=5, omitted_positions=[2])
    assert messages.final_text(r) == f"Delivered 4 of 5 items. Item 3 could not be retrieved.\nOriginal link: {URL}"


def test_unknown_count_message_matches_prd_example():
    r = FinalReport(JobState.UNVERIFIED, URL, delivered=4)
    assert messages.final_text(r) == (
        f"Delivered 4 items. I could not verify whether the source contains additional media.\nOriginal link: {URL}"
    )


def test_singular_and_plural_wording():
    assert "Delivered 1 item." in messages.final_text(FinalReport(JobState.UNVERIFIED, URL, delivered=1))
    text = messages.final_text(FinalReport(JobState.PARTIAL, URL, delivered=2, expected=5, omitted_positions=[1, 3, 4]))
    assert "Items 2, 4, 5 could not be retrieved." in text


def test_success_has_no_final_text_so_the_status_message_is_removed():
    assert messages.final_text(FinalReport(JobState.COMPLETED, URL, delivered=3, expected=3)) is None


def test_cancelled_reports_what_was_already_sent():
    assert messages.final_text(FinalReport(JobState.CANCELLED, URL)) == "Cancelled."
    assert messages.final_text(FinalReport(JobState.CANCELLED, URL, delivered=3)) == "Cancelled. 3 items had already been sent."


def test_rate_limit_with_long_retry_after_mentions_the_wait():
    assert "about 45 min" in messages.error_text(BotError(ErrorCode.RATE_LIMITED, retry_after=2700))


def test_unknown_outcome_never_claims_success():
    r = FinalReport(JobState.DELIVERY_UNKNOWN, URL, delivered=2, unknown_positions=[2, 3])
    t = messages.final_text(r)
    assert "can't tell whether 2 items arrived" in t and "did not resend" in t and URL in t


def test_help_documents_the_required_topics():
    h = messages.HELP_TEXT.lower()
    for needle in ("stor", "original files", "/settings", "/cancel", "never reduce"):
        assert needle in h


def test_untrusted_urls_cannot_inject_markup_because_messages_are_plain_text():
    # No parse_mode is ever used: a URL full of markup characters is just text.
    nasty = "https://x.com/a/status/1700000000000?q=<b>*_`[x](y)"
    assert messages.original_link_line(nasty) == f"Original link: {nasty}"


# --- log redaction ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        (f"fetching {URL}?igsh=abc123 failed", "instagram.com"),
        ("HTTP GET https://x.com/user/status/1700000000000?s=20", "status/1700000000000"),
        (f"token is {fake_bot_token()} ok", fake_bot_token()),
        ("Cookie: sessionid=abcdef1234567890abcdef; csrftoken=zzz", "abcdef1234567890abcdef"),  # secret-scan: allow (deliberately fake redaction fixture)
        ("set-cookie: auth_token=deadbeefdeadbeefdeadbeef", "deadbeefdeadbeefdeadbeef"),  # secret-scan: allow (deliberately fake redaction fixture)
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("x-worker-secret: supersecretvalue1234567890", "supersecretvalue1234567890"),
        ("sessionid=a1b2c3d4e5f6a7b8c9d0e1f2", "a1b2c3d4e5f6a7b8c9d0e1f2"),  # secret-scan: allow (deliberately fake redaction fixture)
        ("opaque " + "A1b2C3d4" * 6, "A1b2C3d4A1b2"),
        ("proxy via socks5://user:pass@10.0.0.1:1080", "user:pass"),
    ],
)
def test_redact_removes_secrets_and_urls(raw, must_not_contain):
    out = redact(raw)
    assert must_not_contain not in out, out


def test_redact_keeps_harmless_diagnostics_and_truncates():
    assert redact("job 8f14e45fceea167a5a36dedd4bea2543 state=RUNNING items=3") == (
        "job 8f14e45fceea167a5a36dedd4bea2543 state=RUNNING items=3"
    )
    out = redact("x " * 5000, max_len=100)
    assert len(out) < 160 and "truncated" in out


def _capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_structured_event_is_json_with_fields_and_redaction():
    logger, stream = _capture()
    log_event(logger, logging.INFO, f"done {URL}", job_id="abc", platform="instagram", note=f"see {URL}", count=3)
    rec = json.loads(stream.getvalue())
    assert rec["job_id"] == "abc" and rec["platform"] == "instagram" and rec["count"] == 3
    assert "instagram.com" not in stream.getvalue()
    assert rec["level"] == "INFO" and rec["ts"].endswith("Z")


def test_exception_tracebacks_are_redacted():
    logger, stream = _capture()
    try:
        raise ValueError(f"cannot fetch {URL} with {fake_bot_token()}")
    except ValueError:
        logger.exception("boom")
    out = stream.getvalue()
    assert "ValueError" in out and "instagram.com" not in out and fake_bot_token() not in out


def test_printf_style_third_party_logging_is_redacted_too():
    logger, stream = _capture()
    logger.warning("request to %s failed (%s)", URL, fake_bot_token())
    out = stream.getvalue()
    assert "instagram.com" not in out and fake_bot_token() not in out


# --- health ----------------------------------------------------------------------------------------------------------
def hb(**kw):
    base = dict(ts=1000.0, poll_age=5.0, db_ok=True, worker_ok=True, disk_free=10_000_000_000, started=0.0)
    base.update(kw)
    return Heartbeat(**base)


def test_healthy_heartbeat():
    assert evaluate_health(hb(), now=1010.0, max_age=90, min_free_bytes=1_000_000_000) == []


@pytest.mark.parametrize(
    ("heartbeat", "now", "needle"),
    [
        (None, 1010.0, "no heartbeat"),
        (hb(ts=1000.0), 1200.0, "stale"),
        (hb(poll_age=500.0), 1010.0, "polling stalled"),
        (hb(poll_age=None, started=0.0), 1010.0, "never succeeded"),
        (hb(db_ok=False), 1010.0, "database"),
        (hb(worker_ok=False), 1010.0, "worker"),
        (hb(disk_free=1_000), 1010.0, "low disk"),
    ],
)
def test_unhealthy_conditions_are_reported(heartbeat, now, needle):
    problems = evaluate_health(heartbeat, now=now, max_age=90, min_free_bytes=1_000_000_000)
    assert any(needle in p for p in problems), problems


def test_polling_not_yet_started_is_tolerated_during_startup_grace():
    assert evaluate_health(hb(poll_age=None, started=990.0), now=1010.0, max_age=90, min_free_bytes=0) == []


def test_heartbeat_roundtrip_is_atomic_and_tolerates_corruption(tmp_path):
    path = tmp_path / "data" / "heartbeat"
    assert read_heartbeat(path) is None
    write_heartbeat(path, hb())
    assert read_heartbeat(path) == hb()
    assert not list(path.parent.glob("*.tmp"))
    path.write_text("{not json")
    assert read_heartbeat(path) is None
