"""Platform router (T03.1): URL platform -> adapter. No generic fallback for unknown platforms."""

from __future__ import annotations

from app.core.contracts import Platform
from app.core.errors import BotError, ErrorCode
from app.extractors.base import ExtractionContext, PlatformAdapter
from app.extractors.instagram import InstagramAdapter
from app.extractors.tiktok import TikTokAdapter
from app.extractors.twitter import TwitterAdapter


def build_adapters(ctx: ExtractionContext) -> dict[Platform, PlatformAdapter]:
    return {
        Platform.TIKTOK: TikTokAdapter(ctx),
        Platform.INSTAGRAM: InstagramAdapter(ctx),
        Platform.X: TwitterAdapter(ctx),
    }


def route(adapters: dict[Platform, PlatformAdapter], platform: Platform) -> PlatformAdapter:
    try:
        return adapters[platform]
    except KeyError:
        raise BotError(ErrorCode.INVALID_URL, f"no adapter for {platform}") from None
