#!/usr/bin/env python3
"""
test_sleep_scoring.py — unit tests for sleep_score.py importance scoring.

Runnable with:
  .venv/bin/python quoin/dev/tests/test_sleep_scoring.py

All tests use fixtures under quoin/dev/tests/fixtures/sleep/.
No pytest required — stdlib unittest / plain assert-based tests.
"""

import sys
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Make sleep_score importable from quoin/scripts/
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from sleep_score import (
    load_config,
    collect_entries,
    score_entries,
    dedup_against_lessons,
    _task_name_from_path,
    RawEntry,
    ScoredEntry,
    DEFAULT_CONFIG,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sleep"
_SIGNALS_YAML = Path(__file__).parent.parent.parent / "memory" / "sleep-signals.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_test(name: str, fn) -> bool:
    """Run one test function; return True on pass, False on fail/skip."""
    try:
        result = fn()
        if result == "SKIP":
            print(f"  SKIP  {name}")
            return True
        print(f"  PASS  {name}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return False
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        return False


@contextmanager
def materialized_fixture(name: str):
    """Copy a fixture directory to a temp dir and stamp all file mtimes to now.

    Prevents mtime-sensitive failures when the repo checkout is older than the
    scan_days window used by collect_entries().  The original fixture directory
    is never written; the temp copy is removed on context exit even if the body
    raises.

    Usage::

        with materialized_fixture("promote_hit") as fdir:
            entries = collect_entries(str(fdir), scan_days=365)
    """
    tmp = tempfile.mkdtemp(prefix="sleep_fx_")
    try:
        dst = Path(tmp) / name
        shutil.copytree(_FIXTURES / name, dst)
        # Stamp every file (and dir) mtime to now so collect_entries() sees them
        # as within any reasonable scan_days window.  os.utime(path, None) sets
        # both atime and mtime to the current time without requiring `import time`.
        for root, dirs, files in os.walk(dst):
            for fname in files:
                os.utime(os.path.join(root, fname), None)
            for dname in dirs:
                os.utime(os.path.join(root, dname), None)
        os.utime(dst, None)
        yield dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_promote_hit():
    """promote_hit/ fixture: at least one entry scores as promote."""
    with materialized_fixture("promote_hit") as fdir:
        entries = collect_entries(str(fdir), scan_days=365)
        _assert(len(entries) > 0, f"Expected entries from promote_hit/, got 0 (fixture_dir={fdir})")

        config = DEFAULT_CONFIG
        scored = score_entries(entries, config)
        _assert(len(scored) > 0, "score_entries() returned empty list")

        promote_entries = [e for e in scored if e.bucket == "promote"]
        _assert(
            len(promote_entries) >= 1,
            f"Expected at least 1 promote entry, got 0. Buckets: {[(e.bucket, e.promote_score, e.forget_score) for e in scored]}",
        )


def test_forget_hit():
    """forget_hit/ fixture: at least one entry scores as forget."""
    with materialized_fixture("forget_hit") as fdir:
        entries = collect_entries(str(fdir), scan_days=365)
        _assert(len(entries) > 0, f"Expected entries from forget_hit/, got 0 (fixture_dir={fdir})")

        config = DEFAULT_CONFIG
        scored = score_entries(entries, config)
        _assert(len(scored) > 0, "score_entries() returned empty list")

        forget_entries = [e for e in scored if e.bucket == "forget"]
        _assert(
            len(forget_entries) >= 1,
            f"Expected at least 1 forget entry, got 0. Buckets: {[(e.bucket, e.promote_score, e.forget_score) for e in scored]}",
        )


def test_middle_band():
    """middle_band/ fixture: at least one entry scores as middle."""
    with materialized_fixture("middle_band") as fdir:
        entries = collect_entries(str(fdir), scan_days=365)
        _assert(len(entries) > 0, f"Expected entries from middle_band/, got 0 (fixture_dir={fdir})")

        config = DEFAULT_CONFIG
        scored = score_entries(entries, config)
        _assert(len(scored) > 0, "score_entries() returned empty list")

        middle_entries = [e for e in scored if e.bucket == "middle"]
        _assert(
            len(middle_entries) >= 1,
            f"Expected at least 1 middle entry, got 0. Buckets: {[(e.bucket, e.promote_score, e.forget_score) for e in scored]}",
        )


def test_dedup_suppress():
    """dedup_suppress/ fixture: overlapping candidate is removed from promote list after dedup."""
    with materialized_fixture("dedup_suppress") as fdir:
        lessons_fixture = fdir / "lessons-learned-fixture.md"

        _assert(lessons_fixture.exists(), f"lessons-learned-fixture.md not found at {lessons_fixture}")

        entries = collect_entries(str(fdir), scan_days=365)
        _assert(len(entries) > 0, f"Expected entries from dedup_suppress/, got 0 (fixture_dir={fdir})")

        config = DEFAULT_CONFIG
        scored = score_entries(entries, config)

        # Before dedup: should have at least one promote entry (the pyyaml entry with user_marked_yes)
        promote_before = [e for e in scored if e.bucket == "promote"]
        _assert(
            len(promote_before) >= 1,
            f"Expected at least 1 promote entry before dedup, got 0. Buckets: {[(e.bucket, e.promote_score) for e in scored]}",
        )

        # Dedup against the fixture lessons
        lessons_text = lessons_fixture.read_text(encoding="utf-8")
        after = dedup_against_lessons(scored, lessons_text)

        # After dedup: the pyyaml promote entry should be filtered out
        promote_after = [e for e in after if e.bucket == "promote"]
        _assert(
            len(promote_after) == 0,
            f"Expected 0 promote entries after dedup, got {len(promote_after)}: {[e.text[:60] for e in promote_after]}",
        )


def test_weight_override():
    """Overriding promote_min_score to 99 means no entries can reach promote bucket."""
    with materialized_fixture("promote_hit") as fdir:
        entries = collect_entries(str(fdir), scan_days=365)
        _assert(len(entries) > 0, "Expected entries from promote_hit/")

        # Override promote_min_score to impossibly high value
        import copy
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["thresholds"]["promote_min_score"] = 99

        scored = score_entries(entries, config)
        promote_entries = [e for e in scored if e.bucket == "promote"]
        _assert(
            len(promote_entries) == 0,
            f"Expected 0 promote entries with min_score=99, got {len(promote_entries)}",
        )


def test_default_weights_present():
    """load_config(signals_yaml_path=...) returns a dict with expected keys; _source sentinel present when pyyaml installed."""
    try:
        import yaml
        pyyaml_available = True
    except ImportError:
        pyyaml_available = False

    if not pyyaml_available:
        # Skip — cannot verify live YAML parse without pyyaml
        return "SKIP"

    if not _SIGNALS_YAML.exists():
        raise AssertionError(f"quoin/memory/sleep-signals.yaml not found at {_SIGNALS_YAML}")

    config = load_config(signals_yaml_path=str(_SIGNALS_YAML))

    # Top-level keys
    _assert("promote" in config, "config missing 'promote' key")
    _assert("forget" in config, "config missing 'forget' key")
    _assert("thresholds" in config, "config missing 'thresholds' key")

    # Expected promote signal keys
    promote = config["promote"]
    _assert("frequency_3plus" in promote, "promote missing 'frequency_3plus'")
    _assert("user_marked_yes" in promote, "promote missing 'user_marked_yes'")

    # Expected forget signal keys
    forget = config["forget"]
    _assert("one_shot" in forget, "forget missing 'one_shot'")
    _assert("user_marked_no" in forget, "forget missing 'user_marked_no'")

    # Expected threshold keys
    thresholds = config["thresholds"]
    _assert("promote_min_score" in thresholds, "thresholds missing 'promote_min_score'")
    _assert("forget_min_score" in thresholds, "thresholds missing 'forget_min_score'")

    # The _source sentinel distinguishes live YAML parse from hardcoded fallback
    _assert(
        config["thresholds"].get("_source") == "claude_md",
        f"Expected thresholds._source == 'claude_md', got {config['thresholds'].get('_source')!r}. "
        "Either pyyaml failed to parse CLAUDE.md or the _source sentinel is missing from the YAML block.",
    )


def test_h2_entries_split_per_entry():
    """capture_insight_format/ fixture: each "## " entry is parsed on its own."""
    with materialized_fixture("capture_insight_format") as fdir:
        entries = collect_entries(str(fdir), scan_days=365)
        _assert(
            len(entries) == 3,
            f"Expected 3 entries (one per '## ' heading), got {len(entries)}: "
            f"{[e.text.splitlines()[0] for e in entries]}",
        )
        first, second, third = entries

        _assert(first.text.startswith("## 09:15"), f"Unexpected first entry: {first.text[:40]!r}")
        _assert("Scratchpad for patterns" not in first.text, "File preamble leaked into the first entry")
        _assert(not first.text.endswith("---"), "Separator rule leaked into the entry text")

        # Per-entry Promote? tags, not one tag set for the whole file.
        _assert(first.promote_tag and not first.no_tag, "First entry should carry Promote?: yes only")
        _assert(not second.promote_tag and not second.no_tag, "Second entry is Promote?: maybe")
        _assert(third.no_tag and not third.promote_tag, "Third entry should carry Promote?: no only")

        # A nested "### " heading and a "## " line quoted in a code block stay
        # inside the entry that contains them.
        _assert("### Detail" in second.text, "Nested heading was split out of its entry")
        _assert("## Cost" in second.text, "Fenced heading was treated as an entry boundary")
        _assert(second.text.startswith("## 13:40"), f"Unexpected second entry: {second.text[:40]!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_tests():
    tests = [
        ("test_promote_hit", test_promote_hit),
        ("test_forget_hit", test_forget_hit),
        ("test_middle_band", test_middle_band),
        ("test_dedup_suppress", test_dedup_suppress),
        ("test_weight_override", test_weight_override),
        ("test_default_weights_present", test_default_weights_present),
        ("test_h2_entries_split_per_entry", test_h2_entries_split_per_entry),
    ]

    print(f"Running {len(tests)} test(s) from {__file__}")
    passed = 0
    failed = 0
    skipped = 0

    for name, fn in tests:
        ok = _run_test(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1

    # Adjust counts for skips (SKIP returns True in _run_test)
    print(f"\nResults: {passed} passed (includes skips), {failed} failed")

    if failed > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run_tests()


# ---------------------------------------------------------------------------
# T-18 (IVG-119) — promoted module-level slug inference + --slug-from-path CLI
# ---------------------------------------------------------------------------

def test_task_name_from_path_insights_form():
    """L497 branch: insights-<date>-<slug>.md → <slug>."""
    assert _task_name_from_path("insights-2026-04-25-auth-refactor.md") == "auth-refactor"


def test_task_name_from_path_sessions_form():
    """L500 branch: sessions/<date>-<slug>.md → <slug>."""
    assert _task_name_from_path("sessions/2026-04-25-auth-refactor.md") == "auth-refactor"


def test_slug_from_path_cli_matches_module_fn():
    """CLI --slug-from-path prints the same slug the module fn returns (heading↔guard)."""
    import subprocess
    script = Path(__file__).parent.parent.parent / "scripts" / "sleep_score.py"
    for src in (
        "insights-2026-04-25-auth-refactor.md",
        "sessions/2026-04-25-auth-refactor.md",
    ):
        run = subprocess.run(
            [sys.executable, str(script), "--slug-from-path", src],
            capture_output=True, text=True,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == _task_name_from_path(src) == "auth-refactor"
