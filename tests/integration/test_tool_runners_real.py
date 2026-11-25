"""T03.2 / T03.3: the runners drive the REAL yt-dlp and gallery-dl binaries.

Only loopback media is used (gallery-dl `directlink`, yt-dlp `generic`), so no platform is contacted.
This proves the command lines are accepted by the pinned tool versions, that options/URLs cannot be
smuggled in as flags, that config files are ignored and that results are parsed from real output.
Platform *logic* is covered separately with synthetic fixtures (tests/contract).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.contracts import ItemKind
from app.core.deadline import Deadline
from app.core.errors import BotError, ErrorCode
from app.extractors.gallerydl_runner import GalleryDlRunner
from app.extractors.ytdlp_runner import YtDlpRunner
from app.media.probe import probe_file
from app.media.quality import select_format
from app.services.cleanup import WorkspaceManager
from app.services.subprocess_runner import SubprocessRunner
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return WorkspaceManager(Path("tmp/jobs")).create("a" * 32)


def dl(seconds=120):
    return Deadline(seconds)


# --- yt-dlp -------------------------------------------------------------------------------------------
async def test_ytdlp_discover_select_and_download_a_real_video(media, media_server, ws):
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    url = f"{media_server}/v_h264_aac.mp4"
    info = await y.discover(url, ws, dl())
    assert len(info.entries) == 1 and info.entries[0].formats
    choice = select_format(info.entries[0].formats)
    paths = await y.download(url, ws, dl(), selector=choice.selector)
    assert len(paths) == 1 and paths[0].parent == ws and paths[0].name.startswith("yt_1_")
    probed = await probe_file(paths[0])
    assert probed.kind is ItemKind.VIDEO and probed.video_codec == "h264" and probed.has_audio


async def test_default_allowlist_refuses_generic_pages(media, media_server, ws):
    """The production allowlist excludes yt-dlp's `generic` extractor: an arbitrary URL is rejected."""
    y = YtDlpRunner(SubprocessRunner())  # default: tiktok, instagram, instagram:story, twitter
    with pytest.raises(BotError) as exc:
        await y.discover(f"{media_server}/v_h264_aac.mp4", ws, dl())
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST


async def test_a_url_can_never_act_as_an_option(ws):
    """The URL is placed after `--`; an option-looking 'URL' is therefore just an unsupported target."""
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    for hostile in ("--exec=echo PWNED > pwned.txt", "-o /tmp/x", "--config-locations /etc/passwd"):
        with pytest.raises(BotError):
            await y.discover(hostile, ws, dl())
    assert not (ws / "pwned.txt").exists()


async def test_user_config_files_are_ignored_even_if_present(media, media_server, ws, tmp_path):
    """A hostile yt-dlp.conf in the (sandboxed) HOME must have no effect: --ignore-config."""
    home_conf = ws / ".home" / ".config" / "yt-dlp"
    home_conf.mkdir(parents=True)
    (home_conf / "config").write_text('--exec "echo PWNED > ' + str(ws / "pwned.txt").replace("\\", "/") + '"\n')
    (ws / "yt-dlp.conf").write_text('--exec "echo PWNED > pwned.txt"\n')
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    url = f"{media_server}/v_h264_silent.mp4"
    info = await y.discover(url, ws, dl())
    await y.download(url, ws, dl(), selector=select_format(info.entries[0].formats).selector)
    assert not (ws / "pwned.txt").exists()


async def test_ytdlp_proxy_flag_is_honoured_and_failure_is_a_retryable_network_error(media_server, ws):
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",), proxy="http://127.0.0.1:9", socket_timeout=3)
    with pytest.raises(BotError) as exc:
        await y.discover("http://example.invalid/x.mp4", ws, dl())
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT and exc.value.auto_retryable


async def test_ytdlp_enforces_the_per_file_size_limit(media, media_server, ws):
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",), max_file_bytes=2000)
    url = f"{media_server}/v_h264_aac.mp4"
    info = await y.discover(url, ws, dl())
    with pytest.raises(BotError) as exc:
        await y.download(url, ws, dl(), selector=select_format(info.entries[0].formats).selector)
    assert exc.value.code is ErrorCode.LIMIT_EXCEEDED


async def test_ytdlp_deadline_expiry_is_reported_as_timeout(media_server, ws):
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",))
    d = Deadline(0)
    with pytest.raises(BotError) as exc:
        await y.discover(f"{media_server}/v_h264_aac.mp4", ws, d)
    assert exc.value.code is ErrorCode.DOWNLOAD_TIMEOUT


# --- gallery-dl ----------------------------------------------------------------------------------------
async def test_gallerydl_discover_parses_real_dump(media, media_server, ws):
    g = GalleryDlRunner(SubprocessRunner())
    dump = await g.discover(f"{media_server}/photo.jpg", ws, dl(), options={})
    assert len(dump.files) == 1 and dump.files[0]["extension"] == "jpg" and dump.directories
    assert dump.errors == [] and not dump.warnings.skipped_media


async def test_gallerydl_download_names_files_by_num_inside_the_workspace(media, media_server, ws):
    g = GalleryDlRunner(SubprocessRunner())
    res = await g.download(f"{media_server}/photo.jpg", ws, dl(), options={}, filename_format="001.{extension}")
    assert list(res.files) == [1]
    f = res.files[1]
    assert f.parent == ws / "gd" and f.read_bytes() == media.jpg.read_bytes()  # bytes preserved (AT14)


async def test_gallerydl_ignores_files_it_did_not_name_itself(media, media_server, ws):
    g = GalleryDlRunner(SubprocessRunner())
    out = ws / "gd"
    out.mkdir()
    (out / "evil.sh").write_text("#!/bin/sh\n")
    (out / "002.jpg.part").write_bytes(b"x")
    res = await g.download(f"{media_server}/photo.jpg", ws, dl(), options={}, filename_format="001.{extension}")
    assert list(res.files) == [1]


async def test_gallerydl_unsupported_url_is_classified(ws):
    g = GalleryDlRunner(SubprocessRunner())
    with pytest.raises(BotError) as exc:
        await g.discover("https://example.invalid/nothing", ws, dl(), options={})
    assert exc.value.code is ErrorCode.UNSUPPORTED_POST  # exit bit 64


async def test_gallerydl_url_cannot_act_as_an_option(ws):
    g = GalleryDlRunner(SubprocessRunner())
    with pytest.raises(BotError):
        await g.discover("--exec=echo PWNED > pwned.txt", ws, dl(), options={})
    assert not (ws / "pwned.txt").exists()


async def test_gallerydl_config_files_are_ignored(media, media_server, ws):
    conf = ws / ".home" / "gallery-dl"
    conf.mkdir(parents=True)
    (conf / "config.json").write_text(
        '{"extractor": {"postprocessors": [{"name": "exec", "command": "echo PWNED > pwned.txt"}]}}'
    )
    (ws / ".home" / "gallery-dl.conf").write_text('{"extractor": {"base-directory": "/tmp/elsewhere"}}')
    g = GalleryDlRunner(SubprocessRunner())
    res = await g.download(f"{media_server}/photo.jpg", ws, dl(), options={}, filename_format="001.{extension}")
    assert res.files and not (ws / "pwned.txt").exists()


@pytest.mark.parametrize("key", ["audio; rm", "A", "a=b", "-x", "", "a b", "../x"])
async def test_unsafe_option_names_are_rejected_before_any_process_starts(ws, key):
    g = GalleryDlRunner(SubprocessRunner())
    with pytest.raises(ValueError):
        await g.discover("https://example.invalid/", ws, dl(), options={key: "false"})


@pytest.mark.parametrize("value", ["x y", "a;b", "$(id)", "a" * 65, "`x`", "a\nb"])
async def test_unsafe_option_values_are_rejected(ws, value):
    g = GalleryDlRunner(SubprocessRunner())
    with pytest.raises(ValueError):
        await g.discover("https://example.invalid/", ws, dl(), options={"audio": value})


@pytest.mark.parametrize("fmt", ["../x.{extension}", "a/b", "{num}; rm", "x y", "a" * 41])
async def test_unsafe_filename_formats_are_rejected(ws, fmt):
    g = GalleryDlRunner(SubprocessRunner())
    with pytest.raises(ValueError):
        await g.download("https://example.invalid/", ws, dl(), options={}, filename_format=fmt)


async def test_gallerydl_budget_kills_runaway_downloads(media, media_server, ws):
    g = GalleryDlRunner(SubprocessRunner(), max_bytes=100)  # smaller than the 1.9 KB test image
    try:
        await g.download(f"{media_server}/photo.jpg", ws, dl(), options={}, filename_format="001.{extension}")
    except BotError as exc:
        assert exc.code in {ErrorCode.LIMIT_EXCEEDED, ErrorCode.EXTRACTOR_CHANGED}
    else:  # the file may finish before the 0.5 s watcher tick; either outcome is acceptable here,
        pass  # the deterministic budget kill is covered in test_subprocess_runner


# --- the operator's session file is read-only: tools get a private per-job copy -------------------------
def _read_only_session(tmp_path):
    import os
    import stat

    jar = tmp_path / "operator-session.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tFALSE\t4102444800\tdummy\tvalue\n", encoding="utf-8"
    )
    os.chmod(jar, stat.S_IREAD)  # like the production mount
    return jar, (jar.read_bytes(), jar.stat().st_mtime_ns)


async def test_ytdlp_works_with_a_read_only_session_file_because_it_only_sees_a_private_copy(
    media, media_server, ws, tmp_path
):
    """Finding (real yt-dlp): handed a READ-ONLY cookies file it saves the jar on exit, dies with a
    PermissionError traceback and exit code 1 - even for a good extraction. The runner therefore never
    passes the operator's file at all."""
    from app.extractors.session_file import COOKIES_NAME

    jar, snapshot = _read_only_session(tmp_path)
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",), cookies=jar)
    info = await y.discover(f"{media_server}/v_h264_aac.mp4", ws, dl(), use_cookies=True)
    assert len(info.entries) == 1
    assert (jar.read_bytes(), jar.stat().st_mtime_ns) == snapshot  # the original is untouched
    assert (ws / ".home" / COOKIES_NAME).exists()  # the tool worked on its own copy


async def test_gallerydl_with_a_session_file_never_modifies_the_original(media, media_server, ws, tmp_path):
    jar, snapshot = _read_only_session(tmp_path)
    g = GalleryDlRunner(SubprocessRunner(), cookies=jar)
    dump = await g.discover(f"{media_server}/p_1.jpg", ws, dl(), options={}, use_cookies=True)
    assert dump.files
    assert (jar.read_bytes(), jar.stat().st_mtime_ns) == snapshot
    assert not list(tmp_path.glob("*.tmp"))  # no write-back temp file next to the operator's file


async def test_calls_that_must_stay_anonymous_do_not_even_copy_the_session(media, media_server, ws, tmp_path):
    jar, _ = _read_only_session(tmp_path)
    y = YtDlpRunner(SubprocessRunner(), allowed_extractors=("generic",), cookies=jar)
    await y.discover(f"{media_server}/v_h264_aac.mp4", ws, dl(), use_cookies=False)
    assert not (ws / ".home" / "session-cookies.txt").exists()
