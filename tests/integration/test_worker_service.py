"""T05.1 / T05.3 / T06.3 / T06.4: the isolated extractor worker service and the bot's client for it.

The service runs for real (aiohttp on loopback) around `LocalExtractionBackend` driven by synthetic tool output.
Covers: lossless round trips, typed errors across the boundary without leaking internals, authentication,
and a battery of hostile requests that must be rejected BEFORE the backend (and so the network) is touched.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from app.config import Settings
from app.core.contracts import ExtractionRequest, ExtractionResult, PostManifest
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.url_validator import validate_url
from app.extractors.local import LocalExtractionBackend
from app.extractors.remote import RemoteExtractionBackend
from app.extractors.service import create_app
from app.extractors.wire import SECRET_HEADER
from tests.conftest import fake_bot_token, requires_ffmpeg
from tests.fake_tools import FakeToolRunner

SECRET = "s3cret-" + "x" * 30
IG_POST = "https://www.instagram.com/p/ABCdef12345/"


class StubBackend:
    """Records calls; lets tests script outcomes and measure concurrency."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.raises: BaseException | None = None
        self.delay = 0.0
        self.running = 0
        self.max_running = 0
        self.last_deadline: Deadline | None = None

    async def _enter(self, name: str, *args) -> None:
        self.calls.append((name, args))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.raises:
                raise self.raises
        finally:
            self.running -= 1

    async def resolve(self, validated, deadline):
        self.last_deadline = deadline
        await self._enter("resolve", validated)
        return validate_url("https://www.tiktok.com/@u/video/7300000000000000000")

    async def discover(self, request, workspace, deadline):
        self.last_deadline = deadline
        await self._enter("discover", request, workspace)
        raise AssertionError("not used")

    async def fetch(self, request, manifest, workspace, deadline):
        await self._enter("fetch", request)
        raise AssertionError("not used")

    async def health(self):
        return True

    async def aclose(self):
        return None


class Svc:
    def __init__(self, backend, tmp_path, *, concurrency=2, secret=SECRET, root: Path | None = None) -> None:
        self.backend = backend
        self.root = root or tmp_path / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = web.AppRunner(create_app(backend, secret=secret, work_root=self.root, concurrency=concurrency))
        self.url = ""

    async def start(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    def job_dir(self) -> Path:
        d = self.root / uuid.uuid4().hex
        d.mkdir()
        return d

    def remote(self, **kw) -> RemoteExtractionBackend:
        s = Settings(
            _env_file=None,
            bot_token=fake_bot_token(),
            worker_mode="remote",
            worker_url=self.url,
            worker_secret=SECRET,
            **kw,
        )
        return RemoteExtractionBackend(s)

    async def post(self, path: str, body, *, secret: str | None = SECRET, raw: bytes | None = None):
        headers = {SECRET_HEADER: secret} if secret is not None else {}
        async with aiohttp.ClientSession() as s:
            kw = {"data": raw} if raw is not None else {"json": body}
            async with s.post(f"{self.url}{path}", headers=headers, **kw) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except Exception:
                    return r.status, None

    async def stop(self):
        await self.runner.cleanup()


@pytest.fixture
async def svc(tmp_path):
    s = await Svc(StubBackend(), tmp_path).start()
    yield s
    await s.stop()


def request_for(url=IG_POST) -> ExtractionRequest:
    v = validate_url(url)
    return ExtractionRequest(
        job_id=uuid.uuid4(), owner_id=1, chat_id=1, origin_message_id=1, original_url=url,
        extraction_url=v.extraction_url, platform=v.platform, post_hint=v.post_hint,
    )  # fmt: skip


def good_discover_body(svc: Svc, **over):
    body = {"request": request_for().model_dump(mode="json"), "workspace": str(svc.job_dir()), "remaining_seconds": 30}
    body.update(over)
    return body


# ============================================================================== authentication
@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET[:-1], SECRET + "x", SECRET.upper()])
async def test_requests_without_the_exact_secret_never_reach_the_backend(svc, secret):
    for path in ("/v1/resolve", "/v1/discover", "/v1/fetch", "/v1/health"):
        status, _ = await (
            svc.post(path, good_discover_body(svc), secret=secret)
            if path != "/v1/health"
            else svc.post(path, {}, secret=secret)
        )
        assert status == 401
    assert svc.backend.calls == []


async def test_liveness_is_open_but_health_is_not(svc):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{svc.url}/v1/live") as r:
            assert r.status == 200
        async with s.get(f"{svc.url}/v1/health") as r:
            assert r.status == 401
        async with s.get(f"{svc.url}/v1/health", headers={SECRET_HEADER: SECRET}) as r:
            assert r.status == 200 and (await r.json())["ok"] in {True, False}


def test_a_weak_secret_is_refused_at_startup():
    with pytest.raises(ValueError):
        create_app(StubBackend(), secret="short", work_root=Path("."))


# ============================================================================== hostile inputs
async def test_workspaces_outside_the_job_root_are_rejected_before_any_work(svc, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    sibling = svc.root.parent / uuid.uuid4().hex
    sibling.mkdir()
    bad = [
        str(outside), str(sibling), str(svc.root), "/etc", "", "relative/path", str(svc.root / "not-a-job-id"),
        str(svc.root / uuid.uuid4().hex),  # well-formed id but the directory does not exist
        str(svc.root / ".." / outside.name), str(svc.root / (uuid.uuid4().hex + "/../..")),
    ]  # fmt: skip
    for ws in bad:
        status, body = await svc.post("/v1/discover", good_discover_body(svc, workspace=ws))
        assert status == 409 and body["error"]["reason"] == "bad_workspace", ws
    assert svc.backend.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
async def test_a_symlinked_workspace_is_rejected(svc, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = svc.root / uuid.uuid4().hex
    os.symlink(target, link)
    status, body = await svc.post("/v1/discover", good_discover_body(svc, workspace=str(link)))
    assert status == 409 and body["error"]["reason"] == "bad_workspace" and svc.backend.calls == []


@pytest.mark.parametrize(
    "extraction_url",
    [
        "https://evil.example/@u/video/7300000000000000000",
        "https://www.tiktok.com.evil.example/@u/video/7300000000000000000",
        "http://127.0.0.1/admin",
        "file:///etc/passwd",
        "https://www.instagram.com/someuser/",  # a profile, not a post
        "https://vm.tiktok.com/ZMabcdEfg/",  # a short link must be resolved first
        "-o /tmp/x",
    ],
)
async def test_the_worker_revalidates_urls_even_though_the_bot_already_did(svc, extraction_url):
    req = request_for().model_dump(mode="json")
    req["extraction_url"] = extraction_url
    status, body = await svc.post("/v1/discover", good_discover_body(svc, request=req))
    assert status == 409 and body["error"]["code"] in {"INVALID_URL", "UNSUPPORTED_POST"}
    assert svc.backend.calls == []


async def test_a_platform_that_contradicts_the_url_is_refused(svc):
    req = request_for().model_dump(mode="json")
    req["platform"] = "tiktok"  # URL is Instagram
    status, body = await svc.post("/v1/discover", good_discover_body(svc, request=req))
    assert status == 409 and body["error"]["reason"] == "bad_request" and svc.backend.calls == []


async def test_resolve_refuses_a_url_that_does_not_match_its_claim(svc):
    claim = {"original_url": "https://vm.tiktok.com/ZMabcdEfg/", "extraction_url": "https://evil.example/ZMabcdEfg/",
             "platform": "tiktok", "kind": "short_link", "post_hint": None, "post_id": None, "username": None}  # fmt: skip
    status, body = await svc.post("/v1/resolve", {"url": claim, "remaining_seconds": 10})
    assert status == 409 and svc.backend.calls == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"null", b'"str"', b"{", b""])
async def test_malformed_bodies_are_400_not_500(svc, raw):
    status, body = await svc.post("/v1/discover", None, raw=raw)
    assert status == 400 and svc.backend.calls == []


@pytest.mark.parametrize("missing", ["request", "workspace", "remaining_seconds"])
async def test_missing_fields_are_rejected_without_a_stack_trace(svc, missing):
    body = good_discover_body(svc)
    body.pop(missing)
    status, resp = await svc.post("/v1/discover", body)
    assert status in {400, 409} and "Traceback" not in str(resp) and svc.backend.calls == []


async def test_oversized_bodies_are_rejected(svc):
    status, _ = await svc.post("/v1/discover", None, raw=b"{" + b" " * (9 * 1024 * 1024) + b"}")
    assert status == 413 and svc.backend.calls == []


async def test_the_time_budget_is_clamped(svc):
    svc.backend.raises = BotError(ErrorCode.NOT_FOUND_OR_EXPIRED)
    await svc.post("/v1/discover", good_discover_body(svc, remaining_seconds=10**9))
    assert svc.backend.last_deadline.total == 900.0  # a caller cannot ask for an unbounded run
    await svc.post("/v1/discover", good_discover_body(svc, remaining_seconds=-5))
    assert svc.backend.last_deadline.total == 1.0


async def test_concurrency_is_bounded(tmp_path):
    backend = StubBackend()
    backend.delay = 0.15
    backend.raises = BotError(ErrorCode.NOT_FOUND_OR_EXPIRED)
    s = await Svc(backend, tmp_path, concurrency=1).start()
    try:
        await asyncio.gather(*(s.post("/v1/discover", good_discover_body(s)) for _ in range(4)))
        assert len(backend.calls) == 4 and backend.max_running == 1
    finally:
        await s.stop()


# ============================================================================== the client
async def test_typed_errors_cross_the_boundary_without_their_internal_detail(svc):
    svc.backend.raises = BotError(
        ErrorCode.ACCESS_RESTRICTED,
        "secret detail https://x.com/leak?token=1",
        reason="private_account",
        retry_after=7,
        position=2,
    )
    remote = svc.remote()
    try:
        with pytest.raises(BotError) as exc:
            await remote.discover(request_for(), svc.job_dir(), Deadline(30))
        e = exc.value
        assert (e.code, e.reason, e.retry_after, e.position) == (ErrorCode.ACCESS_RESTRICTED, "private_account", 7, 2)
        assert e.detail is None and "leak" not in str(e), "free-text detail (may hold URLs) must not cross the boundary"
    finally:
        await remote.aclose()


async def test_an_unexpected_worker_exception_becomes_a_generic_extractor_error_with_no_leak(svc, caplog):
    svc.backend.raises = RuntimeError("boom with https://secret.example/p?k=SECRETVALUE")
    remote = svc.remote()
    try:
        with pytest.raises(BotError) as exc:
            await remote.discover(request_for(), svc.job_dir(), Deadline(30))
        assert exc.value.code is ErrorCode.EXTRACTOR_CHANGED and "SECRETVALUE" not in str(exc.value)
        assert (
            "SECRETVALUE" not in caplog.text and "secret.example" not in caplog.text
        )  # redacted in the worker's logs too
    finally:
        await remote.aclose()


async def test_resolve_round_trip_preserves_the_submitted_url(svc):
    remote = svc.remote()
    try:
        short = validate_url("https://vm.tiktok.com/ZMabcdEfg/")
        out = await remote.resolve(short, Deadline(30))
        assert (
            out.extraction_url == "https://www.tiktok.com/@u/video/7300000000000000000"
            and out.post_id == "7300000000000000000"
        )
    finally:
        await remote.aclose()


async def test_an_unreachable_worker_is_a_retryable_error_not_a_crash(tmp_path):
    s = Settings(
        _env_file=None,
        bot_token=fake_bot_token(),
        worker_mode="remote",
        worker_url="http://127.0.0.1:9",
        worker_secret=SECRET,
    )
    remote = RemoteExtractionBackend(s)
    try:
        with pytest.raises(BotError) as exc:
            await remote.discover(request_for(), tmp_path, Deadline(5))
        assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT and exc.value.reason == "worker_unreachable"
        assert exc.value.auto_retryable
        assert await remote.health() is False
    finally:
        await remote.aclose()


async def test_the_client_sends_the_secret_and_health_reflects_the_worker(svc):
    remote = svc.remote()
    try:
        assert await remote.health() is True
    finally:
        await remote.aclose()
    wrong = RemoteExtractionBackend(
        Settings(
            _env_file=None, bot_token=fake_bot_token(), worker_mode="remote", worker_url=svc.url, worker_secret="z" * 30
        )
    )
    try:
        assert await wrong.health() is False  # wrong secret -> 401 -> unhealthy, never silently accepted
        with pytest.raises(BotError):
            await wrong.discover(request_for(), svc.job_dir(), Deadline(5))
    finally:
        await wrong.aclose()


# ============================================================================== full flow across the boundary
@requires_ffmpeg
async def test_a_full_carousel_round_trip_through_the_worker_equals_the_in_process_result(tmp_path, media):
    tools = FakeToolRunner()
    tools.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    tools.gd_files(IG_POST, {1: media.jpg, 2: media.mp4_h264_aac, 3: media.png})
    settings = Settings(_env_file=None, bot_token=fake_bot_token(), data_dir=tmp_path / "d", work_dir=tmp_path / "jobs")
    svc = await Svc(LocalExtractionBackend(settings, runner=tools), tmp_path).start()
    remote = svc.remote()
    try:
        ws = svc.job_dir()
        req = request_for()
        manifest = await remote.discover(req, ws, Deadline(60))
        result = await remote.fetch(req, manifest, ws, Deadline(60))
        assert isinstance(manifest, PostManifest) and isinstance(result, ExtractionResult)
        assert [(a.position, a.kind.value) for a in result.assets] == [(0, "photo"), (1, "video"), (2, "photo")]
        assert result.completeness == "complete" and all(
            a.local_path.is_relative_to(ws.resolve()) for a in result.assets
        )
        assert result.assets[1].audio_codec == "aac"  # the video item kept its audio across the boundary
    finally:
        await remote.aclose()
        await svc.stop()


@requires_ffmpeg
async def test_the_whole_bot_works_unchanged_across_the_worker_boundary(tmp_path, media):
    """The real bot (polling, queue, runner, sender) drives a real worker service over HTTP; Telegram is the fake
    HTTP server. Byte-exact delivery proves nothing was lost or altered crossing the isolation boundary."""
    import json

    from tests.e2e import start_e2e

    tools = FakeToolRunner()
    tools.gd_dump(IG_POST, "gd_instagram_carousel_mixed")
    tools.gd_files(IG_POST, {1: media.jpg, 2: media.mp4_h264_aac, 3: media.png})
    services: list[Svc] = []

    async def factory(settings: Settings, runner: FakeToolRunner):
        # The worker shares ONLY the job directory with the bot; it has its own backend instance.
        service = await Svc(LocalExtractionBackend(settings, runner=runner), tmp_path, root=settings.work_dir).start()
        services.append(service)
        return service.remote()

    e = await start_e2e(tmp_path, tools=tools, extractor_factory=factory)
    try:
        await e.say(IG_POST, message_id=50)
        await e.idle()
        call = e.server.calls_of("sendMediaGroup")[0]
        items = call.json("media")
        sent = [call.files[i["media"].removeprefix("attach://")][1] for i in items]
        assert [i["type"] for i in items] == ["photo", "video", "photo"]
        assert sent == [media.jpg.read_bytes(), media.mp4_h264_aac.read_bytes(), media.png.read_bytes()]
        (job,) = e.jobs()
        assert job["state"] == "COMPLETED" and job["delivered_count"] == 3
        assert e.server.calls_of("sendMessage")[-1].fields["text"] == f"Original link: {IG_POST}"
        assert (
            json.loads(e.server.calls_of("sendMessage")[-1].fields["reply_markup"])["inline_keyboard"][0][0]["text"]
            == "Original files"
        )
        assert tools.calls_to("gallery_dl"), "the extraction really ran inside the worker service"
        assert list(e.settings.work_dir.glob("*")) == []
    finally:
        await e.stop()
        for service in services:
            await service.stop()
