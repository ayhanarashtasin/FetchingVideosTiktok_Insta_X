"""Delivery planning (T04.3, T04.5, T04.6; PRD sections 4 and 6; architecture section 10).

A PURE function: ordered assets in, an ordered send plan out. No I/O, so every rule is testable.

Rules encoded here
* Original-files mode: every item is a document (bytes preserved), never resized/re-encoded.
* Media mode: a photo is a native photo only if Telegram will accept it as one (size, dimension and
  aspect-ratio limits, image type); otherwise a document. A video is native only if Telegram's own
  player will handle it (H.264/AAC in MP4/MOV, see media.quality); otherwise a document. Nothing is
  ever downscaled or transcoded to fit.
* A file that exceeds the upload cap even as a document is REJECTED with FILE_TOO_LARGE - it is reported,
  never shrunk.
* Source order is the send order. The sequence is cut into contiguous runs of one family (photo/video
  vs document - Telegram forbids mixing them in one album), each run into batches of at most 10;
  a batch of one is sent individually (albums need 2-10 items).
* Captions carry the EXACT submitted URL. Every batch's first item gets one; if the URL does not fit a
  caption it is never truncated - a separate message carries it. Albums (and any multi-step plan) end
  with a separate source-link message, which is also where the `Original files` button goes because a
  media group cannot hold an inline keyboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.contracts import DeliveryMode, ItemKind, MediaAsset
from app.core.errors import ErrorCode
from app.media.quality import is_native_video

GROUP_MAX = 10
CAPTION_LIMIT = 1024  # Telegram: 0-1024 characters after entity parsing
PHOTO_MAX_DIMENSION_SUM = 10_000  # width + height
PHOTO_MAX_ASPECT_RATIO = 20
NATIVE_PHOTO_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


class SendAs(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"


@dataclass(frozen=True)
class Caps:
    photo_bytes: int
    file_bytes: int
    native_hevc: bool = False


@dataclass(frozen=True)
class PlannedItem:
    asset: MediaAsset
    send_as: SendAs
    reason: str | None = None  # why a document was chosen over a native send (for logs / explanations)


@dataclass(frozen=True)
class Step:
    """One Telegram send: a single item (`len == 1`) or an album (2..10 items of one family)."""

    items: tuple[PlannedItem, ...]
    caption: str | None

    @property
    def is_group(self) -> bool:
        return len(self.items) > 1

    @property
    def positions(self) -> list[int]:
        return [i.asset.position for i in self.items]


@dataclass(frozen=True)
class Rejected:
    position: int
    source_item_id: str
    code: ErrorCode
    reason: str


@dataclass(frozen=True)
class DeliveryPlan:
    steps: tuple[Step, ...]
    rejected: tuple[Rejected, ...]
    link_message: str | None  # text of the separate source-link message, or None when not needed
    button_on_media: bool  # 'Original files' button on the single media message itself
    button_on_link: bool  # ...or on the final link message


def caption_for(url: str) -> str | None:
    text = f"Original link: {url}"
    return text if len(text) <= CAPTION_LIMIT else None


def photo_is_native(asset: MediaAsset, caps: Caps) -> tuple[bool, str | None]:
    w, h = asset.width or 0, asset.height or 0
    if asset.mime_type not in NATIVE_PHOTO_MIMES:
        return False, f"{asset.mime_type} is not accepted as a Telegram photo"
    if asset.size_bytes > caps.photo_bytes:
        return False, "larger than Telegram's photo size limit"
    if w <= 0 or h <= 0 or w + h > PHOTO_MAX_DIMENSION_SUM:
        return False, "dimensions exceed Telegram's photo limit"
    if max(w, h) / min(w, h) > PHOTO_MAX_ASPECT_RATIO:
        return False, "aspect ratio exceeds Telegram's photo limit"
    return True, None


def decide(asset: MediaAsset, mode: DeliveryMode, caps: Caps) -> PlannedItem | Rejected:
    """Native send, document fallback, or rejection - never a quality reduction."""

    def reject() -> Rejected:
        return Rejected(
            asset.position, asset.source_item_id, ErrorCode.FILE_TOO_LARGE,
            f"{asset.size_bytes} bytes exceeds the {caps.file_bytes}-byte upload limit",
        )  # fmt: skip

    if mode is DeliveryMode.ORIGINAL:
        return (
            PlannedItem(asset, SendAs.DOCUMENT, "original files requested")
            if asset.size_bytes <= caps.file_bytes
            else reject()
        )

    if asset.kind is ItemKind.PHOTO:
        ok, why = photo_is_native(asset, caps)
        if ok:
            return PlannedItem(asset, SendAs.PHOTO)
        return PlannedItem(asset, SendAs.DOCUMENT, why) if asset.size_bytes <= caps.file_bytes else reject()

    native = asset.size_bytes <= caps.file_bytes and is_native_video(
        container=asset.container, video_codec=asset.video_codec, audio_codec=asset.audio_codec,
        pix_fmt=asset.pix_fmt, native_hevc=caps.native_hevc,
    )  # fmt: skip
    if native:
        return PlannedItem(asset, SendAs.VIDEO)
    if asset.size_bytes > caps.file_bytes:
        return reject()
    return PlannedItem(
        asset, SendAs.DOCUMENT,
        f"{asset.video_codec or 'unknown video codec'}/{asset.audio_codec or 'no audio'} in "
        f"{asset.container or 'unknown container'} is not natively playable in Telegram; "
        "sent as a file to keep quality",
    )  # fmt: skip


def _family(item: PlannedItem) -> str:
    return "document" if item.send_as is SendAs.DOCUMENT else "media"


def chunk(items: list[PlannedItem]) -> list[list[PlannedItem]]:
    """Contiguous same-family runs, each cut into batches of <= 10. Order is never changed."""
    runs: list[list[PlannedItem]] = []
    for item in items:
        if runs and _family(runs[-1][0]) == _family(item):
            runs[-1].append(item)
        else:
            runs.append([item])
    return [run[i : i + GROUP_MAX] for run in runs for i in range(0, len(run), GROUP_MAX)]


def plan_delivery(assets: list[MediaAsset], *, mode: DeliveryMode, caps: Caps, url: str) -> DeliveryPlan:
    decisions = [decide(a, mode, caps) for a in sorted(assets, key=lambda a: a.position)]
    planned = [d for d in decisions if isinstance(d, PlannedItem)]
    rejected = tuple(d for d in decisions if isinstance(d, Rejected))
    caption = caption_for(url)
    steps = tuple(Step(items=tuple(batch), caption=caption) for batch in chunk(planned))

    needs_link = bool(steps) and (len(steps) > 1 or any(s.is_group for s in steps) or caption is None)
    button_allowed = mode is DeliveryMode.MEDIA  # an Original-files job already delivers originals
    return DeliveryPlan(
        steps=steps,
        rejected=rejected,
        link_message=f"Original link: {url}" if needs_link else None,
        button_on_media=bool(steps) and not needs_link and button_allowed,
        button_on_link=needs_link and button_allowed,
    )
