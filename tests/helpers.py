"""Small builders shared by unit and integration tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.core.contracts import DeliveryMode, ExtractionRequest, JobRecord
from app.core.url_validator import ValidatedUrl, validate_url
from app.storage.db import Database

DEFAULT_URL = "https://x.com/someuser/status/1700000000000"


class Clock:
    """Mutable fake clock for deterministic expiry/TTL tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def make_job(
    db: Database,
    *,
    owner_id: int = 111,
    chat_id: int | None = None,
    message_id: int = 1,
    url: str = DEFAULT_URL,
    mode: DeliveryMode = DeliveryMode.MEDIA,
    request_key: str = "",
    ttl_hours: int = 24,
    job_id: str | None = None,
    parent_job_id: str | None = None,
) -> tuple[JobRecord, ExtractionRequest, ValidatedUrl]:
    validated = validate_url(url)
    jid = job_id or uuid.uuid4().hex
    chat = owner_id if chat_id is None else chat_id
    job = db.new_job_record(
        job_id=jid, owner_id=owner_id, chat_id=chat, origin_message_id=message_id,
        original_url=validated.original_url, platform=validated.platform, delivery_mode=mode,
        ttl_hours=ttl_hours, request_key=request_key, parent_job_id=parent_job_id,
    )
    request = ExtractionRequest(
        job_id=uuid.UUID(jid), owner_id=owner_id, chat_id=chat, origin_message_id=message_id,
        original_url=validated.original_url, extraction_url=validated.extraction_url,
        platform=validated.platform, delivery_mode=mode, post_hint=validated.post_hint,
    )
    return job, request, validated
