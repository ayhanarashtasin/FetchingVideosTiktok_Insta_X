"""T05.10: owner alerts - once per episode, never containing URLs or user content."""

from __future__ import annotations

import pytest

from app.services.alerts import AlertMonitor
from tests.fakes import make_bot

GB = 1_000_000_000


def monitor(**kw):
    bot, session = make_bot()
    t = {"now": 1000.0}
    m = AlertMonitor(bot, [111, 222], failure_threshold=kw.pop("threshold", 5), min_free_bytes=GB,
                     clock=lambda: t["now"], **kw)  # fmt: skip
    return m, session, t


def texts(session):
    return [c.text for c in session.of("SendMessage")]


async def test_a_failure_streak_alerts_every_owner_once_until_recovery():
    m, s, _ = monitor()
    assert await m.check(consecutive_failures=4, free_bytes=10 * GB) == []
    sent = await m.check(consecutive_failures=5, free_bytes=10 * GB)
    assert len(sent) == 1 and len(texts(s)) == 2  # one alert, delivered to both owners
    assert await m.check(consecutive_failures=9, free_bytes=10 * GB) == []  # same episode: no repeat
    await m.check(consecutive_failures=0, free_bytes=10 * GB)  # recovered
    assert len(await m.check(consecutive_failures=5, free_bytes=10 * GB)) == 1  # a new streak alerts again


async def test_low_disk_alert_has_a_cooldown():
    m, s, t = monitor()
    assert len(await m.check(consecutive_failures=0, free_bytes=GB // 2)) == 1
    assert await m.check(consecutive_failures=0, free_bytes=GB // 4) == []  # inside the cooldown
    t["now"] += 3601
    assert len(await m.check(consecutive_failures=0, free_bytes=GB // 4)) == 1


async def test_nothing_is_sent_when_everything_is_fine():
    m, s, _ = monitor()
    assert await m.check(consecutive_failures=0, free_bytes=50 * GB) == [] and s.calls == []


async def test_alert_texts_contain_no_urls_or_user_content():
    m, s, _ = monitor()
    await m.check(consecutive_failures=7, free_bytes=GB // 10)
    for text in texts(s):
        assert "http" not in text.lower() and "://" not in text and "@" not in text


async def test_undeliverable_alerts_never_raise():
    m, s, _ = monitor()
    s.fail = lambda method: RuntimeError("telegram down")
    assert len(await m.check(consecutive_failures=5, free_bytes=10 * GB)) == 1  # still reported as sent; no exception


@pytest.mark.parametrize("threshold", [2, 5, 50])
async def test_threshold_is_respected(threshold):
    m, _, _ = monitor(threshold=threshold)
    assert await m.check(consecutive_failures=threshold - 1, free_bytes=10 * GB) == []
    assert len(await m.check(consecutive_failures=threshold, free_bytes=10 * GB)) == 1
