"""END-TO-END against a fake Telegram HTTP server (T02.1, T04.4-T04.9, T05.6, T05.8, T06.3).

The REAL `app.main.run` (polling, middleware, queue, runner, notifier, deliverer, housekeeping, shutdown) and the
REAL aiogram client (HTTP, form/multipart serialisation) are used. Only two things are simulated: Telegram itself
and the external extractor tools (synthetic, see tests/fake_tools.py). So these tests show that a user message
becomes media arriving at the Bot API with the right bytes - but NOT that real Telegram or real platforms behave
the same; that needs a token and a corpus (see tests/live and scripts/telegram_smoke.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.bot import messages
from app.bot.keyboards import original_data
from app.core.contracts import DeliveryMode, JobState, Platform
from app.services.cleanup import WorkspaceManager
from app.services.health import evaluate_health, read_heartbeat
from app.storage.db import Database
from tests.conftest import requires_ffmpeg
from tests.e2e import OWNER, start_e2e
from tests.fake_telegram import FakeTelegramServer

pytestmark = requires_ffmpeg

IG_POST = "https://www.instagram.com/p/ABCdef12345/"
TT_VIDEO = "https://www.tiktok.com/@someuser/video/7300000000000000000"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def app(tmp_path, media):
    e = await start_e2e(tmp_path)
    e.tools.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    e.tools.gd_files(IG_POST, {1: media.jpg, 2: media.mp4_h264_aac, 3: media.png})
    yield e
    await e.stop()


def media_group(server, index=-1):
    call = server.calls_of("sendMediaGroup")[index]
    items = call.json("media")
    uploads = [call.files[i["media"].removeprefix("attach://")] for i in items]
    return call, items, uploads


# ============================================================================== the happy path
async def test_a_carousel_travels_from_a_telegram_message_to_byte_exact_uploads(app, media):  # AT05 / AT14 / AT26
    await app.say(IG_POST, message_id=50)
    await app.idle()

    s = app.server
    order = [c.method for c in s.calls if c.method != "getMe"]
    assert order == [
        "sendMessage",
        "editMessageText",
        "sendMediaGroup",
        "deleteMessage",
    ]
    ack, send, _, _ = s.calls[-4:]
    assert ack.fields["text"] == "Fetching media..." and json.loads(ack.fields["reply_parameters"])["message_id"] == 50
    assert send.fields["text"] == "Sending..."

    (job,) = app.jobs()
    group, items, uploads = media_group(s)
    assert [i["type"] for i in items] == ["photo", "video", "photo"]  # source order and types preserved
    assert items[0]["caption"] == f"{IG_POST}\n240p/120p/100p\nOriginal files: /orig_{job['id']}"
    assert "caption" not in items[1]
    assert items[1]["supports_streaming"] is True and (items[1]["width"], items[1]["height"]) == (160, 120)
    # The bytes that crossed HTTP are the bytes that were downloaded (nothing recompressed or reordered):
    assert [u[1] for u in uploads] == [media.jpg.read_bytes(), media.mp4_h264_aac.read_bytes(), media.png.read_bytes()]
    assert [u[0] for u in uploads] == ["instagram_01.jpg", "instagram_02.mp4", "instagram_03.png"]

    assert (job["state"], job["delivered_count"], job["expected_count"]) == ("COMPLETED", 3, 3)
    assert list(app.settings.work_dir.glob("*")) == [], "the job workspace must be gone"
    assert s.calls[-1].method == "deleteMessage"  # success removes the status message


async def test_original_files_button_refetches_and_sends_the_same_items_as_documents(tmp_path, media):  # AT14 / AT23
    e = await start_e2e(tmp_path)
    try:
        e.tools.gd_dump(IG_POST, "gd_instagram_photo")
        e.tools.gd_files(IG_POST, {1: media.jpg})
        await e.say(IG_POST, message_id=50)
        await e.idle()
        photo = e.server.calls_of("sendPhoto")[-1]
        data = json.loads(photo.fields["reply_markup"])["inline_keyboard"][0][0]["callback_data"]

        await e.tap(data, cq_id="cbq-1")
        await e.server.wait_for(lambda: len(e.server.calls_of("sendDocument")) == 1)
        await e.idle()

        (doc,) = e.server.calls_of("sendDocument")
        assert next(iter(doc.files.values()))[1] == media.jpg.read_bytes()
        assert "reply_markup" not in doc.fields, "an Original-files job offers no Original-files button"
        assert e.server.calls_of("answerCallbackQuery")
        first, second = e.jobs()
        assert (
            second["parent_job_id"] == first["id"]
            and second["delivery_mode"] == "original"
            and second["state"] == "COMPLETED"
        )
        assert e.tools.calls_to("gallery_dl")  # the source was re-fetched, never reconstructed from a Telegram file_id
    finally:
        await e.stop()


async def test_album_original_command_refetches_and_sends_documents(app, media):
    await app.say(IG_POST, message_id=50)
    await app.idle()
    _, items, _ = media_group(app.server)
    cmd = items[0]["caption"].splitlines()[-1].removeprefix("Original files: ")
    assert cmd.startswith("/orig_")

    await app.say(cmd, message_id=51)
    await app.server.wait_for(lambda: len(app.server.calls_of("sendDocument")) == 2)
    await app.idle()

    docs = app.server.calls_of("sendDocument")
    (vid,) = app.server.calls_of("sendVideo")
    assert [
        next(iter(docs[0].files.values()))[1],
        next(iter(vid.files.values()))[1],
        next(iter(docs[1].files.values()))[1],
    ] == [media.jpg.read_bytes(), media.mp4_h264_aac.read_bytes(), media.png.read_bytes()]
    assert len(app.server.calls_of("sendMediaGroup")) == 1  # documents/video don't add another link message
    first, second = app.jobs()
    assert (
        second["parent_job_id"] == first["id"]
        and second["delivery_mode"] == "original"
        and second["state"] == "COMPLETED"
    )


async def test_a_single_video_is_sent_with_sendvideo(tmp_path, media):  # AT01
    e = await start_e2e(tmp_path)
    try:
        from tests.fake_tools import fixture  # noqa: F401

        e.tools.yt_info(TT_VIDEO, "ytdlp_tiktok_video")
        e.tools.yt_file(TT_VIDEO, media.mp4_h264_aac)
        await e.say(f"look {TT_VIDEO}?is_from_webapp=1", message_id=9)
        await e.idle()
        (video,) = e.server.calls_of("sendVideo")
        assert video.fields["caption"] == f"{TT_VIDEO}\n120p"  # query stripped + resolution, no extra words
        assert (video.fields["width"], video.fields["height"], video.fields["supports_streaming"]) == (
            "160",
            "120",
            "true",
        )
        tag = video.fields["video"].removeprefix("attach://")  # aiogram references the multipart part by token
        assert video.files[tag][1] == media.mp4_h264_aac.read_bytes()
        assert "reply_markup" not in video.fields  # no Original files button for videos
        assert not e.server.calls_of("sendMediaGroup")

        await e.say(TT_VIDEO, message_id=10)
        await e.server.wait_for(lambda: len(e.server.calls_of("sendVideo")) == 2)
        await e.idle()
        resend = e.server.calls_of("sendVideo")[-1]
        assert resend.fields["video"] == video.reply["video"]["file_id"] and not resend.files
        assert resend.fields["caption"] == f"{TT_VIDEO}\n120p"
        assert len(e.tools.calls_to("yt_dlp")) == 4  # both requests rechecked and fetched the source
    finally:
        await e.stop()


# ============================================================================== access control
async def test_strangers_and_groups_get_nothing_and_trigger_no_extraction(app):  # AT12
    await app.say(IG_POST, message_id=1, user_id=999)
    await app.say(IG_POST, message_id=2, user_id=OWNER, chat_type="supergroup")
    await app.say("/start", message_id=3, user_id=999)
    await asyncio.sleep(2.5)  # a few poll cycles
    assert [c.method for c in app.server.calls if c.method != "getMe"] == []
    assert app.tools.calls == [] and app.jobs() == []


async def test_public_access_delivers_to_unlisted_users_in_their_own_chats(tmp_path, media):
    e = await start_e2e(tmp_path, public_access=True)
    try:
        e.tools.gd_dump(IG_POST, "gd_instagram_photo")
        e.tools.gd_files(IG_POST, {1: media.jpg})
        await e.tap("set:original", cq_id="set-original", user_id=222)
        await e.server.wait_for(lambda: e.server.calls_of("answerCallbackQuery"))

        await e.say(IG_POST, message_id=1, user_id=444, chat_type="supergroup")
        await e.say(IG_POST, message_id=1, user_id=222)
        await e.say(IG_POST, message_id=1, user_id=333)
        await e.server.wait_for(lambda: len(e.jobs()) >= 2)
        await e.idle()

        jobs = {job["owner_id"]: job for job in e.jobs()}
        assert set(jobs) == {222, 333}
        assert all(job["state"] == "COMPLETED" and job["chat_id"] == uid for uid, job in jobs.items())
        assert [call.chat_id for call in e.server.calls_of("sendDocument")] == [222]
        assert [call.chat_id for call in e.server.calls_of("sendPhoto")] == [333]

        await e.tap(original_data(jobs[333]["id"]), cq_id="other-users-job", user_id=222)
        await e.server.wait_for(lambda: len(e.server.calls_of("answerCallbackQuery")) == 2)
        assert e.server.calls_of("answerCallbackQuery")[-1].fields["text"] == messages.ACTION_NOT_ALLOWED
        assert len(e.jobs()) == 2
    finally:
        await e.stop()


async def test_multiple_links_are_refused_politely(app):  # AT25
    await app.say(f"{IG_POST} https://x.com/a/status/1700000000000", message_id=4)
    await app.server.wait_for(lambda: app.server.calls_of("sendMessage"))
    assert app.server.calls_of("sendMessage")[0].fields["text"] == "Please send one link at a time."
    assert app.jobs() == [] and app.tools.calls == []


async def test_a_redelivered_update_creates_one_job(app):  # AT22
    update = FakeTelegramServer.message_update(OWNER, IG_POST, 77)
    app.server.push(update)
    app.server.push(dict(update))  # the identical update delivered twice
    await app.idle()
    assert len(app.jobs()) == 1 and len(app.server.calls_of("sendMediaGroup")) == 1


# ============================================================================== Telegram-side failures
async def test_a_flood_wait_is_waited_out_and_the_album_is_not_duplicated(app):  # AT20
    app.server.fail("sendMediaGroup", 429, "Too Many Requests: retry after 1", retry_after=1)
    await app.say(IG_POST, message_id=50)
    await app.idle(timeout=15)
    groups = app.server.calls_of("sendMediaGroup")
    assert len(groups) == 2 and [g.reply is not None for g in groups] == [False, True]  # one rejected, one accepted
    assert app.jobs()[0]["state"] == "COMPLETED" and app.jobs()[0]["delivered_count"] == 3


async def test_an_ambiguous_server_error_is_reported_as_unknown_and_never_replayed(app):  # AT18 / AT21
    app.server.fail("sendMediaGroup", 500, "Internal Server Error", times=10)
    await app.say(IG_POST, message_id=50)
    await app.idle()
    assert len(app.server.calls_of("sendMediaGroup")) == 1, (
        "an uncertain delivery must never be sent again automatically"
    )
    (job,) = app.jobs()
    assert job["state"] == "DELIVERY_UNKNOWN"
    final = app.server.calls_of("editMessageText")[-1]
    assert "can't tell whether 3 items arrived" in final.fields["text"] and "did not resend" in final.fields["text"]
    (retry,) = json.loads(final.fields["reply_markup"])["inline_keyboard"][0]
    assert retry["text"] == "Retry" and retry["callback_data"] == f"retry:{job['id']}"


async def test_a_telegram_400_on_an_album_falls_back_to_documents_losslessly(app, media):
    app.server.fail("sendMediaGroup", 400, "Bad Request: wrong file identifier/HTTP URL specified")
    await app.say(IG_POST, message_id=50)
    await app.idle()
    groups = app.server.calls_of("sendMediaGroup")
    assert [g.reply is not None for g in groups] == [False, True]
    assert [i["type"] for i in json.loads(groups[1].fields["media"])] == ["document"] * 3
    assert [f[1] for f in groups[1].files.values()] == [
        media.jpg.read_bytes(),
        media.mp4_h264_aac.read_bytes(),
        media.png.read_bytes(),
    ]
    assert app.jobs()[0]["state"] == "COMPLETED"


# ============================================================================== cancellation / restart / shutdown
def slow_tools(e2e, seconds=30):
    original = e2e.tools.run

    async def slow(*args, **kwargs):
        if "gallery_dl" in args[0]:
            await asyncio.sleep(seconds)
        return await original(*args, **kwargs)

    e2e.tools.run = slow


async def test_cancel_stops_the_job_cleans_up_and_sends_no_media(app):  # AT19
    slow_tools(app)
    await app.say(IG_POST, message_id=50)
    await app.server.wait_for(
        lambda: any(c.fields.get("text") == "Fetching media..." for c in app.server.calls_of("sendMessage"))
    )
    await asyncio.sleep(0.3)
    await app.say("/cancel", message_id=51)
    await app.idle()
    texts = [c.fields.get("text") for c in app.server.calls if c.method in {"sendMessage", "editMessageText"}]
    assert "Cancelling..." in texts and texts[-1] == "Cancelled."
    assert app.jobs()[0]["state"] == "CANCELLED" and not app.server.calls_of("sendMediaGroup")
    assert list(app.settings.work_dir.glob("*")) == []


async def test_shutdown_interrupts_a_running_job_cleans_up_and_closes_cleanly(app):  # T05.6
    slow_tools(app)
    await app.say(IG_POST, message_id=50)
    await app.server.wait_for(
        lambda: any(c.fields.get("text") == "Fetching media..." for c in app.server.calls_of("sendMessage"))
    )
    await asyncio.sleep(0.3)
    assert list(app.settings.work_dir.glob("*")), "the job workspace exists while it runs"
    await asyncio.wait_for(app.stop(), 10)  # SIGTERM-equivalent
    assert app.jobs()[0]["state"] == "INTERRUPTED"
    assert list(app.settings.work_dir.glob("*")) == []
    # the owner was told, with a Retry button (message delivered before the HTTP session closed)
    assert any("restarted while this was running" in (c.fields.get("text") or "") for c in app.server.calls)


async def test_startup_recovery_marks_dead_jobs_removes_orphans_and_notifies(tmp_path):  # AT21 / AT24
    job_id = uuid.uuid4().hex
    seeded: dict[str, Path] = {}

    def seed(settings):
        db = Database(settings.db_path)
        db.migrate()
        rec = db.new_job_record(
            job_id=job_id, owner_id=OWNER, chat_id=OWNER, origin_message_id=33, original_url=IG_POST,
            platform=Platform.INSTAGRAM, delivery_mode=DeliveryMode.MEDIA, ttl_hours=24,
        )  # fmt: skip
        db.reserve_job(rec, max_active_per_user=1)
        db.transition(job_id, JobState.RUNNING)
        db.record_delivery(job_id, 0, [0, 1], "sent", [5, 6])  # two items made it out before the crash
        db.close()
        orphan = WorkspaceManager(settings.work_dir).path_for(job_id)
        orphan.mkdir(parents=True)
        (orphan / "half-downloaded.part").write_bytes(b"x" * 100)
        seeded["orphan"] = orphan

    e = await start_e2e(tmp_path, before_start=seed)
    try:
        await e.server.wait_for(lambda: e.server.calls_of("sendMessage"))
        assert not seeded["orphan"].exists(), "orphaned workspace must be swept at startup"
        (job,) = e.jobs()
        assert (job["state"], job["error_code"]) == ("INTERRUPTED", "INTERRUPTED")
        note = e.server.calls_of("sendMessage")[0]
        assert "restarted" in note.fields["text"] and "2 items had already been sent" in note.fields["text"]
        assert json.loads(note.fields["reply_parameters"])["message_id"] == 33
        (retry,) = json.loads(note.fields["reply_markup"])["inline_keyboard"][0]
        assert retry["callback_data"] == f"retry:{job_id}"
    finally:
        await e.stop()


# ============================================================================== operations
async def test_heartbeat_reports_healthy_polling_and_the_healthcheck_passes(app):  # T05.8
    hb_path = app.settings.heartbeat_file
    await app.server.wait_for(
        lambda: hb_path.exists() and (read_heartbeat(hb_path) or object()).__dict__.get("poll_age") is not None,
        timeout=10,
    )
    hb = read_heartbeat(hb_path)
    assert evaluate_health(hb, max_age=90, min_free_bytes=1_000_000) == []
    assert hb.db_ok and hb.worker_ok and hb.poll_age is not None
    env = {**os.environ, "DATA_DIR": str(app.settings.data_dir), "BOT_TOKEN": "x"}
    proc = subprocess.run(
        [sys.executable, "-m", "app.healthcheck"], cwd=REPO, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


async def test_the_healthcheck_fails_when_the_heartbeat_is_missing(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path / "nothing-here"), "BOT_TOKEN": "x"}
    proc = subprocess.run(
        [sys.executable, "-m", "app.healthcheck"], cwd=REPO, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 1 and "no heartbeat" in proc.stderr


async def test_tiktok_and_instagram_non_native_videos_transcode_and_send_inline(tmp_path, media):
    ig_reel = "https://www.instagram.com/reel/ABCdef12345/"
    e = await start_e2e(tmp_path)
    try:
        e.tools.yt_info(TT_VIDEO, "ytdlp_tiktok_video")
        e.tools.yt_file(TT_VIDEO, media.mp4_hevc_aac)
        e.tools.yt_info(ig_reel, "ytdlp_instagram_reel")
        e.tools.yt_file(ig_reel, media.webm_vp9_opus)

        await e.say(f"{TT_VIDEO}?is_from_webapp=1", message_id=11)
        await e.idle()
        await e.say(f"{ig_reel}?igsh=tracking", message_id=12)
        await e.server.wait_for(lambda: len(e.jobs()) >= 2)
        await e.idle()

        videos = e.server.calls_of("sendVideo")
        assert len(videos) == 2
        assert e.server.calls_of("sendDocument") == []
        assert videos[0].fields["caption"] == f"{TT_VIDEO}\n120p"
        assert videos[1].fields["caption"] == f"{ig_reel}\n120p"
        for call in videos:
            assert (call.fields["width"], call.fields["height"], call.fields["supports_streaming"]) == (
                "160",
                "120",
                "true",
            )
            assert "reply_markup" not in call.fields
    finally:
        await e.stop()
