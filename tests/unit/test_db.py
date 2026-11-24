"""T02.6: migrations, repository functions, parameterised queries, transaction behaviour."""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from app.core.contracts import DeliveryMode, JobState
from app.core.errors import ErrorCode, IllegalTransitionError
from app.storage.db import Database, ReserveOutcome
from tests.helpers import Clock, make_job


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(clock):
    d = Database(":memory:", clock=clock)
    d.migrate()
    yield d
    d.close()


def test_migrations_apply_once(clock):
    d = Database(":memory:", clock=clock)
    assert d.migrate() == ["001_initial.sql"]
    assert d.migrate() == []  # idempotent
    d.close()


def test_file_database_uses_wal_and_restricted_permissions(tmp_path, clock):
    path = tmp_path / "data" / "bot.sqlite3"
    d = Database(path, clock=clock)
    d.migrate()
    mode = d._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o077 == 0, "database must not be group/world readable"
        assert os.stat(path.parent).st_mode & 0o077 == 0
    assert d.check_writable()
    d.close()


def test_reserve_accepts_then_dedupes_then_limits_per_user(db):
    job, _, _ = make_job(db, message_id=1)
    assert db.reserve_job(job, max_active_per_user=1) is ReserveOutcome.ACCEPTED
    # the SAME telegram event again -> duplicate, not a second job and not "busy"
    assert db.reserve_job(job, max_active_per_user=1) is ReserveOutcome.DUPLICATE
    again = job.model_copy(update={"id": "b" * 32})
    assert db.reserve_job(again, max_active_per_user=1) is ReserveOutcome.DUPLICATE
    # an intentional repeat = a NEW message id: not suppressed by dedupe, but the per-user cap applies
    other, _, _ = make_job(db, message_id=2)
    assert db.reserve_job(other, max_active_per_user=1) is ReserveOutcome.USER_BUSY
    assert db.reserve_job(other, max_active_per_user=2) is ReserveOutcome.ACCEPTED


def test_other_users_are_not_blocked(db):
    a, _, _ = make_job(db, owner_id=1, message_id=1)
    b, _, _ = make_job(db, owner_id=2, message_id=1)
    assert db.reserve_job(a, max_active_per_user=1) is ReserveOutcome.ACCEPTED
    assert db.reserve_job(b, max_active_per_user=1) is ReserveOutcome.ACCEPTED


def test_finished_jobs_free_the_user_slot(db):
    first, _, _ = make_job(db, message_id=1)
    db.reserve_job(first, max_active_per_user=1)
    db.transition(first.id, JobState.CANCELLED)
    second, _, _ = make_job(db, message_id=2)
    assert db.reserve_job(second, max_active_per_user=1) is ReserveOutcome.ACCEPTED


def test_callback_request_key_creates_distinct_job_without_overwriting_the_original(db):
    base, _, _ = make_job(db, message_id=5)
    db.reserve_job(base, max_active_per_user=5)
    db.transition(base.id, JobState.RUNNING)
    db.transition(base.id, JobState.PREPARING)
    db.transition(base.id, JobState.UPLOADING)
    db.transition(base.id, JobState.COMPLETED)
    cb, _, _ = make_job(db, message_id=5, mode=DeliveryMode.ORIGINAL, request_key="cbq-1", parent_job_id=base.id)
    assert db.reserve_job(cb, max_active_per_user=1) is ReserveOutcome.ACCEPTED
    # the same callback delivered twice is idempotent
    dup, _, _ = make_job(db, message_id=5, mode=DeliveryMode.ORIGINAL, request_key="cbq-1", parent_job_id=base.id)
    assert db.reserve_job(dup, max_active_per_user=1) is ReserveOutcome.DUPLICATE
    assert db.get_job(base.id).state is JobState.COMPLETED  # untouched


def test_legal_lifecycle_and_illegal_transitions(db):
    job, _, _ = make_job(db)
    db.reserve_job(job, max_active_per_user=1)
    for state in (JobState.RUNNING, JobState.PREPARING, JobState.UPLOADING, JobState.PARTIAL):
        assert db.transition(job.id, state).state is state
    with pytest.raises(IllegalTransitionError):
        db.transition(job.id, JobState.RUNNING)  # terminal -> anything
    j2, _, _ = make_job(db, message_id=2, owner_id=9)
    db.reserve_job(j2, max_active_per_user=1)
    with pytest.raises(IllegalTransitionError):
        db.transition(j2.id, JobState.UPLOADING)  # QUEUED cannot skip ahead
    with pytest.raises(IllegalTransitionError):
        db.transition(j2.id, JobState.COMPLETED)
    with pytest.raises(KeyError):
        db.transition("f" * 32, JobState.RUNNING)


@pytest.mark.parametrize(
    ("path", "final"),
    [
        ((JobState.RUNNING, JobState.FAILED), JobState.FAILED),
        ((JobState.CANCELLED,), JobState.CANCELLED),
        ((JobState.RUNNING, JobState.PREPARING, JobState.CANCELLED), JobState.CANCELLED),
        ((JobState.RUNNING, JobState.PREPARING, JobState.UPLOADING, JobState.DELIVERY_UNKNOWN), JobState.DELIVERY_UNKNOWN),
        ((JobState.RUNNING, JobState.PREPARING, JobState.UPLOADING, JobState.UNVERIFIED), JobState.UNVERIFIED),
        ((JobState.RUNNING, JobState.INTERRUPTED), JobState.INTERRUPTED),
    ],
)
def test_diagram_paths(db, path, final):
    job, _, _ = make_job(db)
    db.reserve_job(job, max_active_per_user=1)
    for state in path:
        db.transition(job.id, state, error_code=ErrorCode.CANCELLED if state is JobState.CANCELLED else None)
    assert db.get_job(job.id).state is final


def test_hostile_url_is_stored_verbatim_and_never_executed(db):
    # The URL validator already rejects whitespace, but the DB layer must be safe on its own:
    # build the record directly so the validator cannot mask a missing parameterisation.
    nasty = "https://x.com/s/status/1';DROP TABLE jobs;-- \" OR 1=1 /* \x00 */"
    job, _, _ = make_job(db)
    job = job.model_copy(update={"original_url": nasty})
    assert db.reserve_job(job, max_active_per_user=1) is ReserveOutcome.ACCEPTED
    assert db.get_job(job.id).original_url == nasty
    assert db.active_jobs()  # the table still exists and holds the job
    db.set_delivery_mode(1, DeliveryMode.MEDIA)  # and other tables are intact


def test_validator_blocks_the_same_payload_before_it_reaches_the_db():
    from app.core.errors import BotError
    from app.core.url_validator import validate_url

    with pytest.raises(BotError):
        validate_url("https://x.com/someuser/status/1700000000000?q=1'; DROP TABLE jobs;--")


def test_delivery_rows_update_counters_and_ordering(db):
    job, _, _ = make_job(db)
    db.reserve_job(job, max_active_per_user=1)
    db.record_delivery(job.id, 0, [0, 1, 2], "sent", [11, 12, 13])
    db.record_delivery(job.id, 1, [3, 4], "unknown", [])
    db.record_delivery(job.id, 2, [5], "failed", [], ErrorCode.FILE_TOO_LARGE)
    db.record_delivery(job.id, 3, [6, 7], "sent", [14, 15])
    j = db.get_job(job.id)
    assert j.delivered_count == 5  # only confirmed ("sent") batches count
    assert j.result_message_ids == [11, 12, 13, 14, 15]
    rows = db.list_deliveries(job.id)
    assert [r.batch_index for r in rows] == [0, 1, 2, 3]
    assert [r.outcome for r in rows] == ["sent", "unknown", "failed", "sent"]
    assert rows[2].error_code == "FILE_TOO_LARGE"


def test_failed_write_rolls_back_completely(db):
    # FK violation (no such job) must leave no partial row behind
    with pytest.raises(sqlite3.IntegrityError):
        db.record_delivery("e" * 32, 0, [0], "sent", [1])
    assert db._conn.execute("SELECT COUNT(*) FROM job_deliveries").fetchone()[0] == 0
    assert not db._conn.in_transaction


def test_startup_recovery_interrupts_only_nonterminal_jobs(db):
    queued, _, _ = make_job(db, owner_id=1)
    running, _, _ = make_job(db, owner_id=2)
    done, _, _ = make_job(db, owner_id=3)
    for j in (queued, running, done):
        db.reserve_job(j, max_active_per_user=1)
    db.transition(running.id, JobState.RUNNING)
    db.transition(done.id, JobState.CANCELLED)
    interrupted = db.interrupt_nonterminal()
    assert {j.id for j in interrupted} == {queued.id, running.id}
    assert db.get_job(queued.id).state is JobState.INTERRUPTED
    assert db.get_job(queued.id).error_code == "INTERRUPTED"
    assert db.get_job(done.id).state is JobState.CANCELLED
    assert db.active_jobs() == []


def test_ttl_purge_removes_expired_urls_and_deliveries(db, clock):
    old, _, _ = make_job(db, owner_id=1, ttl_hours=24)
    db.reserve_job(old, max_active_per_user=1)
    db.record_delivery(old.id, 0, [0], "sent", [1])
    db.transition(old.id, JobState.CANCELLED)
    clock.advance(hours=23)
    fresh, _, _ = make_job(db, owner_id=2, ttl_hours=24)  # TTL starts when the job is created
    db.reserve_job(fresh, max_active_per_user=1)
    db.transition(fresh.id, JobState.CANCELLED)
    clock.advance(hours=2)  # old is now 25h old, fresh is 2h old
    result = db.purge_expired()
    assert (result.jobs, result.deliveries) == (1, 1)
    assert db.get_job(old.id) is None  # its URL is gone
    assert db.get_job(fresh.id) is not None
    assert db._conn.execute("SELECT COUNT(*) FROM job_deliveries").fetchone()[0] == 0


def test_purge_never_deletes_an_active_job(db, clock):
    job, _, _ = make_job(db, ttl_hours=1)
    db.reserve_job(job, max_active_per_user=1)
    db.transition(job.id, JobState.RUNNING)
    clock.advance(hours=5)
    assert db.purge_expired().jobs == 0
    assert db.get_job(job.id) is not None


def test_preferences_default_and_update(db):
    assert db.get_delivery_mode(1, DeliveryMode.MEDIA) is DeliveryMode.MEDIA
    db.set_delivery_mode(1, DeliveryMode.ORIGINAL)
    assert db.get_delivery_mode(1, DeliveryMode.MEDIA) is DeliveryMode.ORIGINAL
    db.set_delivery_mode(1, DeliveryMode.MEDIA)
    assert db.get_delivery_mode(1, DeliveryMode.ORIGINAL) is DeliveryMode.MEDIA
    assert db.get_delivery_mode(2, DeliveryMode.MEDIA) is DeliveryMode.MEDIA  # per owner
