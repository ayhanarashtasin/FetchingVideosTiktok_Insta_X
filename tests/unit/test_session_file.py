"""The operator's session file is read-only and secret: tools only ever see a private per-job copy."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from app.core.errors import BotError, ErrorCode
from app.extractors.session_file import COOKIES_NAME, private_cookie_copy

SESSION_TEXT = "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tFALSE\t4102444800\tdummy\tvalue\n"


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "ws" / ".home"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def original(tmp_path):
    p = tmp_path / "operator-session.txt"
    p.write_text(SESSION_TEXT, encoding="utf-8")
    os.chmod(p, stat.S_IREAD)  # like the read-only production mount
    return p


def test_unset_means_anonymous(home):
    assert private_cookie_copy(None, home) is None
    assert list(home.iterdir()) == []


def test_the_copy_has_the_same_content_lives_in_the_job_home_and_leaves_the_original_alone(home, original):
    before = (original.read_bytes(), original.stat().st_mtime_ns)
    copy = private_cookie_copy(original, home)
    assert copy == home / COOKIES_NAME and copy != original
    assert copy.read_text(encoding="utf-8") == SESSION_TEXT
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_copy_is_private_to_the_worker_user(home, original):
    assert stat.S_IMODE(private_cookie_copy(original, home).stat().st_mode) == 0o600


def test_a_second_call_replaces_the_copy_instead_of_appending(home, original):
    private_cookie_copy(original, home)
    (home / COOKIES_NAME).write_text("tampered by a tool", encoding="utf-8")  # a tool rewrote its copy
    assert private_cookie_copy(original, home).read_text(encoding="utf-8") == SESSION_TEXT


def test_a_missing_file_is_a_typed_error_that_does_not_leak_the_path(home, tmp_path):
    missing = tmp_path / "no-such-dir" / "secret-name.txt"
    with pytest.raises(BotError) as exc:
        private_cookie_copy(missing, home)
    assert exc.value.code is ErrorCode.AUTH_REQUIRED and exc.value.reason == "session_unavailable"
    assert "secret-name" not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_an_implausibly_large_file_is_refused(home, tmp_path):
    big = tmp_path / "huge.txt"
    big.write_bytes(b"#" * 1_000_001)
    with pytest.raises(BotError) as exc:
        private_cookie_copy(big, home)
    assert exc.value.reason == "session_unavailable" and not (home / COOKIES_NAME).exists()


def test_a_directory_in_place_of_the_file_is_refused(home, tmp_path):
    with pytest.raises(BotError):
        private_cookie_copy(tmp_path, home)


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_the_copy_never_follows_a_symlink_planted_at_the_destination(home, original, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite", encoding="utf-8")
    (home / COOKIES_NAME).symlink_to(victim)
    with pytest.raises(BotError):
        private_cookie_copy(original, home)
    assert victim.read_text(encoding="utf-8") == "do not overwrite"
