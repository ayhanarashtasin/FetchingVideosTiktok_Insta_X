"""latency_check projection math, anchored to the numbers measured on the owner's PC (2026-11-26)."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.config import Settings
from scripts.latency_check import (
    INVALID_TOKEN,
    TELEGRAM_API,
    LinkTiming,
    UploadProbe,
    mbit,
    project_total,
    recommend_vps_pairing,
    report,
    required_upload_bytes_per_s,
    resolve_api_url,
    tcp_rtt_ms,
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


def test_recommend_vps_pairing():
    assert "Singapore + BOT_API_MODE=local" in recommend_vps_pairing(158.0, 2.0)
    assert "EU/AMS + hosted API" in recommend_vps_pairing(3.0, 158.0)
    far = recommend_vps_pairing(195.0, 70.0)
    assert "Singapore + BOT_API_MODE=local" in far and "EU/AMS + hosted API" in far
    assert "Singapore + BOT_API_MODE=local" in recommend_vps_pairing(None, 2.5)
    assert "EU/AMS + hosted API" in recommend_vps_pairing(4.0, None)
    assert "failed" in recommend_vps_pairing(None, None).lower()


def test_resolve_api_url():
    settings = Settings(_env_file=None, local_bot_api_url="http://127.0.0.1:8081/")
    assert resolve_api_url(None, False, settings) == TELEGRAM_API
    assert resolve_api_url(None, True, settings) == "http://127.0.0.1:8081"
    assert resolve_api_url("http://127.0.0.1:9000/", False, settings) == "http://127.0.0.1:9000"
    with pytest.raises(SystemExit):
        resolve_api_url("http://127.0.0.1:9000", True, settings)


async def test_tcp_rtt_ms_measures_loopback_and_handles_closed_port():
    async def _noop(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_noop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        rtt = await tcp_rtt_ms("127.0.0.1", port, timeout=2.0)
        assert rtt is not None and 0.0 <= rtt < 1000.0
    finally:
        server.close()
        await server.wait_closed()
    assert await tcp_rtt_ms("127.0.0.1", port, timeout=1.0) is None
