"""Feasibility probe: parsers are validated against REAL tool output from a loopback server."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts import feasibility_probe as probe
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


def _run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True,
                          timeout=120, check=False)


# --- real output from the installed tools ---------------------------------------------------


def test_gallerydl_dump_parser_on_real_output(media_server):
    p = _run("gallery_dl", "--config-ignore", "-j", "--", f"{media_server}/photo.jpg")
    assert p.returncode == 0, p.stderr
    summary = probe.parse_gallerydl_dump(p.stdout)
    assert summary["parsed"] is True
    assert [i["kind"] for i in summary["items"]] == ["photo"]
    assert summary["errors"] == []


def test_gallerydl_dump_parser_classifies_video_extension(media_server):
    p = _run("gallery_dl", "--config-ignore", "-j", "--", f"{media_server}/v_h264_aac.mp4")
    assert [i["kind"] for i in probe.parse_gallerydl_dump(p.stdout)["items"]] == ["video"]


def test_ytdlp_info_parser_on_real_output(media_server):
    p = _run("yt_dlp", "--ignore-config", "--no-playlist", "--no-cache-dir", "-J", "--",
             f"{media_server}/v_h264_aac.mp4")
    assert p.returncode == 0, p.stderr
    summary = probe.parse_ytdlp_info(p.stdout)
    assert summary["parsed"] is True and len(summary["entries"]) == 1
    assert summary["entries"][0]["extractor"] == "Generic"


def test_parsers_tolerate_garbage():
    assert probe.parse_gallerydl_dump("not json")["parsed"] is False
    assert probe.parse_ytdlp_info("null")["parsed"] is False
    assert probe.parse_ytdlp_info("")["parsed"] is False


# --- gallery-dl message shapes (as documented in gallery_dl/extractor/message.py) ----------


def test_gallerydl_dump_collects_errors_counts_and_order():
    dump = json.dumps([
        [2, {"count": 3}],
        [3, "u1", {"num": 1, "extension": "jpg", "width": 10, "height": 20}],
        [3, "u2", {"num": 2, "extension": "mp4", "width": 30, "height": 40}],
        [3, "u3", {"num": 3, "extension": "mp3"}],
        [-1, {"error": "AuthRequired", "message": "login needed"}],
    ])
    s = probe.parse_gallerydl_dump(dump)
    assert s["declared_count"] == 3
    assert [i["kind"] for i in s["items"]] == ["photo", "video", "other:mp3"]
    assert s["errors"] == [{"error": "AuthRequired", "message": "login needed"}]


def test_ytdlp_playlist_entries_and_audio_detection():
    info = {"_type": "playlist", "entries": [
        {"extractor_key": "Instagram", "duration": 5, "formats": [
            {"vcodec": "avc1", "acodec": "none", "width": 640, "height": 640, "tbr": 100},
            {"vcodec": "avc1", "acodec": "mp4a", "width": 1080, "height": 1080, "tbr": 900}]},
        {"extractor_key": "Instagram", "formats": [{"vcodec": "avc1", "acodec": "none", "width": 10, "height": 10}]},
    ]}
    s = probe.parse_ytdlp_info(json.dumps(info))
    assert [(e["best_width"], e["has_audio"]) for e in s["entries"]] == [(1080, True), (10, False)]


# --- ffprobe + audio logic --------------------------------------------------------------------


def test_audio_from_files_ignores_images_and_reports_silent_videos(media):
    jpg = probe.ffprobe_summary(media.jpg)
    with_audio = probe.ffprobe_summary(media.mp4_h264_aac)
    silent = probe.ffprobe_summary(media.mp4_h264_silent)
    assert probe.audio_from_files([jpg]) is None  # a JPEG is not a video
    assert probe.audio_from_files([jpg, with_audio]) is True
    assert probe.audio_from_files([with_audio, silent]) is False
    assert with_audio["has_audio"] and with_audio["vcodec"] == "h264"
    assert (jpg["width"], jpg["height"]) == (320, 240)


# --- corpus + verdict + sanitisation ------------------------------------------------------------


def test_corpus_parsing():
    text = """
    # comment
    P02 https://www.tiktok.com/@u/photo/7300000000000000000 count=6   # trailing comment
    p05 https://www.instagram.com/p/ABCdef12345/ kinds=Photo,video,photo
    """
    entries = probe.parse_corpus(text)
    assert entries[0]["id"] == "P02" and entries[0]["count"] == 6
    assert entries[1]["id"] == "P05" and entries[1]["kinds"] == ["photo", "video", "photo"]


@pytest.mark.parametrize("bad", ["P01", "P01 https://x.com/a/status/1 bogus=1"])
def test_corpus_rejects_malformed_lines(bad):
    with pytest.raises(ValueError):
        probe.parse_corpus(bad)


def gd(items):
    return {"summary": {"items": [{"num": i + 1, "kind": k, "width": 100, "height": 50} for i, k in enumerate(items)],
                        "errors": []}, "blocker": None, "rc": 0}


def test_evaluate_verifies_count_and_order():
    entry = {"id": "P05", "url": "https://u", "kinds": ["photo", "video", "photo"]}
    ok = probe.evaluate(entry, {"validator": "accepted", "tools": {"gallery-dl": gd(["photo", "video", "photo"])}})
    assert ok["ok"] and ok["matches_expected"] is True and ok["items"] == 3
    wrong_order = probe.evaluate(entry, {"validator": "accepted", "tools": {"gallery-dl": gd(["photo", "photo", "video"])}})
    assert wrong_order["matches_expected"] is False


def test_evaluate_records_blockers_without_retrying_around_them():
    blocked = {"validator": "accepted", "tools": {
        "gallery-dl": {"blocker": "AUTH_REQUIRED", "summary": {}},
        "yt-dlp": {"skipped": "pattern"},
    }}
    v = probe.evaluate({"id": "P06", "url": "https://u"}, blocked)
    assert v["ok"] is False and v["blocker"] == "AUTH_REQUIRED"


def test_evaluate_validator_rejection_is_not_a_network_attempt():
    v = probe.evaluate({"id": "P01", "url": "https://evil.example/"}, {"validator": "rejected INVALID_URL", "tools": {}})
    assert v["blocker"] == "VALIDATOR_REJECTED"


def test_summary_and_matrix_contain_no_urls_or_identifiers():
    url = "https://www.tiktok.com/@secretuser/photo/7312345678901234567"
    entry = {"id": "P02", "url": url, "count": 2}
    v = probe.evaluate(entry, {"validator": "accepted", "tools": {"gallery-dl": gd(["photo", "photo"])}})
    live = probe.summarise_live([v], {"gallery-dl": "1.0"})
    blob = json.dumps(live) + probe.render_matrix_md({"live": live})
    assert "secretuser" not in blob and "7312345678901234567" not in blob and "tiktok.com" not in blob
    row = next(r for r in probe.render_matrix_md({"live": live}).splitlines() if r.startswith("| TikTok photo-mode"))
    assert "1/1 URLs extracted" in row and "matched 1/1" in row


def test_untested_rows_stay_not_tested():
    md = probe.render_matrix_md({})
    assert md.count("Not tested") == len(probe.MATRIX)


def test_blocker_classification():
    assert probe.classify_blocker("HTTP Error 429: Too Many Requests") == "RATE_LIMITED"
    assert probe.classify_blocker("Account credentials required") == "AUTH_REQUIRED"
    assert probe.classify_blocker("NotFoundError: story") == "NOT_FOUND_OR_EXPIRED"
    assert probe.classify_blocker("weird failure") == "EXTRACTOR_ERROR"


def test_replace_block_requires_markers():
    text = "a\n<!-- MATRIX:START -->\nold\n<!-- MATRIX:END -->\nb"
    assert "new" in probe.replace_block(text, "MATRIX", "new") and "old" not in probe.replace_block(text, "MATRIX", "new")
    with pytest.raises(SystemExit):
        probe.replace_block("no markers", "MATRIX", "x")
