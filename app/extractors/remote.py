"""Client for the optional separate extractor worker service (T05.1, WORKER_MODE=remote)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Settings
from app.core.contracts import ExtractionRequest, ExtractionResult, PostManifest
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.core.net_guard import tuned_socket_factory
from app.core.url_validator import ValidatedUrl
from app.extractors.wire import MAX_BODY_BYTES, SECRET_HEADER, url_from_wire, url_to_wire
from app.logging_setup import log_event

log = logging.getLogger(__name__)
GRACE_SECONDS = 15.0  # the worker enforces the deadline; this only bounds a hung connection


class RemoteExtractionBackend:
    def __init__(self, settings: Settings, *, session: aiohttp.ClientSession | None = None) -> None:
        self._base = settings.worker_url.rstrip("/")
        self._secret = settings.resolved_worker_secret()
        self._session = session
        self._owns_session = session is None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={SECRET_HEADER: self._secret}, trust_env=False, raise_for_status=False,
                connector=aiohttp.TCPConnector(limit=8, keepalive_timeout=60.0, socket_factory=tuned_socket_factory),
            )  # fmt: skip
        return self._session

    async def _call(self, path: str, body: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=max(deadline.remaining(), 1.0) + GRACE_SECONDS)
        payload = {**body, "remaining_seconds": max(deadline.remaining(), 1.0)}
        try:
            async with self._http().post(f"{self._base}{path}", json=payload, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except BotError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
            log_event(log, logging.WARNING, "extractor worker unreachable", error_type=type(exc).__name__)
            raise BotError(
                ErrorCode.DOWNLOAD_TIMEOUT, "extractor worker unreachable", reason="worker_unreachable"
            ) from None
        if status == 200 and isinstance(data, dict):
            return data
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and "code" in error:
            try:
                raise BotError.from_wire(error)
            except ValueError:
                pass
        raise BotError(ErrorCode.EXTRACTOR_CHANGED, f"worker returned HTTP {status}")

    async def resolve(self, validated: ValidatedUrl, deadline: Deadline) -> ValidatedUrl:
        data = await self._call("/v1/resolve", {"url": url_to_wire(validated)}, deadline)
        return url_from_wire(data["url"])

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        data = await self._call(
            "/v1/discover", {"request": request.model_dump(mode="json"), "workspace": str(workspace)}, deadline
        )
        return PostManifest.model_validate(data["manifest"])

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> ExtractionResult:
        data = await self._call(
            "/v1/fetch",
            {
                "request": request.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
                "workspace": str(workspace),
            },
            deadline,
        )
        return ExtractionResult.model_validate(data["result"])

    async def health(self) -> bool:
        try:
            async with self._http().get(f"{self._base}/v1/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError, OSError):
            return False

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()


__all__ = ["MAX_BODY_BYTES", "RemoteExtractionBackend"]
