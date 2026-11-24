"""The committed extractor fixtures must equal what the generator produces (no hand-edited pseudo-captures)."""

from __future__ import annotations

import json

from tests.fixtures import make_fixtures


def test_committed_fixtures_match_the_generator():
    committed = {p.stem: p.read_text(encoding="utf-8") for p in make_fixtures.OUT.glob("*.json")}
    expected = {
        name: json.dumps(payload, indent=1, sort_keys=True) + "\n" for name, payload in make_fixtures.FIXTURES.items()
    }
    assert committed.keys() == expected.keys()
    for name, text in expected.items():
        assert committed[name].replace("\r\n", "\n") == text, f"{name}.json drifted; run make_fixtures.py"


def test_every_fixture_is_marked_synthetic():
    """So a fixture can never be mistaken for a real platform capture."""
    for name, payload in make_fixtures.FIXTURES.items():
        blob = json.dumps(payload)
        if name.startswith("gd_error") or name == "gd_empty":
            continue  # error/empty dumps carry no metadata to mark
        assert '"_synthetic": true' in blob, name


def test_fixtures_contain_no_real_looking_urls():
    for payload in make_fixtures.FIXTURES.values():
        blob = json.dumps(payload)
        for real in ("tiktokcdn", "cdninstagram", "fbcdn.net", "twimg.com", "instagram.com/p/real"):
            assert real not in blob
