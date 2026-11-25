"""ops/test-report.md must only cite tests that exist, and every acceptance test must have a row."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "ops" / "test-report.md"


def defined_tests() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
        }
    return names


def test_every_test_the_report_cites_exists():
    text = REPORT.read_text(encoding="utf-8")
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", text))
    assert len(cited) > 80, "the report should cite its evidence"
    missing = sorted(cited - defined_tests())
    assert not missing, f"ops/test-report.md cites tests that do not exist (renamed or removed?): {missing}"


def test_every_acceptance_test_has_a_row_and_a_live_status():
    text = REPORT.read_text(encoding="utf-8")
    for n in range(1, 27):
        row = next((ln for ln in text.splitlines() if ln.startswith(f"| AT{n:02d} |")), None)
        assert row is not None, f"AT{n:02d} has no row in the report"
        assert "not run" in row.lower(), f"AT{n:02d}: the live/device status must say 'not run' until it really was run"


def test_the_report_does_not_claim_live_or_device_results():
    text = REPORT.read_text(encoding="utf-8").lower()
    for forbidden in ("live pass", "live-tested", "verified on android", "verified on ios", "passed on a vps"):
        assert forbidden not in text
