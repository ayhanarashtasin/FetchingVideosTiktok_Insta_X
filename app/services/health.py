"""Heartbeat + health evaluation (T05.8).

The running bot rewrites a small JSON heartbeat file every few seconds. `app.healthcheck` reads it
and fails when the event loop is frozen, polling has stalled, the database is not writable, disk
headroom is gone, or the worker is unreachable.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Heartbeat:
    ts: float  # wall-clock time of this write
    poll_age: float | None  # seconds since the last successful getUpdates (None = never yet)
    db_ok: bool
    worker_ok: bool
    disk_free: int
    started: float


def write_heartbeat(path: Path, hb: Heartbeat) -> None:
    """Atomic write so the health check never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(hb)), encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        return Heartbeat(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def evaluate_health(
    hb: Heartbeat | None,
    *,
    now: float | None = None,
    max_age: float,
    min_free_bytes: int,
    startup_grace: float = 60.0,
) -> list[str]:
    """Return the list of problems (empty = healthy)."""
    now = time.time() if now is None else now
    if hb is None:
        return ["no heartbeat file"]
    problems: list[str] = []
    if now - hb.ts > max_age:
        problems.append(f"heartbeat is stale ({now - hb.ts:.0f}s old): event loop stuck or process dead")
    if hb.poll_age is None:
        if now - hb.started > startup_grace:
            problems.append("polling has never succeeded")
    elif hb.poll_age > max_age:
        problems.append(f"polling stalled ({hb.poll_age:.0f}s since last successful getUpdates)")
    if not hb.db_ok:
        problems.append("database is not writable")
    if not hb.worker_ok:
        problems.append("extractor worker is unreachable")
    if hb.disk_free < min_free_bytes:
        problems.append(f"low disk space ({hb.disk_free // 1_000_000} MB free)")
    return problems
