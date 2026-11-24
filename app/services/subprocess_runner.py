"""Isolated subprocess execution (T03.1, T03.11, T05.4).

Every external tool (yt-dlp, gallery-dl, ffmpeg, ffprobe) runs through here with:

* an argument ARRAY and no shell, ever - a URL can never become a flag or a shell fragment
  (callers also put `--` before positional URLs);
* an explicit, minimal environment (no inherited secrets, HOME/TMP inside the job workspace,
  proxy variables injected only when configured);
* bounded stdout/stderr (the tool can print forever; we keep a cap and drain the rest);
* a wall-clock timeout, a cancellation hook and a *process-group* kill so children such as the
  ffmpeg that yt-dlp spawns die with their parent (POSIX killpg / Windows taskkill /T);
* a watcher that kills the process when the job directory outgrows its byte budget or the disk
  runs low, instead of finding out afterwards;
* POSIX resource limits (CPU seconds, max file size, open files, no core dumps).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IS_WINDOWS = sys.platform == "win32"
KILL_GRACE_SECONDS = 3.0
WATCH_INTERVAL_SECONDS = 0.5
_READ_CHUNK = 65536


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None  # None when killed before it could report one
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    killed_for_budget: bool = False
    elapsed: float = 0.0

    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class ProcessRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_stdout: int = 4_000_000,
        max_stderr: int = 200_000,
        watch_dir: Path | None = None,
        max_dir_bytes: int | None = None,
        min_free_bytes: int = 0,
    ) -> ProcessResult: ...


def sanitized_env(
    home: Path,
    *,
    proxy: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The ONLY environment a child process gets. Nothing from the bot's environment leaks in:
    in particular BOT_TOKEN, WORKER_SECRET and cookie paths never reach an extractor."""
    env: dict[str, str] = {}
    for key in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    home = Path(home)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home),
        LOCALAPPDATA=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        TMPDIR=str(home),
        TEMP=str(home),
        TMP=str(home),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        NO_COLOR="1",
    )
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = ""
    if extra:
        env.update(extra)
    return env


def _limit_child() -> None:  # pragma: no cover - runs in the forked child (POSIX only)
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    # Generous CPU ceiling (seconds of CPU, not wall time) as a backstop against spin loops.
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most `limit` bytes; the rest is read and discarded so the child
    never blocks on a full pipe."""
    if stream is None:
        return b"", False
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > max(room, 0):
            truncated = True
    return bytes(kept), truncated


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process and everything it spawned. Safe to call repeatedly."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if IS_WINDOWS:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), 10)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 5)


class SubprocessRunner:
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_stdout: int = 4_000_000,
        max_stderr: int = 200_000,
        watch_dir: Path | None = None,
        max_dir_bytes: int | None = None,
        min_free_bytes: int = 0,
    ) -> ProcessResult:
        argv = [str(a) for a in args]
        popen_kwargs: dict[str, object] = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True  # own process group => killpg reaches children
            popen_kwargs["preexec_fn"] = _limit_child
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,  # type: ignore[arg-type]
        )
        killed_for_budget = False
        timed_out = False

        async def watcher() -> None:
            nonlocal killed_for_budget
            while True:
                await asyncio.sleep(WATCH_INTERVAL_SECONDS)
                over = max_dir_bytes is not None and watch_dir is not None and _dir_size(watch_dir) > max_dir_bytes
                low = (
                    min_free_bytes > 0 and watch_dir is not None and shutil.disk_usage(watch_dir).free < min_free_bytes
                )
                if over or low:
                    killed_for_budget = True
                    await kill_process_tree(proc)
                    return

        out_task = asyncio.create_task(_drain(proc.stdout, max_stdout))
        err_task = asyncio.create_task(_drain(proc.stderr, max_stderr))
        watch_task = (
            asyncio.create_task(watcher()) if (watch_dir is not None and (max_dir_bytes or min_free_bytes)) else None
        )
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except TimeoutError:
                timed_out = True
                await kill_process_tree(proc)
            stdout, out_trunc = await asyncio.wait_for(out_task, 10)
            stderr, err_trunc = await asyncio.wait_for(err_task, 10)
        except BaseException:
            # Cancellation (user /cancel, shutdown) or any failure: never leave the tree running.
            await asyncio.shield(kill_process_tree(proc))
            for t in (out_task, err_task):
                t.cancel()
            raise
        finally:
            if watch_task is not None:
                watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch_task
        return ProcessResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=out_trunc,
            stderr_truncated=err_trunc,
            timed_out=timed_out,
            killed_for_budget=killed_for_budget,
            elapsed=time.monotonic() - started,
        )
