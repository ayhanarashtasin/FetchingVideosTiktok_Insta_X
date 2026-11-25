"""yt-dlp runner (T03.2): metadata discovery, best-format selection input, targeted download.

* Invoked as a subprocess only, through `ProcessRunner` (argument array, minimal environment).
* `--ignore-config` + a sandboxed HOME: no user/system config is ever read.
* `--use-extractors` is an explicit allowlist (`generic`, which follows arbitrary pages, is excluded).
* The URL is always the last argument after `--`, so it can never act as an option.
* `--no-playlist` plus `--playlist-items` keep it to the single target post.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.core.contracts import FormatDescriptor
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.classify import classify_ytdlp
from app.extractors.cli_flags import YTDLP_ALLOWED_EXTRACTORS
from app.extractors.session_file import private_cookie_copy
from app.media.probe import file_name_is_safe
from app.services.subprocess_runner import ProcessRunner, sanitized_env

MAX_JSON_BYTES = 8_000_000
PATHS_FILE = ".paths.txt"
INFO_JSON_NAME = ".yt_info.json"
CONCURRENT_FRAGMENTS = 8
_NONE = "none"


@dataclass(frozen=True)
class YtFormatSource:
    """Worker-internal download hint for one format; never placed on wire contracts or logged."""

    url: str
    protocol: str | None = None
    ext: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class YtEntry:
    id: str
    width: int | None
    height: int | None
    duration: float | None
    extractor_key: str | None
    formats: list[FormatDescriptor] = field(default_factory=list)
    display_id: str | None = None
    sources: dict[str, YtFormatSource] = field(default_factory=dict)


@dataclass(frozen=True)
class YtInfo:
    entries: list[YtEntry]
    is_playlist: bool
    info_json: Path | None = None


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and value > 0 else None


def _format(raw: dict) -> FormatDescriptor | None:
    fid = raw.get("format_id")
    if not isinstance(fid, str) or not fid:
        return None
    if raw.get("ext") == "mhtml" or raw.get("protocol") == "mhtml" or "storyboard" in str(raw.get("format_note", "")):
        return None  # storyboards are image sheets, not video
    vcodec, acodec = raw.get("vcodec"), raw.get("acodec")
    both_unknown = vcodec is None and acodec is None
    has_video = (vcodec != _NONE) if not both_unknown else True
    has_audio = (acodec != _NONE) if not both_unknown else True
    # yt-dlp: None means "unknown", the string 'none' means "absent". Unknown is treated as present.
    size = raw.get("filesize") or raw.get("filesize_approx")
    tbr = raw.get("tbr")
    return FormatDescriptor(
        format_id=fid,
        has_video=has_video,
        has_audio=has_audio,
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
        vcodec=vcodec if isinstance(vcodec, str) else None,
        acodec=acodec if isinstance(acodec, str) else None,
        ext=raw.get("ext") if isinstance(raw.get("ext"), str) else None,
        tbr=float(tbr) if isinstance(tbr, int | float) else None,
        filesize=int(size) if isinstance(size, int | float) else None,
        note=str(raw["format_note"])[:80] if raw.get("format_note") else None,
    )


def _source(raw: dict, fallback_headers: dict[str, str]) -> YtFormatSource | None:
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        return None
    headers = dict(fallback_headers)
    raw_headers = raw.get("http_headers")
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            if isinstance(k, str) and isinstance(v, str):
                headers[k] = v
    cookies = raw.get("cookies")
    if isinstance(cookies, str) and cookies.strip():
        headers.setdefault("Cookie", cookies.strip())
    proto = raw.get("protocol")
    ext = raw.get("ext")
    return YtFormatSource(
        url=url,
        protocol=proto if isinstance(proto, str) else None,
        ext=ext if isinstance(ext, str) else None,
        headers=headers,
    )


def parse_info(stdout: str) -> YtInfo:
    """Parse `yt-dlp -J` output (a single video, or a playlist of the post's entries)."""
    try:
        info = json.loads(stdout)
    except ValueError:
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, "yt-dlp output was not JSON") from None
    if not isinstance(info, dict):
        raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "yt-dlp returned no information")
    is_playlist = info.get("_type") == "playlist"
    raw_entries = info.get("entries") if is_playlist else [info]
    entries: list[YtEntry] = []
    for e in raw_entries or []:
        if not isinstance(e, dict):
            continue  # unavailable entries are reported as null
        entry_headers: dict[str, str] = {}
        if isinstance(e.get("http_headers"), dict):
            entry_headers = {k: v for k, v in e["http_headers"].items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(e.get("cookies"), str) and e["cookies"].strip():
            entry_headers.setdefault("Cookie", e["cookies"].strip())
        formats: list[FormatDescriptor] = []
        sources: dict[str, YtFormatSource] = {}
        for r in e.get("formats") or []:
            if not isinstance(r, dict):
                continue
            fmt = _format(r)
            if fmt is not None:
                formats.append(fmt)
                src = _source(r, entry_headers)
                if src is not None:
                    sources[fmt.format_id] = src
        if not formats and isinstance(e.get("url"), str):  # direct-URL entry without a format list
            raw_direct = {**e, "format_id": str(e.get("format_id") or "direct")}
            single = _format(raw_direct)
            if single is not None:
                formats = [single]
                src = _source(raw_direct, entry_headers)
                if src is not None:
                    sources[single.format_id] = src
        display_id = str(e.get("display_id") or e.get("webpage_url_basename") or "").strip() or None
        entries.append(
            YtEntry(
                id=str(e.get("id") or ""),
                width=_int(e.get("width")),
                height=_int(e.get("height")),
                duration=float(e["duration"]) if isinstance(e.get("duration"), int | float) else None,
                extractor_key=e.get("extractor_key") if isinstance(e.get("extractor_key"), str) else None,
                formats=formats,
                display_id=display_id,
                sources=sources,
            )
        )
    return YtInfo(entries=entries, is_playlist=is_playlist)


class YtDlpRunner:
    def __init__(
        self,
        runner: ProcessRunner,
        *,
        python: str | None = None,
        proxy: str | None = None,
        cookies: Path | None = None,
        allowed_extractors: tuple[str, ...] = YTDLP_ALLOWED_EXTRACTORS,
        socket_timeout: int = 20,
        max_items: int = 30,
        max_file_bytes: int = 4_000_000_000,
        min_free_bytes: int = 0,
        step_timeout: float = 90.0,
    ) -> None:
        self._runner = runner
        self._python = python or sys.executable
        self._proxy = proxy
        self._cookies = cookies
        self._allowed = allowed_extractors
        self._socket_timeout = socket_timeout
        self._max_items = max_items
        self._max_file_bytes = max_file_bytes
        self._min_free = min_free_bytes
        self._step_timeout = step_timeout

    def _home(self, workspace: Path) -> Path:
        home = workspace / ".home"
        home.mkdir(exist_ok=True)
        return home

    def _session(self, workspace: Path, use_cookies: bool) -> Path | None:
        """A private per-job copy of the session file, or None when this call must stay anonymous."""
        return private_cookie_copy(self._cookies, self._home(workspace)) if use_cookies else None

    def _base(self, *, cookies: Path | None) -> list[str]:
        args = [
            self._python,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-cache-dir",
            "--no-warnings",
            "--no-progress",
            "--use-extractors",
            ",".join(self._allowed),
            "--socket-timeout",
            str(self._socket_timeout),
            "--retries",
            "1",
            "--extractor-retries",
            "1",
        ]
        if self._proxy:
            args += ["--proxy", self._proxy]
        if cookies is not None:
            args += ["--cookies", str(cookies)]  # a private per-job copy (yt-dlp saves its jar on exit)
        return args

    async def discover(
        self,
        url: str,
        workspace: Path,
        deadline: Deadline,
        *,
        use_cookies: bool = False,
        hint: str | None = None,
        session_supported: bool = False,
    ) -> YtInfo:
        args = [
            *self._base(cookies=self._session(workspace, use_cookies)),
            "--playlist-items",
            f"1:{self._max_items}",
            "-J",
            "--",
            url,
        ]
        res = await self._runner.run(
            args,
            cwd=workspace,
            env=sanitized_env(self._home(workspace), proxy=self._proxy),
            timeout=deadline.cap(self._step_timeout),
            max_stdout=MAX_JSON_BYTES,
        )
        if res.timed_out:
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "yt-dlp discovery timed out")
        if res.stdout_truncated:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "yt-dlp metadata too large")
        if res.returncode != 0:
            raise classify_ytdlp(res.stderr_text(), res.returncode, session_supported=session_supported, hint=hint)
        raw_json = res.stdout_text()
        info = parse_info(raw_json)
        if not info.entries:
            raise BotError(ErrorCode.NOT_FOUND_OR_EXPIRED, "yt-dlp found no entries")
        info_json: Path | None = None
        if any(e.sources for e in info.entries):
            dest = self._home(workspace) / INFO_JSON_NAME
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(dest, flags, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(raw_json.encode("utf-8"))
            info_json = dest
        return YtInfo(entries=info.entries, is_playlist=info.is_playlist, info_json=info_json)

    async def download(
        self,
        url: str,
        workspace: Path,
        deadline: Deadline,
        *,
        selector: str,
        info_json: Path | None = None,
        max_file_bytes: int | None = None,
        item_index: int | None = None,
        use_cookies: bool = False,
        prefix: str = "yt",
        hint: str | None = None,
        session_supported: bool = False,
    ) -> list[Path]:
        """Download one item and return the final file paths (after any lossless merge)."""
        if not re.fullmatch(r"[a-z]{1,8}", prefix):
            raise ValueError("unsafe file prefix")
        args = self._base(cookies=self._session(workspace, use_cookies))
        if item_index is not None:
            args += ["--playlist-items", str(int(item_index))]
        # `--print` would imply --quiet and hide yt-dlp's own explanation of a skipped download
        # ("File is larger than max-filesize"), so the final paths go to a file instead. yt-dlp
        # resolves that name against the `home` path, i.e. the workspace.
        paths_file = workspace / PATHS_FILE
        paths_file.unlink(missing_ok=True)
        size_cap = min(self._max_file_bytes, max_file_bytes) if max_file_bytes is not None else self._max_file_bytes
        args += [
            "-f",
            selector,
            "--concurrent-fragments",
            str(CONCURRENT_FRAGMENTS),
            "--merge-output-format",
            "mp4/mkv",  # stream-copy merge only; never a re-encode
            "--max-filesize",
            str(size_cap),
            "-P",
            f"home:{workspace}",
            "-P",
            f"temp:{workspace / '.tmp'}",
            "-o",
            f"{prefix}_%(playlist_index|1)s_%(id)s.%(ext)s",
            "--restrict-filenames",
            "--no-mtime",
            "--no-part",
            "--print-to-file",
            "after_move:filepath",
            PATHS_FILE,
        ]
        if info_json is not None and await asyncio.to_thread(info_json.is_file):
            args += ["--load-info-json", str(info_json)]
        else:
            args += ["--", url]
        (workspace / ".tmp").mkdir(exist_ok=True)
        res = await self._runner.run(
            args,
            cwd=workspace,
            env=sanitized_env(self._home(workspace), proxy=self._proxy),
            timeout=deadline.cap(max(self._step_timeout, deadline.remaining())),
            watch_dir=workspace,
            max_dir_bytes=self._max_file_bytes,
            min_free_bytes=self._min_free,
        )
        if res.killed_for_budget:
            raise BotError(ErrorCode.LIMIT_EXCEEDED, "download exceeded the byte or disk budget")
        if res.timed_out:
            raise BotError(ErrorCode.DOWNLOAD_TIMEOUT, "yt-dlp download timed out")
        text = res.stderr_text() + "\n" + res.stdout_text()
        if res.returncode != 0:
            raise classify_ytdlp(text, res.returncode, session_supported=session_supported, hint=hint)
        try:
            listed = paths_file.read_text(encoding="utf-8")
        except OSError:
            listed = ""
        paths = self._parse_paths(listed, workspace)
        if not paths:
            if re.search(r"larger than max-?filesize", text, re.IGNORECASE):
                raise BotError(ErrorCode.LIMIT_EXCEEDED, "file exceeds max-filesize", reason="filesize")
            raise BotError(ErrorCode.EXTRACTOR_CHANGED, "yt-dlp produced no file", reason="no_output")
        return paths

    @staticmethod
    def _parse_paths(stdout: str, workspace: Path) -> list[Path]:
        root = workspace.resolve()
        paths: list[Path] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line)
            if not p.is_absolute():
                p = workspace / p
            try:
                p.resolve().relative_to(root)
            except (ValueError, OSError):
                raise BotError(ErrorCode.MEDIA_INVALID, "yt-dlp reported a path outside the workspace") from None
            if not file_name_is_safe(p.name) or p.suffix in {".part", ".ytdl", ".json"}:
                continue
            paths.append(p)
        return paths
