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
* Captions carry the submitted URL stripped of query parameters and fragments (`?` / `#`). Every batch's
  first item gets one; if the URL does not fit a caption it is never truncated - a separate message carries
  it. Albums with photos (and any multi-step plan) end with a separate source-link message, which is also
  where the `Original files` button goes because a media group cannot hold an inline keyboard.
"""

from __future__ import annotations

from collections.abc import Sequence
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


def display_url(url: str) -> str:
    """Strip tracking query parameters and fragments (keep everything before `?` / `#`)."""
    return url.split("?", 1)[0].split("#", 1)[0]


def asset_resolution(asset: MediaAsset) -> str | None:
    """Compact resolution tag like `720p` / `1080p` / `360p` (shortest side for portrait/landscape)."""
    if asset.width and asset.height:
        return f"{min(asset.width, asset.height)}p"
    if asset.height:
        return f"{asset.height}p"
    return None


def original_command_line(job_id: str) -> str:
    return f"Original files: /orig_{job_id}"


def caption_for(url: str, resolutions: Sequence[str] = ()) -> str | None:
    clean = display_url(url)
    unique = [r for r in dict.fromkeys(resolutions) if r]
    if unique:
        with_res = f"{clean}\n{'/'.join(unique)}"
        if len(with_res) <= CAPTION_LIMIT:
            return with_res
    return clean if len(clean) <= CAPTION_LIMIT else None


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

    if mode is DeliveryMode.ORIGINAL and asset.kind is ItemKind.PHOTO:
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


def plan_delivery(
    assets: list[MediaAsset], *, mode: DeliveryMode, caps: Caps, url: str, job_id: str | None = None
) -> DeliveryPlan:
    decisions = [decide(a, mode, caps) for a in sorted(assets, key=lambda a: a.position)]
    planned = [d for d in decisions if isinstance(d, PlannedItem)]
    rejected = tuple(d for d in decisions if isinstance(d, Rejected))
    batches = chunk(planned)
    steps = tuple(
        Step(
            items=tuple(batch),
            caption=caption_for(url, [r for i in batch if (r := asset_resolution(i.asset))]),
        )
        for batch in batches
    )
    has_photo = any(i.asset.kind is ItemKind.PHOTO for i in planned)
    button_allowed = mode is DeliveryMode.MEDIA and has_photo
    # Single-batch albums cannot carry an inline keyboard in Telegram, so embed a 1-tap /orig_<id>
    # command in the caption instead of sending a second message whenever it fits within CAPTION_LIMIT.
    embedded_command = False
    if len(steps) == 1 and steps[0].is_group and button_allowed and job_id is not None and steps[0].caption is not None:
        with_cmd = f"{steps[0].caption}\n{original_command_line(job_id)}"
        if len(with_cmd) <= CAPTION_LIMIT:
            steps = (Step(items=steps[0].items, caption=with_cmd),)
            embedded_command = True
    all_res = list(dict.fromkeys(r for i in planned if (r := asset_resolution(i.asset))))
    any_caption_missing = any(s.caption is None for s in steps)
    needs_link = bool(steps) and (
        len(steps) > 1
        or any_caption_missing
        or (any(s.is_group for s in steps) and button_allowed and not embedded_command)
    )
    clean = display_url(url)
    link_text = f"{clean}\n{'/'.join(all_res)}" if all_res else clean
    return DeliveryPlan(
        steps=steps,
        rejected=rejected,
        link_message=link_text if needs_link else None,
        button_on_media=bool(steps) and not needs_link and button_allowed and not embedded_command,
        button_on_link=needs_link and button_allowed,
    )
