"""Internal data contracts (architecture section 7).

These are plain pydantic models, not public APIs. They are also the wire format between the
trusted bot process and the isolated extractor worker, so everything here must stay JSON
serialisable and must never carry credentials or signed CDN URLs.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import ErrorCode


class Platform(StrEnum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    X = "x"


class DeliveryMode(StrEnum):
    MEDIA = "media"
    ORIGINAL = "original"


class PostType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    CAROUSEL = "carousel"
    STORY = "story"


class Access(StrEnum):
    PERMITTED_PUBLIC = "permitted_public"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class ItemKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class Processing(StrEnum):
    UNCHANGED = "unchanged"
    MERGED = "merged"
    REMUXED = "remuxed"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    FAILED = "failed"  # DeliveryOutcome only


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PREPARING = "PREPARING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_STATES = frozenset(
    {
        JobState.COMPLETED,
        JobState.PARTIAL,
        JobState.UNVERIFIED,
        JobState.DELIVERY_UNKNOWN,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
    }
)
ACTIVE_STATES = frozenset(set(JobState) - TERMINAL_STATES)

# Lifecycle diagram (architecture section 11). QUEUED -> INTERRUPTED is added because startup
# recovery must also mark jobs that were still waiting in the (non-persistent) queue.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.INTERRUPTED}),
    JobState.RUNNING: frozenset({JobState.PREPARING, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}),
    JobState.PREPARING: frozenset({JobState.UPLOADING, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}),
    JobState.UPLOADING: frozenset(
        {
            JobState.COMPLETED,
            JobState.PARTIAL,
            JobState.UNVERIFIED,
            JobState.DELIVERY_UNKNOWN,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.INTERRUPTED,
        }
    ),
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------------------
# Extraction side
# --------------------------------------------------------------------------------------


class FormatDescriptor(_Model):
    """Transient description of one provider format. `format_id` is an opaque provider id."""

    format_id: str
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    ext: str | None = None
    tbr: float | None = None
    filesize: int | None = None
    note: str | None = None


class ItemDescriptor(_Model):
    source_item_id: str
    position: Annotated[int, Field(ge=0)]
    kind: ItemKind
    width: int | None = None
    height: int | None = None
    candidate_formats: list[FormatDescriptor] = Field(default_factory=list)


class PostManifest(_Model):
    platform: Platform
    post_id: str
    post_type: PostType
    access: Access
    expected_count: int | None = None
    items: list[ItemDescriptor]
    # True when the source itself (not our filtering) established expected_count.
    count_verified: bool = False
    # Extractor warnings that signal silently skipped media (see gallery-dl "Missing media").
    skipped_media_warning: bool = False

    @model_validator(mode="after")
    def _items_ordered(self) -> PostManifest:
        positions = [i.position for i in self.items]
        if positions != sorted(set(positions)):
            raise ValueError("manifest item positions must be strictly increasing")
        return self


class ExtractionRequest(_Model):
    job_id: UUID
    owner_id: int
    chat_id: int
    origin_message_id: int
    original_url: str
    extraction_url: str
    resolved_url: str | None = None
    platform: Platform
    delivery_mode: DeliveryMode = DeliveryMode.MEDIA
    post_hint: str | None = None


class MediaAsset(_Model):
    source_item_id: str
    position: Annotated[int, Field(ge=0)]
    local_path: Path
    kind: ItemKind
    mime_type: str
    size_bytes: Annotated[int, Field(ge=0)]
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    sha256: str | None = None
    source_format: str | None = None
    processing: Processing = Processing.UNCHANGED
    container: str | None = None
    pix_fmt: str | None = None

    @field_validator("local_path")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("local_path must be absolute")
        return v


class ItemFailure(_Model):
    position: int | None = None
    source_item_id: str | None = None
    code: ErrorCode
    message: str


class ExtractionResult(_Model):
    assets: list[MediaAsset]
    expected_count: int | None = None
    omissions: list[ItemFailure] = Field(default_factory=list)
    completeness: Literal["complete", "partial", "unverified"] = "unverified"

    @model_validator(mode="after")
    def _ordered(self) -> ExtractionResult:
        positions = [a.position for a in self.assets]
        if positions != sorted(set(positions)):
            raise ValueError("result assets must be unique and ordered by source position")
        return self


def derive_completeness(
    *,
    expected_count: int | None,
    delivered_or_extracted: int,
    omissions: int,
    count_verified: bool,
    skipped_media_warning: bool = False,
) -> Literal["complete", "partial", "unverified"]:
    """Source-completeness rule shared by all adapters.

    `complete` requires that the *source* told us how many items exist (not merely how many
    survived our own filtering), that every one of them was retrieved, and that the extractor
    did not warn about silently skipped media. Anything we cannot prove is `unverified`.
    """
    if omissions > 0 or skipped_media_warning:
        return "partial"
    if expected_count is None or not count_verified:
        return "unverified"
    if delivered_or_extracted == expected_count:
        return "complete"
    return "partial"


# --------------------------------------------------------------------------------------
# Delivery side
# --------------------------------------------------------------------------------------


class DeliveryOutcome(_Model):
    confirmed_positions: list[int] = Field(default_factory=list)
    telegram_message_ids: list[int] = Field(default_factory=list)
    failed_positions: list[int] = Field(default_factory=list)
    unknown_positions: list[int] = Field(default_factory=list)
    completeness: Completeness = Completeness.FAILED
    # Why each failed position failed (additive to the architecture contract; used for messages).
    failure_codes: dict[int, ErrorCode] = Field(default_factory=dict)


class JobRecord(_Model):
    """Row of the `jobs` table. Holds the submitted URL, so it is purged after the TTL."""

    id: str
    owner_id: int
    chat_id: int
    origin_message_id: int
    original_url: str
    platform: Platform
    delivery_mode: DeliveryMode
    state: JobState
    request_key: str = ""
    expected_count: int | None = None
    delivered_count: int = 0
    result_message_ids: list[int] = Field(default_factory=list)
    error_code: str | None = None
    parent_job_id: str | None = None
    status_message_id: int | None = None
    created_at: str
    updated_at: str
    expires_at: str
