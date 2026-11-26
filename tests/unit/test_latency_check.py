"""latency_check projection math, anchored to the numbers measured on the owner's PC (2026-11-26)."""

from __future__ import annotations

import math

from scripts.latency_check import (
    INVALID_TOKEN,
    LinkTiming,
    UploadProbe,
    mbit,
    project_total,
    report,
    required_upload_bytes_per_s,
)


def test_projection_reproduces_the_measured_152_second_x_job():
    # 1080p X video: 24,337,627 bytes, pipeline 4.1 s, Telegram upload measured at ~165 KB/s.
    total = project_total(4.1, 24_337_627, 165_000, overhead_s=0.0)
    assert 150 < total < 153


def test_required_rate_and_impossible_budget():
    # 24.3 MB with a 4.1 s pipeline and 1.5 s Telegram allowance leaves 0.4 s -> ~487 Mbit/s needed.
    need = required_upload_bytes_per_s(4.1, 24_337_627, 1.5, 6.0)
    assert 60_000_000 < need < 61_000_000
    # A pipeline that already overruns the budget can never fit, whatever the upload rate.
    assert required_upload_bytes_per_s(5.0, 1, 1.5, 6.0) == math.inf
    assert project_total(1.0, 1, 0.0, 0.0) == math.inf
    assert mbit(math.inf) == "inf" and mbit(125_000) == "1.0 Mbit/s"


def test_report_counts_fail_and_pass_and_errors(capsys):
    slow_home = UploadProbe(bytes_sent=8_000_000, seconds=58.8, status=401, first_request_s=0.5)  # ~136 KB/s
    fast_vps = UploadProbe(bytes_sent=8_000_000, seconds=0.4, status=401, first_request_s=0.01)  # 20 MB/s
    x1080 = LinkTiming("https://x.com/u/status/1", {"discover": 0.4, "download+probe+prepare": 1.0}, 24_337_627)
    broken = LinkTiming("https://x.com/u/status/2", error="BotError: NOT_FOUND_OR_EXPIRED")
    assert report([x1080], slow_home, budget_s=6, overhead_s=1.5) == 1
    assert report([x1080], fast_vps, budget_s=6, overhead_s=1.5) == 0
    assert report([x1080, broken], None, budget_s=6, overhead_s=1.5) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "PASS" in out and "NOT_FOUND_OR_EXPIRED" in out


def test_probe_token_can_never_be_a_real_bot_token():
    bot_id, secret = INVALID_TOKEN.split(":")
    assert int(bot_id) == 0 and set(secret) == {"A"}
