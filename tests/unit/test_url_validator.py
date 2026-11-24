"""T02.4 / T06.1 / T06.4: URL parsing, host validation, original-URL preservation (AT13, AT25)."""

from __future__ import annotations

import pytest

from app.core.contracts import Platform
from app.core.errors import BotError, ErrorCode
from app.core.url_validator import (
    MAX_URL_LENGTH,
    UrlKind,
    find_urls,
    parse_hosts_config,
    parse_message_url,
    validate_resolved,
    validate_url,
)

# (submitted URL, platform, kind, hint, canonical extraction URL)
ACCEPTED = [
    ("https://www.tiktok.com/@some.user_1/video/7300000000000000000", Platform.TIKTOK,
     UrlKind.POST, "video", "https://www.tiktok.com/@some.user_1/video/7300000000000000000"),
    ("https://www.tiktok.com/@someuser/photo/7300000000000000000?is_from_webapp=1&_r=1",
     Platform.TIKTOK, UrlKind.POST, "photo",
     "https://www.tiktok.com/@someuser/photo/7300000000000000000"),
    ("https://vm.tiktok.com/ZMabcdEfg/", Platform.TIKTOK, UrlKind.SHORT_LINK, None,
     "https://vm.tiktok.com/ZMabcdEfg/"),
    ("https://vt.tiktok.com/ZSabcdEfg", Platform.TIKTOK, UrlKind.SHORT_LINK, None,
     "https://vt.tiktok.com/ZSabcdEfg/"),
    ("https://www.tiktok.com/t/ZTabcdEfg/", Platform.TIKTOK, UrlKind.SHORT_LINK, None,
     "https://www.tiktok.com/t/ZTabcdEfg/"),
    ("https://www.instagram.com/p/ABCdef12345/", Platform.INSTAGRAM, UrlKind.POST, "post",
     "https://www.instagram.com/p/ABCdef12345/"),
    ("https://www.instagram.com/p/ABCdef12345/?img_index=2&igsh=xyz", Platform.INSTAGRAM,
     UrlKind.POST, "post", "https://www.instagram.com/p/ABCdef12345/"),
    ("https://www.instagram.com/reel/ABCdef12345/", Platform.INSTAGRAM, UrlKind.POST, "reel",
     "https://www.instagram.com/reel/ABCdef12345/"),
    ("https://instagram.com/someuser/reel/ABCdef12345/", Platform.INSTAGRAM, UrlKind.POST,
     "reel", "https://www.instagram.com/reel/ABCdef12345/"),
    ("https://www.instagram.com/stories/someuser/3000000000000000000/?utm_source=ig_story",
     Platform.INSTAGRAM, UrlKind.POST, "story",
     "https://www.instagram.com/stories/someuser/3000000000000000000/"),
    ("https://www.instagram.com/share/p/BAbcdefGh/", Platform.INSTAGRAM, UrlKind.SHORT_LINK,
     None, "https://www.instagram.com/share/p/BAbcdefGh/"),
    ("https://x.com/someuser/status/1700000000000000000", Platform.X, UrlKind.POST, "tweet",
     "https://x.com/someuser/status/1700000000000000000"),
    ("https://twitter.com/someuser/status/1700000000000000000?s=20&t=abc", Platform.X,
     UrlKind.POST, "tweet", "https://x.com/someuser/status/1700000000000000000"),
    ("https://mobile.twitter.com/someuser/status/1700000000000000000/photo/2", Platform.X,
     UrlKind.POST, "tweet", "https://x.com/someuser/status/1700000000000000000"),
    ("https://x.com/i/status/1700000000000000000", Platform.X, UrlKind.POST, "tweet",
     "https://x.com/i/status/1700000000000000000"),
    ("HTTPS://X.COM/someuser/status/1700000000000000000", Platform.X, UrlKind.POST, "tweet",
     "https://x.com/someuser/status/1700000000000000000"),
    ("http://x.com/someuser/status/1700000000000000000", Platform.X, UrlKind.POST, "tweet",
     "https://x.com/someuser/status/1700000000000000000"),  # http upgraded for extraction
    ("https://x.com:443/someuser/status/1700000000000000000", Platform.X, UrlKind.POST,
     "tweet", "https://x.com/someuser/status/1700000000000000000"),
]


@pytest.mark.parametrize(("submitted", "platform", "kind", "hint", "extraction"), ACCEPTED)
def test_accepted_shapes(submitted, platform, kind, hint, extraction):
    v = validate_url(submitted)
    assert v.platform is platform
    assert v.kind is kind
    assert v.post_hint == hint
    assert v.extraction_url == extraction
    # FR-08: the exact submitted URL is preserved, separate from the extraction URL.
    assert v.original_url == submitted


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.com/@u/video/7300000000000000000",
        "https://tiktok.com.evil.com/@u/video/7300000000000000000",  # suffix spoof
        "https://www.instagram.com.evil.com/p/ABCdef12345/",
        "https://nottiktok.com/@u/video/7300000000000000000",  # substring spoof
        "https://x.com.evil.example/u/status/1700000000000",
        "https://evil.com/?u=https://x.com/u/status/1700000000000",  # embedded URL
        "https://x.com@evil.com/u/status/1700000000000",  # userinfo trick
        "https://user:pw@x.com/u/status/1700000000000",
        "https://x.com:8443/u/status/1700000000000",  # nonstandard port
        "https://x.com:80/u/status/1700000000000",
        "https://127.0.0.1/u/status/1700000000000",
        "https://[::1]/u/status/1700000000000",
        "https://2130706433/u/status/1700000000000",  # decimal IPv4
        "https://0x7f.0.0.1/u/status/1700000000000",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/u/status/1700000000000",
        "ftp://x.com/u/status/1700000000000",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://x.com\\@evil.com/u/status/1700000000000",  # backslash confusion
        "https://х.com/u/status/1700000000000",  # Cyrillic 'x' homograph
        "https://x.com\u0000/u/status/1700000000000",
        "https://x.com/u/status/1700000000000\n",
        "https://fxtwitter.com/u/status/1700000000000",  # third-party mirror
        "https://t.co/abcdefg",
        "",
    ],
)
def test_rejected_urls_are_invalid(bad):
    with pytest.raises(BotError) as exc:
        validate_url(bad)
    assert exc.value.code is ErrorCode.INVALID_URL


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.tiktok.com/@someuser", "tiktok_path"),
        ("https://www.tiktok.com/", "tiktok_path"),
        ("https://www.tiktok.com/@someuser/video/12", "tiktok_path"),  # id too short
        ("https://www.tiktok.com/tag/foo", "tiktok_path"),
        ("https://vm.tiktok.com/", "tiktok_short"),
        ("https://www.instagram.com/someuser/", "ig_path"),
        ("https://www.instagram.com/explore/tags/foo/", "ig_path"),
        ("https://www.instagram.com/stories/someuser/", "story_id_required"),
        ("https://www.instagram.com/stories/highlights/17800000000000000/", "highlights_unsupported"),
        ("https://x.com/someuser", "x_path"),
        ("https://x.com/home", "x_path"),
        ("https://x.com/someuser/status/1700000000000/analytics", "x_path"),
        ("https://x.com/search?q=foo", "x_path"),
    ],
)
def test_allowed_host_but_unsupported_path(url, reason):
    with pytest.raises(BotError) as exc:
        validate_url(url)
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST
    assert exc.value.reason == reason


def test_length_limit():
    ok_prefix = "https://x.com/someuser/status/1700000000000"
    with pytest.raises(BotError) as exc:
        validate_url(ok_prefix + "?" + "a" * MAX_URL_LENGTH)
    assert exc.value.code is ErrorCode.INVALID_URL
    assert exc.value.reason == "too_long"


# --- message-level parsing (FR-02, AT25) --------------------------------------------------


def test_url_embedded_in_text_with_trailing_punctuation():
    text = "check this out (https://x.com/someuser/status/1700000000000)."
    v = parse_message_url(text)
    assert v.original_url == "https://x.com/someuser/status/1700000000000"


def test_surrounding_quotes_are_not_part_of_the_url():
    v = parse_message_url('see “https://x.com/someuser/status/1700000000000” please')
    assert v.original_url == "https://x.com/someuser/status/1700000000000"


def test_multiple_urls_are_rejected():  # AT25
    text = "https://x.com/a/status/1700000000000 and https://x.com/b/status/1700000000001"
    with pytest.raises(BotError) as exc:
        parse_message_url(text)
    assert exc.value.code is ErrorCode.INVALID_URL
    assert exc.value.reason == "multiple_urls"


def test_same_url_repeated_counts_once():
    u = "https://x.com/a/status/1700000000000"
    assert parse_message_url(f"{u} {u}").original_url == u


def test_supported_plus_unrelated_url_is_still_multiple():
    with pytest.raises(BotError) as exc:
        parse_message_url("https://x.com/a/status/1700000000000 https://example.com/")
    assert exc.value.reason == "multiple_urls"


def test_no_url_in_message():
    with pytest.raises(BotError) as exc:
        parse_message_url("hello there, no link")
    assert exc.value.code is ErrorCode.INVALID_URL
    assert exc.value.reason == "no_url"


def test_schemeless_links_are_not_accepted():
    with pytest.raises(BotError) as exc:
        parse_message_url("x.com/someuser/status/1700000000000")
    assert exc.value.reason == "no_url"


def test_entity_url_is_used_when_text_hides_it():
    v = parse_message_url("tap here", entity_urls=["https://x.com/a/status/1700000000000"])
    assert v.post_id == "1700000000000"


def test_find_urls_dedupes_and_keeps_order():
    text = "https://x.com/a/status/1700000000000 https://x.com/b/status/1700000000001"
    assert find_urls(text, ["https://x.com/a/status/1700000000000"]) == [
        "https://x.com/a/status/1700000000000",
        "https://x.com/b/status/1700000000001",
    ]


# --- configured extra hosts ---------------------------------------------------------------


def test_extra_hosts_must_be_explicitly_configured():
    url = "https://mobile.tiktok.com/@u/video/7300000000000000000"
    with pytest.raises(BotError):
        validate_url(url)
    extra = parse_hosts_config(["mobile.tiktok.com=tiktok"])
    assert validate_url(url, extra_hosts=extra).platform is Platform.TIKTOK


@pytest.mark.parametrize("entry", ["nohost", "x.com/path=x", "a@b=x", "x.com=nonsense", "=x"])
def test_bad_extra_host_entries_rejected(entry):
    with pytest.raises(ValueError):
        parse_hosts_config([entry])


# --- resolved short links -------------------------------------------------------------------


def test_resolved_link_must_be_same_platform_exact_post():
    good = validate_resolved("https://www.tiktok.com/@u/video/7300000000000000000?x=1", Platform.TIKTOK)
    assert good.kind is UrlKind.POST
    with pytest.raises(BotError) as mismatch:
        validate_resolved("https://x.com/u/status/1700000000000", Platform.TIKTOK)
    assert mismatch.value.reason == "platform_mismatch"
    with pytest.raises(BotError) as still_short:
        validate_resolved("https://vm.tiktok.com/ZMabcdEfg/", Platform.TIKTOK)
    assert still_short.value.code is ErrorCode.UNSUPPORTED_POST
    with pytest.raises(BotError):
        validate_resolved("https://evil.com/@u/video/7300000000000000000", Platform.TIKTOK)
