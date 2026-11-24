"""T05.3 / T06.4: security gates that must hold for ARBITRARY input (seeded fuzzing, no network).

Hand-written cases live next to each component; this file states the invariants and hammers them:

* the URL validator never crashes, and anything it accepts is a canonical HTTPS URL on an allowed host;
* callback payloads only ever parse when they are exactly `prefix + 32 lowercase hex`;
* the log redactor removes every planted secret, wherever it appears;
* file names from extractors are only accepted if harmless;
* nothing sent to Telegram is ever parsed as markup (no `parse_mode`).
"""

from __future__ import annotations

import random
import re
import string
from urllib.parse import urlsplit

import pytest

from app.bot.keyboards import ORIGINAL_PREFIX, RETRY_PREFIX, parse_job_callback
from app.core.errors import BotError, ErrorCode
from app.core.url_validator import UrlKind, find_urls, validate_url
from app.logging_setup import redact
from app.media.probe import file_name_is_safe

RNG_SEED = 20261124
CANONICAL_HOSTS = {"www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "www.instagram.com", "x.com"}
VALID_SAMPLES = [
    "https://www.tiktok.com/@someuser/video/7300000000000000000",
    "https://www.tiktok.com/@someuser/photo/7300000000000000000?x=1",
    "https://vm.tiktok.com/ZMabcdEfg/",
    "https://www.instagram.com/p/ABCdef12345/?igsh=zz",
    "https://www.instagram.com/reel/ABCdef12345/",
    "https://www.instagram.com/stories/someuser/3000000000000000001/",
    "https://x.com/someuser/status/1700000000000000000",
    "https://twitter.com/someuser/status/1700000000000000000/photo/1",
    "https://mobile.twitter.com/i/status/1700000000000000000",
]
ALPHABET = string.ascii_letters + string.digits + "./:@?#%[]\\ \t\n-_=&;,'\"<>{}|^`~!$*()+ах。．​\x00\x7f"


def mutate(rng: random.Random, text: str) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 4)):
        op = rng.choice("ird")
        pos = rng.randrange(len(chars) + 1)
        if op == "i" or not chars:
            chars.insert(pos, rng.choice(ALPHABET))
        elif op == "r":
            chars[min(pos, len(chars) - 1)] = rng.choice(ALPHABET)
        else:
            del chars[min(pos, len(chars) - 1)]
    return "".join(chars)


def assert_safe_outcome(candidate: str) -> bool:
    """Run the validator; assert every accepted result honours the safety invariants. True if accepted."""
    try:
        v = validate_url(candidate)
    except BotError as err:
        assert err.code in {ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_POST}
        return False
    parts = urlsplit(v.extraction_url)
    assert parts.scheme == "https" and parts.hostname in CANONICAL_HOSTS, (candidate, v.extraction_url)
    assert "@" not in parts.netloc and parts.port is None and not parts.query and not parts.fragment
    assert v.extraction_url.isascii() and not re.search(r"[\s\\\x00-\x1f\x7f]", v.extraction_url)
    assert v.original_url == candidate, "the submitted URL is preserved byte for byte"
    assert v.kind in {UrlKind.POST, UrlKind.SHORT_LINK}
    return True


def test_validator_fuzz_pure_noise_never_crashes_and_never_accepts():
    rng = random.Random(RNG_SEED)
    for _ in range(4000):
        noise = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 80)))
        assert_safe_outcome(noise)


def test_validator_fuzz_mutated_valid_urls_keep_the_invariants():
    rng = random.Random(RNG_SEED + 1)
    accepted = rejected = 0
    for _ in range(6000):
        candidate = mutate(rng, rng.choice(VALID_SAMPLES))
        accepted += assert_safe_outcome(candidate)
        rejected += 1
    assert accepted > 100 and accepted < rejected, "the corpus exercises both outcomes"


def test_unmutated_samples_are_all_accepted():
    assert all(assert_safe_outcome(u) for u in VALID_SAMPLES)


@pytest.mark.parametrize(
    "confusable",
    [
        "https://www.tiktok.com。evil.example/@u/video/7300000000000000000",  # ideographic full stop
        "https://www．tiktok.com/@u/video/7300000000000000000",  # fullwidth full stop
        "https://www.tiktok.com​.evil.example/@u/video/7300000000000000000",  # zero-width space
        "https://www.tіktok.com/@u/video/7300000000000000000",  # Cyrillic i
        "https://www.tiktok.com%2eevil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com%00.evil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com\\@evil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com#@evil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com?@evil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com" + ":443" + "@evil.example/@u/video/7300000000000000000",  # userinfo lookalike
        "https:///www.tiktok.com/@u/video/7300000000000000000",
        "https:www.tiktok.com/@u/video/7300000000000000000",
        "https://[www.tiktok.com]/@u/video/7300000000000000000",
        "https://www.tiktok.com." + ":443" + "@127.0.0.1/",  # userinfo lookalike pointing at loopback
        "https://0x7f000001/@u/video/7300000000000000000",
        "https://2130706433/@u/video/7300000000000000000",
        "https://017700000001/@u/video/7300000000000000000",
    ],
)
def test_host_confusion_attempts_are_rejected(confusable):
    assert assert_safe_outcome(confusable) is False


def test_embedded_urls_in_free_text_never_smuggle_a_second_target():
    rng = random.Random(RNG_SEED + 2)
    for _ in range(500):
        text = " ".join(
            rng.choice([*VALID_SAMPLES, "hello", "see:", "(", ")", "https://evil.example/x"])
            for _ in range(rng.randint(1, 4))
        )
        urls = find_urls(text)
        assert len(urls) == len(set(urls))
        assert all(u.startswith(("http://", "https://")) and not re.search(r"\s", u) for u in urls)


# --- callbacks ------------------------------------------------------------------------------------------------------
def test_callback_parser_accepts_only_prefix_plus_32_lowercase_hex():
    rng = random.Random(RNG_SEED + 3)
    good = "0123456789abcdef" * 2
    assert parse_job_callback(ORIGINAL_PREFIX + good, ORIGINAL_PREFIX) == good
    for _ in range(5000):
        payload = "".join(rng.choice(string.printable + "а\u0000") for _ in range(rng.randint(0, 70)))
        out = parse_job_callback(payload, ORIGINAL_PREFIX)
        assert out is None or (re.fullmatch(r"[0-9a-f]{32}", out) and payload == ORIGINAL_PREFIX + out)
    for bad in (good + "0", good[:-1], good.upper(), " " + good, good + "\n", good[:-1] + "g", "../" * 11):
        assert parse_job_callback(ORIGINAL_PREFIX + bad, ORIGINAL_PREFIX) is None
        assert parse_job_callback(RETRY_PREFIX + bad, ORIGINAL_PREFIX) is None


# --- redaction -----------------------------------------------------------------------------------------------------------
def test_redactor_removes_every_planted_secret_wherever_it_sits():
    rng = random.Random(RNG_SEED + 4)

    def token():
        return (
            str(rng.randint(10**8, 10**10 - 1))
            + ":"
            + "".join(rng.choice(string.ascii_letters + string.digits + "_-") for _ in range(35))
        )

    def cookie():
        name = rng.choice(["sessionid", "auth_token", "sid_tt", "ds_user_id", "csrftoken", "ct0"])
        return name, "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(rng.randint(16, 40)))

    def url():
        return f"https://{rng.choice(['www.instagram.com', 'x.com', 'vm.tiktok.com'])}/p/{rng.randint(10**6, 10**9)}/?igsh={rng.randint(10**6, 10**9)}"

    def filler() -> str:
        words = ["job", "state=RUNNING", "items=3", "ok", "failed:", "-", "[x]"]
        return " ".join(rng.choice(words) for _ in range(rng.randint(0, 6)))

    for _ in range(1500):
        tok, (cname, cval), u = token(), cookie(), url()
        line = f"{filler()} {rng.choice([f'bot {tok}', f'token={tok}', tok])} {filler()} {cname}={cval} {filler()} fetching {u} {filler()}"
        out = redact(line)
        assert tok not in out and cval not in out and u not in out and "igsh=" not in out, out


# --- file names -----------------------------------------------------------------------------------------------------------
def test_filename_gate_never_lets_a_dangerous_name_through():
    rng = random.Random(RNG_SEED + 5)
    for _ in range(4000):
        name = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 24)))
        if file_name_is_safe(name):
            assert name and "/" not in name and not name.startswith("-") and all(ord(c) >= 32 for c in name)


# --- no markup, ever ------------------------------------------------------------------------------------------------------
def test_the_bot_never_enables_a_parse_mode(make_settings):
    from app.main import build_bot

    bot = build_bot(make_settings())
    assert bot.default.parse_mode is None, "captions and messages carry untrusted text and must stay plain"


async def test_no_request_to_telegram_ever_carries_a_parse_mode(tmp_path, media):
    """Whole-app check at the HTTP level: a URL stuffed with markup characters is delivered verbatim."""
    from tests.e2e import start_e2e

    # Markdown/HTML-sensitive characters that DO survive URL tokenisation (`<`, `>`, quotes and backticks end a
    # URL in pasted text, so they can never reach a caption in the first place).
    nasty = "https://www.instagram.com/p/ABCdef12345/?q=*_[x](y)~|{}&igsh=%3Cb%3E%3Ca%3E"
    e = await start_e2e(tmp_path)
    try:
        e.tools.gd_dump("https://www.instagram.com/p/ABCdef12345/", "gd_instagram_photo")
        e.tools.gd_files("https://www.instagram.com/p/ABCdef12345/", {1: media.jpg})
        await e.say(nasty, message_id=5)
        await e.idle()
        sent = [c for c in e.server.calls if c.method != "getMe"]
        assert sent and all("parse_mode" not in c.fields for c in sent)
        photo = e.server.calls_of("sendPhoto")[0]
        assert photo.fields["caption"] == f"Original link: {nasty}"  # byte-for-byte, as plain text
    finally:
        await e.stop()
