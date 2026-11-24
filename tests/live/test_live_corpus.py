"""OPT-IN live tests (T03.12, T06.5). Never run by default (`-m 'not live'` in pyproject).

    cp tests/live/corpus.example.txt tests/live/corpus.txt   # permission-cleared URLs ONLY
    uv run pytest -m live -q

Each corpus line is pushed through the REAL pipeline (URL validation -> safe short-link resolution ->
adapter discover/fetch -> ffprobe validation) against the live platforms, and the result is checked
against the `count=` / `kinds=` hints you gave from what you can see in the post. Nothing is sent to
Telegram and downloaded files are deleted with the temp directory.

This module contains no URLs. Without a corpus every test is skipped - it can never hit a platform on
its own, and a failing live run is evidence to record in ops/feasibility.md, not something to "fix" by
adding a mock.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.core.contracts import ExtractionRequest, ItemKind
from app.core.deadline import Deadline
from app.core.errors import BotError
from app.core.url_validator import UrlKind, validate_url
from app.extractors.local import LocalExtractionBackend
from scripts.feasibility_probe import parse_corpus

pytestmark = pytest.mark.live

CORPUS = Path(__file__).with_name("corpus.txt")
ENTRIES = parse_corpus(CORPUS.read_text(encoding="utf-8")) if CORPUS.exists() else []


def _id(entry: dict) -> str:
    return f"{entry['id']}@line{entry['line']}"  # never the URL itself


@pytest.mark.skipif(not ENTRIES, reason="no tests/live/corpus.txt (see tests/live/README.md)")
@pytest.mark.parametrize("entry", ENTRIES, ids=[_id(e) for e in ENTRIES])
async def test_live_entry_through_the_real_pipeline(entry, tmp_path):
    settings = Settings(
        _env_file=None,
        bot_token="123456789:" + "A" * 35,  # never used: nothing is sent to Telegram
        allowed_user_ids=[1],
        data_dir=tmp_path / "data",
        work_dir=tmp_path / "work",
    )
    backend = LocalExtractionBackend(settings)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    deadline = Deadline(settings.extraction_timeout_seconds)
    try:
        validated = validate_url(entry["url"])
        if validated.kind is UrlKind.SHORT_LINK:
            validated = await backend.resolve(validated, deadline)
        request = ExtractionRequest(
            job_id=uuid.uuid4(),
            owner_id=1,
            chat_id=1,
            origin_message_id=1,
            original_url=entry["url"],
            extraction_url=validated.extraction_url,
            platform=validated.platform,
            post_hint=validated.post_hint,
        )
        manifest = await backend.discover(request, workspace, deadline)
        result = await backend.fetch(request, manifest, workspace, deadline)
    except BotError as err:
        # A typed block (auth wall, rate limit, expired Story, ...) is a RESULT to record, not a bug to hide.
        pytest.fail(f"{entry['id']}: blocked with {err.code.value} ({err.reason}) - record it in ops/feasibility.md")
    finally:
        await backend.aclose()

    kinds = [a.kind.value for a in result.assets]
    assert result.assets, "no media retrieved"
    assert [a.position for a in result.assets] == sorted({a.position for a in result.assets}), (
        "order must be source order"
    )
    if "count" in entry:
        assert len(result.assets) == entry["count"], f"expected {entry['count']} items, got {len(result.assets)}"
    if "kinds" in entry:
        assert kinds == entry["kinds"], f"expected order {entry['kinds']}, got {kinds}"
    assert result.omissions == [], f"unexpected omissions: {[o.model_dump() for o in result.omissions]}"
    for asset in result.assets:
        assert asset.size_bytes > 0 and asset.width and asset.height
        if asset.kind is ItemKind.VIDEO:
            assert asset.duration_seconds and asset.video_codec
