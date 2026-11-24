"""Dependency pinning guard (T00.3, T01.7): extractors are pinned exactly and consistently."""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXTRACTORS = ("yt-dlp", "gallery-dl")


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _pins_in_pyproject() -> dict[str, str]:
    pins = {}
    for dep in _pyproject()["project"]["dependencies"]:
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;]+)", dep.strip())
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def _lock_versions() -> dict[str, str]:
    versions = {}
    for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if m:
            versions[m.group(1).lower()] = m.group(2)
    return versions


@pytest.mark.parametrize("name", EXTRACTORS)
def test_extractors_are_pinned_exactly(name):
    assert name in _pins_in_pyproject(), f"{name} must be pinned with == in pyproject.toml"


@pytest.mark.parametrize("name", EXTRACTORS)
def test_pin_lock_and_installed_version_agree(name):
    pinned = _pins_in_pyproject()[name]
    assert _lock_versions()[name] == pinned, "re-run: uv lock && uv export ... -o requirements.lock"
    assert metadata.version(name) == pinned, "venv is out of sync: run `uv sync`"


def test_lock_file_is_hashed_and_has_no_editable_or_vcs_entries():
    text = (REPO / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    assert not re.search(r"^(-e |git\+|https?://)", text, re.M), "no editable/VCS/URL installs allowed"


def test_python_version_is_pinned_to_one_minor():
    requires = _pyproject()["project"]["requires-python"]
    assert requires == ">=3.12,<3.13"
    assert (REPO / ".python-version").read_text().strip() == "3.12"
