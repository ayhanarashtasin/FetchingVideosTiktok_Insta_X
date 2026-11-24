"""T03.1 / T03.11 / T05.4: the isolated process runner, exercised with REAL processes.

Covers: no-shell argument handling, environment isolation, bounded output, timeouts, process-TREE
kill (grandchildren die too), cancellation, disk-budget kill, closed stdin.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

import pytest

from app.services.subprocess_runner import SubprocessRunner, sanitized_env

PY = sys.executable


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


def env_for(ws, **kw):
    return sanitized_env(ws, **kw)


async def run(ws, code, *, timeout=20, **kw):
    return await SubprocessRunner().run([PY, "-c", code], cwd=ws, env=env_for(ws), timeout=timeout, **kw)


async def test_captures_output_and_exit_code(ws):
    r = await run(ws, "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    assert r.returncode == 3 and r.stdout_text().strip() == "out" and r.stderr_text().strip() == "err"
    assert not r.timed_out and not r.killed_for_budget


async def test_arguments_are_never_interpreted_by_a_shell(ws):
    nasty = ["; echo PWNED", "$(whoami)", "`id`", "&& del /f x", "| cat", "--flag=1", "'quoted'", "a b  c", "é中"]
    r = await SubprocessRunner().run(
        [PY, "-c", "import sys, json; print(json.dumps(sys.argv[1:]))", *nasty],
        cwd=ws,
        env=env_for(ws),
        timeout=20,
    )
    import json

    assert json.loads(r.stdout_text()) == nasty  # delivered byte-for-byte as separate argv items


async def test_child_environment_is_isolated_from_the_bots_secrets(ws, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456789:SECRET-SHOULD-NOT-LEAK")
    monkeypatch.setenv("WORKER_SECRET", "also-secret")
    monkeypatch.setenv("INSTAGRAM_COOKIES_FILE", "/run/secrets/cookies")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    r = await run(ws, "import os, json; print(json.dumps(dict(os.environ)))")
    import json

    child_env = json.loads(r.stdout_text())
    assert not any(
        k.upper() in {"BOT_TOKEN", "WORKER_SECRET", "INSTAGRAM_COOKIES_FILE", "AWS_SECRET_ACCESS_KEY"}
        for k in child_env
    )
    assert "SECRET-SHOULD-NOT-LEAK" not in r.stdout_text()
    assert os.path.normcase(child_env["HOME"]) == os.path.normcase(str(ws))  # tools cannot read ~/.config
    assert child_env["PYTHONUTF8"] == "1"


def test_proxy_variables_only_when_configured(ws):
    assert "HTTPS_PROXY" not in sanitized_env(ws)
    env = sanitized_env(ws, proxy="http://egress-proxy:3128")
    assert env["HTTPS_PROXY"] == env["https_proxy"] == env["HTTP_PROXY"] == "http://egress-proxy:3128"
    assert env["NO_PROXY"] == ""  # nothing may bypass the proxy


async def test_output_is_bounded_and_the_child_is_not_blocked(ws):
    code = "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('e' * 400_000)"
    r = await run(ws, code, max_stdout=1000, max_stderr=500)
    assert r.returncode == 0  # the child finished: excess output was drained, not left to block it
    assert len(r.stdout) == 1000 and r.stdout_truncated
    assert len(r.stderr) == 500 and r.stderr_truncated


async def test_stdin_is_closed(ws):
    r = await run(ws, "import sys; print(repr(sys.stdin.read()))", timeout=10)
    assert r.returncode == 0 and r.stdout_text().strip() == "''"  # EOF immediately, no hang


async def test_timeout_kills_the_process(ws):
    t0 = time.monotonic()
    r = await run(ws, "import time; time.sleep(60)", timeout=1.0)
    assert r.timed_out and time.monotonic() - t0 < 15


GRANDCHILD = """
import subprocess, sys, time
child = (
    "import time, sys\\n"
    "f = open(sys.argv[1], 'ab', buffering=0)\\n"
    "while True:\\n"
    "    f.write(b'x'); time.sleep(0.05)\\n"
)
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]])
time.sleep(60)
"""


async def _grows(path, seconds=0.6):
    before = path.stat().st_size
    await asyncio.sleep(seconds)
    return path.stat().st_size > before


async def _wait_for_file(path, tries=100):
    for _ in range(tries):
        if path.exists() and path.stat().st_size > 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("grandchild never started")


async def test_timeout_kills_grandchildren_too(ws):
    beat = ws / "beat.bin"
    r = await SubprocessRunner().run([PY, "-c", GRANDCHILD, str(beat)], cwd=ws, env=env_for(ws), timeout=2.0)
    assert r.timed_out
    await _wait_for_file(beat)
    assert not await _grows(beat), "the grandchild (e.g. ffmpeg spawned by yt-dlp) survived its parent's timeout"


async def test_cancellation_kills_the_whole_tree_and_propagates(ws):
    beat = ws / "beat.bin"
    task = asyncio.create_task(
        SubprocessRunner().run([PY, "-c", GRANDCHILD, str(beat)], cwd=ws, env=env_for(ws), timeout=60)
    )
    await _wait_for_file(beat)
    assert await _grows(beat), "sanity: the grandchild is alive before cancel"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await _grows(beat), "cancelled job left a running process behind"


async def test_directory_byte_budget_kills_the_process(ws):
    code = (
        "import time\nfor i in range(1000):\n    open(f'part{i}.bin','wb').write(b'x'*200_000)\n    time.sleep(0.05)\n"
    )
    t0 = time.monotonic()
    r = await run(ws, code, timeout=30, watch_dir=ws, max_dir_bytes=500_000)
    assert r.killed_for_budget and time.monotonic() - t0 < 15
    assert sum(p.stat().st_size for p in ws.glob("part*.bin")) < 5_000_000  # stopped long before 200 MB


async def test_low_disk_headroom_kills_the_process(ws):
    free = shutil.disk_usage(ws).free
    r = await run(ws, "import time; time.sleep(30)", timeout=30, watch_dir=ws, min_free_bytes=free + 10**12)
    assert r.killed_for_budget


async def test_no_watcher_overhead_when_unconfigured(ws):
    r = await run(ws, "print(1)")
    assert r.returncode == 0 and not r.killed_for_budget


async def test_missing_binary_is_an_error_not_a_hang(ws):
    with pytest.raises((FileNotFoundError, OSError)):
        await SubprocessRunner().run(["definitely-not-a-real-binary-xyz"], cwd=ws, env=env_for(ws), timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
async def test_core_dumps_are_disabled_on_posix(ws):
    r = await run(
        ws,
        "import resource; print(resource.getrlimit(resource.RLIMIT_CORE)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0])",
    )
    assert r.stdout_text().split() == ["0", "1024"]
