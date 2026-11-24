#!/usr/bin/env python3
"""Pure-stdlib secret / forbidden-file scanner (T00.7). Used by the pre-commit hook and tests.

    python scripts/scan_secrets.py --staged      # what is about to be committed (hook mode)
    python scripts/scan_secrets.py --all         # everything git could add (tracked + untracked,
                                                 #   minus .gitignore'd files)
    python scripts/scan_secrets.py path [path..] # explicit files

Never prints a matched secret - only file, line, rule and a 4-character prefix hint.
Suppress a deliberate false positive by putting ``secret-scan: allow`` on the same line.
Exit status: 0 clean, 1 findings, 2 usage/git error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ALLOW_MARKER = "secret-scan: allow"
MAX_BYTES = 2_000_000

# --- content rules ----------------------------------------------------------------------
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    (
        "session-cookie-netscape",
        re.compile(
            r"\t(?:sessionid|sessionid_ss|auth_token|ct0|sid_tt|ds_user_id|ttwid|msToken|"
            r"csrftoken|rur)\t[^\t\r\n]{8,}"
        ),
    ),
    (
        "session-cookie-inline",
        re.compile(r"\b(?:sessionid|auth_token|sid_tt|ds_user_id)=[A-Za-z0-9%_.\-]{16,}"),
    ),
    ("telegram-api-hash", re.compile(r"(?i)api[_-]?hash[\"']?\s*[=:]\s*[\"']?[0-9a-f]{32}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("url-with-credentials", re.compile(r"\bhttps?://[^/\s:@]+:[^/\s:@]{3,}@[^/\s]+")),
)

# --- forbidden paths (never commit, whatever the content) -----------------------------------
MEDIA_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".m4a", ".opus",
     ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
)
FORBIDDEN_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm", ".session", ".pem", ".key", ".p12"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    hint: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.rule} ({self.hint})"


def forbidden_path_rule(path: str) -> str | None:
    p = PurePosixPath(path.replace("\\", "/"))
    name = p.name.lower()
    directories = {part.lower() for part in p.parts[:-1]}
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "forbidden-env-file"
    if "secrets" in directories:
        return "file-inside-secrets-dir"
    if re.fullmatch(r"cookies.*\.txt", name):
        return "cookies-file"
    if p.suffix.lower() in FORBIDDEN_SUFFIXES:
        return "forbidden-file-type"
    if p.suffix.lower() in MEDIA_EXTENSIONS:
        return "media-file (downloaded third-party media must never be committed)"
    return None


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule, pattern in RULES:
            m = pattern.search(line)
            if m:
                findings.append(Finding(path, lineno, rule, f"starts with {m.group(0)[:4]!r}..."))
    return findings


def scan_file(root: Path, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    bad = forbidden_path_rule(rel)
    if bad:
        findings.append(Finding(rel, 0, bad, "path is never allowed in the repository"))
    full = root / rel
    try:
        if not full.is_file() or full.stat().st_size > MAX_BYTES:
            return findings
        data = full.read_bytes()
    except OSError:
        return findings
    if b"\x00" in data[:4096]:  # binary
        return findings
    findings.extend(scan_text(rel, data.decode("utf-8", errors="replace")))
    return findings


def _git(root: Path, *args: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True, timeout=60
    ).stdout
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def staged_files(root: Path) -> list[str]:
    return _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")


def all_candidate_files(root: Path) -> list[str]:
    return _git(root, "ls-files", "-co", "--exclude-standard", "-z")


def scan_paths(root: Path, rel_paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in rel_paths:
        findings.extend(scan_file(root, rel))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args == ["--staged"]:
            files = staged_files(root)
        elif args == ["--all"]:
            files = all_candidate_files(root)
        elif args and not args[0].startswith("--"):
            files = args
        else:
            print(__doc__)
            return 2
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        print(f"secret-scan: git failed: {exc}", file=sys.stderr)
        return 2
    findings = scan_paths(root, files)
    for f in findings:
        print(f"secret-scan: {f}", file=sys.stderr)
    if findings:
        print(f"secret-scan: {len(findings)} finding(s); commit blocked.", file=sys.stderr)
        return 1
    print(f"secret-scan: clean ({len(files)} file(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
