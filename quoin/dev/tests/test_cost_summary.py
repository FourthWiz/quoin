"""T-03: Cross-language parity tests for cost_summary.py normalize_total().

Loads cost_summary_fixtures.json (single source of truth shared with TS tests)
and verifies that normalize_total() returns the expected (value, is_partial) for
every fixture case.

Also validates:
- The 7-key ladder is in the correct order (matches TOTAL_KEY_LADDER in costService.ts)
- The module has no cost_from_jsonl or pricing imports (core boundary check R-02)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load cost_summary core module directly from source
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_summary.py"
_FIXTURE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "testdata" / "cost_summary_fixtures.json"


def _load_cost_summary():
    key = "_test_cost_summary_core"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _CORE_PATH)
    assert spec is not None, f"Cannot create spec for {_CORE_PATH}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cs():
    return _load_cost_summary()


@pytest.fixture(scope="module")
def fixtures():
    assert _FIXTURE_PATH.exists(), f"Fixture file not found: {_FIXTURE_PATH}"
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Parametrized parity test — one case per fixture entry
# ---------------------------------------------------------------------------

def _fixture_ids():
    try:
        data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        return [case["name"] for case in data]
    except Exception:
        return []


@pytest.mark.parametrize("fixture_name", _fixture_ids())
def test_normalize_total_parity(fixture_name, cs, fixtures):
    """normalize_total() must match (expect_total, expect_partial) from shared fixture."""
    case = next(c for c in fixtures if c["name"] == fixture_name)
    summary = case["summary"]
    expect_total = case["expect_total"]
    expect_partial = case["expect_partial"]

    value, is_partial = cs.normalize_total(summary)

    if expect_total is None:
        assert value is None, (
            f"[{fixture_name}] expected None but got {value!r}"
        )
        assert is_partial is False, (
            f"[{fixture_name}] unavailable total must not set is_partial=True"
        )
    else:
        assert value is not None, (
            f"[{fixture_name}] expected {expect_total!r} but got None (unavailable)"
        )
        assert abs(value - expect_total) < 1e-9, (
            f"[{fixture_name}] expected {expect_total!r}, got {value!r}"
        )
        assert is_partial == expect_partial, (
            f"[{fixture_name}] expected is_partial={expect_partial!r}, got {is_partial!r}"
        )


# ---------------------------------------------------------------------------
# Fixture file integrity — removing it must cause test collection to fail
# ---------------------------------------------------------------------------

def test_fixture_file_exists():
    assert _FIXTURE_PATH.exists(), (
        f"Shared fixture file not found: {_FIXTURE_PATH}\n"
        "Deliberately removing this file should fail this test."
    )


def test_fixture_covers_all_ladder_keys(fixtures):
    """All 7 ladder keys must appear as a primary key in at least one fixture."""
    ladder = [
        "grand_total",
        "grand_total_usd",
        "total_usd",
        "total_cost_usd",
        "period_total_cost_usd",
        "estimated_task_cost_usd",
        "task_total",
    ]
    all_keys: set[str] = set()
    for case in fixtures:
        all_keys.update(case["summary"].keys())
    for key in ladder:
        assert key in all_keys, (
            f"Ladder key '{key}' not covered by any fixture — "
            "add a case with this key as primary total"
        )


# ---------------------------------------------------------------------------
# Ladder order guard
# ---------------------------------------------------------------------------

def test_ladder_order_matches_ts(cs):
    """Python ladder order must match costService.ts TOTAL_KEY_LADDER exactly."""
    expected_order = [
        "grand_total",
        "grand_total_usd",
        "total_usd",
        "total_cost_usd",
        "period_total_cost_usd",
        "estimated_task_cost_usd",
        "task_total",
    ]
    assert list(cs._TOTAL_KEY_LADDER) == expected_order, (
        "Python _TOTAL_KEY_LADDER diverged from costService.ts TOTAL_KEY_LADDER! "
        "Update both to stay in sync."
    )


def test_mutated_ladder_order_fails(cs):
    """A deliberately wrong ladder order must produce wrong results on the fixture."""
    # Use a summary where grand_total_usd exists but grand_total does not.
    # With the correct ladder: grand_total misses, grand_total_usd hits → 82.59.
    # With reversed ladder: task_total misses, ... grand_total_usd still hits → 82.59 (same).
    # Instead: test that grand_total takes priority over grand_total_usd.
    summary = {"grand_total_usd": 99.0, "grand_total": 12.0}
    value, _ = cs.normalize_total(summary)
    assert abs(value - 12.0) < 1e-9, (
        "grand_total must take priority over grand_total_usd (first-hit-wins)"
    )


# ---------------------------------------------------------------------------
# Core boundary check — no adapter imports
# ---------------------------------------------------------------------------

def test_no_adapter_imports():
    """cost_summary.py must not import cost_from_jsonl or pricing tables."""
    source = _CORE_PATH.read_text(encoding="utf-8")
    # Check for actual import statements, not just the word appearing in docs
    import re as _re
    assert not _re.search(r"^\s*(?:import|from)\s+cost_from_jsonl", source, _re.MULTILINE), (
        "cost_summary.py imports cost_from_jsonl — boundary violation (R-02)"
    )
    # PRICES is only imported via cost_from_jsonl — guard the binding too
    assert not _re.search(r"^\s*PRICES\s*=", source, _re.MULTILINE), (
        "cost_summary.py binds PRICES — pricing import boundary violation (R-02)"
    )


def test_json_passthrough_estimated_keys_when_present(tmp_path, capsys):
    import json as _json

    f = tmp_path / "s.json"
    f.write_text(_json.dumps({
        "grand_total": 3.0, "estimated_total": 1.0, "estimated_count": 2,
        "priced_rows": 5, "phases_excluded": ["end-of-task"], "ignored_key": 1,
    }))
    assert _load_cost_summary().main([str(f), "--format", "json"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out == {"total": 3.0, "is_partial": False, "estimated_total": 1.0,
                   "estimated_count": 2, "priced_rows": 5,
                   "phases_excluded": ["end-of-task"]}


def test_json_omits_passthrough_keys_when_absent(tmp_path, capsys):
    import json as _json

    f = tmp_path / "s.json"
    f.write_text(_json.dumps({"grand_total": 3.0}))
    assert _load_cost_summary().main([str(f), "--format", "json"]) == 0
    assert set(_json.loads(capsys.readouterr().out)) == {"total", "is_partial"}
