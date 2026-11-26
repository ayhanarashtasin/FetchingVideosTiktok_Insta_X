"""In-process extraction backend (T03.1): resolver + adapters + finalize behind the ExtractionBackend port.

Runs directly in the bot's process by default (`WORKER_MODE=inprocess`), or behind the optional
separate worker service (`app.extractors.service`, `WORKER_MODE=remote`) via `RemoteExtractionBackend`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

from app.config import Settings
from app.core.contracts import Access, ExtractionRequest, ExtractionResult, ItemKind, Platform, PostManifest, PostType
from app.core.deadline import Deadline
from app.core.errors import BotError
from app.core.url_validator import ValidatedUrl, parse_hosts_config
from app.extractors.base import DirectUrlItem, ExtractionContext, PlatformAdapter, telegram_url_cap
from app.extractors.direct_download import DirectMediaDownloader
from app.extractors.fast_api import FastApiClient
from app.extractors.finalize import finalize_outcome
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.resolver import ShortLinkResolver
from app.extractors.router import build_adapters, route
from app.extractors.ytdlp_runner import YtDlpRunner
from app.logging_setup import log_event
from app.media.prepare import prepare_assets
from app.services.subprocess_runner import ProcessRunner, SubprocessRunner

log = logging.getLogger(__name__)
_DIRECT_VIDEO_EXTS = frozenset({"mp4", "mov"})
_DIRECT_PHOTO_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})


class LocalExtractionBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: ProcessRunner | None = None,
        resolver: ShortLinkResolver | None = None,
        adapters: dict[Platform, PlatformAdapter] | None = None,
        downloader: DirectMediaDownloader | None = None,
        fast_api: FastApiClient | None = None,
        enable_direct_telegram_url: bool | None = None,
    ) -> None:
        self.settings = settings
        self.runner: ProcessRunner = runner or SubprocessRunner()
        proxy = settings.egress_proxy_url
        cookies = settings.instagram_cookies_file
        self._direct_telegram_url = (
            enable_direct_telegram_url
            if enable_direct_telegram_url is not None
            else (runner is None and settings.direct_telegram_url)
        )
        # Enable direct CDN download, warm in-process yt-dlp, and fast HTTP APIs when running real tools
        # (or when explicitly injected); offline tests injecting FakeToolRunner keep deterministic playback.
        self.downloader: DirectMediaDownloader | None = (
            downloader
            if downloader is not None
            else (
                DirectMediaDownloader(
                    proxy=proxy,
                    max_bytes=settings.max_temp_bytes_per_job,
                    min_free_bytes=settings.min_free_disk_bytes,
                )
                if runner is None
                else None
            )
        )
        self.fast_api: FastApiClient | None = (
            fast_api
            if fast_api is not None
            else (FastApiClient(proxy=proxy) if (runner is None and settings.fast_api_enabled) else None)
        )
        ctx = ExtractionContext(
            ytdlp=YtDlpRunner(
                self.runner,
                proxy=proxy,
                cookies=cookies,
                max_items=settings.max_items_per_job,
                max_file_bytes=settings.max_temp_bytes_per_job,
                min_free_bytes=settings.min_free_disk_bytes,
                in_process=(runner is None),
            ),
            gallerydl=GalleryDlRunner(
                self.runner,
                proxy=proxy,
                cookies=cookies,
                max_bytes=settings.max_temp_bytes_per_job,
                min_free_bytes=settings.min_free_disk_bytes,
            ),
            max_items=settings.max_items_per_job,
            downloader=self.downloader,
            fast_api=self.fast_api,
            file_cap_bytes=settings.file_cap_bytes,
            runner=self.runner,
            native_hevc=settings.native_hevc,
            min_free_bytes=settings.min_free_disk_bytes,
        )
        self.adapters = adapters or build_adapters(ctx)
        self.resolver = resolver or ShortLinkResolver(
            proxy=proxy, extra_hosts=parse_hosts_config(settings.extra_allowed_hosts)
        )

    async def direct_url_candidates(
        self, job_id: object, manifest: PostManifest, deadline: Deadline
    ) -> list[DirectUrlItem] | None:
        """Return public cookie-free CDN URLs discovered for `job_id` (in-process Media-mode fast send).

        Never returns URLs for Stories, restricted posts, incomplete/unverified manifests, or items
        needing cookies/custom headers. Does not consume the discovery cache so fallback `fetch()` works.

        Each URL's real size is probed first (one 1-byte range request each, concurrently). If any item
        is KNOWN to exceed Telegram's fetch-by-URL ceiling (5 MB photo / 20 MB video), returns None:
        Telegram would refuse it anyway, so the job goes straight to download + upload instead of paying
        a doomed Telegram round trip first (e.g. every 1080p X video over 20 MB).
        """
        candidates = self._direct_url_candidates(job_id, manifest)
        if not candidates or self.downloader is None:
            return candidates
        # Every probe is settled (each is capped at a few seconds), so none is left running unobserved.
        results = await asyncio.gather(
            *(self.downloader.probe_size(c.url, deadline) for c in candidates), return_exceptions=True
        )
        if any(isinstance(r, BotError) for r in results):
            return None  # unsafe/unresolvable CDN host: let fetch() report it through the normal path
        for r in results:
            if isinstance(r, BaseException):
                raise r
        sized = [dataclasses.replace(c, size_bytes=s) for c, s in zip(candidates, results, strict=True)]
        over = [c for c in sized if c.exceeds_url_cap()]
        if over:
            log_event(
                log,
                logging.INFO,
                "direct url send skipped: over telegram fetch-by-url limit",
                job_id=str(job_id),
                positions=",".join(str(c.position) for c in over),
                largest_mb=round(max(c.size_bytes or 0 for c in over) / 1e6, 1),
                limit_mb=round(telegram_url_cap(over[0].kind) / 1e6),
            )
            return None
        return sized

    def _direct_url_candidates(self, job_id: object, manifest: PostManifest) -> list[DirectUrlItem] | None:
        if (
            not self._direct_telegram_url
            or manifest.access is not Access.PERMITTED_PUBLIC
            or manifest.post_type is PostType.STORY
            or not manifest.items
            or not manifest.count_verified
            or manifest.skipped_media_warning
            or (manifest.expected_count is not None and len(manifest.items) != manifest.expected_count)
        ):
            return None
        adapter = self.adapters.get(manifest.platform)
        cache = getattr(adapter, "cache", None)
        found = cache.peek(job_id) if cache is not None else None
        if found is None or not found.direct_telegram_ok or found.use_cookies:
            return None
        out: list[DirectUrlItem] = []
        for item in manifest.items:
            url, ext = found.urls.get(item.position, ("", ""))
            ext = ext.lower()
            if not url.startswith("https://"):
                return None
            if item.kind is ItemKind.PHOTO and ext not in _DIRECT_PHOTO_EXTS:
                return None
            if item.kind is ItemKind.VIDEO and ext not in _DIRECT_VIDEO_EXTS:
                return None
            out.append(
                DirectUrlItem(
                    position=item.position,
                    kind=item.kind,
                    url=url,
                    width=item.width,
                    height=item.height,
                    duration=found.durations.get(item.position),
                    variants=found.variants.get(item.position, ()),
                )
            )
        return out

    def drop_cached_discovery(self, job_id: object, platform: Platform) -> None:
        adapter = self.adapters.get(platform)
        cache = getattr(adapter, "cache", None)
        if cache is not None:
            cache.take(job_id)

    async def resolve(self, validated: ValidatedUrl, deadline: Deadline) -> ValidatedUrl:
        return await self.resolver.resolve(validated, deadline)

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        return await route(self.adapters, request.platform).discover(request, workspace, deadline)

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> ExtractionResult:
        outcome = await route(self.adapters, request.platform).fetch(request, manifest, workspace, deadline)
        result = await finalize_outcome(
            outcome,
            manifest,
            workspace,
            runner=self.runner,
            max_total_bytes=self.settings.max_temp_bytes_per_job,
            max_items=self.settings.max_items_per_job,
        )
        # Ensure Media-mode videos are Telegram-playable: lossless remux first, else H.264/AAC transcode.
        assets = await prepare_assets(
            result.assets,
            mode=request.delivery_mode,
            native_hevc=self.settings.native_hevc,
            runner=self.runner,
            min_free_bytes=self.settings.min_free_disk_bytes,
            max_file_bytes=self.settings.file_cap_bytes,
            deadline=deadline,
        )
        return result.model_copy(update={"assets": assets})

    async def health(self) -> bool:
        return True  # in-process: nothing to be unreachable

    async def aclose(self) -> None:
        await self.resolver.aclose()
        if self.downloader is not None:
            await self.downloader.aclose()
        if self.fast_api is not None:
            await self.fast_api.aclose()
