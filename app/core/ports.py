"""Interfaces between the orchestrator and its collaborators.

`JobRunner` only knows these protocols, which keeps the state machine free of aiogram, subprocess
and filesystem-layout details and makes it testable with in-memory fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.bot.messages import FinalReport
from app.core.contracts import (
    DeliveryOutcome,
    ExtractionRequest,
    ExtractionResult,
    JobRecord,
    PostManifest,
)
from app.core.deadline import Deadline
from app.core.url_validator import ValidatedUrl


class ExtractionBackend(Protocol):
    """Untrusted-world side: resolves, discovers and downloads. In production it lives in the
    isolated worker container; in development it runs in-process behind the same interface."""

    async def resolve(self, validated: ValidatedUrl, deadline: Deadline) -> ValidatedUrl:
        """Follow a short link safely and return the validated exact-post URL."""

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest: ...

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> ExtractionResult: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class Deliverer(Protocol):
    """Plans and performs the Telegram sends for an extraction result.

    Must record each batch outcome in the database as soon as it is known, and must report a
    cancellation that interrupts an in-flight send as `unknown` for that batch.
    """

    async def deliver(
        self, job: JobRecord, request: ExtractionRequest, result: ExtractionResult, workspace: Path
    ) -> DeliveryOutcome: ...


class Notifier(Protocol):
    async def ack(self, job: JobRecord) -> None:
        """Send the prompt acknowledgement ('Fetching media...')."""

    async def status(self, job: JobRecord, text: str) -> None:
        """Coarse, throttled progress update (edits the acknowledgement)."""

    async def finish(self, job: JobRecord, report: FinalReport) -> None:
        """Replace the status message with the final outcome (or remove it on full success)."""
