"""T02.9 / T05.3 / T05.6: workspace creation, containment, symlink/path-escape defence, orphan sweep."""

from __future__ import annotations

import os
import sys
import uuid
from datetime import timedelta

import pytest

from app.core.errors import BotError, ErrorCode
from app.services.cleanup import WorkspaceManager


def jid() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def ws(tmp_path):
    t = {"now": 1_000_000.0}
    m = WorkspaceManager(tmp_path / "jobs", orphan_ttl=timedelta(minutes=30), clock=lambda: t["now"])
    m.t = t  # type: ignore[attr-defined]
    return m


def try_symlink(link, target, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")


def test_create_makes_private_directory_and_registers_it(ws):
    j = jid()
    path = ws.create(j)
    assert path.is_dir() and path.parent == ws.base and ws.is_active(j)
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o700


def test_shared_group_mode_is_group_writable_but_never_world_accessible(tmp_path):
    m = WorkspaceManager(tmp_path / "jobs", shared_group=True)
    path = m.create(jid())
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o770


@pytest.mark.parametrize("bad", ["", "..", "../etc", "abc", "G" * 32, "a" * 31, "a" * 33, "a/b" + "c" * 29, "A" * 32])
def test_job_ids_are_validated(ws, bad):
    with pytest.raises(ValueError):
        ws.create(bad)


def test_release_removes_files_and_is_idempotent(ws):
    j = jid()
    path = ws.create(j)
    (path / "a.bin").write_bytes(b"x" * 10)
    (path / "sub").mkdir()
    (path / "sub" / "b.bin").write_bytes(b"y")
    ws.release(j)
    assert not path.exists() and not ws.is_active(j)
    ws.release(j)  # second call must not raise


def test_release_handles_read_only_files(ws):
    j = jid()
    path = ws.create(j)
    f = path / "ro.bin"
    f.write_bytes(b"x")
    os.chmod(f, 0o444)
    ws.release(j)
    assert not path.exists()


# --- containment ---------------------------------------------------------------------------------
def test_accepts_regular_file_inside_workspace(ws):
    path = ws.create(jid())
    f = path / "001.jpg"
    f.write_bytes(b"data")
    assert ws.resolve_inside(path, f) == f.resolve()
    assert ws.resolve_inside(path, "001.jpg") == f.resolve()  # relative names resolve inside


def test_rejects_paths_outside_the_workspace(ws, tmp_path):
    a, b = ws.create(jid()), ws.create(jid())
    other = b / "secret.bin"
    other.write_bytes(b"x")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    for bad in (outside, other, a / ".." / "outside.bin", a / ".." / b.name / "secret.bin"):
        with pytest.raises(BotError) as exc:
            ws.resolve_inside(a, bad)
        assert exc.value.code is ErrorCode.MEDIA_INVALID


def test_rejects_directories_and_missing_files(ws):
    path = ws.create(jid())
    (path / "dir").mkdir()
    for bad in (path / "dir", path / "missing.jpg"):
        with pytest.raises(BotError):
            ws.resolve_inside(path, bad)


def test_rejects_symlinked_file_even_if_target_is_inside(ws):
    path = ws.create(jid())
    real = path / "real.jpg"
    real.write_bytes(b"x")
    try_symlink(path / "link.jpg", real)
    with pytest.raises(BotError):
        ws.resolve_inside(path, path / "link.jpg")


def test_rejects_symlink_escaping_to_a_secret_file(ws, tmp_path):
    secret = tmp_path / "token.txt"
    secret.write_text("super secret")
    path = ws.create(jid())
    try_symlink(path / "innocent.jpg", secret)
    with pytest.raises(BotError):
        ws.resolve_inside(path, path / "innocent.jpg")


def test_rejects_file_reached_through_a_symlinked_directory(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.jpg").write_bytes(b"x")
    path = ws.create(jid())
    try_symlink(path / "d", outside, directory=True)
    with pytest.raises(BotError):
        ws.resolve_inside(path, path / "d" / "x.jpg")


def test_release_never_follows_a_symlink_out_of_the_workspace(ws, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    j = jid()
    path = ws.create(j)
    try_symlink(path / "escape", victim, directory=True)
    ws.release(j)
    assert (victim / "keep.txt").read_text() == "precious"


def test_tree_size_counts_files_but_not_symlink_targets(ws, tmp_path):
    path = ws.create(jid())
    (path / "a.bin").write_bytes(b"x" * 100)
    (path / "n").mkdir()
    (path / "n" / "b.bin").write_bytes(b"y" * 50)
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * 10_000)
    try_symlink(path / "l.bin", big)
    assert ws.tree_size(path) < 10_000


# --- sweeping ---------------------------------------------------------------------------------------
def test_startup_sweep_removes_every_job_shaped_directory_and_nothing_else(ws):
    orphan1, orphan2 = jid(), jid()
    for j in (orphan1, orphan2):
        d = ws.base / j
        d.mkdir()
        (d / "f").write_bytes(b"x")
    foreign = ws.base / "not-a-job-dir"
    foreign.mkdir()
    foreign_file = ws.base / "README.txt"
    foreign_file.write_text("keep me")
    removed = ws.sweep(startup=True)
    assert set(removed) == {orphan1, orphan2}
    assert foreign.exists() and foreign_file.exists()


def test_periodic_sweep_spares_active_jobs_and_young_dirs(ws):
    active = jid()
    ws.create(active)
    young, old = jid(), jid()
    for j in (young, old):
        (ws.base / j).mkdir()
    os.utime(ws.base / old, (ws.t["now"] - 3600, ws.t["now"] - 3600))  # 1 h old
    os.utime(ws.base / active, (ws.t["now"] - 7200, ws.t["now"] - 7200))  # old but ACTIVE
    os.utime(ws.base / young, (ws.t["now"] - 60, ws.t["now"] - 60))
    removed = ws.sweep()
    assert removed == [old]
    assert (ws.base / active).exists() and (ws.base / young).exists()
    assert ws.is_active(active)


def test_free_bytes_is_reported(ws):
    assert ws.free_bytes() > 0
