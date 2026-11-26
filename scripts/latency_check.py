#!/usr/bin/env python3
"""Can THIS host turn a link into an HD video in Telegram within a time budget (default 6 s)?

    uv run python scripts/latency_check.py <link> [<link> ...]
    uv run python scripts/latency_check.py --budget 4 --upload-mb 8 https://x.com/<user>/status/<id>

Run it on the machine that runs the bot today AND on any VPS you are considering, before moving the bot.
Budget tip: the bot's `duration_s` excludes Telegram handing the update to the bot and the client showing the
reply (~2 s on the 2026-11-26 home measurement), so `--budget 4` approximates a 6 s stopwatch target.
It measures the two things that decide the answer, without delivering anything to anyone:

1. Upload throughput to api.telegram.org through the bot's own aiohttp stack (tuned 1 MiB socket buffers).
   `--upload-mb` of random bytes are POSTed as a multipart `sendDocument` with a deliberately INVALID token:
   Telegram reads the whole body before it answers 401, so the timing is the real upload path. The bot token
   is never used or sent (`.env` is only loaded for the extractor settings); nothing is sent to any chat.
2. The bot's real extraction pipeline per link (short-link resolve, discover, CDN size probe, download +
   ffprobe + prepare) in a throw-away directory. Telegram is not contacted.

Per link it prints: pipeline time + size / measured upload rate + `--telegram-overhead` (an ASSUMPTION for
Telegram's own processing, default 1.5 s) against the budget, and the upload rate that link would need.
A link within Telegram's fetch-by-URL limits (cookie-free public CDN, <= 20 MB video, <= 5 MB photo) can skip
the upload entirely when DIRECT_TELEGRAM_URL=true; that path is timed on Telegram's side and is only flagged.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from app.config import Settings  # noqa: E402
from app.core.contracts import ExtractionRequest  # noqa: E402
from app.core.deadline import Deadline  # noqa: E402
from app.core.net_guard import tuned_socket_factory  # noqa: E402
from app.core.url_validator import UrlKind, parse_message_url  # noqa: E402
from app.extractors.local import LocalExtractionBackend  # noqa: E402

TELEGRAM_API = "https://api.telegram.org"
INVALID_TOKEN = "000000:" + "A" * 35  # can never be a real bot: Telegram reads the body, then answers 401


@dataclass(frozen=True)
class UploadProbe:
    bytes_sent: int
    seconds: float
    status: int
    first_request_s: float  # TCP + TLS + one round trip to api.telegram.org

    @property
    def bytes_per_s(self) -> float:
        return self.bytes_sent / self.seconds if self.seconds > 0 else 0.0


@dataclass
class LinkTiming:
    url: str
    stages: dict[str, float] = field(default_factory=dict)
    size_bytes: int | None = None
    resolution: str | None = None
    url_path_eligible: bool = False
    error: str | None = None

    @property
    def pipeline_s(self) -> float:
        return sum(self.stages.values())


def project_total(pipeline_s: float, size_bytes: int, upload_bytes_per_s: float, overhead_s: float) -> float:
    """Send-to-delivered estimate for the upload path: our pipeline + the upload + Telegram's processing."""
    if upload_bytes_per_s <= 0:
        return float("inf")
    return pipeline_s + size_bytes / upload_bytes_per_s + overhead_s


def required_upload_bytes_per_s(pipeline_s: float, size_bytes: int, overhead_s: float, budget_s: float) -> float:
    """Upload rate to Telegram this link needs to fit the budget (inf when the pipeline alone overruns it)."""
    room = budget_s - pipeline_s - overhead_s
    return size_bytes / room if room > 0 else float("inf")


def mbit(bytes_per_s: float) -> str:
    return "inf" if bytes_per_s == float("inf") else f"{bytes_per_s * 8 / 1e6:.1f} Mbit/s"


async def probe_telegram_upload(size_bytes: int) -> UploadProbe:
    connector = aiohttp.TCPConnector(socket_factory=tuned_socket_factory, limit=1, keepalive_timeout=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        t0 = time.monotonic()
        async with session.get(f"{TELEGRAM_API}/") as resp:
            await resp.read()
        first = time.monotonic() - t0
        form = aiohttp.FormData()
        form.add_field("chat_id", "1")
        form.add_field(
            "document", os.urandom(size_bytes), filename="probe.bin", content_type="application/octet-stream"
        )
        t0 = time.monotonic()
        async with session.post(f"{TELEGRAM_API}/bot{INVALID_TOKEN}/sendDocument", data=form) as resp:
            await resp.read()
            status = resp.status
        return UploadProbe(size_bytes, time.monotonic() - t0, status, first)


async def time_link(url: str, backend: LocalExtractionBackend, root: Path) -> LinkTiming:
    out = LinkTiming(url=url.split("?", 1)[0])
    workspace = root / uuid.uuid4().hex
    workspace.mkdir()
    deadline = Deadline(180)
    try:
        validated = parse_message_url(url, [])
        if validated.kind is UrlKind.SHORT_LINK:
            t0 = time.monotonic()
            validated = await backend.resolve(validated, deadline)
            out.stages["resolve"] = time.monotonic() - t0
        request = ExtractionRequest(
            job_id=uuid.uuid4(), owner_id=1, chat_id=1, origin_message_id=1, original_url=url,
            extraction_url=validated.extraction_url, resolved_url=validated.extraction_url,
            platform=validated.platform, post_hint=validated.post_hint,
        )  # fmt: skip
        t0 = time.monotonic()
        manifest = await backend.discover(request, workspace, deadline)
        out.stages["discover"] = time.monotonic() - t0
        t0 = time.monotonic()
        out.url_path_eligible = bool(await backend.direct_url_candidates(request.job_id, manifest, deadline))
        out.stages["url_probe"] = time.monotonic() - t0
        t0 = time.monotonic()
        result = await backend.fetch(request, manifest, workspace, deadline)
        out.stages["download+probe+prepare"] = time.monotonic() - t0
        out.size_bytes = sum(a.size_bytes for a in result.assets)
        dims = [min(a.width, a.height) for a in result.assets if a.width and a.height]
        out.resolution = "/".join(f"{d}p" for d in dict.fromkeys(dims)) or None
    except Exception as exc:  # a diagnostic tool reports, it does not crash
        out.error = f"{type(exc).__name__}: {getattr(exc, 'code', '')} {exc}".strip()
    return out


def report(links: list[LinkTiming], upload: UploadProbe | None, *, budget_s: float, overhead_s: float) -> int:
    failures = 0
    print()
    for link in links:
        print(f"* {link.url}")
        if link.error:
            print(f"    ERROR {link.error}")
            failures += 1
            continue
        stages = "  ".join(f"{k}={v:.2f}s" for k, v in link.stages.items())
        size = link.size_bytes or 0
        print(f"    {size / 1e6:.1f} MB {link.resolution or ''}  pipeline {link.pipeline_s:.1f} s  ({stages})")
        if link.url_path_eligible:
            print("    URL path eligible: with DIRECT_TELEGRAM_URL=true Telegram fetches this from the CDN itself "
                  "(no upload; Telegram-side time not measured here)")  # fmt: skip
        need = required_upload_bytes_per_s(link.pipeline_s, size, overhead_s, budget_s)
        if upload is None:
            print(f"    needs >= {mbit(need)} upload to Telegram to fit {budget_s:g} s")
            continue
        total = project_total(link.pipeline_s, size, upload.bytes_per_s, overhead_s)
        ok = total <= budget_s
        failures += 0 if ok else 1
        print(f"    upload path: ~{total:.1f} s at the measured rate -> {'PASS' if ok else 'FAIL'} "
              f"(budget {budget_s:g} s; needs >= {mbit(need)} to Telegram)")  # fmt: skip
    return failures


async def amain(args: argparse.Namespace) -> int:
    upload: UploadProbe | None = None
    if args.upload_mb > 0:
        size = int(args.upload_mb * 1_000_000)
        print(f"Measuring upload to {TELEGRAM_API} with {size / 1e6:.1f} MB of random bytes (invalid token, no "
              "delivery)...", flush=True)  # fmt: skip
        upload = await probe_telegram_upload(size)
        print(f"  {upload.bytes_sent / 1e6:.1f} MB in {upload.seconds:.1f} s -> {upload.bytes_per_s / 1e6:.2f} MB/s "
              f"({mbit(upload.bytes_per_s)}); first request {upload.first_request_s:.2f} s; "
              f"HTTP {upload.status} (401 expected)")  # fmt: skip
    settings = Settings()
    backend = LocalExtractionBackend(settings)
    links: list[LinkTiming] = []
    try:
        with tempfile.TemporaryDirectory(prefix="latency-check-", ignore_cleanup_errors=True) as tmp:
            for url in args.links:
                print(f"Timing the bot pipeline for {url.split('?', 1)[0]} ...", flush=True)
                links.append(await time_link(url, backend, Path(tmp)))
    finally:
        await backend.aclose()
    failures = report(links, upload, budget_s=args.budget, overhead_s=args.telegram_overhead)
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("links", nargs="*", help="public TikTok / Instagram / X post links to time")
    parser.add_argument("--budget", type=float, default=6.0, help="send-to-delivered target in seconds")
    parser.add_argument("--upload-mb", type=float, default=8.0, help="upload probe size in MB (0 = skip)")
    parser.add_argument(
        "--telegram-overhead", type=float, default=1.5, help="assumed Telegram-side processing seconds per send"
    )
    raise SystemExit(asyncio.run(amain(parser.parse_args())))


if __name__ == "__main__":
    main()
