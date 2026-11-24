"""T00.7: the pre-commit secret scan must catch planted secrets and pass on the real repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import scan_secrets as scan

REPO = Path(__file__).resolve().parents[2]


def rules(findings):
    return {f.rule for f in findings}


# Secret-shaped samples are assembled at runtime so this file itself stays clean.
TOKEN = "123456789" + ":" + "A" * 35
COOKIE_LINE = ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\t" + "a1b2c3d4e5f6" * 3
API_HASH = "api_hash = '" + "0123456789abcdef" * 2 + "'"
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a" * 36
URL_WITH_CREDS = "https://" + "user:hunter2pass" + "@example.com/path"


@pytest.mark.parametrize(
    ("sample", "rule"),
    [
        (f"BOT_TOKEN={TOKEN}", "telegram-bot-token"),
        (f"x = '{TOKEN}'", "telegram-bot-token"),
        (COOKIE_LINE, "session-cookie-netscape"),
        ("Cookie: sessionid=" + "a1b2c3d4e5f6a7b8c9", "session-cookie-inline"),
        (API_HASH, "telegram-api-hash"),
        (PRIVATE_KEY, "private-key-block"),
        (AWS, "aws-access-key"),
        (GH, "github-token"),
        (URL_WITH_CREDS, "url-with-credentials"),
    ],
)
def test_content_rules_detect_planted_secrets(sample, rule):
    assert rule in rules(scan.scan_text("f.txt", sample))


def test_findings_never_contain_the_secret():
    (finding,) = scan.scan_text("f.txt", f"BOT_TOKEN={TOKEN}")
    assert TOKEN not in str(finding)
    assert str(finding).count("1234") == 1  # only the 4-char hint


def test_allow_marker_suppresses_a_deliberate_false_positive():
    assert not scan.scan_text("f.txt", f"example = '{TOKEN}'  # secret-scan: allow")


def test_ordinary_text_and_placeholders_are_clean():
    clean = "BOT_TOKEN=replace_me\nALLOWED_USER_IDS=123456789\nsee https://x.com/user/status/1\n"
    assert not scan.scan_text("f.txt", clean)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", "forbidden-env-file"),
        (".env.production", "forbidden-env-file"),
        ("deploy/.env.local", "forbidden-env-file"),
        ("secrets/instagram_cookies.txt", "file-inside-secrets-dir"),
        ("ops/secrets/token", "file-inside-secrets-dir"),
        ("cookies.txt", "cookies-file"),
        ("data/bot.sqlite3", "forbidden-file-type"),
        ("x.session", "forbidden-file-type"),
        ("clip.mp4", "media-file"),
        ("a/b/photo.JPG", "media-file"),
        ("story.webm", "media-file"),
    ],
)
def test_forbidden_paths(path, expected):
    got = scan.forbidden_path_rule(path)
    assert got is not None and got.startswith(expected)


@pytest.mark.parametrize("path", [".env.example", "app/main.py", "secrets_policy.md", "ops/deploy.md"])
def test_allowed_paths(path):
    assert scan.forbidden_path_rule(path) is None


def test_scan_file_flags_path_and_content(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "x.txt").write_text("hello")
    (tmp_path / "notes.txt").write_text(f"token {TOKEN}\n")
    findings = scan.scan_paths(tmp_path, ["secrets/x.txt", "notes.txt"])
    assert rules(findings) == {"file-inside-secrets-dir", "telegram-bot-token"}


def test_binary_and_oversized_files_are_skipped_for_content(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" + TOKEN.encode())
    assert not scan.scan_paths(tmp_path, ["blob.bin"])


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_the_actual_repository_is_clean():
    """Everything git could add (tracked + untracked, minus .gitignore) passes the scan."""
    files = scan.all_candidate_files(REPO)
    assert files, "expected to find candidate files"
    findings = scan.scan_paths(REPO, files)
    assert not findings, "\n".join(str(f) for f in findings)


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_gitignore_actually_ignores_secret_locations():
    for path in (
        ".env",
        "secrets/instagram_cookies.txt",
        "data/bot.sqlite3",
        "tmp/jobs/x/clip.mp4",
        "tests/live/corpus.txt",
        "downloads/a.jpg",
        "cookies.txt",
    ):
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path], timeout=30)
        assert r.returncode == 0, f"{path} is not git-ignored"
    # ...but the templates must stay trackable (a too-broad `corpus.*.txt` rule once ignored the example)
    for template in (".env.example", "tests/live/corpus.example.txt"):
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", template], timeout=30)
        assert r.returncode == 1, f"{template} must not be git-ignored"
