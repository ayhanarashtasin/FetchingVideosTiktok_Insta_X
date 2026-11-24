"""SQLite metadata store (T02.6).

Synchronous `sqlite3` behind one lock: every statement is a sub-millisecond local operation and
the bot is a bounded personal-use service, so a thread-pool/async wrapper would add complexity
without benefit. All SQL is parameterised. Reservation and state changes run in explicit
transactions; the database runs in WAL mode.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from app.core.contracts import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    DeliveryMode,
    JobRecord,
    JobState,
    Platform,
)
from app.core.errors import ErrorCode, IllegalTransitionError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


class ReserveOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    USER_BUSY = "user_busy"


@dataclass(frozen=True)
class PurgeResult:
    jobs: int
    deliveries: int


@dataclass(frozen=True)
class DeliveryRow:
    batch_index: int
    positions: list[int]
    outcome: str
    message_ids: list[int]
    error_code: str | None


class Database:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._path = str(path)
        self._clock = clock
        self._lock = threading.RLock()
        if self._path != ":memory:":
            parent = Path(self._path).parent
            parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(parent, 0o700)  # no-op semantics on Windows
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE ... COMMIT / ROLLBACK)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ migrations
    def migrate(self) -> list[str]:
        """Apply pending migrations in order. Returns the names applied."""
        applied: list[str] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {r["name"] for r in self._conn.execute("SELECT name FROM schema_migrations")}
            for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if sql_file.name in done:
                    continue
                script = sql_file.read_text(encoding="utf-8")
                # executescript() commits implicitly, so wrap it manually for atomicity.
                self._conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nINSERT INTO schema_migrations (name, applied_at) VALUES "
                    + f"('{sql_file.name}', '{iso(self._clock())}');"
                    + "\nCOMMIT;"
                )
                applied.append(sql_file.name)
        return applied

    def check_writable(self) -> bool:
        """Health probe: can we write a row inside a rolled-back transaction?"""
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute("CREATE TABLE IF NOT EXISTS _health (x INTEGER)")
                    self._conn.execute("INSERT INTO _health VALUES (1)")
                finally:
                    self._conn.execute("ROLLBACK")
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            owner_id=row["owner_id"],
            chat_id=row["chat_id"],
            origin_message_id=row["origin_message_id"],
            original_url=row["original_url"],
            platform=Platform(row["platform"]),
            delivery_mode=DeliveryMode(row["delivery_mode"]),
            state=JobState(row["state"]),
            request_key=row["request_key"],
            expected_count=row["expected_count"],
            delivered_count=row["delivered_count"],
            result_message_ids=json.loads(row["result_message_ids"]),
            error_code=row["error_code"],
            parent_job_id=row["parent_job_id"],
            status_message_id=row["status_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    def new_job_record(
        self,
        *,
        job_id: str,
        owner_id: int,
        chat_id: int,
        origin_message_id: int,
        original_url: str,
        platform: Platform,
        delivery_mode: DeliveryMode,
        ttl_hours: int,
        request_key: str = "",
        parent_job_id: str | None = None,
    ) -> JobRecord:
        now = self._clock()
        return JobRecord(
            id=job_id,
            owner_id=owner_id,
            chat_id=chat_id,
            origin_message_id=origin_message_id,
            original_url=original_url,
            platform=platform,
            delivery_mode=delivery_mode,
            state=JobState.QUEUED,
            request_key=request_key,
            parent_job_id=parent_job_id,
            created_at=iso(now),
            updated_at=iso(now),
            expires_at=iso(now + timedelta(hours=ttl_hours)),
        )

    def reserve_job(self, job: JobRecord, *, max_active_per_user: int) -> ReserveOutcome:
        """Atomically enforce the per-user active-job cap and insert the job.

        The unique key makes a repeated Telegram event (same chat, message, mode and request key)
        a no-op instead of a second job.
        """
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._tx() as conn:
            dup = conn.execute(
                "SELECT 1 FROM jobs WHERE chat_id = ? AND origin_message_id = ? "
                "AND delivery_mode = ? AND request_key = ?",
                (job.chat_id, job.origin_message_id, job.delivery_mode.value, job.request_key),
            ).fetchone()
            if dup:
                return ReserveOutcome.DUPLICATE
            (active,) = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE owner_id = ? AND state IN ({placeholders})",  # noqa: S608 - only '?' placeholders are interpolated; every value is bound
                (job.owner_id, *[s.value for s in ACTIVE_STATES]),
            ).fetchone()
            if active >= max_active_per_user:
                return ReserveOutcome.USER_BUSY
            conn.execute(
                "INSERT INTO jobs (id, owner_id, chat_id, origin_message_id, original_url, platform, "
                "delivery_mode, state, request_key, expected_count, delivered_count, result_message_ids, "
                "error_code, parent_job_id, status_message_id, created_at, updated_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.owner_id, job.chat_id, job.origin_message_id, job.original_url,
                    job.platform.value, job.delivery_mode.value, job.state.value, job.request_key,
                    job.expected_count, job.delivered_count, json.dumps(job.result_message_ids),
                    job.error_code, job.parent_job_id, job.status_message_id,
                    job.created_at, job.updated_at, job.expires_at,
                ),
            )
        return ReserveOutcome.ACCEPTED

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def transition(
        self,
        job_id: str,
        new_state: JobState,
        *,
        error_code: ErrorCode | str | None = None,
        expected_count: int | None = None,
    ) -> JobRecord:
        """Move a job along the lifecycle diagram; illegal moves raise IllegalTransitionError."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(row["state"])
            if new_state not in ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise IllegalTransitionError(f"{current.value} -> {new_state.value}")
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, "
                "error_code = COALESCE(?, error_code), expected_count = COALESCE(?, expected_count) "
                "WHERE id = ?",
                (
                    new_state.value, iso(self._clock()),
                    str(error_code) if error_code is not None else None,
                    expected_count, job_id,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)

    def set_status_message(self, job_id: str, message_id: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE jobs SET status_message_id = ?, updated_at = ? WHERE id = ?",
                         (message_id, iso(self._clock()), job_id))

    def record_delivery(
        self,
        job_id: str,
        batch_index: int,
        positions: list[int],
        outcome: str,
        message_ids: list[int],
        error_code: ErrorCode | str | None = None,
    ) -> None:
        """Persist one batch outcome and keep the job's confirmed counters in step."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO job_deliveries (job_id, batch_index, positions, outcome, "
                "message_ids, error_code, created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, batch_index, json.dumps(positions), outcome, json.dumps(message_ids),
                 str(error_code) if error_code is not None else None, iso(self._clock())),
            )
            if outcome == "sent":
                row = conn.execute("SELECT delivered_count, result_message_ids FROM jobs WHERE id = ?",
                                   (job_id,)).fetchone()
                if row:
                    ids = json.loads(row["result_message_ids"]) + message_ids
                    conn.execute(
                        "UPDATE jobs SET delivered_count = ?, result_message_ids = ?, updated_at = ? WHERE id = ?",
                        (row["delivered_count"] + len(positions), json.dumps(ids), iso(self._clock()), job_id),
                    )

    def list_deliveries(self, job_id: str) -> list[DeliveryRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_deliveries WHERE job_id = ? ORDER BY batch_index", (job_id,)
            ).fetchall()
        return [
            DeliveryRow(r["batch_index"], json.loads(r["positions"]), r["outcome"],
                        json.loads(r["message_ids"]), r["error_code"])
            for r in rows
        ]

    def active_jobs(self, owner_id: int | None = None) -> list[JobRecord]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        sql = f"SELECT * FROM jobs WHERE state IN ({placeholders})"  # noqa: S608 - only '?' placeholders are interpolated; every value is bound
        params: list[object] = [s.value for s in ACTIVE_STATES]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", params).fetchall()
        return [self._row_to_job(r) for r in rows]

    # ------------------------------------------------------------------ recovery / retention
    def interrupt_nonterminal(self) -> list[JobRecord]:
        """Startup recovery: every job left in a non-terminal state belongs to a dead process."""
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        now = iso(self._clock())
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders})", [s.value for s in ACTIVE_STATES]  # noqa: S608 - only '?' placeholders are interpolated; every value is bound
            ).fetchall()
            conn.execute(
                f"UPDATE jobs SET state = ?, error_code = ?, updated_at = ? WHERE state IN ({placeholders})",  # noqa: S608 - only '?' placeholders are interpolated; every value is bound
                (JobState.INTERRUPTED.value, ErrorCode.INTERRUPTED.value, now, *[s.value for s in ACTIVE_STATES]),
            )
        interrupted = []
        for r in rows:
            job = self._row_to_job(r)
            interrupted.append(job.model_copy(update={"state": JobState.INTERRUPTED,
                                                      "error_code": ErrorCode.INTERRUPTED.value}))
        return interrupted

    def purge_expired(self) -> PurgeResult:
        """Delete jobs (and, by cascade, their delivery rows) whose TTL has passed.

        Only terminal jobs are purged; a still-active job is never deleted from under its worker.
        """
        now = iso(self._clock())
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._tx() as conn:
            (deliveries,) = conn.execute(
                f"SELECT COUNT(*) FROM job_deliveries WHERE job_id IN "  # noqa: S608 - only '?' placeholders interpolated
                f"(SELECT id FROM jobs WHERE expires_at <= ? AND state IN ({placeholders}))",
                (now, *[s.value for s in TERMINAL_STATES]),
            ).fetchone()
            cur = conn.execute(
                f"DELETE FROM jobs WHERE expires_at <= ? AND state IN ({placeholders})",  # noqa: S608 - only '?' placeholders are interpolated; every value is bound
                (now, *[s.value for s in TERMINAL_STATES]),
            )
        return PurgeResult(jobs=cur.rowcount, deliveries=deliveries)

    # ------------------------------------------------------------------ preferences
    def get_delivery_mode(self, owner_id: int, default: DeliveryMode) -> DeliveryMode:
        with self._lock:
            row = self._conn.execute("SELECT delivery_mode FROM preferences WHERE owner_id = ?",
                                     (owner_id,)).fetchone()
        return DeliveryMode(row["delivery_mode"]) if row else default

    def set_delivery_mode(self, owner_id: int, mode: DeliveryMode) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO preferences (owner_id, delivery_mode) VALUES (?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET delivery_mode = excluded.delivery_mode",
                (owner_id, mode.value),
            )
