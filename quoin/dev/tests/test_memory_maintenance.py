"""IVG-50 S-3 tests for memory-maintenance pattern config and consumers.

Tests:
  Tier 1 (CI-safe):
    - Both new memory files exist in source and in TIER1_MEMORY_FILES
    - Source files contain no literal ~/.claude/ and yaml has no ${CLAUDE_PLUGIN_ROOT}

  Pattern behavior (tmp_path, importlib-load core):
    - archived orphan suppressed; without patterns it is an error (regression guard)
    - archived dangling link suppressed
    - read_only orphan suppressed and appears in result["read_only"]
    - read_only dangling link still an error (read_only does NOT suppress dangling)
    - ignore-matched fact-file absent from orphans entirely
    - load_patterns(None) -> all-empty; missing file is not an error
    - result["forward"] == [] still holds with patterns (MAJ-4 guard)
    - three S-2 "forward in data" JSON tests still green
    - hand-parser correctness: assert load_patterns(yaml) == expected inline dict;
      optional PyYAML cross-check (skipped if absent — MIN-3)
    - wrapper re-exports: load_patterns/classify/DEFAULT_PATTERN_FILE same objects

  /sleep filter (tmp_path, importlib-load sleep_score):
    - read_only-source entry never in soft-forget bucket (demoted to middle)
    - ignore-source produces zero entries
    - negative control: demotion fails if protected flag is cleared (lesson 2026-06-15)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from textwrap import dedent

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]   # quoin/ repo root
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"
MEMORY_DIR = REPO_ROOT / "quoin" / "memory"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "memory_check.py"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "memory_check.py"
SLEEP_SCORE_SRC = REPO_ROOT / "quoin" / "scripts" / "sleep_score.py"

SHIPPED_YAML = MEMORY_DIR / "memory-maintenance.yaml"
SHIPPED_MD = MEMORY_DIR / "memory-maintenance.md"


# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------

def _load_installer():
    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_core():
    spec = importlib.util.spec_from_file_location("_mc_core_s3", CORE_IMPL_SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("_mc_wrapper_s3", WRAPPER_SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_sleep_score():
    spec = importlib.util.spec_from_file_location("_sleep_score_s3", SLEEP_SCORE_SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MC = _load_core()
WRAPPER = _load_wrapper()
SS = _load_sleep_score()


# ---------------------------------------------------------------------------
# Tier 1: installer membership + source file existence + token guards
# ---------------------------------------------------------------------------


def test_source_md_exists():
    """memory-maintenance.md must exist in quoin/memory/."""
    assert SHIPPED_MD.is_file(), f"Source file missing: {SHIPPED_MD}"


def test_source_yaml_exists():
    """memory-maintenance.yaml must exist in quoin/memory/."""
    assert SHIPPED_YAML.is_file(), f"Source file missing: {SHIPPED_YAML}"


def test_md_no_literal_claude_home():
    """memory-maintenance.md must not contain a literal ~/.claude/ path."""
    text = SHIPPED_MD.read_text(encoding="utf-8")
    assert "~/.claude/" not in text, (
        "memory-maintenance.md contains a literal '~/.claude/' path. "
        "Use ${CLAUDE_PLUGIN_ROOT} for deploy paths (rule T-07/b)."
    )


def test_yaml_no_quoin_home_token():
    """memory-maintenance.yaml must not contain ${CLAUDE_PLUGIN_ROOT} (T-01 acceptance)."""
    text = SHIPPED_YAML.read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}" not in text, (
        "memory-maintenance.yaml contains a ${CLAUDE_PLUGIN_ROOT} token. "
        "The YAML is glob-only and must be token-free."
    )


# ---------------------------------------------------------------------------
# Helper: build memory dir fixture
# ---------------------------------------------------------------------------

def _build_memory_dir(root: Path, links: list[str], files: list[str]) -> Path:
    """Populate *root* as a memory dir with MEMORY.md and sibling fact-files."""
    lines = []
    for fname in links:
        title = fname.replace(".md", "").replace("_", " ").title()
        lines.append(f"- [{title}]({fname}) — test fixture")
    (root / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for fname in files:
        (root / fname).write_text(f"# {fname}\nTest fixture.\n", encoding="utf-8")
    return root


def _write_pattern_file(path: Path, archived: list[str], read_only: list[str],
                        ignore: list[str]) -> Path:
    """Write a memory-maintenance.yaml fixture to *path*."""
    lines = ["version: 1"]
    if archived:
        lines.append("archived:")
        for g in archived:
            lines.append(f'  - "{g}"')
    if read_only:
        lines.append("read_only:")
        for g in read_only:
            lines.append(f'  - "{g}"')
    if ignore:
        lines.append("ignore:")
        for g in ignore:
            lines.append(f'  - "{g}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_patterns() / classify() tests
# ---------------------------------------------------------------------------

def test_load_patterns_none_returns_empty():
    """load_patterns(None) must return all-empty lists, never raise."""
    result = MC.load_patterns(None)
    assert result == {"archived": [], "read_only": [], "ignore": []}, (
        f"Expected all-empty, got {result}"
    )


def test_load_patterns_missing_file_returns_empty(tmp_path):
    """load_patterns on a missing file must return all-empty (advisory only)."""
    missing = tmp_path / "nonexistent.yaml"
    result = MC.load_patterns(missing)
    assert result == {"archived": [], "read_only": [], "ignore": []}


def test_load_patterns_ships_yaml():
    """load_patterns on the shipped yaml must round-trip to the documented dict."""
    result = MC.load_patterns(SHIPPED_YAML)
    expected = {
        "archived": ["archive_*.md", "deprecated_*.md"],
        "read_only": ["pinned_*.md", "feedback_*.md"],
        "ignore": ["*.draft.md"],
    }
    assert result == expected, (
        f"Shipped YAML did not round-trip to expected dict.\n"
        f"  Got:      {result}\n"
        f"  Expected: {expected}"
    )


def test_load_patterns_pyyaml_crosscheck():
    """Optional: cross-check hand-parser against yaml.safe_load (skipped if absent)."""
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        pytest.skip("pyyaml not installed — skipping cross-check (MIN-3)")

    result_hand = MC.load_patterns(SHIPPED_YAML)
    raw = yaml.safe_load(SHIPPED_YAML.read_text(encoding="utf-8"))
    expected = {
        "archived": raw.get("archived", []),
        "read_only": raw.get("read_only", []),
        "ignore": raw.get("ignore", []),
    }
    assert result_hand == expected, (
        f"Hand-parser diverged from yaml.safe_load:\n"
        f"  Hand:     {result_hand}\n"
        f"  PyYAML:   {expected}"
    )


def test_classify_precedence(tmp_path):
    """classify() must respect precedence: ignore > archived > read_only > active."""
    yaml_file = _write_pattern_file(
        tmp_path / "patterns.yaml",
        archived=["arch_*.md"],
        read_only=["ro_*.md"],
        ignore=["*.draft.md"],
    )
    patterns = MC.load_patterns(yaml_file)

    assert MC.classify("something.draft.md", patterns) == "ignore"
    assert MC.classify("arch_old.md", patterns) == "archived"
    assert MC.classify("ro_pinned.md", patterns) == "read_only"
    assert MC.classify("regular_fact.md", patterns) == "active"


# ---------------------------------------------------------------------------
# check() pattern behavior
# ---------------------------------------------------------------------------

def test_archived_orphan_suppressed(tmp_path):
    """archived file: orphan suppressed (ok=True). Without patterns: ok=False."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # MEMORY.md links: only active_a.md; archive_old.md is on disk but not linked
    _build_memory_dir(mem, links=["active_a.md"], files=["active_a.md", "archive_old.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=["archive_*.md"], read_only=[], ignore=[])

    # With patterns: archive_old.md is suppressed from orphans → ok
    result_with = MC.check(mem, pattern_file=pf)
    assert result_with["ok"] is True, (
        f"archived orphan should not flip ok. result={result_with}"
    )
    assert "archive_old.md" in result_with["archived"]
    assert "archive_old.md" not in result_with["orphans"]

    # Without patterns (regression guard): archive_old.md IS an orphan → not ok
    result_without = MC.check(mem)
    assert result_without["ok"] is False, (
        "Without patterns, an unlinked archived file should be an orphan error. "
        "Suppression must be load-bearing."
    )
    assert "archive_old.md" in result_without["orphans"]


def test_archived_dangling_suppressed(tmp_path):
    """archived link target missing: dangling suppressed (ok=True)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # MEMORY.md links archive_gone.md, but it doesn't exist on disk
    _build_memory_dir(mem, links=["active_a.md", "archive_gone.md"],
                      files=["active_a.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=["archive_*.md"], read_only=[], ignore=[])

    result = MC.check(mem, pattern_file=pf)
    assert result["ok"] is True, (
        f"archived dangling link should not flip ok. result={result}"
    )
    assert "archive_gone.md" not in result["dangling"]


def test_read_only_orphan_suppressed(tmp_path):
    """read_only file: orphan suppressed and appears in result['read_only'] (CRIT-1)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # feedback_tool.md is on disk but not linked
    _build_memory_dir(mem, links=["active_a.md"], files=["active_a.md", "feedback_tool.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=["feedback_*.md"], ignore=[])

    result = MC.check(mem, pattern_file=pf)
    assert result["ok"] is True, (
        f"read_only orphan should not flip ok. result={result}"
    )
    assert "feedback_tool.md" in result["read_only"]
    assert "feedback_tool.md" not in result["orphans"]


def test_read_only_orphan_negative_control(tmp_path):
    """Without patterns, read_only file is still an orphan error (suppression load-bearing)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _build_memory_dir(mem, links=["active_a.md"], files=["active_a.md", "feedback_tool.md"])

    result = MC.check(mem)
    assert result["ok"] is False, (
        "Without patterns, feedback_tool.md should be an orphan error. "
        "read_only suppression must be load-bearing."
    )
    assert "feedback_tool.md" in result["orphans"]


def test_read_only_dangling_still_error(tmp_path):
    """read_only does NOT suppress dangling errors — only archived does (D-05)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # MEMORY.md links feedback_gone.md, but it doesn't exist on disk
    _build_memory_dir(mem, links=["active_a.md", "feedback_gone.md"],
                      files=["active_a.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=["feedback_*.md"], ignore=[])

    result = MC.check(mem, pattern_file=pf)
    assert result["ok"] is False, (
        "read_only should NOT suppress dangling links — that is archived's job."
    )
    assert "feedback_gone.md" in result["dangling"]


def test_ignore_fact_file_absent(tmp_path):
    """ignore-matched file absent from orphans, result keys, and dangling entirely."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # draft_notes.draft.md is on disk; not linked
    _build_memory_dir(mem, links=["active_a.md"],
                      files=["active_a.md", "notes.draft.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=[], ignore=["*.draft.md"])

    result = MC.check(mem, pattern_file=pf)
    assert result["ok"] is True
    assert "notes.draft.md" not in result["orphans"]
    assert "notes.draft.md" not in result.get("archived", [])
    assert "notes.draft.md" not in result.get("read_only", [])


def test_forward_key_still_empty_with_patterns(tmp_path):
    """result['forward'] == [] still holds with patterns supplied (MAJ-4 guard)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _build_memory_dir(mem, links=["a.md"], files=["a.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=["arch_*.md"], read_only=[], ignore=[])

    result = MC.check(mem, pattern_file=pf)
    assert "forward" in result
    assert result["forward"] == [], f"Expected forward==[], got {result['forward']}"


def test_json_output_includes_new_keys(tmp_path):
    """--json output must include 'archived' and 'read_only' keys (S-3 addition)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _build_memory_dir(mem, links=["a.md"], files=["a.md", "feedback_x.md"])
    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=["feedback_*.md"], ignore=[])

    result = MC.check(mem, pattern_file=pf)
    assert "archived" in result
    assert "read_only" in result
    assert "forward" in result  # S-2 forward-compat stub still present


def test_s2_json_tests_still_green(tmp_path):
    """S-2 JSON-forward tests (forward in data + forward == []) stay green without patterns."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _build_memory_dir(mem, links=["a.md", "b.md"], files=["a.md", "b.md"])

    data = MC.check(mem)
    assert "forward" in data, "S-2: 'forward' key must be in result dict"
    assert data["forward"] == [], "S-2: result['forward'] must always be []"


# ---------------------------------------------------------------------------
# Wrapper re-export identity (T-05)
# The wrapper uses importlib to load core into _CORE, then re-exports via
# globals()[name] = getattr(_CORE, name). When we load the wrapper via
# _load_wrapper() and load core via _load_core() they are distinct Python
# module instances; function identity via `is` would always fail.
# The correct check is: the wrapper's _CORE attribute IS the module that
# wrapper.load_patterns came from — i.e., wrapper.load_patterns is
# wrapper._CORE.load_patterns (same _CORE instance within the wrapper).
# ---------------------------------------------------------------------------

def test_wrapper_reexports_load_patterns():
    """wrapper.load_patterns must come from wrapper._CORE (re-export guard)."""
    assert hasattr(WRAPPER, "load_patterns"), (
        "Wrapper does not expose load_patterns — globals() loop may be too narrow."
    )
    # The re-exported function must be the same object as wrapper._CORE.load_patterns
    assert WRAPPER.load_patterns is WRAPPER._CORE.load_patterns, (
        "wrapper.load_patterns is not the same object as wrapper._CORE.load_patterns. "
        "The globals() re-export loop is broken for this name."
    )


def test_wrapper_reexports_classify():
    """wrapper.classify must come from wrapper._CORE (re-export guard)."""
    assert hasattr(WRAPPER, "classify"), (
        "Wrapper does not expose classify — globals() loop may be too narrow."
    )
    assert WRAPPER.classify is WRAPPER._CORE.classify, (
        "wrapper.classify is not the same object as wrapper._CORE.classify."
    )


def test_wrapper_reexports_default_pattern_file():
    """wrapper.DEFAULT_PATTERN_FILE must equal core.DEFAULT_PATTERN_FILE."""
    assert WRAPPER.DEFAULT_PATTERN_FILE == MC.DEFAULT_PATTERN_FILE, (
        "Wrapper DEFAULT_PATTERN_FILE differs from core value."
    )


# ---------------------------------------------------------------------------
# /sleep filter tests (sleep_score.py)
# ---------------------------------------------------------------------------

def _make_insights_file(path: Path, promote_tag: bool = False) -> Path:
    """Write a minimal insights file with one entry."""
    tag = "Promote?: yes" if promote_tag else "Promote?: no"
    content = dedent(f"""\
        ### Entry 1
        This is a test insight entry with enough text to pass the 10-char threshold.
        {tag}
    """)
    path.write_text(content, encoding="utf-8")
    return path


def test_sleep_read_only_source_never_soft_forgotten(tmp_path):
    """Entry from a read_only-matched source must not appear in soft-forget bucket."""
    scan_dir = tmp_path / "daily"
    scan_dir.mkdir()

    # Create an insights file whose name matches feedback_*.md
    insights = scan_dir / "insights-2026-06-18.md"
    _make_insights_file(insights, promote_tag=False)  # no promote tag → likely forget

    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=["insights-*.md"], ignore=[])

    patterns = SS._load_maintenance_patterns(str(pf))
    entries = SS.collect_entries(str(scan_dir), scan_days=999, patterns=patterns)

    # Must have at least one entry (the file is NOT ignored, just protected)
    assert len(entries) > 0, "Expected at least one entry from a read_only-matched file"

    # All entries must be marked protected
    for e in entries:
        assert e.protected is True, f"Expected protected=True, got {e}"

    # Build a config that would normally force forget (high forget weight)
    config = {
        "promote": {"frequency_3plus": 10, "user_marked_yes": 10, "cross_task_2plus": 10,
                    "structural_fit": 10, "survival": 10, "cost_bearing": 10},
        "forget": {"one_shot": 10, "user_marked_no": 10, "resolved_and_shipped": 10,
                   "stale_30days": 10, "duplicate": 10, "sub_threshold_cost": 10},
        "thresholds": {"promote_min_score": 100, "promote_max_forget": 0,
                       "forget_min_score": 1, "forget_max_promote": 100,
                       "forget_quiet_floor": 4, "scan_window_days": 30,
                       "cost_bearing_floor_usd": 0.50, "stale_days": 0},
    }
    scored = SS.score_entries(entries, config)

    # No entry from this protected source should be in the "forget" bucket
    forget_entries = [e for e in scored if e.bucket == "forget"]
    assert len(forget_entries) == 0, (
        f"Protected source entry appeared in soft-forget bucket: {forget_entries}"
    )


def test_sleep_ignore_source_produces_zero_entries(tmp_path):
    """Entry from an ignore-matched source must produce zero entries."""
    scan_dir = tmp_path / "daily"
    scan_dir.mkdir()

    insights = scan_dir / "insights-2026-06-18.md"
    _make_insights_file(insights)

    pf = _write_pattern_file(tmp_path / "p.yaml",
                             archived=[], read_only=[], ignore=["insights-*.md"])

    patterns = SS._load_maintenance_patterns(str(pf))
    entries = SS.collect_entries(str(scan_dir), scan_days=999, patterns=patterns)

    assert len(entries) == 0, (
        f"ignore-matched source should produce zero entries, got {len(entries)}"
    )


def test_sleep_demotion_requires_protected_flag(tmp_path):
    """Negative control: demotion must not fire when protected=False (lesson 2026-06-15)."""
    scan_dir = tmp_path / "daily"
    scan_dir.mkdir()

    insights = scan_dir / "insights-2026-06-18.md"
    _make_insights_file(insights, promote_tag=False)

    # No patterns file → no protection
    entries = SS.collect_entries(str(scan_dir), scan_days=999, patterns=None)
    assert len(entries) > 0, "Expected entries without patterns"

    # All entries should be protected=False
    for e in entries:
        assert e.protected is False, f"Expected protected=False without patterns, got {e}"

    # With a high forget config, entries should land in "forget" (not demoted)
    config = {
        "promote": {"frequency_3plus": 100, "user_marked_yes": 100,
                    "cross_task_2plus": 100, "structural_fit": 100,
                    "survival": 100, "cost_bearing": 100},
        "forget": {"one_shot": 10, "user_marked_no": 0, "resolved_and_shipped": 0,
                   "stale_30days": 0, "duplicate": 0, "sub_threshold_cost": 0},
        "thresholds": {"promote_min_score": 1000, "promote_max_forget": 0,
                       "forget_min_score": 1, "forget_max_promote": 1000,
                       "forget_quiet_floor": 4, "scan_window_days": 30,
                       "cost_bearing_floor_usd": 0.50, "stale_days": 9999},
    }
    scored = SS.score_entries(entries, config)
    forget_entries = [e for e in scored if e.bucket == "forget"]

    assert len(forget_entries) > 0, (
        "Negative control failed: expected at least one forget-bucket entry when "
        "protected=False and forget score is high. Demotion may be firing unconditionally."
    )
