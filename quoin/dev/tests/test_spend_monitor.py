"""T-08: Unit tests for spend_monitor.py — aggregation, render, by-task, memo cache.

Tests use a fixture fake HOME under tmp_path to isolate from real ~/.claude/projects/.
Fixture JSONL files contain:
  - opus row with today's timestamp
  - sonnet row with today's timestamp
  - haiku row with today's timestamp
  - opus row with yesterday's timestamp (EXCLUDED from today)
  - malformed JSON line (skipped, no crash)
  - row with message.usage present but no timestamp (excluded, counted in skipped_no_ts)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load spend_monitor module from source (wrapper path)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "spend_monitor.py"


def _load_spend_monitor():
    """Load spend_monitor core module directly (avoids needing it installed)."""
    key = "_test_spend_monitor_core"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _CORE_PATH)
    assert spec is not None, f"Cannot create spec for {_CORE_PATH}"
    assert spec.loader is not None, f"Spec has no loader for {_CORE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sm():
    """spend_monitor module fixture."""
    return _load_spend_monitor()


# ---------------------------------------------------------------------------
# Helpers: build fixture JSONL lines
# ---------------------------------------------------------------------------

def _ts_today() -> str:
    """ISO-8601 UTC timestamp for today (now)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_yesterday() -> str:
    """ISO-8601 UTC timestamp for yesterday."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def _make_row(model: str, input_tokens: int, output_tokens: int, ts: str) -> str:
    """Build a JSONL row that parse_session_today will count."""
    row = {
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    return json.dumps(row)


def _make_row_no_ts(model: str, input_tokens: int) -> str:
    """Build a row that has usage but NO timestamp — must be excluded and counted in skipped_no_ts."""
    row = {
        "message": {
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }
    return json.dumps(row)


def _build_fixture_jsonl(path: Path) -> dict:
    """Write a fixture JSONL and return expected USD totals for 'today' rows only.

    Lines:
      1. opus today row: 100 input, 50 output
      2. sonnet today row: 200 input, 100 output
      3. haiku today row: 300 input, 150 output
      4. opus yesterday row: 500 input, 200 output  ← EXCLUDED
      5. malformed line  ← skipped, no crash
      6. row with usage but no timestamp  ← excluded, counted in skipped_no_ts
    """
    sm = _load_spend_monitor()

    lines = [
        _make_row("claude-opus-4-7", 100, 50, _ts_today()),
        _make_row("claude-sonnet-4-6", 200, 100, _ts_today()),
        _make_row("claude-haiku-4-5-20251001", 300, 150, _ts_today()),
        _make_row("claude-opus-4-7", 500, 200, _ts_yesterday()),   # excluded
        "not-json-at-all{{{",                                        # malformed
        _make_row_no_ts("claude-opus-4-7", 999),                    # no-ts, skipped
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Compute expected today_usd from PRICES (hand-computed)
    prices = sm.PRICES
    opus_usd = (
        100 * prices["claude-opus-4-7"]["input"]
        + 50 * prices["claude-opus-4-7"]["output"]
    ) / 1_000_000
    sonnet_usd = (
        200 * prices["claude-sonnet-4-6"]["input"]
        + 100 * prices["claude-sonnet-4-6"]["output"]
    ) / 1_000_000
    haiku_usd = (
        300 * prices["claude-haiku-4-5-20251001"]["input"]
        + 150 * prices["claude-haiku-4-5-20251001"]["output"]
    ) / 1_000_000

    return {
        "expected_today_usd": opus_usd + sonnet_usd + haiku_usd,
        "opus_usd": opus_usd,
        "sonnet_usd": sonnet_usd,
        "haiku_usd": haiku_usd,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_home(tmp_path: Path):
    """Create a fake HOME with a single fixture JSONL under .claude/projects/<hash>/."""
    sm = _load_spend_monitor()

    home = tmp_path / "home"
    proj_hash = sm.project_hash(str(tmp_path))
    proj_dir = home / ".claude" / "projects" / proj_hash
    proj_dir.mkdir(parents=True)

    test_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    expected = _build_fixture_jsonl(jsonl_path)

    return {
        "home": home,
        "proj_hash": proj_hash,
        "proj_dir": proj_dir,
        "jsonl_path": jsonl_path,
        "uuid": test_uuid,
        "expected": expected,
    }


# ---------------------------------------------------------------------------
# T-08: parse_session_today tests
# ---------------------------------------------------------------------------

def test_parse_session_today_excludes_yesterday(sm, fixture_home):
    """today_usd excludes the yesterday row; equals hand-computed sum of today rows."""
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(fixture_home["jsonl_path"], day_start, day_end)

    expected_usd = fixture_home["expected"]["expected_today_usd"]
    actual_usd = sum(result["per_model_cost"].values())
    assert abs(actual_usd - expected_usd) < 1e-9, (
        f"today_usd mismatch: got {actual_usd}, expected {expected_usd}"
    )


def test_parse_session_today_skipped_no_ts(sm, fixture_home):
    """Row with message.usage but no timestamp is excluded; skipped_no_ts >= 1."""
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(fixture_home["jsonl_path"], day_start, day_end)
    assert result["skipped_no_ts"] >= 1, (
        f"Expected skipped_no_ts >= 1, got: {result['skipped_no_ts']}"
    )


def test_parse_session_today_malformed_line_skipped(sm, tmp_path):
    """Malformed JSONL line is skipped (no crash), matching cost_from_jsonl behavior."""
    p = tmp_path / "malformed.jsonl"
    p.write_text("not-json\n" + _make_row("claude-opus-4-7", 100, 50, _ts_today()) + "\n")
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(p, day_start, day_end)
    # No crash, at least the valid line is counted
    assert result["per_model_cost"].get("claude-opus-4-7", 0) > 0


def test_parse_session_today_empty_file(sm, tmp_path):
    """Empty/missing JSONL → today_usd == 0, no crash."""
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(p, day_start, day_end)
    assert sum(result["per_model_cost"].values()) == 0.0


# ---------------------------------------------------------------------------
# IVG-249 T-09: parse_session_today's additive unknown_models/priceable keys
# ---------------------------------------------------------------------------

def test_parse_session_today_returns_new_keys_priced_day(sm, fixture_home):
    """A priced (all-known-model) fixture → unknown_models==[], priceable True."""
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(fixture_home["jsonl_path"], day_start, day_end)
    assert "unknown_models" in result and "priceable" in result
    assert result["unknown_models"] == [], f"got {result['unknown_models']}"
    assert result["priceable"] is True


def test_parse_session_today_unknown_model_not_priceable(sm, tmp_path):
    """An unknown-model row seen in-window → unknown_models non-empty, priceable False."""
    p = tmp_path / "unknown.jsonl"
    p.write_text(_make_row("claude-imaginary-9", 100, 50, _ts_today()) + "\n")
    day_start, day_end = sm._local_day_bounds()
    result = sm.parse_session_today(p, day_start, day_end)
    assert result["unknown_models"] == ["claude-imaginary-9"], f"got {result['unknown_models']}"
    assert result["priceable"] is False


def test_empty_in_window_stays_priceable_no_partial_flag(sm, tmp_path):
    """R2-MAJ-3 regression: a today-dated ledger row whose JSONL exists but has
    zero rows inside the local-day window (only a yesterday row) must NOT set
    by_task_partial — priceable is True for this case (the routine empty-window
    skip), distinct from the all-unknown-model case which DOES set the flag."""
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    proj_hash_str = sm.project_hash(str(project_root))

    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    test_uuid = "22222222-3333-4444-5555-666666666666"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    # Only a YESTERDAY row — parse_session_today's per_model_cost is empty for
    # today's window, but the row itself is a known, priceable model.
    jsonl_path.write_text(_make_row("claude-opus-4-7", 500, 200, _ts_yesterday()) + "\n")

    day_start, day_end = sm._local_day_bounds()
    direct = sm.parse_session_today(jsonl_path, day_start, day_end)
    assert direct["per_model_cost"] == {}, "expected empty in-window result"
    assert direct["priceable"] is True, (
        "empty-in-window result must stay priceable — no unknown model was seen"
    )

    artifacts_dir = project_root / ".workflow_artifacts" / "empty-window-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
        f"{test_uuid} | {today_str} | implement | opus | task | \"empty window\"\n"
    )

    snap = sm.aggregate_today(home=home, project_root=project_root)
    assert "empty-window-task" not in snap.by_task, (
        "priceable-but-zero session must not contribute a $0 by_task entry"
    )
    assert snap.by_task_partial is False, (
        "the routine empty-window skip must NOT set by_task_partial"
    )


def test_ledger_all_unknown_model_sets_by_task_partial(sm, tmp_path):
    """A today-dated ledger row whose JSONL is entirely unknown-model rows
    must set by_task_partial True and contribute NOTHING to by_task (never a
    silent $0) — distinct from the ordinary empty-window skip above."""
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    proj_hash_str = sm.project_hash(str(project_root))

    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    test_uuid = "33333333-4444-5555-6666-777777777777"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    jsonl_path.write_text(_make_row("claude-imaginary-9", 100, 50, _ts_today()) + "\n")

    artifacts_dir = project_root / ".workflow_artifacts" / "unknown-model-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
        f"{test_uuid} | {today_str} | implement | opus | task | \"unknown model\"\n"
    )

    snap = sm.aggregate_today(home=home, project_root=project_root)
    assert "unknown-model-task" not in snap.by_task, (
        f"all-unknown-model session must not contribute to by_task: {snap.by_task}"
    )
    assert snap.by_task_partial is True


def test_ledger_mixed_model_keeps_priced_portion_sets_partial(sm, tmp_path):
    """IVG-249 fix-round F-02: a today-dated ledger row whose JSONL mixes a
    priced (known-model) row with an unknown-model row must KEEP the priced
    portion in by_task/by_phase (never silently drop it) AND set
    by_task_partial True — aligning scan_ledgers_today's legacy branch with
    aggregate_today's today-pane policy, which already keeps the priced
    portion of a mixed session. This is the round-3 critic's pre-declared
    untested mixed-model case (review-1.md F-02)."""
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    proj_hash_str = sm.project_hash(str(project_root))

    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    test_uuid = "44444444-5555-6666-7777-888888888888"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    lines = [
        _make_row("claude-opus-4-7", 100, 50, _ts_today()),
        _make_row("claude-imaginary-9", 200, 100, _ts_today()),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    artifacts_dir = project_root / ".workflow_artifacts" / "mixed-model-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
        f"{test_uuid} | {today_str} | implement | opus | task | \"mixed model\"\n"
    )

    prices = sm.PRICES
    expected_opus_usd = (
        100 * prices["claude-opus-4-7"]["input"]
        + 50 * prices["claude-opus-4-7"]["output"]
    ) / 1_000_000

    snap = sm.aggregate_today(home=home, project_root=project_root)
    assert "mixed-model-task" in snap.by_task, (
        f"mixed-model session's priced portion must NOT be dropped: {snap.by_task}"
    )
    assert abs(snap.by_task["mixed-model-task"] - expected_opus_usd) < 1e-9, (
        f"by_task should hold ONLY the priced (opus) portion, "
        f"got {snap.by_task['mixed-model-task']}, expected {expected_opus_usd}"
    )
    assert snap.by_task_partial is True, (
        "mixed-model session must still set by_task_partial (unknown model present)"
    )


# ---------------------------------------------------------------------------
# T-08: aggregate_today tests
# ---------------------------------------------------------------------------

def test_aggregate_today_today_usd(sm, fixture_home):
    """today_usd matches hand-computed sum for today rows only."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    expected = fixture_home["expected"]["expected_today_usd"]
    assert abs(snap.today_usd - expected) < 1e-9, (
        f"today_usd={snap.today_usd}, expected={expected}"
    )


def test_aggregate_today_by_model_has_all_three(sm, fixture_home):
    """by_model has opus, sonnet, haiku entries."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    assert "opus" in snap.by_model, "opus missing from by_model"
    assert "sonnet" in snap.by_model, "sonnet missing from by_model"
    assert "haiku" in snap.by_model, "haiku missing from by_model"


def test_aggregate_today_by_model_pct_sums_to_100(sm, fixture_home):
    """by_model_pct sums to approximately 100 (tolerance ±2, other bucket absorbs remainder)."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    total_pct = sum(snap.by_model_pct.values())
    assert abs(total_pct - 100) <= 2, (
        f"by_model_pct sum should be ~100 (±2), got {total_pct}: {snap.by_model_pct}"
    )


def test_aggregate_today_empty_home(sm, tmp_path):
    """Empty HOME (no JSONL) → today_usd == 0.0, render contains $0.00."""
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    snap = sm.aggregate_today(home=empty_home)
    assert snap.today_usd == 0.0
    rendered = sm.render_compact(snap)
    assert "$0.00" in rendered


def test_aggregate_today_scope_global_label(sm, fixture_home):
    """--scope global render header contains '(all)'."""
    snap = sm.aggregate_today(home=fixture_home["home"], scope="global")
    rendered = sm.render_compact(snap)
    assert "(all)" in rendered, f"'(all)' not in render: {rendered!r}"


def test_aggregate_today_scope_project_label(sm, fixture_home):
    """--scope project render header contains '(proj)'."""
    snap = sm.aggregate_today(home=fixture_home["home"], scope="project")
    rendered = sm.render_compact(snap)
    assert "(proj)" in rendered, f"'(proj)' not in render: {rendered!r}"


# ---------------------------------------------------------------------------
# IVG-249 T-09 (CRIT-1): SpendSnapshot.today_partial + aggregate_today wiring
# ---------------------------------------------------------------------------

def test_spend_snapshot_today_partial_defaults_false(sm):
    """SpendSnapshot() constructed with no args defaults today_partial=False."""
    snap = sm.SpendSnapshot()
    assert snap.today_partial is False


def test_aggregate_today_priced_day_today_partial_false(sm, fixture_home):
    """An all-known-model fixture day → today_partial stays False (unchanged)."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    assert snap.today_partial is False


def test_aggregate_today_sets_today_partial_true_on_unpriced_contribution(sm, tmp_path):
    """A today-file containing an unknown-model row → today_partial True."""
    home = tmp_path / "home"
    proj_hash_str = sm.project_hash(str(tmp_path))
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / "unknown-today.jsonl"
    jsonl_path.write_text(_make_row("claude-imaginary-9", 100, 50, _ts_today()) + "\n")

    snap = sm.aggregate_today(home=home)
    assert snap.today_partial is True


# ---------------------------------------------------------------------------
# T-08: render_compact tests
# ---------------------------------------------------------------------------

def test_render_compact_contains_token_spend(sm, fixture_home):
    """render_compact output contains 'TOKEN SPEND'."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    rendered = sm.render_compact(snap)
    assert "TOKEN SPEND" in rendered, f"'TOKEN SPEND' not in render:\n{rendered}"


def test_render_compact_line_width(sm, fixture_home):
    """All render lines are <= width."""
    width = 38
    snap = sm.aggregate_today(home=fixture_home["home"])
    rendered = sm.render_compact(snap, width=width)
    for line in rendered.splitlines():
        assert len(line) <= width, (
            f"Line exceeds width={width}: {line!r} (len={len(line)})"
        )


def test_render_compact_footer_present(sm, fixture_home):
    """Render footer contains '⟳' and 'live' or 'once' marker."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    rendered_live = sm.render_compact(snap, live=True)
    assert "⟳" in rendered_live, f"Footer missing ⟳: {rendered_live!r}"
    assert "live" in rendered_live, f"Footer missing 'live': {rendered_live!r}"

    rendered_once = sm.render_compact(snap, live=False)
    assert "once" in rendered_once, f"Footer missing 'once': {rendered_once!r}"


def test_render_compact_per_model_pct(sm, fixture_home):
    """Render contains per-model lines with %."""
    snap = sm.aggregate_today(home=fixture_home["home"])
    rendered = sm.render_compact(snap)
    assert "%" in rendered, f"No % in render:\n{rendered}"


# ---------------------------------------------------------------------------
# IVG-249 T-09 (CRIT-1 / R2-MAJ-2): render_compact today-line partial suffix
# ---------------------------------------------------------------------------

def test_render_compact_today_partial_suffix_present(sm):
    """today_partial=True → today line carries the '(partial)' suffix, and
    every rendered line still fits the width=38 truncation contract."""
    snap = sm.SpendSnapshot(today_usd=4.21, today_partial=True)
    rendered = sm.render_compact(snap, width=38)
    lines = rendered.splitlines()
    today_line = next(ln for ln in lines if ln.strip().startswith("today"))
    assert "today (partial)" in today_line, f"got: {today_line!r}"
    for line in lines:
        assert len(line) <= 38, f"Line exceeds width=38: {line!r}"


def test_render_compact_today_partial_suffix_absent_when_false(sm):
    """today_partial=False → today line has the plain 'today' label, no suffix."""
    snap = sm.SpendSnapshot(today_usd=4.21, today_partial=False)
    rendered = sm.render_compact(snap, width=38)
    lines = rendered.splitlines()
    today_line = next(ln for ln in lines if ln.strip().startswith("today"))
    assert "(partial)" not in today_line, f"got: {today_line!r}"


# ---------------------------------------------------------------------------
# T-08: by-task tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_with_ledger(tmp_path: Path):
    """Fixture: fake HOME + a project_root with a cost-ledger.md that has a today-dated UUID row."""
    sm = _load_spend_monitor()

    home = tmp_path / "home"
    project_root = tmp_path / "project"
    proj_hash_str = sm.project_hash(str(project_root))

    # Set up JSONL
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    test_uuid = "11111111-2222-3333-4444-555555555555"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    # Simple: one opus row today
    jsonl_path.write_text(
        _make_row("claude-opus-4-7", 100, 50, _ts_today()) + "\n"
    )

    # Set up cost-ledger.md under .workflow_artifacts/test-task/
    artifacts_dir = project_root / ".workflow_artifacts" / "test-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
        f"{test_uuid} | {today_str} | implement | sonnet | task | \"test\"\n"
    )

    return {
        "home": home,
        "project_root": project_root,
        "proj_hash": proj_hash_str,
        "test_uuid": test_uuid,
        "jsonl_path": jsonl_path,
    }


def test_by_task_populated(sm, fixture_with_ledger):
    """Fixture ledger + JSONL → by_task populated with positive USD; by_task_partial False."""
    snap = sm.aggregate_today(
        home=fixture_with_ledger["home"],
        project_root=fixture_with_ledger["project_root"],
    )
    assert snap.by_task, f"by_task should be populated: {snap.by_task}"
    assert "test-task" in snap.by_task, f"'test-task' not in by_task: {snap.by_task}"
    assert snap.by_task["test-task"] > 0, f"test-task USD should be > 0: {snap.by_task}"
    assert not snap.by_task_partial, "by_task_partial should be False"


def test_by_task_usd_matches_parse_session_today(sm, fixture_with_ledger):
    """by_task USD for resolved UUID equals parse_session_today sum for that JSONL."""
    day_start, day_end = sm._local_day_bounds()
    direct_result = sm.parse_session_today(
        fixture_with_ledger["jsonl_path"], day_start, day_end
    )
    direct_usd = sum(direct_result["per_model_cost"].values())

    snap = sm.aggregate_today(
        home=fixture_with_ledger["home"],
        project_root=fixture_with_ledger["project_root"],
    )
    snap_usd = snap.by_task.get("test-task", 0.0)
    assert abs(snap_usd - direct_usd) < 1e-9, (
        f"by_task USD {snap_usd} != parse_session_today USD {direct_usd}"
    )


def test_by_task_na_uuid(sm, tmp_path):
    """Ledger with only 'na' UUID rows → by_task == {}, by_task_partial True, header still populated."""
    home = tmp_path / "home"
    home.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    # JSONL with spend
    proj_hash_str = sm.project_hash(str(project_root))
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / "real-uuid.jsonl"
    jsonl.write_text(_make_row("claude-opus-4-7", 100, 50, _ts_today()) + "\n")

    artifacts_dir = project_root / ".workflow_artifacts" / "na-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
        f"na | {today_str} | implement | haiku | task | \"na test\"\n"
    )

    snap = sm.aggregate_today(home=home, project_root=project_root)
    assert snap.by_task == {}, f"by_task should be empty with na-UUID: {snap.by_task}"
    assert snap.by_task_partial is True, "by_task_partial should be True"
    # Header still populated (global spend from JSONL)
    assert snap.today_usd > 0, "today_usd should still be populated from JSONL"


def test_no_task_flag_suppresses_block(sm, fixture_with_ledger):
    """--no-task (show_task=False) → by-task block absent from render."""
    snap = sm.aggregate_today(
        home=fixture_with_ledger["home"],
        project_root=fixture_with_ledger["project_root"],
    )
    rendered = sm.render_compact(snap, show_task=False)
    assert "by task" not in rendered, (
        f"by-task block should be absent with show_task=False:\n{rendered}"
    )


def test_no_project_root_by_task_empty(sm, fixture_home):
    """Monitor run with no resolvable project_root → by_task == {}, header still populated."""
    snap = sm.aggregate_today(home=fixture_home["home"], project_root=None)
    assert snap.by_task == {}, f"by_task should be empty with no project_root: {snap.by_task}"
    assert snap.today_usd >= 0, "today_usd should still be populated"


# ---------------------------------------------------------------------------
# T-08: memo cache tests (T-02)
# ---------------------------------------------------------------------------

def test_memo_cache_no_reparse(sm, fixture_home):
    """Second aggregate_today with same cache and unchanged files does not re-parse."""
    parse_count = [0]
    original_parse = sm.parse_session_today

    def counting_parse(path, start, end):
        parse_count[0] += 1
        return original_parse(path, start, end)

    # Patch parse_session_today
    sm.parse_session_today = counting_parse
    try:
        cache: dict = {}
        snap1 = sm.aggregate_today(home=fixture_home["home"], cache=cache)
        count_after_first = parse_count[0]

        # Second call with same cache — files unchanged, no re-parse
        snap2 = sm.aggregate_today(home=fixture_home["home"], cache=cache)
        count_after_second = parse_count[0]

        assert count_after_second == count_after_first, (
            f"Second call with unchanged files should not re-parse. "
            f"Parses after first: {count_after_first}, after second: {count_after_second}"
        )
        assert abs(snap1.today_usd - snap2.today_usd) < 1e-9
    finally:
        sm.parse_session_today = original_parse


# ---------------------------------------------------------------------------
# T-05: by_phase breakdown tests (D-04)
# ---------------------------------------------------------------------------

def test_by_phase_populated(sm, fixture_with_ledger):
    """by_phase populated from ledger PHASE column; phase key matches ledger value."""
    snap = sm.aggregate_today(
        home=fixture_with_ledger["home"],
        project_root=fixture_with_ledger["project_root"],
    )
    assert snap.by_phase, f"by_phase should be non-empty: {snap.by_phase}"
    assert "implement" in snap.by_phase, (
        f"'implement' not in by_phase — expected phase from fixture ledger: {snap.by_phase}"
    )
    assert snap.by_phase["implement"] > 0, (
        f"by_phase['implement'] should be > 0: {snap.by_phase}"
    )


def test_by_phase_uuid_dedup(sm, tmp_path):
    """Same UUID referenced by two ledger tasks → by_phase counts it exactly once."""
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    proj_hash_str = sm.project_hash(str(project_root))

    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    test_uuid = "aaaa1111-bbbb-2222-cccc-3333dddd4444"
    jsonl_path = proj_dir / f"{test_uuid}.jsonl"
    jsonl_path.write_text(
        _make_row("claude-opus-4-7", 100, 50, _ts_today()) + "\n"
    )

    day_start, day_end = sm._local_day_bounds()
    direct = sm.parse_session_today(jsonl_path, day_start, day_end)
    direct_usd = sum(direct["per_model_cost"].values())
    assert direct_usd > 0, "Fixture JSONL must produce positive USD"

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Two tasks referencing the same UUID with different phases
    for task_name in ("task-a", "task-b"):
        d = project_root / ".workflow_artifacts" / task_name
        d.mkdir(parents=True)
        phase = "implement" if task_name == "task-a" else "review"
        (d / "cost-ledger.md").write_text(
            f"# Cost Ledger\nUUID | DATE | PHASE | MODEL | task | NOTE\n"
            f"{test_uuid} | {today_str} | {phase} | sonnet | task | \"dedup test\"\n"
        )

    snap = sm.aggregate_today(home=home, project_root=project_root)

    # Total by_phase sum must equal direct_usd (counted once, not twice)
    phase_total = sum(snap.by_phase.values())
    assert abs(phase_total - direct_usd) < 1e-9, (
        f"UUID dedup failed: by_phase sum {phase_total} != direct_usd {direct_usd} "
        f"(UUID counted {phase_total / direct_usd:.1f}x). by_phase={snap.by_phase}"
    )


def test_scan_ledgers_today_early_return_3tuple(sm, tmp_path):
    """scan_ledgers_today returns a 3-tuple ({}, {}, False) when .workflow_artifacts is absent."""
    project_root = tmp_path / "no-artifacts"
    project_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    day_start, day_end = sm._local_day_bounds()
    result = sm.scan_ledgers_today(project_root, day_start, day_end, home=home)

    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 3, f"Expected 3-tuple, got {len(result)}-tuple: {result}"
    by_task, by_phase, partial = result
    assert by_task == {}, f"by_task should be empty: {by_task}"
    assert by_phase == {}, f"by_phase should be empty: {by_phase}"
    assert partial is False, f"partial should be False: {partial}"


def test_snapshot_has_by_phase_fields(sm):
    """SpendSnapshot has by_phase and by_phase_partial fields with correct defaults."""
    snap = sm.SpendSnapshot()
    assert hasattr(snap, "by_phase"), "SpendSnapshot missing by_phase field"
    assert hasattr(snap, "by_phase_partial"), "SpendSnapshot missing by_phase_partial field"
    assert snap.by_phase == {}, f"Default by_phase should be {{}}: {snap.by_phase}"
    assert snap.by_phase_partial is False, (
        f"Default by_phase_partial should be False: {snap.by_phase_partial}"
    )

def test_memo_cache_forces_reparse_on_mtime_change(sm, fixture_home):
    """Touching a fixture file's mtime between calls forces a re-parse of only that file."""
    parse_count = [0]
    original_parse = sm.parse_session_today

    def counting_parse(path, start, end):
        parse_count[0] += 1
        return original_parse(path, start, end)

    sm.parse_session_today = counting_parse
    try:
        cache: dict = {}
        sm.aggregate_today(home=fixture_home["home"], cache=cache)
        count_after_first = parse_count[0]

        # Touch the file to change mtime
        jsonl_path = fixture_home["jsonl_path"]
        jsonl_path.touch()

        sm.aggregate_today(home=fixture_home["home"], cache=cache)
        count_after_second = parse_count[0]

        assert count_after_second > count_after_first, (
            f"After mtime change, file should be re-parsed. "
            f"Before: {count_after_first}, after: {count_after_second}"
        )
    finally:
        sm.parse_session_today = original_parse


# ---------------------------------------------------------------------------
# T-08: --watch/--interval precedence tests
# ---------------------------------------------------------------------------

def test_watch_interval_precedence(fixture_home):
    """--watch 5 --interval 2 → effective_interval is 2 (--interval wins, D-03)."""
    # Replicate the resolution logic from main()
    argv = ["--watch", "5", "--interval", "2", "--once", "--json", "--home", str(fixture_home["home"])]
    parser = _build_parser()
    args = parser.parse_args(argv)
    effective_interval = (
        args.interval if args.interval is not None
        else (args.watch if args.watch is not None else 3)
    )
    assert effective_interval == 2, (
        f"--watch 5 --interval 2 → effective_interval should be 2, got {effective_interval}"
    )


def _build_parser():
    """Replicate the argparse parser from spend_monitor.main() for testing."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", nargs="?", const=3, type=int)
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--scope", choices=["global", "project"], default="global")
    parser.add_argument("--width", type=int, default=38)
    parser.add_argument("--no-task", action="store_true")
    parser.add_argument("--task-limit", type=int, default=3)
    parser.add_argument("--home", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


# ---------------------------------------------------------------------------
# T-08: main() integration tests
# ---------------------------------------------------------------------------

def test_main_once_json_output(sm, fixture_home):
    """main(["--once", "--json", "--home", ...]) emits valid JSON with expected keys."""
    argv = ["--once", "--json", "--home", str(fixture_home["home"])]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sm.main(argv)
    assert rc == 0, f"main() returned non-zero: {rc}"
    data = json.loads(buf.getvalue())
    assert "today_usd" in data
    assert "by_model" in data
    assert "by_model_pct" in data
    assert "scope" in data


def test_main_once_json_output_includes_today_partial(sm, fixture_home):
    """IVG-249 T-09 (CRIT-1c): --json payload includes 'today_partial' matching
    the snapshot — extends the existing 'today_usd' in data pin above."""
    argv = ["--once", "--json", "--home", str(fixture_home["home"])]
    import io
    from contextlib import redirect_stdout

    snap = sm.aggregate_today(home=fixture_home["home"])

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sm.main(argv)
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert "today_partial" in data
    assert data["today_partial"] == snap.today_partial


def test_main_once_compact_output(sm, fixture_home):
    """main with fixture JSONL → output contains TOKEN SPEND, today line, % per model."""
    argv = ["--once", "--compact", "--home", str(fixture_home["home"])]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sm.main(argv)
    assert rc == 0
    out = buf.getvalue()
    assert "TOKEN SPEND" in out, f"'TOKEN SPEND' not in output:\n{out}"
    assert "today" in out, f"'today' not in output:\n{out}"
    assert "%" in out, f"No % in output:\n{out}"


def test_main_empty_home(sm, tmp_path):
    """Empty HOME → main exits 0, output contains $0.00."""
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    argv = ["--once", "--home", str(empty_home)]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sm.main(argv)
    assert rc == 0
    assert "$0.00" in buf.getvalue()


# ---------------------------------------------------------------------------
# T-08 (stage 4): scan_ledgers_today inline-first precedence
# ---------------------------------------------------------------------------

def test_scan_ledgers_resolved_col8_bypasses_jsonl_with_empty_home(sm, tmp_path):
    """A resolved col-8 row must contribute its inline usd even when the JSONL
    tree is completely empty — proving no JSONL lookup occurred (R-01/R-04)."""
    project_root = tmp_path / "project"
    home = tmp_path / "empty-home"  # deliberately no .claude/projects/ tree
    artifacts_dir = project_root / ".workflow_artifacts" / "inline-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\n"
        f"onbehalf-uuid | {today_str} | implement | opus | task | \"resolved\" | 0 | "
        f"usd=2.5;tok=1000;src=nested_jsonl\n"
    )

    day_start, day_end = sm._local_day_bounds()
    by_task, by_phase, by_task_partial = sm.scan_ledgers_today(project_root, day_start, day_end, home=home)

    assert by_task.get("inline-task") == pytest.approx(2.5)
    assert by_phase.get("implement") == pytest.approx(2.5)
    assert by_task_partial is False


def test_scan_ledgers_unresolved_col8_sets_partial_never_adds_zero(sm, tmp_path):
    """An unresolvable col-8 row must set by_task_partial=True and add NOTHING
    (never a $0 contribution) — D-3's never-silent-$0 discipline."""
    project_root = tmp_path / "project"
    home = tmp_path / "empty-home"
    artifacts_dir = project_root / ".workflow_artifacts" / "unresolvable-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\n"
        f"na-jsonl-uuid | {today_str} | implement | opus | task | \"unresolved\" | 0 | "
        f"tok=45;src=unresolved\n"
    )

    day_start, day_end = sm._local_day_bounds()
    by_task, by_phase, by_task_partial = sm.scan_ledgers_today(project_root, day_start, day_end, home=home)

    assert "unresolvable-task" not in by_task
    assert by_task_partial is True


def test_scan_ledgers_load_core_fail_open_degrades_to_legacy(sm, tmp_path, monkeypatch):
    """When classify_attribution fails to load (simulated via monkeypatch),
    every row must be treated as legacy — inline-first silently disabled,
    never a crash (R-06)."""
    monkeypatch.setattr(sm, "classify_attribution", None)

    project_root = tmp_path / "project"
    home = tmp_path / "empty-home"  # no JSONL — legacy resolution will fail
    artifacts_dir = project_root / ".workflow_artifacts" / "failopen-task"
    artifacts_dir.mkdir(parents=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    ledger = artifacts_dir / "cost-ledger.md"
    ledger.write_text(
        f"# Cost Ledger\n"
        f"some-uuid | {today_str} | implement | opus | task | \"col8 ignored\" | 0 | "
        f"usd=2.5;tok=1000;src=nested_jsonl\n"
    )

    day_start, day_end = sm._local_day_bounds()
    # Must not raise even though classify_attribution is None.
    by_task, by_phase, by_task_partial = sm.scan_ledgers_today(project_root, day_start, day_end, home=home)

    # Fail-open means the col-8 usd is IGNORED (treated as legacy); since no
    # JSONL exists for this UUID, it degrades to unresolved/partial, NOT the
    # inline $2.5 — proving inline-first precedence was actually bypassed.
    assert "failopen-task" not in by_task
    assert by_task_partial is True


def test_prices_single_source(sm):
    """PRICES is the only pricing dict in spend_monitor — no second price dict defined."""
    import inspect
    source = inspect.getsource(sm)
    # The only PRICES = {...} should be the one imported from cost_from_jsonl
    # (the assignment in spend_monitor.py is just 'PRICES = _cfj.PRICES', not a new dict)
    # Count dict literals that look like price tables: input/output keys with float values
    import re
    # A new price dict would look like: {"input": N.N, "output": N.N, ...}
    # The only place this should appear is in cost_from_jsonl, not here
    price_dict_re = re.compile(r'"input"\s*:\s*\d+\.\d+.*?"output"\s*:\s*\d+\.\d+', re.DOTALL)
    matches = price_dict_re.findall(source)
    assert len(matches) == 0, (
        f"spend_monitor.py must not define its own price table (found {len(matches)} matches). "
        "Use PRICES = _cfj.PRICES only."
    )


# ---------------------------------------------------------------------------
# MODEL_TIERS tier-table unification (fable tier)
# ---------------------------------------------------------------------------

def test_fable_bucket_present_in_by_model(sm, tmp_path):
    """A today-file with only a fable row classifies to 'fable', not 'other'."""
    home = tmp_path / "home"
    proj_hash_str = sm.project_hash(str(tmp_path))
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / "fable-today.jsonl"
    jsonl_path.write_text(_make_row("claude-fable-5-1", 1000, 500, _ts_today()) + "\n")

    snap = sm.aggregate_today(home=home)
    assert "fable" in snap.by_model, f"fable missing from by_model: {snap.by_model}"
    assert snap.by_model["fable"] > 0
    assert "other" not in snap.by_model, f"fable spend leaked into other: {snap.by_model}"


def test_fable_and_opus_pct_tolerance(sm, tmp_path):
    """fable + opus rows both classify correctly and by_model_pct still sums ~100."""
    home = tmp_path / "home"
    proj_hash_str = sm.project_hash(str(tmp_path))
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / "fable-opus-today.jsonl"
    jsonl_path.write_text(
        _make_row("claude-fable-5-1", 1000, 500, _ts_today()) + "\n"
        + _make_row("claude-opus-5", 1000, 500, _ts_today()) + "\n"
    )

    snap = sm.aggregate_today(home=home)
    assert set(snap.by_model) == {"fable", "opus"}, f"got {snap.by_model}"
    total_pct = sum(snap.by_model_pct.values())
    assert abs(total_pct - 100) <= 2, (
        f"by_model_pct sum should be ~100 (±2), got {total_pct}: {snap.by_model_pct}"
    )


def test_render_compact_fable_row_ordering(sm, tmp_path):
    """render_compact places the fable row before opus, and every line fits width=38."""
    home = tmp_path / "home"
    proj_hash_str = sm.project_hash(str(tmp_path))
    proj_dir = home / ".claude" / "projects" / proj_hash_str
    proj_dir.mkdir(parents=True)
    jsonl_path = proj_dir / "fable-opus-today.jsonl"
    jsonl_path.write_text(
        _make_row("claude-fable-5-1", 1000, 500, _ts_today()) + "\n"
        + _make_row("claude-opus-5", 1000, 500, _ts_today()) + "\n"
    )

    width = 38
    snap = sm.aggregate_today(home=home)
    rendered = sm.render_compact(snap, width=width)
    assert "fable" in rendered, f"'fable' not in render:\n{rendered}"
    assert rendered.index("fable") < rendered.index("opus"), (
        f"fable should render before opus:\n{rendered}"
    )
    for line in rendered.splitlines():
        assert len(line) <= width, f"Line exceeds width={width}: {line!r} (len={len(line)})"


def test_short_model_other_stays_terminal(sm):
    """Unknown slugs classify to 'other', and 'other' is the last entry in _model_order()."""
    assert sm._short_model("deepseek/deepseek-v4-pro") == "other"
    assert sm._short_model("claude-mythos-5-1") == "other"
    assert sm._short_model("") == "other"
    assert sm._model_order()[-1] == "other"


def test_model_tiers_single_source_for_mapping_and_ordering(sm, monkeypatch):
    """MODEL_TIERS is the single source for both classification and display order."""
    # Without monkeypatch: _model_order() is exactly MODEL_TIERS's tiers, plus
    # OTHER_TIER appended, with no duplicate tier strings.
    order = sm._model_order()
    assert order == [tier for _sub, tier in sm.MODEL_TIERS] + [sm.OTHER_TIER]
    assert len(order) == len(set(order)), f"duplicate tier in _model_order(): {order}"

    # With monkeypatch: adding a synthetic row changes BOTH classification and
    # ordering from the same edit — proof the two derive from one constant.
    monkeypatch.setattr(sm, "MODEL_TIERS", sm.MODEL_TIERS + (("zzsynthetic", "zztier"),))
    assert sm._short_model("model-zzsynthetic-1") == "zztier"
    assert sm._model_order() == ["fable", "opus", "sonnet", "haiku", "zztier", "other"]


def test_other_is_not_a_model_tiers_row(sm):
    """'other' must stay a fallthrough, never a row of MODEL_TIERS.

    A substring row would only match IDs containing the literal text 'other',
    so unknown slugs would stop classifying.
    """
    assert "other" not in [sub for sub, _tier in sm.MODEL_TIERS]
    assert "other" not in [tier for _sub, tier in sm.MODEL_TIERS]
