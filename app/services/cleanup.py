"""Per-job workspaces, path containment and orphan cleanup (T02.9, T05.6, FR-13).

Every job gets its own directory named by an unguessable id. Anything the extractor produces is
untrusted: a path is only accepted if it is a regular file that really lives inside that job's
directory (no symlinks, no `..`, no other job's files).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from app.core.errors import BotError, ErrorCode

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def share_with_group(root: Path, *, keep_private: frozenset[str] = frozenset()) -> int:
    """Make everything under `root` readable, writable and deletable by the shared group. Returns entries changed.

    The extractor worker and the bot are different uids in one group and exchange job files through one
    directory. Anything the worker (or a tool it spawned) creates must stay removable by the bot, whatever
    umask that tool chose; otherwise cleanup fails with PermissionError and workspaces pile up until the
    disk is full. Group bits only - never world bits - and symlinks are never followed or modified.
    Names in `keep_private` (e.g. a per-job copy of a session file) are left exactly as they are.
    """
    if os.name == "nt":  # no POSIX permission model
        return 0
    changed = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            if name in keep_private:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    continue
                want = st.st_mode | (0o070 if stat.S_ISDIR(st.st_mode) else 0o060)
                if want != st.st_mode:
                    os.chmod(path, stat.S_IMODE(want))
                    changed += 1
            except OSError:
                continue  # not ours to change (or already gone); the unchanged entry is reported by cleanup
    with contextlib.suppress(OSError):
        os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode | 0o070))
    return changed


def relaxed_mode(mode: int) -> int:
    """Add owner read/write/execute and keep every other bit. A failed delete must never revoke the GROUP's
    access to a directory: the worker may still need it to clean up the entries it owns."""
    return stat.S_IMODE(mode) | stat.S_IRWXU


class WorkspaceManager:
    def __init__(
        self,
        base: Path,
        *,
        orphan_ttl: timedelta = timedelta(minutes=30),
        shared_group: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Extractors run with the job directory as their cwd and also receive paths inside it.
        self.base = Path(base).absolute()
        self._orphan_ttl = orphan_ttl.total_seconds()
        # 0o700 by default; 0o770 when the extractor worker (a different uid in the same group)
        # must write into the directory. Never world-accessible.
        self._mode = 0o770 if shared_group else 0o700
        self._clock = clock
        self._active: set[str] = set()
        self.base.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.base, self._mode)

    # ------------------------------------------------------------------ lifecycle
    def create(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid job id for workspace")
        path = self.base / job_id
        path.mkdir(mode=self._mode, parents=False, exist_ok=False)
        with contextlib.suppress(OSError):
            os.chmod(path, self._mode)  # mkdir honours the umask; enforce the exact mode
        self._active.add(job_id)
        return path

    def release(self, job_id: str) -> None:
        """Delete the job's directory. Always call from a `finally` block."""
        self._active.discard(job_id)
        if JOB_ID_RE.fullmatch(job_id):
            self._remove(self.base / job_id)

    def clear(self, workspace: Path) -> None:
        """Empty a job directory but keep it (used between retry attempts)."""
        for child in list(Path(workspace).iterdir()):
            self._remove(child)

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active

    def path_for(self, job_id: str) -> Path:
        return self.base / job_id

    # ------------------------------------------------------------------ containment
    def resolve_inside(self, workspace: Path, candidate: Path | str) -> Path:
        """Return the real path of `candidate` if (and only if) it is a regular file inside
        `workspace`, reached without traversing a symlink. Raises MEDIA_INVALID otherwise."""
        workspace = Path(workspace)
        p = Path(candidate)
        if not p.is_absolute():
            p = workspace / p
        try:
            lexical_rel = Path(os.path.abspath(p)).relative_to(os.path.abspath(workspace))
        except ValueError:
            raise BotError(ErrorCode.MEDIA_INVALID, "path is outside the job workspace") from None
        if ".." in lexical_rel.parts:
            raise BotError(ErrorCode.MEDIA_INVALID, "path traversal")
        try:
            real = p.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BotError(ErrorCode.MEDIA_INVALID, "file missing or unresolvable") from None
        if p.is_symlink() or real != workspace.resolve() / lexical_rel:
            raise BotError(ErrorCode.MEDIA_INVALID, "symlink in path")
        try:
            mode = real.lstat().st_mode
        except OSError:
            raise BotError(ErrorCode.MEDIA_INVALID, "file vanished") from None
        if not stat.S_ISREG(mode):
            raise BotError(ErrorCode.MEDIA_INVALID, "not a regular file")
        return real

    def open_validated(self, workspace: Path, candidate: Path | str, *, expected_size: int) -> int:
        """Open a worker-produced file and return a read-only fd that is safe to upload from.

        The extractor shares the work directory with the bot, so a path that was
        valid a moment ago could be swapped for a symlink to one of the BOT's secrets before the
        upload starts. Opening with O_NOFOLLOW and then verifying the *descriptor* (regular file,
        expected size) closes that window: later changes to the path cannot affect the bytes we
        read. The caller owns the fd and must `os.close` it.
        """
        real = self.resolve_inside(workspace, candidate)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(real, flags)
        except OSError:
            raise BotError(ErrorCode.MEDIA_INVALID, "cannot open file safely") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
                raise BotError(ErrorCode.MEDIA_INVALID, "file changed after validation")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def tree_size(self, workspace: Path) -> int:
        """Total bytes below `workspace` without following symlinks."""
        total = 0
        for root, _dirs, files in os.walk(workspace, followlinks=False):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(root, name)).st_size
        return total

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.base).free

    # ------------------------------------------------------------------ sweeping
    def sweep(self, *, startup: bool = False) -> list[str]:
        """Remove orphaned job directories. Never touches an active job.

        At process start nothing is active, so every job-shaped directory is an orphan.
        While running, only directories older than `orphan_ttl` that no live job owns are removed.
        """
        removed: list[str] = []
        now = self._clock()
        try:
            entries = list(self.base.iterdir())
        except OSError:
            return removed
        for entry in entries:
            if not JOB_ID_RE.fullmatch(entry.name) or entry.name in self._active:
                continue  # not ours, or owned by a live job
            try:
                age = now - entry.lstat().st_mtime
            except OSError:
                continue
            if startup or age >= self._orphan_ttl:
                self._remove(entry)
                removed.append(entry.name)
        return removed

    def _remove(self, path: Path) -> None:
        def on_error(func: Callable[..., object], p: str, exc: BaseException) -> None:
            # Read-only files (notably on Windows) block rmtree; relax and retry once.
            with contextlib.suppress(OSError):
                os.chmod(p, relaxed_mode(os.lstat(p).st_mode))
                func(p)

        try:
            if path.is_symlink():
                path.unlink()  # never follow a symlink out of the workspace
            elif path.exists():
                shutil.rmtree(path, onexc=on_error)
        except OSError as exc:
            # Loud on purpose: a workspace that cannot be removed is a leak that ends in a full disk.
            log.error(
                "could not remove workspace %s: %s (check work directory permissions)",
                path.name,
                type(exc).__name__,
            )
