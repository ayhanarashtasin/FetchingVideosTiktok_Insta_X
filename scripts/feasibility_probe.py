#!/usr/bin/env python3
"""Extraction feasibility probe (Phase 1, T01.1-T01.10).

    uv run python scripts/feasibility_probe.py offline
        No network. Records tool versions/licences, which extractor class claims each PRD URL
        shape ("pattern match only"), and whether every CLI flag the project relies on exists.

    uv run python scripts/feasibility_probe.py live --corpus tests/live/corpus.txt [--download]
        The ONLY mode that touches platforms, and only for URLs you list in the private,
        git-ignored corpus file (permission-cleared posts you own/are licensed to use).
        See tests/live/README.md for the corpus format.

Safety rules (tasks.md section 1): only hosts accepted by the app's own URL validator are ever
requested; one URL at a time with a delay; a CAPTCHA/challenge/auth/rate-limit signal is
RECORDED as a blocker and the probe moves on - it never retries around it or tries to bypass it.

Outputs
    ops/feasibility-raw/results-<utc>.json   full raw results incl. URLs   (git-ignored)
    ops/feasibility-results.json             sanitized summary, NO URLs     (committed)
    ops/feasibility.md                       regenerated sections between the marker comments
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.errors import BotError  # noqa: E402
from app.core.url_validator import validate_url  # noqa: E402
from app.extractors.cli_flags import (  # noqa: E402
    GALLERYDL_REQUIRED_FLAGS,
    YTDLP_ALLOWED_EXTRACTORS,
    YTDLP_REQUIRED_FLAGS,
    missing_flags,
)

RESULTS_JSON = ROOT / "ops" / "feasibility-results.json"
RAW_DIR = ROOT / "ops" / "feasibility-raw"
DOC = ROOT / "ops" / "feasibility.md"
MAX_CAPTURE = 8_000_000

# Dummy-ID URL shapes from the PRD, used ONLY for offline pattern matching (never fetched).
SHAPES: dict[str, str] = {
    "P01 TikTok video": "https://www.tiktok.com/@someuser/video/7300000000000000000",
    "P01 TikTok short (vm)": "https://vm.tiktok.com/ZMabcdEfg/",
    "P01 TikTok short (vt)": "https://vt.tiktok.com/ZSabcdEfg/",
    "P01 TikTok short (/t/)": "https://www.tiktok.com/t/ZTabcdEfg/",
    "P02 TikTok photo": "https://www.tiktok.com/@someuser/photo/7300000000000000000",
    "P03 Instagram post": "https://www.instagram.com/p/ABCdef12345/",
    "P04 Instagram reel": "https://www.instagram.com/reel/ABCdef12345/",
    "P05 Instagram carousel": "https://www.instagram.com/p/ABCdef12345/?img_index=2",
    "P06/P07 Instagram story": "https://www.instagram.com/stories/someuser/3000000000000000000/",
    "IG share link": "https://www.instagram.com/share/p/BAbcdefGh/",
    "P08/P09 X (x.com)": "https://x.com/someuser/status/1700000000000000000",
    "P08/P09 X (twitter.com)": "https://twitter.com/someuser/status/1700000000000000000",
    "excluded: IG highlight": "https://www.instagram.com/stories/highlights/17800000000000000/",
    "excluded: IG story w/o id": "https://www.instagram.com/stories/someuser/",
    "excluded: TikTok profile": "https://www.tiktok.com/@someuser",
    "excluded: IG profile": "https://www.instagram.com/someuser/",
    "excluded: X profile": "https://x.com/someuser",
}

# Feasibility matrix rows: (row label, candidate, required verification, probe content ids)
MATRIX: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("TikTok video and short links", "yt-dlp", "Video + audio, correct target, quality", ("P01",)),
    ("TikTok photo-mode carousel", "gallery-dl", "Full ordered images, not thumbnails", ("P02",)),
    ("Instagram photo", "gallery-dl", "Highest available image", ("P03",)),
    ("Instagram video / Reel", "yt-dlp", "Playable source video + audio", ("P04",)),
    ("Instagram mixed carousel", "gallery-dl, targeted video fallback", "All media in source order", ("P05",)),
    ("Instagram photo Story", "gallery-dl", "Active, public, exact Story ID", ("P06",)),
    ("Instagram video Story", "gallery-dl / yt-dlp", "Active, public, exact Story ID", ("P07",)),
    ("X photos", "gallery-dl", "Single/multiple, order", ("P08",)),
    ("X video / mixed media", "yt-dlp / gallery-dl", "Correct attachments, audio, order", ("P09",)),
    ("Hosted Telegram delivery", "Bot API", "Native media, documents and source caption", ("T01.8",)),
    ("Local Telegram delivery", "Local Bot API", "Quality-preserving upload >50 MB", ("T01.9",)),
]

PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "avif", "heic"}
VIDEO_EXT = {"mp4", "m4v", "mov", "webm", "mkv"}


# ----------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------
def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tool_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def binary_version(binary: str) -> str:
    exe = shutil.which(binary)
    if not exe:
        return "not found"
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30).stdout
        return out.splitlines()[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def licence_of(dist: str) -> str:
    try:
        md = metadata.metadata(dist)
    except metadata.PackageNotFoundError:
        return "unknown"
    return md.get("License-Expression") or (md.get("License") or "").strip() or "unknown"


def sandbox_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "LANG"}}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home),
        XDG_CONFIG_HOME=str(home),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    return env


def run(cmd: list[str], env: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, env=env, timeout=timeout, check=False)
        out = p.stdout[:MAX_CAPTURE].decode("utf-8", "replace")
        err = p.stderr[:MAX_CAPTURE].decode("utf-8", "replace")
        return {
            "rc": p.returncode,
            "out": out,
            "err": err,
            "timeout": False,
            "seconds": round(time.monotonic() - started, 1),
        }
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": "", "err": "", "timeout": True, "seconds": float(timeout)}


BLOCKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("RATE_LIMITED", re.compile(r"429|rate.?limit|too many requests|challenge|captcha|verify you are human", re.I)),
    (
        "AUTH_REQUIRED",
        re.compile(r"log.?in|login required|credentials|authentication|sign in|AuthRequired|cookies", re.I),
    ),
    (
        "ACCESS_RESTRICTED",
        re.compile(r"private|protected|not authorized|forbidden|403|unauthori[sz]ed|restricted", re.I),
    ),
    ("NOT_FOUND_OR_EXPIRED", re.compile(r"404|not found|NotFoundError|unavailable|expired|removed|deleted", re.I)),
    ("UNSUPPORTED", re.compile(r"unsupported url|no suitable extractor|NoExtractor", re.I)),
)


def classify_blocker(text: str) -> str:
    for code, pattern in BLOCKER_PATTERNS:
        if pattern.search(text):
            return code
    return "EXTRACTOR_ERROR"


# ----------------------------------------------------------------------------------------
# metadata parsing (pure functions; unit-tested)
# ----------------------------------------------------------------------------------------
def parse_gallerydl_dump(stdout: str) -> dict[str, Any]:
    """Summarise `gallery-dl -j` output: ordered kinds, dims, declared counts, error entries."""
    try:
        messages = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {"parsed": False, "items": [], "errors": [], "declared_count": None}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    declared: int | None = None
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, list) or not msg:
            continue
        if msg[0] == 2 and len(msg) >= 2 and isinstance(msg[-1], dict):
            c = msg[-1].get("count")
            declared = c if isinstance(c, int) else declared
        elif msg[0] == 3 and len(msg) >= 3 and isinstance(msg[2], dict):
            meta = msg[2]
            ext = str(meta.get("extension") or "").lower()
            kind = "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else f"other:{ext or '?'}"
            items.append(
                {
                    "num": meta.get("num"),
                    "kind": kind,
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                    "type": meta.get("type"),
                }
            )
        elif msg[0] == -1 and isinstance(msg[-1], dict):
            errors.append({"error": str(msg[-1].get("error")), "message": str(msg[-1].get("message"))[:200]})
    return {"parsed": True, "items": items, "errors": errors, "declared_count": declared}


def parse_ytdlp_info(stdout: str) -> dict[str, Any]:
    """Summarise `yt-dlp -J` output: entries, best format, audio presence."""
    try:
        info = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {"parsed": False, "entries": []}
    if not isinstance(info, dict):
        return {"parsed": False, "entries": []}
    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        formats = [f for f in (e.get("formats") or []) if isinstance(f, dict)]
        vids = [f for f in formats if f.get("vcodec") not in (None, "none")]
        best = max(
            vids, key=lambda f: ((f.get("height") or 0) * (f.get("width") or 0), f.get("tbr") or 0), default=None
        )
        has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or e.get("acodec") not in (
            None,
            "none",
            "",
        )
        out.append(
            {
                "extractor": e.get("extractor_key"),
                "format_count": len(formats),
                "best_width": (best or e).get("width"),
                "best_height": (best or e).get("height"),
                "best_vcodec": (best or {}).get("vcodec"),
                "has_audio": bool(has_audio),
                "duration": e.get("duration"),
            }
        )
    return {"parsed": True, "entries": out}


def ffprobe_summary(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"error": "ffprobe not found"}
    # NB: `-nostdin` is an ffmpeg-only option and makes ffprobe abort; stdin is closed instead.
    p = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return {"error": "unprobeable"}
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    return {
        "format": fmt.get("format_name"),
        "size": int(fmt.get("size") or 0),
        "width": (v or {}).get("width"),
        "height": (v or {}).get("height"),
        "vcodec": (v or {}).get("codec_name"),
        "acodec": (a or {}).get("codec_name"),
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "has_audio": a is not None,
        "has_video": v is not None,
    }


IMAGE_FORMATS = frozenset({"image2", "png_pipe", "jpeg_pipe", "webp_pipe", "gif", "avif", "heif"})


def audio_from_files(files: list[dict[str, Any]]) -> bool | None:
    """True/False whether every real VIDEO file kept an audio stream; None if there is no video.

    ffprobe reports a JPEG as a one-frame 'video' stream, so image containers are excluded.
    """
    videos = [
        f
        for f in files
        if f.get("has_video") and f.get("format") not in IMAGE_FORMATS and "image2" not in str(f.get("format"))
    ]
    if not videos:
        return None
    return all(bool(f.get("has_audio")) for f in videos)


def parse_corpus(text: str) -> list[dict[str, Any]]:
    """`ID URL [count=N] [kinds=photo,video,...]` per line; `#` starts a comment."""
    entries: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"corpus line {lineno}: expected 'ID URL [count=N] [kinds=a,b]'")
        entry: dict[str, Any] = {"id": parts[0].upper(), "url": parts[1], "line": lineno}
        for opt in parts[2:]:
            key, _, value = opt.partition("=")
            if key == "count" and value.isdigit():
                entry["count"] = int(value)
            elif key == "kinds" and value:
                entry["kinds"] = value.lower().split(",")
            else:
                raise ValueError(f"corpus line {lineno}: unknown option {opt!r}")
        entries.append(entry)
    return entries


def base_id(content_id: str) -> str:
    return re.match(r"P0\d", content_id).group(0) if re.match(r"P0\d", content_id) else content_id


# ----------------------------------------------------------------------------------------
# offline mode
# ----------------------------------------------------------------------------------------
def offline_results() -> dict[str, Any]:
    warnings.filterwarnings("ignore")
    from gallery_dl import extractor as gd_extractor
    from yt_dlp.extractor import gen_extractor_classes
    from yt_dlp.extractor.generic import GenericIE

    ytdlp_classes = [c for c in gen_extractor_classes() if c is not GenericIE]
    routing = []
    for label, url in SHAPES.items():
        yt = [c.IE_NAME for c in ytdlp_classes if c.suitable(url)]
        try:
            g = gd_extractor.find(url)
            gd = f"{g.category}:{g.subcategory}" if g else None
        except Exception as exc:
            gd = f"error:{type(exc).__name__}"
        try:
            v = validate_url(url)
            verdict = f"accepted ({v.kind.value}, {v.post_hint or 'resolve first'})"
        except BotError as exc:
            verdict = f"rejected {exc.code.value}"
        routing.append({"case": label, "ytdlp": yt, "gallerydl": gd, "app_validator": verdict})

    env = sandbox_env(Path(tempfile.gettempdir()))
    helps = {
        "yt-dlp": run([sys.executable, "-m", "yt_dlp", "--help"], env, 60)["out"],
        "gallery-dl": run([sys.executable, "-m", "gallery_dl", "--help"], env, 60)["out"],
    }
    flags = {
        "yt-dlp": missing_flags(helps["yt-dlp"], YTDLP_REQUIRED_FLAGS),
        "gallery-dl": missing_flags(helps["gallery-dl"], GALLERYDL_REQUIRED_FLAGS),
    }
    return {
        "generated": utcnow(),
        "python": sys.version.split()[0],
        "tools": {
            "yt-dlp": {"version": tool_version("yt-dlp"), "license": licence_of("yt-dlp"), "invocation": "subprocess"},
            "gallery-dl": {
                "version": tool_version("gallery-dl"),
                "license": licence_of("gallery-dl"),
                "invocation": "subprocess ONLY (GPL-2.0-only: never imported by project code)",
            },
            "ffmpeg": {"version": binary_version("ffmpeg")},
            "ffprobe": {"version": binary_version("ffprobe")},
            "aiogram": {"version": tool_version("aiogram"), "license": licence_of("aiogram")},
        },
        "ytdlp_allowed_extractors": list(YTDLP_ALLOWED_EXTRACTORS),
        "routing": routing,
        "missing_flags": flags,
    }


def render_offline_md(off: dict[str, Any]) -> str:
    lines = [
        f"_Generated by `scripts/feasibility_probe.py offline` on {off['generated']} (Python {off['python']})._",
        "",
        "**This section is static evidence only. No request was made to any platform.**",
        "",
        "### Tool versions and licences (T01.7)",
        "",
        "| Component | Version | Licence | How it is used |",
        "| --- | --- | --- | --- |",
    ]
    for name, t in off["tools"].items():
        lines.append(f"| {name} | {t['version']} | {t.get('license', '-')} | {t.get('invocation', 'library/binary')} |")
    lines += [
        "",
        "### Which extractor claims each URL shape (pattern match only)",
        "",
        "| URL shape (dummy IDs) | yt-dlp (non-generic) | gallery-dl | App URL validator |",
        "| --- | --- | --- | --- |",
    ]
    for r in off["routing"]:
        lines.append(
            f"| {r['case']} | {', '.join(r['ytdlp']) or '-'} | {r['gallerydl'] or '-'} | {r['app_validator']} |"
        )
    miss = off["missing_flags"]
    lines += [
        "",
        "### CLI options the project depends on (checked against the installed `--help`)",
        "",
        f"- yt-dlp: {'all present' if not miss['yt-dlp'] else 'MISSING ' + ', '.join(miss['yt-dlp'])}",
        f"- gallery-dl: {'all present' if not miss['gallery-dl'] else 'MISSING ' + ', '.join(miss['gallery-dl'])}",
        f"- yt-dlp extractor allowlist (`--use-extractors`): {', '.join(off['ytdlp_allowed_extractors'])}",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------
# live mode
# ----------------------------------------------------------------------------------------
def live_probe_one(
    entry: dict[str, Any], *, download: bool, cookies: Path | None, timeout: int, keep: bool
) -> dict[str, Any]:
    from gallery_dl import extractor as gd_extractor
    from yt_dlp.extractor import gen_extractor_classes
    from yt_dlp.extractor.generic import GenericIE

    url = entry["url"]
    result: dict[str, Any] = {"id": entry["id"], "tools": {}}
    try:
        validated = validate_url(url)
    except BotError as exc:
        result["validator"] = f"rejected {exc.code.value}"
        return result
    result["validator"] = f"accepted ({validated.kind.value})"
    platform = validated.platform.value

    home = Path(tempfile.mkdtemp(prefix="probe-home-"))
    env = sandbox_env(home)
    cookie_args_yt = ["--cookies", str(cookies)] if cookies and platform == "instagram" else []
    cookie_args_gd = ["-C", str(cookies)] if cookies and platform == "instagram" else []
    workdir = RAW_DIR / "downloads" / uuid.uuid4().hex
    try:
        # --- yt-dlp ---
        yt_ok = any(c.suitable(url) for c in gen_extractor_classes() if c is not GenericIE)
        if yt_ok:
            r = run(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-playlist",
                    "--no-cache-dir",
                    "--no-warnings",
                    "--use-extractors",
                    ",".join(YTDLP_ALLOWED_EXTRACTORS),
                    "--socket-timeout",
                    "20",
                    "--retries",
                    "1",
                    "--extractor-retries",
                    "1",
                    *cookie_args_yt,
                    "-J",
                    "--",
                    url,
                ],
                env,
                timeout,
            )
            summary = parse_ytdlp_info(r["out"]) if r["rc"] == 0 else {"parsed": False, "entries": []}
            res = {
                "rc": r["rc"],
                "timeout": r["timeout"],
                "summary": summary,
                "blocker": None if r["rc"] == 0 else classify_blocker(r["err"]),
                "stderr_head": r["err"][:400],
            }
            if r["rc"] == 0 and download:
                workdir.mkdir(parents=True, exist_ok=True)
                d = run(
                    [
                        sys.executable,
                        "-m",
                        "yt_dlp",
                        "--ignore-config",
                        "--no-playlist",
                        "--no-cache-dir",
                        "--no-warnings",
                        "--use-extractors",
                        ",".join(YTDLP_ALLOWED_EXTRACTORS),
                        "--socket-timeout",
                        "20",
                        "--retries",
                        "1",
                        "--extractor-retries",
                        "1",
                        *cookie_args_yt,
                        "-P",
                        f"home:{workdir}",
                        "-o",
                        "yt_%(id)s_%(playlist_index|1)s.%(ext)s",
                        "--restrict-filenames",
                        "--no-mtime",
                        "--no-part",
                        "--no-progress",
                        "--merge-output-format",
                        "mp4/mkv",
                        "--",
                        url,
                    ],
                    env,
                    timeout * 3,
                )
                res["download_rc"] = d["rc"]
                res["files"] = [ffprobe_summary(p) for p in sorted(workdir.glob("yt_*")) if p.is_file()]
            result["tools"]["yt-dlp"] = res
        else:
            result["tools"]["yt-dlp"] = {"skipped": "no non-generic yt-dlp extractor matches this URL (pattern)"}

        # --- gallery-dl ---
        gd_ok = gd_extractor.find(url) is not None
        if gd_ok:
            opts = ["-o", "audio=false", "-o", "covers=false", "-o", "subtitles=false"]
            r = run(
                [
                    sys.executable,
                    "-m",
                    "gallery_dl",
                    "--config-ignore",
                    "-j",
                    *opts,
                    "--http-timeout",
                    "20",
                    "-R",
                    "1",
                    *cookie_args_gd,
                    "--",
                    url,
                ],
                env,
                timeout,
            )
            summary = (
                parse_gallerydl_dump(r["out"]) if r["out"].strip() else {"parsed": False, "items": [], "errors": []}
            )
            blocked = r["rc"] not in (0, None) or bool(summary.get("errors")) or not summary.get("items")
            res = {
                "rc": r["rc"],
                "timeout": r["timeout"],
                "summary": summary,
                "blocker": classify_blocker(r["err"] + json.dumps(summary.get("errors", []))) if blocked else None,
                "stderr_head": r["err"][:400],
            }
            if not blocked and download:
                gdir = workdir / "gd"
                gdir.mkdir(parents=True, exist_ok=True)
                d = run(
                    [
                        sys.executable,
                        "-m",
                        "gallery_dl",
                        "--config-ignore",
                        "-D",
                        str(gdir),
                        "-f",
                        "{num:>03}.{extension}",
                        *opts,
                        "--http-timeout",
                        "20",
                        "-R",
                        "1",
                        "--no-mtime",
                        "--no-part",
                        *cookie_args_gd,
                        "--",
                        url,
                    ],
                    env,
                    timeout * 3,
                )
                res["download_rc"] = d["rc"]
                res["files"] = [
                    ffprobe_summary(p) | {"name_prefix": p.name[:3]} for p in sorted(gdir.glob("*")) if p.is_file()
                ]
            result["tools"]["gallery-dl"] = res
        else:
            result["tools"]["gallery-dl"] = {"skipped": "no gallery-dl extractor matches this URL (pattern)"}
    finally:
        shutil.rmtree(home, ignore_errors=True)
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)  # never retain third-party media
    return result


def evaluate(entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Reduce one raw result to a sanitized verdict (no URL, no IDs, no titles)."""
    verdict: dict[str, Any] = {
        "content": base_id(entry["id"]),
        "ok": False,
        "tool": None,
        "items": None,
        "kinds": None,
        "blocker": None,
        "audio": None,
        "max_dims": None,
        "matches_expected": None,
        "downloaded_ok": None,
    }
    if str(result.get("validator", "")).startswith("rejected"):
        verdict["blocker"] = "VALIDATOR_REJECTED"
        return verdict
    for tool, res in result["tools"].items():
        if "skipped" in res or res.get("blocker"):
            if res.get("blocker") and not verdict["blocker"]:
                verdict["blocker"] = res["blocker"]
            continue
        if tool == "gallery-dl":
            items = res["summary"].get("items", [])
            good = bool(items)
            kinds = [i["kind"] for i in items]
            dims = max(((i.get("width") or 0) * (i.get("height") or 0) for i in items), default=0)
        else:
            entries = res["summary"].get("entries", [])
            good = bool(entries)
            kinds = ["video"] * len(entries)
            dims = max(((e.get("best_width") or 0) * (e.get("best_height") or 0) for e in entries), default=0)
        if not good:
            continue
        if not verdict["ok"]:
            files = res.get("files")
            if files is not None:
                audio = audio_from_files(files)  # ground truth from ffprobe of the real download
            elif tool == "yt-dlp":
                audio = all(e.get("has_audio") for e in res["summary"].get("entries", []))  # metadata claim only
            else:
                audio = None
            verdict.update(
                ok=True, tool=tool, items=len(kinds), kinds=kinds, blocker=None, max_dims=dims or None, audio=audio
            )
            if files is not None:
                verdict["downloaded_ok"] = (
                    bool(files) and all("error" not in f for f in files) and res.get("download_rc") == 0
                )
            exp_ok = True
            if "count" in entry:
                exp_ok &= entry["count"] == len(kinds)
            if "kinds" in entry:
                exp_ok &= entry["kinds"] == kinds
            verdict["matches_expected"] = exp_ok if ("count" in entry or "kinds" in entry) else None
    return verdict


def summarise_live(verdicts: list[dict[str, Any]], tool_versions: dict[str, str]) -> dict[str, Any]:
    by_content: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        s = by_content.setdefault(
            v["content"],
            {
                "urls": 0,
                "ok": 0,
                "blockers": {},
                "tools": {},
                "downloaded_ok": 0,
                "expected_checked": 0,
                "expected_ok": 0,
            },
        )
        s["urls"] += 1
        s["ok"] += 1 if v["ok"] else 0
        if v["blocker"]:
            s["blockers"][v["blocker"]] = s["blockers"].get(v["blocker"], 0) + 1
        if v["tool"]:
            s["tools"][v["tool"]] = s["tools"].get(v["tool"], 0) + 1
        s["downloaded_ok"] += 1 if v.get("downloaded_ok") else 0
        if v["matches_expected"] is not None:
            s["expected_checked"] += 1
            s["expected_ok"] += 1 if v["matches_expected"] else 0
    return {"date": utcnow(), "tool_versions": tool_versions, "by_content": by_content}


def matrix_status(content_ids: tuple[str, ...], results: dict[str, Any]) -> str:
    live = results.get("live", {}).get("by_content", {})
    parts = []
    for cid in content_ids:
        s = live.get(cid)
        if not s:
            continue
        tools = ", ".join(f"{t} x{n}" for t, n in s["tools"].items()) or "no working tool"
        text = f"{s['ok']}/{s['urls']} URLs extracted ({tools})"
        if s["expected_checked"]:
            text += f"; order/count matched {s['expected_ok']}/{s['expected_checked']}"
        if s["downloaded_ok"]:
            text += f"; download+ffprobe OK {s['downloaded_ok']}"
        if s["blockers"]:
            text += "; blockers: " + ", ".join(f"{k} x{v}" for k, v in s["blockers"].items())
        parts.append(text)
    telegram = results.get("telegram", {})
    for cid in content_ids:
        if cid in telegram:
            parts.append(telegram[cid])
    if not parts:
        return "Not tested"
    return f"{'; '.join(parts)} ({results.get('live', {}).get('date', '')[:10]})"


def render_matrix_md(results: dict[str, Any]) -> str:
    lines = ["| Content | Candidate | Required verification | Result |", "| --- | --- | --- | --- |"]
    for label, cand, need, ids in MATRIX:
        lines.append(f"| {label} | {cand} | {need} | {matrix_status(ids, results)} |")
    return "\n".join(lines)


def replace_block(text: str, name: str, body: str) -> str:
    start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        raise SystemExit(f"{DOC} is missing the {start} / {end} markers")
    return pattern.sub(lambda _m: f"{start}\n{body}\n{end}", text)


def write_outputs(results: dict[str, Any]) -> None:
    RESULTS_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if DOC.exists():
        doc = DOC.read_text(encoding="utf-8")
        if "offline" in results:
            doc = replace_block(doc, "OFFLINE", render_offline_md(results["offline"]))
        doc = replace_block(doc, "MATRIX", render_matrix_md(results))
        DOC.write_text(doc, encoding="utf-8")


def load_results() -> dict[str, Any]:
    if RESULTS_JSON.exists():
        with contextlib.suppress(ValueError):
            return json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    return {}


# ----------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("offline", help="no-network static evidence")
    lp = sub.add_parser("live", help="probe the private corpus against real platforms")
    lp.add_argument("--corpus", type=Path, default=ROOT / "tests" / "live" / "corpus.txt")
    lp.add_argument("--download", action="store_true", help="also download + ffprobe (files are deleted afterwards)")
    lp.add_argument("--cookies", type=Path, help="normal-session Netscape cookies file (Instagram only)")
    lp.add_argument("--delay", type=float, default=8.0, help="seconds between URLs (be gentle; default 8)")
    lp.add_argument("--timeout", type=int, default=120)
    lp.add_argument("--only", help="comma-separated content ids, e.g. P02,P05")
    lp.add_argument("--keep", action="store_true", help="keep downloaded files (they stay git-ignored)")
    args = ap.parse_args(argv)

    results = load_results()
    if args.mode == "offline":
        results["offline"] = offline_results()
        write_outputs(results)
        print(render_offline_md(results["offline"]))
        problems = [f"{t}: {m}" for t, m in results["offline"]["missing_flags"].items() if m]
        return 1 if problems else 0

    if not args.corpus.exists():
        print(
            f"No corpus at {args.corpus}. Copy tests/live/corpus.example.txt and fill it with "
            "permission-cleared URLs (see tests/live/README.md).",
            file=sys.stderr,
        )
        return 2
    entries = parse_corpus(args.corpus.read_text(encoding="utf-8"))
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        entries = [e for e in entries if base_id(e["id"]) in wanted]
    if not entries:
        print("Corpus has no matching entries.", file=sys.stderr)
        return 2
    if args.cookies and not args.cookies.exists():
        print(f"Cookies file not found: {args.cookies}", file=sys.stderr)
        return 2

    raw, verdicts = [], []
    for i, entry in enumerate(entries):
        print(f"[{i + 1}/{len(entries)}] {entry['id']} ...", flush=True)
        r = live_probe_one(entry, download=args.download, cookies=args.cookies, timeout=args.timeout, keep=args.keep)
        raw.append({**r, "url": entry["url"]})
        v = evaluate(entry, r)
        verdicts.append(v)
        print(f"    ok={v['ok']} tool={v['tool']} items={v['items']} blocker={v['blocker']}", flush=True)
        if i < len(entries) - 1:
            time.sleep(args.delay)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"results-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    versions = {
        "yt-dlp": tool_version("yt-dlp"),
        "gallery-dl": tool_version("gallery-dl"),
        "ffprobe": binary_version("ffprobe"),
    }
    live = summarise_live(verdicts, versions)
    prior = results.get("live", {}).get("by_content", {})
    live["by_content"] = {**prior, **live["by_content"]}  # a partial re-run keeps other content ids
    results["live"] = live
    write_outputs(results)
    print(f"\nRaw results (contain URLs, git-ignored): {raw_path}")
    print(f"Sanitized summary: {RESULTS_JSON}\nMatrix regenerated in: {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
