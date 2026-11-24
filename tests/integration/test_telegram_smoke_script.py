"""The smoke script is the owner's evidence generator (T01.8/T01.9) - so it is itself tested, against the fake
Telegram HTTP server (never real Telegram), including the claims it records."""

from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from scripts import telegram_smoke as smoke
from tests.conftest import fake_bot_token, requires_ffmpeg
from tests.fake_telegram import FakeTelegramServer

pytestmark = requires_ffmpeg
OWNER = 111


@pytest.fixture
async def server():
    s = FakeTelegramServer()
    await s.start()
    yield s
    await s.stop()


@pytest.fixture
async def bot(server):
    b = Bot(
        token=fake_bot_token(), session=AiohttpSession(api=TelegramAPIServer.from_base(server.base_url, is_local=True))
    )
    yield b
    await b.session.close()


async def test_hosted_run_proves_original_bytes_are_preserved_and_records_t01_8(server, bot, tmp_path):
    media = smoke.make_media(tmp_path, large=False)
    steps = await smoke.run_smoke(bot, OWNER, media, large=False, mode="hosted")
    by = {s.name: s for s in steps}
    assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps]
    assert "bytes preserved" in by["original photo as document"].detail  # file_size == the source byte count
    assert (
        "caption present" in by["native photo + caption"].detail and "1280x720" in by["native photo + caption"].detail
    )
    sent = [c.method for c in server.calls if c.method != "getMe"]
    assert sent == ["sendPhoto", "sendDocument", "sendVideo", "sendMediaGroup", "sendMessage"]
    summary = smoke.summarise(steps, "hosted", False)
    assert list(summary) == ["T01.8"] and summary["T01.8"].startswith("PASS")


async def test_a_failing_step_makes_the_run_fail_and_is_recorded_as_such(server, bot, tmp_path):
    server.fail("sendVideo", 400, "Bad Request: wrong file")
    steps = await smoke.run_smoke(bot, OWNER, smoke.make_media(tmp_path, large=False), large=False, mode="hosted")
    assert [s.name for s in steps if not s.ok] == ["native video"]
    assert smoke.summarise(steps, "hosted", False)["T01.8"].startswith("FAIL")


def with_large_file(media, tmp_path, size=6_000_000):
    big = tmp_path / "large.mp4"
    big.write_bytes(b"\0" * size)  # stand-in: the script's real >50 MB generator is exercised by hand
    return {**media, "large": big}


async def test_hosted_profile_refusing_the_large_file_is_the_expected_outcome(server, bot, tmp_path):
    media = with_large_file(smoke.make_media(tmp_path, large=False), tmp_path)
    server.max_upload_bytes = 4_000_000  # model the hosted API's ceiling (scaled down for the test)
    steps = await smoke.run_smoke(bot, OWNER, media, large=True, mode="hosted")
    assert all(s.ok for s in steps[:-1]), "the ordinary steps stay under the ceiling and must pass"
    big = steps[-1]
    assert big.name.startswith("large") and big.ok
    assert "refused" in big.detail and "Entity" in big.detail and "as expected" in big.detail
    assert smoke.summarise(steps, "hosted", True)["T01.8"].startswith("PASS")


async def test_hosted_profile_unexpectedly_accepting_it_is_flagged_not_hidden(server, bot, tmp_path):
    media = with_large_file(smoke.make_media(tmp_path, large=False), tmp_path)
    server.max_upload_bytes = None  # the "hosted" API did NOT refuse: our size-limit assumption would be wrong
    steps = await smoke.run_smoke(bot, OWNER, media, large=True, mode="hosted")
    big = steps[-1]
    assert not big.ok and "NOT as expected" in big.detail
    assert smoke.summarise(steps, "hosted", True)["T01.8"].startswith("FAIL")


async def test_local_profile_accepting_the_large_file_is_the_expected_outcome(server, bot, tmp_path):
    media = with_large_file(smoke.make_media(tmp_path, large=False), tmp_path)
    server.max_upload_bytes = None
    steps = await smoke.run_smoke(bot, OWNER, media, large=True, mode="local")
    assert steps[-1].ok and "accepted" in steps[-1].detail and "as expected" in steps[-1].detail
    summary = smoke.summarise(steps, "local", True)
    assert list(summary) == ["T01.9"] and summary["T01.9"].startswith("PASS")


async def test_local_profile_refusing_the_large_file_fails_t01_9(server, bot, tmp_path):
    media = with_large_file(smoke.make_media(tmp_path, large=False), tmp_path)
    server.max_upload_bytes = 4_000_000  # a misconfigured "local" server that still enforces the small ceiling
    steps = await smoke.run_smoke(bot, OWNER, media, large=True, mode="local")
    assert not steps[-1].ok and smoke.summarise(steps, "local", True)["T01.9"].startswith("FAIL")


def test_a_local_profile_run_only_counts_for_t01_9_and_only_with_the_large_upload():
    ok = [
        smoke.Step("native video", True, "m"),
        smoke.Step("large file (> 50 MB)", True, "52.0 MB: accepted on the local profile - as expected"),
    ]
    assert list(smoke.summarise(ok, "local", True)) == ["T01.9"]
    assert smoke.summarise(ok[:1], "local", False) == {}, (
        "a local run without the >50 MB attempt proves nothing about T01.9"
    )
    assert "T01.8" not in smoke.summarise(ok, "local", True), "a local run is not evidence for the hosted API"
