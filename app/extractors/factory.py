"""Choose the extraction backend from configuration."""

from __future__ import annotations

from app.config import Settings
from app.core.ports import ExtractionBackend


def build_extraction_backend(settings: Settings) -> ExtractionBackend:
    if settings.worker_mode == "remote":
        from app.extractors.remote import RemoteExtractionBackend  # optional separate worker over HTTP

        return RemoteExtractionBackend(settings)
    from app.extractors.local import LocalExtractionBackend  # default: runs in-process

    return LocalExtractionBackend(settings)
