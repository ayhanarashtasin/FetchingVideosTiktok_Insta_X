"""In-memory collaborators for handler / runner / integration tests. No network, no sleeping."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, InputFile, Message, Update, User

from app.bot.messages import FinalReport
from app.config import Settings
from app.core.contracts import (
    Access,
    DeliveryOutcome,
    ExtractionRequest,
    ExtractionResult,
    ItemDescriptor,
    ItemFailure,
    ItemKind,
    JobRecord,
    MediaAsset,
    PostManifest,
    PostType,
)
from app.core.deadline import Deadline
from app.core.errors import ErrorCode
from app.core.url_validator import ValidatedUrl
from tests.conftest import fake_bot_token

OWNER = 111
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# aiogram: a recording session
# --------------------------------------------------------------------------------------
class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.uploads: list[tuple[str | None, bytes]] = []
        self.timeouts: list[int | None] = []
        self.fail: Callable[[TelegramMethod], Exception | None] | None = None
        self._ids = itertools.count(5000)

    async def close(self) -> None:
        return None

    async def stream_content(self, *a: Any, **kw: Any) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        yield b""

    def _message(self, chat_id: int, text: str | None = None) -> Message:
        return Message(message_id=next(self._ids), date=NOW, chat=Chat(id=int(chat_id), type="private"), text=text)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod,
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        self.timeouts.append(timeout)
        if self.fail is not None:
            exc = self.fail(method)
            if exc is not None:
                raise exc
        name = type(method).__name__
        chat_id = getattr(method, "chat_id", 0)
        await self._drain_uploads(bot, method)
        if name in {"SendMessage"}:
            return self._message(chat_id, getattr(method, "text", None))
        if name in {"SendPhoto", "SendVideo", "SendDocument"}:
            return self._message(chat_id)
        if name == "SendMediaGroup":
            return [self._message(chat_id) for _ in method.media]  # type: ignore[attr-defined]
        if name in {"EditMessageText", "EditMessageReplyMarkup", "EditMessageCaption"}:
            return self._message(chat_id)
        return True

    async def _drain_uploads(self, bot: Bot, method: TelegramMethod) -> None:
        """Read every InputFile of a successful request, as the real HTTP layer would, so tests can
        assert on the exact bytes that would reach Telegram."""
        files = [
            v for v in (getattr(method, a, None) for a in ("photo", "video", "document")) if isinstance(v, InputFile)
        ]
        media = getattr(method, "media", None)
        if isinstance(media, list):
            files += [m.media for m in media if isinstance(getattr(m, "media", None), InputFile)]
        for f in files:
            self.uploads.append((f.filename, b"".join([chunk async for chunk in f.read(bot)])))

    def of(self, name: str) -> list[TelegramMethod]:
        return [c for c in self.calls if type(c).__name__ == name]

    def texts(self) -> list[str]:
        return [
            str(getattr(c, "text", "")) for c in self.calls if type(c).__name__ in {"SendMessage", "EditMessageText"}
        ]


def make_bot(session: FakeSession | None = None) -> tuple[Bot, FakeSession]:
    session = session or FakeSession()
    return Bot(token=fake_bot_token(), session=session), session


# --------------------------------------------------------------------------------------
# Update builders
# --------------------------------------------------------------------------------------
_update_ids = itertools.count(1)
_message_ids = itertools.count(100)


def message_update(
    text: str | None = "hello",
    *,
    user_id: int = OWNER,
    chat_id: int | None = None,
    chat_type: str = "private",
    message_id: int | None = None,
    is_bot: bool = False,
    caption: str | None = None,
    entities: list | None = None,
    photo: bool = False,
    update_id: int | None = None,
) -> Update:
    cid = user_id if chat_id is None else chat_id
    msg = Message(
        message_id=message_id if message_id is not None else next(_message_ids),
        date=NOW,
        chat=Chat(id=cid, type=chat_type),
        from_user=User(id=user_id, is_bot=is_bot, first_name="T"),
        text=text if not photo else None,
        caption=caption,
        entities=entities if not photo else None,
        caption_entities=entities if photo else None,
        photo=[{"file_id": "x", "file_unique_id": "y", "width": 1, "height": 1}] if photo else None,
    )
    return Update(update_id=update_id if update_id is not None else next(_update_ids), message=msg)


def callback_update(
    data: str,
    *,
    user_id: int = OWNER,
    chat_id: int | None = None,
    chat_type: str = "private",
    cq_id: str | None = None,
    message_id: int = 9,
) -> Update:
    cid = user_id if chat_id is None else chat_id
    cq = CallbackQuery(
        id=cq_id or uuid.uuid4().hex,
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        chat_instance="ci",
        data=data,
        message=Message(message_id=message_id, date=NOW, chat=Chat(id=cid, type=chat_type), text="x"),
    )
    return Update(update_id=next(_update_ids), callback_query=cq)


# --------------------------------------------------------------------------------------
# Extraction / delivery / notification fakes
# --------------------------------------------------------------------------------------
def asset_in(
    workspace: Path,
    position: int,
    *,
    kind: ItemKind = ItemKind.PHOTO,
    data: bytes | None = None,
    name: str | None = None,
) -> MediaAsset:
    """Create a real file inside `workspace` and describe it as a MediaAsset."""
    payload = data if data is not None else (b"\xff\xd8" + bytes([position]) * 20)
    path = workspace / (name or f"{position:03d}.{'jpg' if kind is ItemKind.PHOTO else 'mp4'}")
    path.write_bytes(payload)
    return MediaAsset(
        source_item_id=f"item{position}",
        position=position,
        local_path=path.resolve(),
        kind=kind,
        mime_type="image/jpeg" if kind is ItemKind.PHOTO else "video/mp4",
        size_bytes=len(payload),
        width=100,
        height=100,
    )


class FakeExtractor:
    """Configurable `ExtractionBackend`. `plan` yields one entry per attempt: an Exception to
    raise, or an int N meaning 'succeed with N photo items'."""

    def __init__(
        self,
        plan: list[Any] | None = None,
        *,
        items: int = 2,
        completeness: str = "complete",
        access: Access = Access.PERMITTED_PUBLIC,
        omissions: list | None = None,
        post_type: PostType = PostType.PHOTO,
        drop: set[int] | None = None,
    ) -> None:
        self.plan = list(plan) if plan is not None else None
        self.items = items
        self.completeness = completeness
        self.access = access
        self.omissions = list(omissions or [])
        self.post_type = post_type
        self.drop = set(drop or ())  # positions the "extractor" could not retrieve (become omissions)
        self.resolve_calls = 0
        self.discover_calls = 0
        self.fetch_calls = 0
        self.attempts = 0
        self.block: asyncio.Event | None = None  # set to hold fetch() open (cancel tests)
        self.entered_fetch = asyncio.Event()
        self.cancelled = False
        self.healthy = True
        self.seen_requests: list[ExtractionRequest] = []
        self.closed = False

    async def resolve(self, validated: ValidatedUrl, deadline: Deadline) -> ValidatedUrl:
        from app.core.url_validator import validate_url

        self.resolve_calls += 1
        return validate_url("https://www.tiktok.com/@resolved/video/7300000000000000000")

    def _step(self) -> Any:
        self.attempts += 1
        if self.plan:
            return self.plan.pop(0)
        return self.items

    async def discover(self, request: ExtractionRequest, workspace: Path, deadline: Deadline) -> PostManifest:
        self.discover_calls += 1
        self.seen_requests.append(request)
        step = self._step()
        if isinstance(step, Exception):
            raise step
        self._current = step
        n = int(step)
        return PostManifest(
            platform=request.platform,
            post_id="1",
            post_type=self.post_type,
            access=self.access,
            expected_count=n,
            count_verified=True,
            items=[ItemDescriptor(source_item_id=f"item{i}", position=i, kind=ItemKind.PHOTO) for i in range(n)],
        )

    async def fetch(
        self, request: ExtractionRequest, manifest: PostManifest, workspace: Path, deadline: Deadline
    ) -> ExtractionResult:
        self.fetch_calls += 1
        self.entered_fetch.set()
        try:
            if self.block is not None:
                await self.block.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        assets = [asset_in(workspace, i) for i in range(self._current) if i not in self.drop]
        omissions = [
            *self.omissions,
            *[
                ItemFailure(position=i, source_item_id=f"item{i}", code=ErrorCode.EXTRACTOR_CHANGED, message="missing")
                for i in sorted(self.drop)
                if i < self._current
            ],
        ]
        return ExtractionResult(
            assets=assets, expected_count=manifest.expected_count, omissions=omissions, completeness=self.completeness
        )  # type: ignore[arg-type]

    async def health(self) -> bool:
        return self.healthy

    async def aclose(self) -> None:
        self.closed = True


class FakeDeliverer:
    """Records what it was asked to deliver and mimics the real contract: persist each batch."""

    def __init__(
        self,
        db=None,
        *,
        outcome: DeliveryOutcome | None = None,
        error: Exception | None = None,
        hang_after_first_batch: bool = False,
    ) -> None:
        self.db = db
        self.outcome = outcome
        self.error = error
        self.hang = hang_after_first_batch
        self.calls: list[tuple[JobRecord, ExtractionResult, Path]] = []
        self.entered = asyncio.Event()
        self.files_seen_at_delivery: list[bool] = []

    async def deliver(
        self, job: JobRecord, request: ExtractionRequest, result: ExtractionResult, workspace: Path
    ) -> DeliveryOutcome:
        self.calls.append((job, result, workspace))
        self.files_seen_at_delivery = [a.local_path.exists() for a in result.assets]
        self.entered.set()
        if self.error is not None:
            raise self.error
        positions = [a.position for a in result.assets]
        if self.hang and self.db is not None and positions:
            self.db.record_delivery(job.id, 0, positions[:1], "sent", [900])
            await asyncio.sleep(3600)
        if self.outcome is not None:
            return self.outcome
        if self.db is not None and positions:
            self.db.record_delivery(job.id, 0, positions, "sent", [800 + i for i in positions])
        return DeliveryOutcome(
            confirmed_positions=positions, telegram_message_ids=[800 + i for i in positions], completeness="complete"
        )  # type: ignore[arg-type]


class RecordingNotifier:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.finals: list[tuple[str, FinalReport]] = []
        self.fail_finish = False

    async def ack(self, job: JobRecord) -> None:
        self.acks.append(job.id)

    async def status(self, job: JobRecord, text: str) -> None:
        self.statuses.append((job.id, text))

    async def finish(self, job: JobRecord, report: FinalReport) -> None:
        if self.fail_finish:
            raise RuntimeError("telegram down")
        self.finals.append((job.id, report))

    def status_texts(self) -> list[str]:
        return [t for _, t in self.statuses]


def build_test_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "bot_token": fake_bot_token(),
        "allowed_user_ids": [OWNER],
        "data_dir": tmp_path / "data",
        "work_dir": tmp_path / "work",
        "status_edit_min_interval_seconds": 0,
        "retry_base_delay_seconds": 0.01,
        "retry_max_delay_seconds": 0.05,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)
