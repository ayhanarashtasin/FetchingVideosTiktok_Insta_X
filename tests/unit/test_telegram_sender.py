"""T04.4 / T04.5 / T04.6 / T04.8 / T06.3: Telegram delivery against a recording session that really
reads every uploaded stream (AT01-AT04, AT11, AT14, AT17, AT18, AT19, AT20, AT22).

Covers: exact methods and parameters, captions with the exact submitted URL, album batching, byte-exact
uploads, definite-vs-ambiguous failure classification, flood-wait retry, lossless document fallback,
no replay after an uncertain outcome, cancellation, and safe file-descriptor handling.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetMe

from app.core.contracts import (
    Completeness,
    DeliveryMode,
    ExtractionResult,
    ItemKind,
    MediaAsset,
)
from app.core.errors import ErrorCode
from app.services.cleanup import WorkspaceManager
from app.services.telegram_sender import FdInputFile, TelegramDeliverer, classify_send_error
from app.storage.db import Database
from tests.fakes import OWNER, FakeSession, build_test_settings, make_bot
from tests.helpers import make_job

URL = "https://www.instagram.com/p/ABCdef12345/?igsh=tracking"
M = GetMe()  # any TelegramMethod works as the "method" of an aiogram exception


class Env:
    def __init__(self, tmp_path: Path, mode: DeliveryMode = DeliveryMode.MEDIA, **settings) -> None:
        self.settings = build_test_settings(tmp_path, **settings)
        self.db = Database(":memory:")
        self.db.migrate()
        self.ws_mgr = WorkspaceManager(tmp_path / "jobs", orphan_ttl=timedelta(minutes=30))
        job, req, _ = make_job(self.db, owner_id=OWNER, message_id=42, url=URL, mode=mode)
        self.db.reserve_job(job, max_active_per_user=1)
        self.job, self.req = job, req
        self.ws = self.ws_mgr.create(job.id)
        self.bot, self.session = make_bot()
        self.sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        self.deliverer = TelegramDeliverer(self.bot, self.db, self.settings, self.ws_mgr, sleep=fake_sleep)
        self.assets: list[MediaAsset] = []

    def add(self, kind: ItemKind = ItemKind.PHOTO, *, data: bytes | None = None, **kw) -> MediaAsset:
        pos = len(self.assets)
        payload = data if data is not None else bytes([pos + 1]) * (200 + pos)
        suffix = ".jpg" if kind is ItemKind.PHOTO else ".mp4"
        path = self.ws / f"{pos:03d}{suffix}"
        path.write_bytes(payload)
        fields = dict(
            source_item_id=f"i{pos}", position=pos, local_path=path.resolve(), kind=kind,
            mime_type="image/jpeg" if kind is ItemKind.PHOTO else "video/mp4", size_bytes=len(payload),
            width=1080, height=1350, container="jpeg" if kind is ItemKind.PHOTO else "mp4",
        )  # fmt: skip
        if kind is ItemKind.VIDEO:
            fields.update(video_codec="h264", audio_codec="aac", pix_fmt="yuv420p", duration_seconds=7.6)
        fields.update(kw)
        asset = MediaAsset(**fields)
        self.assets.append(asset)
        return asset

    async def deliver(self):
        self.job = self.db.get_job(self.job.id)
        result = ExtractionResult(assets=self.assets, expected_count=len(self.assets), completeness="complete")
        return await self.deliverer.deliver(self.job, self.req, result, self.ws)

    def methods(self) -> list[str]:
        return [type(c).__name__ for c in self.session.calls]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


def fail_n(session: FakeSession, exc_factory, *, times=1, only="SendPhoto"):
    state = {"n": 0}

    def rule(method):
        if type(method).__name__ == only and state["n"] < times:
            state["n"] += 1
            return exc_factory()
        return None

    session.fail = rule


# ===================================================================================== single items
async def test_single_photo_native_with_exact_url_caption_reply_button_and_exact_bytes(env):  # AT03 / AT14
    a = env.add(data=b"\xff\xd8JPEGDATA" * 50)
    out = await env.deliver()
    (call,) = env.session.calls
    assert type(call).__name__ == "SendPhoto" and call.chat_id == OWNER
    assert call.caption == f"Original link: {URL}"  # the exact submitted URL, tracking parameter included
    assert call.reply_parameters.message_id == 42 and call.reply_parameters.allow_sending_without_reply
    (button,) = [b for row in call.reply_markup.inline_keyboard for b in row]
    assert button.text == "Original files" and button.callback_data == f"orig:{env.job.id}"
    assert env.session.uploads == [("instagram_01.jpg", a.local_path.read_bytes())]  # byte-exact stream
    assert (
        out.confirmed_positions == [0]
        and out.completeness is Completeness.COMPLETE
        and len(out.telegram_message_ids) == 1
    )
    assert env.session.timeouts == [env.settings.upload_timeout_seconds]


async def test_single_video_is_sent_natively_and_streamable(env):  # AT01 / AT04
    env.add(ItemKind.VIDEO, data=b"\x00\x00\x00\x18ftypmp42" + b"x" * 500)
    await env.deliver()
    (call,) = env.session.calls
    assert type(call).__name__ == "SendVideo"
    assert (call.width, call.height, call.duration, call.supports_streaming) == (1080, 1350, 8, True)
    assert call.caption == f"Original link: {URL}" and call.reply_markup is not None


async def test_incompatible_video_goes_as_a_document_not_transcoded(env):  # AT16
    env.add(ItemKind.VIDEO, container="mkv", video_codec="vp9", audio_codec="opus", data=b"MKVDATA" * 40)
    await env.deliver()
    (call,) = env.session.calls
    assert type(call).__name__ == "SendDocument" and call.disable_content_type_detection is True
    assert env.session.uploads[0][1] == b"MKVDATA" * 40  # original bytes untouched


async def test_original_mode_sends_documents_without_the_button(tmp_path):  # AT14
    e = Env(tmp_path, DeliveryMode.ORIGINAL)
    a = e.add(data=b"ORIGINALPHOTO" * 30)
    out = await e.deliver()
    (call,) = e.session.calls
    assert type(call).__name__ == "SendDocument" and call.reply_markup is None
    assert e.session.uploads == [("instagram_01.jpg", a.local_path.read_bytes())]
    assert out.completeness is Completeness.COMPLETE
    e.db.close()


# ===================================================================================== albums
async def test_album_is_one_media_group_with_a_caption_on_the_first_item_and_a_closing_link_message(env):  # AT05
    for _ in range(3):
        env.add()
    out = await env.deliver()
    assert env.methods() == ["SendMediaGroup", "SendMessage"]
    group, link = env.session.calls
    assert [type(m).__name__ for m in group.media] == ["InputMediaPhoto"] * 3
    assert [m.caption for m in group.media] == [f"Original link: {URL}", None, None]
    assert getattr(group, "reply_markup", None) is None  # media groups cannot hold a keyboard
    assert group.reply_parameters.message_id == 42
    assert link.text == f"Original link: {URL}" and link.chat_id == OWNER
    (button,) = [b for row in link.reply_markup.inline_keyboard for b in row]
    assert button.callback_data == f"orig:{env.job.id}"
    assert out.confirmed_positions == [0, 1, 2] and len(out.telegram_message_ids) == 4  # 3 media + the link


async def test_mixed_album_keeps_item_types_and_order(env):
    env.add()
    env.add(ItemKind.VIDEO, data=b"VID" * 100)
    env.add()
    await env.deliver()
    group = env.session.calls[0]
    assert [type(m).__name__ for m in group.media] == ["InputMediaPhoto", "InputMediaVideo", "InputMediaPhoto"]
    assert group.media[1].supports_streaming is True and group.media[1].duration == 8
    assert [u[1] for u in env.session.uploads] == [
        a.local_path.read_bytes() for a in env.assets
    ]  # order == source order


async def test_eleven_photos_are_ten_in_a_group_then_one_single_then_the_link(env):  # AT11
    for _ in range(11):
        env.add()
    out = await env.deliver()
    assert env.methods() == ["SendMediaGroup", "SendPhoto", "SendMessage"]
    assert len(env.session.calls[0].media) == 10
    assert env.session.calls[1].caption == f"Original link: {URL}"  # every batch is captioned
    assert env.session.calls[1].reply_markup is None  # the button lives on the final link message
    assert env.session.calls[1].reply_parameters is None  # only the FIRST send replies to the request
    assert out.confirmed_positions == list(range(11))
    rows = env.db.list_deliveries(env.job.id)
    assert [(r.batch_index, len(r.positions), r.outcome) for r in rows] == [(0, 10, "sent"), (1, 1, "sent")]
    assert env.db.get_job(env.job.id).delivered_count == 11


async def test_original_mode_albums_are_document_groups(tmp_path):
    e = Env(tmp_path, DeliveryMode.ORIGINAL)
    for _ in range(3):
        e.add()
    await e.deliver()
    group = e.session.calls[0]
    assert [type(m).__name__ for m in group.media] == ["InputMediaDocument"] * 3
    assert all(m.disable_content_type_detection for m in group.media)
    assert e.session.calls[-1].reply_markup is None  # no "Original files" button in original mode
    e.db.close()


async def test_a_long_url_is_never_truncated_it_gets_its_own_message(env):
    long_url = "https://x.com/someuser/status/1700000000000?" + "a=1&" * 300
    env.job = env.job.model_copy(update={"original_url": long_url})
    env.db._conn.execute("UPDATE jobs SET original_url = ? WHERE id = ?", (long_url, env.job.id))
    env.add()
    await env.deliver()
    photo, link = env.session.calls
    assert photo.caption is None
    assert link.text == f"Original link: {long_url}"


# ===================================================================================== partial / rejected
async def test_oversized_file_is_reported_not_shrunk_and_the_rest_is_delivered(env):  # AT17 / AT18
    env.add()
    env.add(ItemKind.VIDEO, size_bytes=60_000_000, data=b"V" * 100)  # claims 60 MB on the hosted profile
    env.add()
    out = await env.deliver()
    assert out.confirmed_positions == [0, 2] and out.failed_positions == [1]
    assert out.failure_codes == {1: ErrorCode.FILE_TOO_LARGE} and out.completeness is Completeness.PARTIAL
    assert len(env.session.uploads) == 2  # the oversized item was never attempted


async def test_the_local_profile_accepts_what_hosted_rejects(tmp_path):
    """Same 3 KB video; caps lowered so the hosted ceiling (1 KB) rejects it and the local one (100 KB) allows it."""
    caps = dict(hosted_file_max_bytes=1000, local_file_max_bytes=100_000)
    hosted = Env(tmp_path / "h", bot_api_mode="hosted", **caps)
    hosted.add(ItemKind.VIDEO, data=b"V" * 3000)
    rejected = await hosted.deliver()
    assert rejected.failure_codes == {0: ErrorCode.FILE_TOO_LARGE} and hosted.session.uploads == []

    local = Env(tmp_path / "l", bot_api_mode="local", **caps)
    local.add(ItemKind.VIDEO, data=b"V" * 3000)
    accepted = await local.deliver()
    assert accepted.confirmed_positions == [0] and accepted.failed_positions == []
    hosted.db.close()
    local.db.close()


# ===================================================================================== failure handling
async def test_flood_wait_is_waited_out_then_sent_exactly_once(env):  # AT20
    env.add()
    fail_n(env.session, lambda: TelegramRetryAfter(M, "Flood control", 3), times=1)
    out = await env.deliver()
    assert env.sleeps == [4.0]  # retry_after + 1
    assert env.methods() == ["SendPhoto", "SendPhoto"] and len(env.session.uploads) == 1  # one SUCCESSFUL send
    assert out.confirmed_positions == [0]


async def test_persistent_flood_wait_is_a_definite_failure_and_stops_further_sends(env):
    for _ in range(12):
        env.add()
    fail_n(env.session, lambda: TelegramRetryAfter(M, "Flood control", 5), times=99, only="SendMediaGroup")
    out = await env.deliver()
    assert env.methods().count("SendMediaGroup") == 3  # 1 try + 2 bounded retries
    assert set(out.failed_positions) == set(range(12)) and out.unknown_positions == []
    assert out.failure_codes[0] is ErrorCode.RATE_LIMITED and out.completeness is Completeness.FAILED


async def test_a_long_flood_wait_is_not_slept_through(env):
    env.add()
    fail_n(env.session, lambda: TelegramRetryAfter(M, "Flood control", 3600), times=99)
    out = await env.deliver()
    assert env.sleeps == [] and out.failed_positions == [0]


async def test_ambiguous_network_failure_is_unknown_not_resent_and_halts_the_rest(env):  # AT18 / AT21
    for _ in range(25):
        env.add()  # 10 + 10 + 5 -> three batches
    fail_n(env.session, lambda: TelegramNetworkError(M, "Request timeout error"), times=99, only="SendMediaGroup")
    out = await env.deliver()
    assert env.methods() == ["SendMediaGroup"], "an uncertain send must NOT be replayed and nothing may follow it"
    assert out.unknown_positions == list(range(10))
    assert out.confirmed_positions == [] and set(out.failed_positions) == set(range(10, 25))  # never attempted
    rows = env.db.list_deliveries(env.job.id)
    assert [(r.batch_index, r.outcome) for r in rows] == [(0, "unknown")]


@pytest.mark.parametrize("exc", [TelegramServerError(M, "Bad Gateway"), TimeoutError(), ConnectionResetError()])
async def test_server_errors_and_timeouts_are_ambiguous_too(env, exc):
    env.add()
    fail_n(env.session, lambda: exc, times=99)
    out = await env.deliver()
    assert out.unknown_positions == [0] and env.methods() == ["SendPhoto"]


async def test_too_large_is_a_definite_rejection_not_an_unknown(env):
    """TelegramEntityTooLarge subclasses TelegramNetworkError; it must still be classified as DEFINITE."""
    assert issubclass(TelegramEntityTooLarge, TelegramNetworkError)
    env.add()
    fail_n(env.session, lambda: TelegramEntityTooLarge(M, "Request Entity Too Large"), times=99)
    out = await env.deliver()
    assert out.failed_positions == [0] and out.unknown_positions == []
    assert out.failure_codes == {0: ErrorCode.FILE_TOO_LARGE}


async def test_a_rejected_native_photo_is_resent_once_as_a_document_losslessly(env):
    a = env.add(data=b"PHOTOBYTES" * 20)
    fail_n(env.session, lambda: TelegramBadRequest(M, "PHOTO_INVALID_DIMENSIONS"), times=1)
    out = await env.deliver()
    assert env.methods() == ["SendPhoto", "SendDocument"]
    assert env.session.uploads == [("instagram_01.jpg", a.local_path.read_bytes())]  # same bytes, now as a file
    assert out.confirmed_positions == [0] and out.completeness is Completeness.COMPLETE


async def test_a_rejected_album_falls_back_to_a_document_album(env):
    for _ in range(3):
        env.add()
    fail_n(env.session, lambda: TelegramBadRequest(M, "bad album"), times=1, only="SendMediaGroup")
    out = await env.deliver()
    groups = [c for c in env.session.calls if type(c).__name__ == "SendMediaGroup"]
    assert [type(m).__name__ for m in groups[1].media] == ["InputMediaDocument"] * 3
    assert out.confirmed_positions == [0, 1, 2]


async def test_if_the_document_fallback_is_rejected_too_it_is_a_definite_failure_and_the_rest_continues(env):
    for _ in range(11):
        env.add()
    state = {"n": 0}

    def rule(method):
        if type(method).__name__ in {"SendMediaGroup"}:
            state["n"] += 1
            return TelegramBadRequest(M, "nope")
        return None

    env.session.fail = rule
    out = await env.deliver()
    assert set(out.failed_positions) == set(range(10)) and out.confirmed_positions == [10]  # the single still went out
    assert out.failure_codes[0] is ErrorCode.UPLOAD_FAILED and out.completeness is Completeness.PARTIAL


async def test_a_blocked_bot_stops_everything(env):
    for _ in range(12):
        env.add()
    fail_n(
        env.session, lambda: TelegramForbiddenError(M, "bot was blocked by the user"), times=99, only="SendMediaGroup"
    )
    out = await env.deliver()
    assert env.methods() == ["SendMediaGroup"] and out.confirmed_positions == []


async def test_failure_of_the_closing_link_message_does_not_change_the_outcome(env):
    for _ in range(3):
        env.add()
    fail_n(env.session, lambda: TelegramNetworkError(M, "boom"), times=99, only="SendMessage")
    out = await env.deliver()
    assert out.confirmed_positions == [0, 1, 2] and out.completeness is Completeness.COMPLETE


async def test_cancellation_during_a_send_is_recorded_as_unknown_and_propagates(env):
    env.add()

    async def hang(method, *a, **kw):
        await asyncio.sleep(3600)

    env.session.make_request = hang  # type: ignore[method-assign]
    task = asyncio.create_task(env.deliver())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = env.db.list_deliveries(env.job.id)
    assert (row.outcome, row.error_code, row.positions) == ("unknown", "CANCELLED", [0])


async def test_batches_are_persisted_as_they_complete(env):
    for _ in range(11):
        env.add()
    await env.deliver()
    j = env.db.get_job(env.job.id)
    assert j.delivered_count == 11 and len(j.result_message_ids) >= 11


# ===================================================================================== file safety
async def test_a_file_whose_size_changed_after_validation_is_refused(env):
    a = env.add(data=b"X" * 300)
    a.local_path.write_bytes(b"X" * 301)  # the (untrusted) worker appended a byte after probing
    out = await env.deliver()
    assert env.session.uploads == [] and out.failure_codes == {0: ErrorCode.MEDIA_INVALID}


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
async def test_a_symlink_to_a_bot_secret_is_never_uploaded(env, tmp_path):
    secret = tmp_path / "bot_token"
    secret.write_text("123456789:SUPERSECRET")
    a = env.add(data=b"X" * 21)
    a.local_path.unlink()
    os.symlink(secret, a.local_path)
    out = await env.deliver()
    assert env.session.uploads == [] and out.failure_codes == {0: ErrorCode.MEDIA_INVALID}
    assert b"SUPERSECRET" not in b"".join(u[1] for u in env.session.uploads)


@pytest.mark.skipif(sys.platform == "win32", reason="replacing an open file is a sharing violation on Windows")
async def test_swapping_the_path_after_open_cannot_change_the_uploaded_bytes(env, tmp_path):
    a = env.add(data=b"GENUINE" * 30)
    fd = env.ws_mgr.open_validated(env.ws, a.local_path, expected_size=a.size_bytes)
    try:
        evil = tmp_path / "evil"
        evil.write_bytes(b"SECRET" * 35)
        os.replace(evil, a.local_path)  # the worker swaps the file after the bot validated it
        f = FdInputFile(fd, "x.jpg", a.size_bytes)
        data = b"".join([c async for c in f.read(None)])  # type: ignore[arg-type]
        assert data == b"GENUINE" * 30
    finally:
        os.close(fd)


async def test_fd_input_file_is_rereadable_and_chunked(env):
    a = env.add(data=bytes(range(256)) * 40)
    fd = env.ws_mgr.open_validated(env.ws, a.local_path, expected_size=a.size_bytes)
    try:
        f = FdInputFile(fd, "x.bin", a.size_bytes, chunk_size=1000)
        first = [c async for c in f.read(None)]  # type: ignore[arg-type]
        second = b"".join([c async for c in f.read(None)])  # type: ignore[arg-type]
        assert all(len(c) <= 1000 for c in first) and b"".join(first) == second == a.local_path.read_bytes()
    finally:
        os.close(fd)


async def test_file_descriptors_are_closed_after_every_batch(env):
    for _ in range(3):
        env.add()
    fail_n(env.session, lambda: TelegramNetworkError(M, "x"), times=99, only="SendMediaGroup")
    opened: list[int] = []
    original = env.ws_mgr.open_validated

    def spy(*args, **kw):
        fd = original(*args, **kw)
        opened.append(fd)
        return fd

    env.ws_mgr.open_validated = spy  # type: ignore[method-assign]
    await env.deliver()
    assert len(opened) == 3
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)  # closed


# ===================================================================================== classification table
@pytest.mark.parametrize(
    ("exc", "outcome", "code", "abort"),
    [
        (TelegramEntityTooLarge(M, "x"), "failed", ErrorCode.FILE_TOO_LARGE, False),
        (TelegramRetryAfter(M, "x", 5), "failed", ErrorCode.RATE_LIMITED, True),
        (TelegramForbiddenError(M, "x"), "failed", ErrorCode.UPLOAD_FAILED, True),
        (TelegramBadRequest(M, "x"), "failed", ErrorCode.UPLOAD_FAILED, False),
        (TelegramNetworkError(M, "x"), "unknown", ErrorCode.DELIVERY_UNKNOWN, True),
        (TelegramServerError(M, "x"), "unknown", ErrorCode.DELIVERY_UNKNOWN, True),
        (TimeoutError(), "unknown", ErrorCode.DELIVERY_UNKNOWN, True),
        (ConnectionResetError(), "unknown", ErrorCode.DELIVERY_UNKNOWN, True),
    ],
)
def test_error_classification(exc, outcome, code, abort):
    r = classify_send_error(exc)
    assert (r.outcome, r.code, r.abort) == (outcome, code, abort)


def test_programming_errors_are_not_swallowed_as_delivery_outcomes():
    with pytest.raises(ValueError):
        classify_send_error(ValueError("bug"))


async def test_unrelated_exceptions_inside_a_send_propagate(env):
    env.add()
    fail_n(env.session, lambda: ValueError("bug"), times=99)
    with pytest.raises(ValueError):
        await env.deliver()


def test_unused_imports_guard():
    assert uuid and OWNER
