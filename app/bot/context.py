"""Shared application context injected into handlers (`ctx` workflow-data key)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.config import Settings
from app.core.contracts import DeliveryMode, ExtractionRequest, Platform
from app.core.job_queue import JobQueue, SubmitResult
from app.core.ports import Notifier
from app.core.url_validator import ValidatedUrl
from app.storage.db import Database


@dataclass
class AppContext:
    settings: Settings
    db: Database
    queue: JobQueue
    notifier: Notifier
    extra_hosts: dict[str, Platform] = field(default_factory=dict)

    def enqueue(
        self,
        *,
        owner_id: int,
        chat_id: int,
        origin_message_id: int,
        validated: ValidatedUrl,
        mode: DeliveryMode,
        request_key: str = "",
        parent_job_id: str | None = None,
    ) -> SubmitResult:
        """Create a job for an already-validated URL and try to reserve a queue slot."""
        job_id = uuid.uuid4().hex
        job = self.db.new_job_record(
            job_id=job_id,
            owner_id=owner_id,
            chat_id=chat_id,
            origin_message_id=origin_message_id,
            original_url=validated.original_url,
            platform=validated.platform,
            delivery_mode=mode,
            ttl_hours=self.settings.job_metadata_ttl_hours,
            request_key=request_key,
            parent_job_id=parent_job_id,
        )
        request = ExtractionRequest(
            job_id=uuid.UUID(job_id),
            owner_id=owner_id,
            chat_id=chat_id,
            origin_message_id=origin_message_id,
            original_url=validated.original_url,
            extraction_url=validated.extraction_url,
            platform=validated.platform,
            delivery_mode=mode,
            post_hint=validated.post_hint,
        )
        return self.queue.submit(job, request, validated)
