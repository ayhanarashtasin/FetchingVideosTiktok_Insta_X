#!/usr/bin/env python3
"""Telegram delivery smoke test (T01.8, T01.9). Sends ONLY synthetic media generated on the spot.

    uv run python scripts/telegram_smoke.py            # hosted Bot API: photo, original document, video, album
    uv run python scripts/telegram_smoke.py --large    # also a >50 MB file (expects: hosted refuses, local accepts)

Needs `.env` with BOT_TOKEN and ALLOWED_USER_IDS, and you must have sent /start to the bot once. It sends to
the FIRST allowed user id. It never prints the token.

What it proves (and records in ops/feasibility-results.json -> the matrix in ops/feasibility.md):
  * native photo vs document: Telegram's re-compressed photo dimensions/size next to the document whose
    `file_size` must equal the original byte count (original files really are preserved);
  * caption placement for single media and for an album's first item;
  * a mixed photo/video album and the final source-link message with the inline button;
  * with --large: whether a >50 MB upload is refused (hosted) or accepted (local Bot API profile).
Do not run the hosted and local profiles against the same token at the same time (see ops/deploy.md).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo  # noqa: E402

from app.bot.keyboards import original_keyboard  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core.errors import ConfigError  # noqa: E402
from app.main import build_bot  # noqa: E402

LARGE_BYTES = 52_000_000  # just over the hosted API's 50 MB general-file limit


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


def _ffmpeg(*args: str) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("ffmpeg is required to generate the synthetic media")
    subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)


def make_media(directory: Path, *, large: bool) -> dict[str, Path]:
    photo, video, big = directory / "photo.jpg", directory / "video.mp4", directory / "large.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=3000x2000:rate=1", "-frames:v", "1", "-q:v", "2", str(photo))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))  # fmt: skip
    files = {"photo": photo, "video": video}
    if large:
        # Incompressible-ish noise at a high bitrate so the file really is > 50 MB.
        _ffmpeg("-f", "lavfi", "-i", "nullsrc=size=1280x720:rate=30,geq=random(1)*255:128:128", "-t", "40",
                "-c:v", "libx264", "-b:v", "14M", "-minrate", "14M", "-maxrate", "14M", "-bufsize", "28M",
                "-pix_fmt", "yuv420p", str(big))  # fmt: skip
        files["large"] = big
    return files


async def attempt(coro: Any) -> tuple[Any, str | None]:
    """Run one Bot API call; return (result, None) or (None, short error text)."""
    try:
        return await coro, None
    except TelegramAPIError as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


async def run_smoke(bot: Bot, owner_id: int, media: dict[str, Path], *, large: bool, mode: str) -> list[Step]:
    """One `Step` per check. Telegram is the only judge: nothing is assumed to have worked."""
    steps: list[Step] = []
    url = "https://example.invalid/synthetic-smoke-test"  # a placeholder: no real post is involved
    caption = f"Original link: {url}"
    original_size = media["photo"].stat().st_size

    msg, err = await attempt(bot.send_photo(owner_id, FSInputFile(media["photo"]), caption=caption))
    if msg is None:
        steps.append(Step("native photo + caption", False, err or "no reply"))
    else:
        best = max(msg.photo or [], key=lambda p: (p.width or 0) * (p.height or 0), default=None)
        shown = f"{best.width}x{best.height}, {best.file_size} bytes" if best else "dimensions not reported"
        steps.append(
            Step(
                "native photo + caption",
                bool(msg.caption),
                f"source 3000x2000 / {original_size} bytes -> Telegram shows {shown}; "
                f"caption {'present' if msg.caption else 'MISSING'}",
            )
        )

    doc, err = await attempt(
        bot.send_document(owner_id, FSInputFile(media["photo"]), caption=caption, disable_content_type_detection=True)
    )
    if doc is None or doc.document is None:
        steps.append(Step("original photo as document", False, err or "reply carried no document"))
    else:
        same = doc.document.file_size == original_size
        steps.append(
            Step(
                "original photo as document",
                same,
                f"file_size {doc.document.file_size} vs source {original_size}: "
                f"{'bytes preserved' if same else 'MISMATCH'}",
            )
        )

    vid, err = await attempt(
        bot.send_video(
            owner_id,
            FSInputFile(media["video"]),
            supports_streaming=True,
            width=640,
            height=360,
            duration=3,
            caption=caption,
        )
    )
    steps.append(Step("native video", vid is not None, f"message {vid.message_id}" if vid else err or "no reply"))

    album, err = await attempt(
        bot.send_media_group(
            owner_id,
            [
                InputMediaPhoto(media=FSInputFile(media["photo"]), caption=caption),
                InputMediaVideo(
                    media=FSInputFile(media["video"]), supports_streaming=True, width=640, height=360, duration=3
                ),
                InputMediaPhoto(media=FSInputFile(media["photo"])),
            ],
        )
    )
    if album is None:
        steps.append(Step("mixed album + final link", False, err or "no reply"))
    else:
        link, link_err = await attempt(bot.send_message(owner_id, caption, reply_markup=original_keyboard("0" * 32)))
        steps.append(
            Step(
                "mixed album + final link",
                len(album) == 3 and link is not None,
                f"{len(album)} items (photo, video, photo); link message "
                f"{'sent with the Original files button' if link else 'FAILED: ' + str(link_err)}",
            )
        )

    if large:
        size = media["large"].stat().st_size
        big, err = await attempt(bot.send_document(owner_id, FSInputFile(media["large"])))
        accepted = big is not None
        as_expected = (not accepted) if mode == "hosted" else accepted
        steps.append(
            Step(
                "large file (> 50 MB)",
                as_expected,
                f"{size / 1e6:.1f} MB: {'accepted' if accepted else 'refused (' + str(err) + ')'} on the {mode} profile"
                f" - {'as expected' if as_expected else 'NOT as expected'}",
            )
        )
    return steps


def summarise(steps: list[Step], mode: str, large: bool) -> dict[str, str]:
    """Matrix entries. A hosted run is evidence for T01.8 only; a local run for T01.9 only (and only
    if the >50 MB upload was actually attempted)."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    base = [s for s in steps if not s.name.startswith("large")]
    big = next((s for s in steps if s.name.startswith("large")), None)
    verdict = "PASS" if steps and all(s.ok for s in steps) else "FAIL"
    text = f"{verdict} ({date}, {mode} Bot API): " + "; ".join(f"{s.name}: {'ok' if s.ok else 'FAILED'}" for s in base)
    if big is not None:
        text += f" | {big.detail}"
    if mode == "hosted":
        return {"T01.8": text}
    return {"T01.9": text} if large and big is not None else {}


async def amain(args: argparse.Namespace) -> int:
    settings = Settings()
    try:
        settings.validate_for_startup()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    if not settings.allowed_user_ids:
        print("ALLOWED_USER_IDS is empty: nobody to send the smoke test to.", file=sys.stderr)
        return 2
    owner = settings.allowed_user_ids[0]
    bot = build_bot(settings)
    try:
        me = await bot.get_me()
        print(
            f"Connected as @{me.username} ({settings.bot_api_mode} Bot API). Sending synthetic media to user {owner}..."
        )
        with tempfile.TemporaryDirectory(prefix="smoke-") as tmp:
            media = make_media(Path(tmp), large=args.large)
            steps = await run_smoke(bot, owner, media, large=args.large, mode=settings.bot_api_mode)
    finally:
        await bot.session.close()
    width = max(len(s.name) for s in steps) if steps else 10
    for s in steps:
        print(f"  [{'PASS' if s.ok else 'FAIL'}] {s.name:<{width}}  {s.detail}")
    if args.record:
        from scripts.feasibility_probe import load_results, write_outputs

        results = load_results()
        results.setdefault("telegram", {}).update(summarise(steps, settings.bot_api_mode, args.large))
        write_outputs(results)
        print("Recorded in ops/feasibility-results.json and the matrix in ops/feasibility.md")
    print("Also eyeball the chat on Android and iOS (T06.6): album order, playable video, clickable link.")
    return 0 if all(s.ok for s in steps) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--large", action="store_true", help="also upload a >50 MB synthetic file")
    ap.add_argument("--no-record", dest="record", action="store_false", help="do not write the results file")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
