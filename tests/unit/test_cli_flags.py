"""Every extractor option the project relies on must exist in the INSTALLED tool versions."""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.extractors.cli_flags import (
    GALLERYDL_REQUIRED_FLAGS,
    YTDLP_REQUIRED_FLAGS,
    missing_flags,
)


def _help(module: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def ytdlp_help() -> str:
    return _help("yt_dlp")


@pytest.fixture(scope="module")
def gallerydl_help() -> str:
    return _help("gallery_dl")


def test_ytdlp_flags_exist(ytdlp_help):
    assert missing_flags(ytdlp_help, YTDLP_REQUIRED_FLAGS) == []


def test_gallerydl_flags_exist(gallerydl_help):
    assert missing_flags(gallerydl_help, GALLERYDL_REQUIRED_FLAGS) == []


def test_missing_flag_detection_is_exact():
    text = "  --no-playlist   do x\n  --max-filesize-extra  y\n"
    assert missing_flags(text, ("--no-playlist",)) == []
    # a longer option that merely starts with the name must not satisfy the requirement
    assert missing_flags(text, ("--max-filesize",)) == ["--max-filesize"]
    assert missing_flags(text, ("--nope",)) == ["--nope"]
