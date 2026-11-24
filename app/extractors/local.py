"""In-process extraction backend (T03.1): resolver + adapters + finalize behind the ExtractionBackend port.

In production the SAME class runs inside the isolated worker service (`app.extractors.service`), and
the bot talks to it through `RemoteExtractionBackend`. Run directly in the bot's process (development
only) it is NOT isolated from the bot's environment.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.core.contracts import ExtractionRequest, ExtractionResult, Platform, PostManifest
from app.core.deadline import Deadline
from app.core.url_validator import ValidatedUrl, parse_hosts_config
from app.extractors.base import ExtractionContext, PlatformAdapter
from app.extractors.finalize import finalize_outcome
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.resolver import ShortLinkResolver
from app.extractors.router import build_adapters, route
from app.extractors.ytdlp_runner import YtDlpRunner
from app.media.prepare import prepare_assets
from app.services.subprocess_runner import ProcessRunner, SubprocessRunner


class LocalExtractionBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: ProcessRunner | None = None,
        resolver: ShortLinkResolver | None = None,
        adapters: dict[Platform, PlatformAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.runner: ProcessRunner = runner or SubprocessRunner()
        proxy = settings.egress_proxy_url
        cookies = settings.instagram_cookies_file
        ctx = ExtractionContext(
            ytdlp=YtDlpRunner(
                self.runner,
                proxy=proxy,
                cookies=cookies,
                max_items=settings.max_items_per_job,
                max_file_bytes=settings.max_temp_bytes_per_job,
                min_free_bytes=settings.min_free_disk_bytes,
            ),
            gallerydl=GalleryDlRunner(
                self.runner,
                proxy=proxy,
                cookies=cookies,
                max_bytes=settings.max_temp_bytes_per_job,
                min_free_bytes=settings.min_free_disk_bytes,
            ),
            max_items=settings.max_items_per_job,
        )
        self.adapters = adapters or build_adapters(ctx)
        self.resolver = resolver or ShortLinkResolver(
            proxy=proxy, extra_hosts=parse_hosts_config(settings.extra_allowed_hosts)
        )

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
        # The one permitted transformation: a provably lossless stream-copy remux (Media mode only).
        assets = await prepare_assets(
            result.assets,
            mode=request.delivery_mode,
            native_hevc=self.settings.native_hevc,
            runner=self.runner,
        )
        return result.model_copy(update={"assets": assets})

    async def health(self) -> bool:
        return True  # in-process: nothing to be unreachable

    async def aclose(self) -> None:
        await self.resolver.aclose()
