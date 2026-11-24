"""gallery-dl runner (T03.3). Subprocess ONLY - gallery-dl is GPL-2.0-only and is never imported.

* `--config-ignore` + a sandboxed HOME: no config file is read; every option is passed explicitly
  with `-o key=value` (validated), never assembled from untrusted text.
* `-j` (dump) gives structured item metadata without downloading; the download call names its
  files `NNN.ext` by the item's `num`, so files map back to manifest positions by identity.
* Failures come back as structured `[-1, {...}]` entries plus stderr; both are classified.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.classify import GdError, GdWarnings, classify_gallerydl, classify_text, scan_gallerydl_warnings
from app.services.subprocess_runner import ProcessRunner, sanitized_env

MAX_DUMP_BYTES = 8_000_000
_OPT_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_OPT_VALUE = re.compile(r"^[A-Za-z0-9_.:,-]{0,64}$")
FILENAME_FORMAT = "{num:>03}.{extension}"
_NUM_FILE = re.compile(r"^(\d{3})\.([A-Za-z0-9]{1,8})$")


@dataclass(frozen=True)
class GdDump:
    directories: list[dict[str, Any]]
    files: list[dict[str, Any]]  # per-file metadata, in extractor order
    errors: list[GdError]
    warnings: GdWarnings
    returncode: int | None
    stderr: str = ""


@dataclass(frozen=True)
class GdDownload:
    files: dict[int, Path]  # item `num` -> local file
    returncode: int | None
    warnings: GdWarnings
    stderr: str = field(default="", repr=False)


def parse_dump(stdout: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[GdError]]:
    """Parse `gallery-dl -j`: [[2, {dir}], [3, "url", {file}], [-1, {"error":..., "message":...}], ...]."""
    try:
        messages = json.loads(stdout) if stdout.strip() else []
    except ValueError:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "gallery-dl output was not JSON") from None
    if not isinstance(messages, list):
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "unexpected gallery-dl output shape")
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    errors: list[GdError] = []
    for msg in messages:
        if not isinstance(msg, list) or not msg or not isinstance(msg[0], int):
            continue
        kind = msg[0]
        if kind == 2 and isinstance(msg[-1], dict):
            directories.append(msg[-1])
        elif kind == 3 and len(msg) >= 3 and isinstance(msg[2], dict):
            files.append(
                {
                    **msg[2],
                    "_scheme": str(msg[1]).split(":", 1)[0]
                    if isinstance(msg[1], str) and msg[1].startswith("ytdl:")
                    else "",
                }
            )
        elif kind == -1 and isinstance(msg[-1], dict):
            errors.append(GdError(str(msg[-1].get("error", "")), str(msg[-1].get("message", ""))))
        # kind 6 (queue) is deliberately ignored: following other URLs would leave the target post.
    return directories, files, errors


class GalleryDlRunner:
    def __init__(
        self,
        runner: ProcessRunner,
        *,
        python: str | None = None,
        proxy: str | None = None,
        cookies: Path | None = None,
        http_timeout: int = 20,
        max_bytes: int = 4_000_000_000,
        min_free_bytes: int = 0,
        step_timeout: float = 90.0,
    ) -> None:
        self._runner = runner
        self._python = python or sys.executable
        self._proxy = proxy
        self._cookies = cookies
        self._http_timeout = http_timeout
        self._max_bytes = max_bytes
        self._min_free = min_free_bytes
        self._step_timeout = step_timeout

    @staticmethod
    def _option_args(options: Mapping[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in options.items():
            if not _OPT_KEY.fullmatch(key) or not _OPT_VALUE.fullmatch(str(value)):
                raise ValueError(f"unsafe gallery-dl option {key!r}")
            args += ["-o", f"{key}={value}"]
        return args

    def _home(self, workspace: Path) -> Path:
        home = workspace / ".home"
        home.mkdir(exist_ok=True)
        return home

    def _base(self, options: Mapping[str, str], *, use_cookies: bool) -> list[str]:
        args = [
            self._python,
            "-m",
            "gallery_dl",
            "--config-ignore",
            *self._option_args(options),
            "--http-timeout",
            str(self._http_timeout),
            "--retries",
            "1",
        ]
        if self._proxy:
            args += ["--proxy", self._proxy]
        if use_cookies and self._cookies is not None:
            args += ["--cookies", str(self._cookies)]
        return args

    async def discover(
        self,
        url: str,
        workspace: Path,
        deadline: Deadline,
        *,
        options: Mapping[str, str],
        use_cookies: bool = False,
        hint: str | None = None,
        session_supported: bool = False,
        allow_empty: bool = False,
        empty_means_no_media: bool = False,
    ) -> GdDump:
        """Dump item metadata for ONE target URL without downloading anything.

        `allow_empty` is for extractors that legitimately yield only a Directory record (e.g. a
        profile `/info`); everywhere else an empty result is an error.
        """
        args = [*self._base(options, use_cookies=use_cookies), "--dump-json", "--", url]
        res = await self._runner.run(
            args,
            cwd=workspace,
            env=sanitized_env(self._home(workspace), proxy=self._proxy),
            timeout=deadline.cap(self._step_timeout),
            max_stdout=MAX_DUMP_BYTES,
        )
        if res.timed_out:
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "gallery-dl discovery timed out")
        if res.stdout_truncated:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "gallery-dl metadata too large")
        stderr = res.stderr_text()
        directories, files, errors = parse_dump(res.stdout_text())
        warnings = scan_gallerydl_warnings(stderr)
        if errors or (res.returncode not in (0, None) and not files):
            raise classify_gallerydl(errors, res.returncode, stderr, session_supported=session_supported, hint=hint)
        if not files and empty_means_no_media and not errors and res.returncode in (0, None) and not stderr.strip():
            # The extractor ran cleanly and simply found nothing to download (e.g. a text-only post).
            raise BotError(ErrorCode.UNSUPPORTED_POST, "post has no photos or videos", reason="no_media")
        if not files and not (allow_empty and directories):
            # Extractors such as TikTok's log failures instead of raising, so "no items" is an error.
            raise classify_text(stderr or "no items extracted", session_supported=session_supported, hint=hint)
        return GdDump(directories, files, errors, warnings, res.returncode, stderr)

    async def download(
        self,
        url: str,
        workspace: Path,
        deadline: Deadline,
        *,
        options: Mapping[str, str],
        use_cookies: bool = False,
        hint: str | None = None,
        session_supported: bool = False,
        subdir: str = "gd",
        filename_format: str = FILENAME_FORMAT,
    ) -> GdDownload:
        """Download the target's files into `workspace/subdir` as `NNN.ext` and map them by `num`."""
        if not re.fullmatch(r"[a-z]{1,8}", subdir):
            raise ValueError("unsafe subdirectory")
        if not re.fullmatch(r"[A-Za-z0-9_.{}:>-]{1,40}", filename_format) or "/" in filename_format:
            raise ValueError("unsafe filename format")
        out = workspace / subdir
        out.mkdir(exist_ok=True)
        args = [
            *self._base(options, use_cookies=use_cookies),
            "--directory",
            str(out),
            "--filename",
            filename_format,
            "--no-mtime",
            "--no-part",
            "--",
            url,
        ]
        res = await self._runner.run(
            args,
            cwd=workspace,
            env=sanitized_env(self._home(workspace), proxy=self._proxy),
            timeout=deadline.cap(max(self._step_timeout, deadline.remaining())),
            watch_dir=workspace,
            max_dir_bytes=self._max_bytes,
            min_free_bytes=self._min_free,
        )
        if res.killed_for_budget:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "download exceeded the byte or disk budget")
        if res.timed_out:
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "gallery-dl download timed out")
        stderr = res.stderr_text()
        files: dict[int, Path] = {}
        for p in sorted(out.iterdir()):
            m = _NUM_FILE.match(p.name)
            if not m or p.is_symlink() or not p.is_file() or p.suffix.lower() == ".part":
                continue  # anything we did not name ourselves is ignored, never sent
            files[int(m.group(1))] = p
        if not files:
            raise classify_gallerydl([], res.returncode, stderr, session_supported=session_supported, hint=hint)
        return GdDownload(files, res.returncode, scan_gallerydl_warnings(stderr), stderr)
