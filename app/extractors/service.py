"""The isolated extractor worker (T05.1): a small private HTTP service around `LocalExtractionBackend`.

    python -m app.extractors.service

Deployed as its own container with NO Telegram token, NO database, a read-only root filesystem, a non-root
identity and a network whose only exit is the allowlisting egress proxy. The bot (the trusted orchestrator)
calls it; nothing else can: every request needs the shared secret (constant-time comparison), and each call is
validated again here because this is the component that talks to the internet:

* the URL must pass the full validator and match the claimed platform/kind;
* the workspace must be exactly `<WORK_DIR>/<32 hex>` (never an arbitrary path);
* the time budget is clamped; concurrency is bounded.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from app.config import Settings
from app.core.contracts import ExtractionRequest, PostManifest
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.ports import ExtractionBackend
from app.core.url_validator import UrlKind, parse_hosts_config, validate_url
from app.extractors.wire import MAX_BODY_BYTES, SECRET_HEADER, url_from_wire, url_to_wire
from app.logging_setup import log_event, redact, setup_logging
from app.services.cleanup import JOB_ID_RE

log = logging.getLogger("app.worker")
MIN_BUDGET, MAX_BUDGET = 1.0, 900.0


def _error(err: BotError) -> web.Response:
    return web.json_response({"error": err.to_wire()}, status=409)


def create_app(
    backend: ExtractionBackend,
    *,
    secret: str,
    work_root: Path,
    concurrency: int = 2,
    extra_hosts: dict[str, Any] | None = None,
) -> web.Application:
    if len(secret) < 24:
        raise ValueError("worker secret must be at least 24 characters")
    secret_bytes = secret.encode()
    sem = asyncio.Semaphore(max(1, concurrency))
    root = Path(work_root).resolve()

    @web.middleware
    async def authenticate(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path == "/v1/live":  # container liveness only; reveals nothing
            return await handler(request)
        presented = request.headers.get(SECRET_HEADER, "").encode()
        if not hmac.compare_digest(presented, secret_bytes):  # constant time: no timing oracle on the secret
            log_event(log, logging.WARNING, "rejected worker request: bad credentials", path=request.path)
            return web.json_response({"error": {"code": "UNAUTHORIZED"}}, status=401)
        return await handler(request)

    def workspace_of(body: dict[str, Any]) -> Path:
        ws = Path(str(body.get("workspace", "")))
        if not JOB_ID_RE.fullmatch(ws.name) or ws.is_symlink() or ws.parent.resolve() != root or not ws.is_dir():
            raise BotError(ErrorCode.INVALID_URL, "workspace outside the job root", reason="bad_workspace")
        return ws

    def budget_of(body: dict[str, Any]) -> Deadline:
        try:
            seconds = float(body["remaining_seconds"])
        except (KeyError, TypeError, ValueError):
            raise BotError(ErrorCode.INVALID_URL, "missing time budget", reason="bad_request") from None
        return Deadline(min(max(seconds, MIN_BUDGET), MAX_BUDGET))

    def checked_request(body: dict[str, Any]) -> ExtractionRequest:
        request = ExtractionRequest.model_validate(body["request"])
        v = validate_url(request.extraction_url, extra_hosts=extra_hosts)  # again, here, at the network edge
        if v.platform is not request.platform or v.kind is not UrlKind.POST:
            raise BotError(ErrorCode.INVALID_URL, "request does not match its URL", reason="bad_request")
        return request

    async def guarded(
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]], request: web.Request
    ) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": {"code": "INVALID_URL", "reason": "bad_request"}}, status=400)
        try:
            async with sem:
                return web.json_response(await handler(body))
        except BotError as err:
            return _error(err)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(log, logging.ERROR, "worker call failed", error_type=type(exc).__name__, error=redact(exc))
            return _error(BotError(ErrorCode.EXTRACTOR_CHANGED))

    async def resolve(body: dict[str, Any]) -> dict[str, Any]:
        v = url_from_wire(body["url"])
        checked = validate_url(v.extraction_url, extra_hosts=extra_hosts)
        if checked.platform is not v.platform or checked.kind is not v.kind:
            raise BotError(ErrorCode.INVALID_URL, "url does not match its claim", reason="bad_request")
        out = await backend.resolve(
            type(v)(v.original_url, checked.extraction_url, checked.platform, checked.kind, checked.post_hint,
                    checked.post_id, checked.username),
            budget_of(body),
        )  # fmt: skip
        return {"url": url_to_wire(out)}

    async def discover(body: dict[str, Any]) -> dict[str, Any]:
        manifest = await backend.discover(checked_request(body), workspace_of(body), budget_of(body))
        return {"manifest": manifest.model_dump(mode="json")}

    async def fetch(body: dict[str, Any]) -> dict[str, Any]:
        manifest = PostManifest.model_validate(body["manifest"])
        result = await backend.fetch(checked_request(body), manifest, workspace_of(body), budget_of(body))
        return {"result": result.model_dump(mode="json")}

    async def live(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def health(_: web.Request) -> web.Response:
        ok = await backend.health()
        tools = {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe")}
        return web.json_response({"ok": ok and all(tools.values()), "tools": tools}, status=200 if ok else 503)

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/v1/live", live)
    app.router.add_get("/v1/health", health)

    async def post_resolve(r: web.Request) -> web.Response:
        return await guarded(resolve, r)

    async def post_discover(r: web.Request) -> web.Response:
        return await guarded(discover, r)

    async def post_fetch(r: web.Request) -> web.Response:
        return await guarded(fetch, r)

    app.router.add_post("/v1/resolve", post_resolve)
    app.router.add_post("/v1/discover", post_discover)
    app.router.add_post("/v1/fetch", post_fetch)
    return app


def main() -> None:
    from app.extractors.local import LocalExtractionBackend

    settings = Settings()
    setup_logging(settings.log_level)
    secret = settings.resolved_worker_secret()
    if len(secret) < 24:
        raise SystemExit("WORKER_SECRET(_FILE) must be at least 24 characters")
    if settings.egress_proxy_url is None:
        log_event(log, logging.WARNING, "EGRESS_PROXY_URL is not set: outbound traffic is NOT policy-restricted")
    backend = LocalExtractionBackend(settings)
    app = create_app(
        backend,
        secret=secret,
        work_root=settings.work_dir,
        concurrency=settings.max_concurrent_jobs,
        extra_hosts=parse_hosts_config(settings.extra_allowed_hosts),
    )

    async def close_backend(_: web.Application) -> None:
        await backend.aclose()

    app.on_cleanup.append(close_backend)
    web.run_app(app, host=settings.worker_host, port=settings.worker_port, print=None, access_log=None)


if __name__ == "__main__":
    main()
