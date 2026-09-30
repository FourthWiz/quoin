"""IVG-258 T-14: `/cleanup` Step 5c run-state sweep, the nine-family
regression, and the `_DOCS_TO_TESTS` routing rows this stage adds.

`cleanup/SKILL.md` carried zero `_DOCS_TO_TESTS` rows before this stage --
an affected-area gate after a cleanup-only edit selected no test at all.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "quoin"
CLEANUP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"
SLEEP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "sleep" / "SKILL.md"
AFFECTED_TESTS = SOURCE_ROOT / "core" / "scripts" / "affected_tests.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 5c
# ---------------------------------------------------------------------------


def test_step_5c_present_with_both_globs():
    text = _text(CLEANUP_SKILL)
    assert "Step 5c" in text
    assert "run-state-*.json" in text
    assert "run-notes-*.md" in text
    assert "QUOIN_CLEANUP_RUNSTATE_WINDOW" in text


def test_step_5c_sweeps_abandoned_tmp_scratch_files():
    text = _text(CLEANUP_SKILL)
    assert "run-state-*.json.*.tmp" in text
    assert "QUOIN_CLEANUP_SENTINEL_WINDOW" in text


def test_sweep_uses_trash_move_not_rm():
    step5c = _text(CLEANUP_SKILL).split("Step 5c", 1)[1].split("Step 6", 1)[0]
    assert "trash_move" in step5c
    assert "rm -f" not in step5c
    assert "fsops.py rm" not in step5c


def test_hard_delete_check_catches_helper_form():
    fake = "Step 5c: trash_move a\npython3 fsops.py rm \"a\"\nStep 6"
    fake_step5c = fake.split("Step 5c", 1)[1].split("Step 6", 1)[0]
    assert "fsops.py rm" in fake_step5c


def test_window_knob_default_is_30():
    text = _text(CLEANUP_SKILL)
    assert "QUOIN_CLEANUP_RUNSTATE_WINDOW:-30" in text


# ---------------------------------------------------------------------------
# Closed-scope statements + /sleep comparison table
# ---------------------------------------------------------------------------


def test_both_closed_scope_statements_name_run_state():
    text = _text(CLEANUP_SKILL)
    first = "NEVER targets `lessons-learned.md`, `forgotten/`, or any source file"
    second = "ONLY trash-moves files under `.workflow_artifacts/memory/`"
    idx1 = text.index(first)
    idx2 = text.index(second)
    window1 = text[idx1 : idx1 + 400]
    window2 = text[idx2 : idx2 + 400]
    assert "run-state-" in window1 and "run-notes-" in window1
    assert "run-state-" in window2 and "run-notes-" in window2


def test_sleep_comparison_table_carries_the_cleanup_only_qualifier():
    text = _text(CLEANUP_SKILL)
    table_idx = text.index("## Relationship to /sleep --purge --sentinels")
    after_table = text[table_idx:]
    assert "is `/cleanup`-only" in after_table
    assert "sentinel_globs()" in after_table


# ---------------------------------------------------------------------------
# Nine-family regression (this stage must not touch the allow-list)
# ---------------------------------------------------------------------------


def test_allow_list_still_has_exactly_nine_numbered_entries():
    text = _text(CLEANUP_SKILL)
    match = re.search(
        r"## Hardcoded sentinel allow-list \(9 families\)\n\n(.*?)\n\nThese families",
        text,
        re.S,
    )
    assert match, "allow-list section not found in the expected shape"
    entries = re.findall(r"^\d+\.\s+`[^`]+`", match.group(1), re.M)
    assert len(entries) == 9


def test_no_run_state_entry_added_to_allow_list():
    text = _text(CLEANUP_SKILL)
    match = re.search(
        r"## Hardcoded sentinel allow-list \(9 families\)\n\n(.*?)\n\nThese families",
        text,
        re.S,
    )
    assert match
    assert "run-state" not in match.group(1)
    assert "run-notes" not in match.group(1)


def test_sleep_skill_md_unmodified_by_this_stage():
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--quiet", "--", "quoin/adapters/claude/skills/sleep/SKILL.md"],
        capture_output=True,
    )
    assert result.returncode == 0, "sleep/SKILL.md has an uncommitted diff"


# ---------------------------------------------------------------------------
# _DOCS_TO_TESTS routing rows
# ---------------------------------------------------------------------------


def _load_docs_to_tests():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_quoin_core_affected_tests_t14", AFFECTED_TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._DOCS_TO_TESTS


def test_docs_to_tests_routes_cleanup_skill_md():
    pairs = _load_docs_to_tests()
    routed = {test for doc, test in pairs if doc == "quoin/adapters/claude/skills/cleanup/SKILL.md"}
    assert routed, "cleanup/SKILL.md has no _DOCS_TO_TESTS rows"
    assert any("test_cleanup_runstate_sweep.py" in t for t in routed)


def test_docs_to_tests_routes_run_and_thorough_plan_to_the_new_tests():
    pairs = _load_docs_to_tests()
    run_routed = {test for doc, test in pairs if doc == "quoin/adapters/claude/skills/run/SKILL.md"}
    tp_routed = {
        test for doc, test in pairs if doc == "quoin/adapters/claude/skills/thorough_plan/SKILL.md"
    }
    assert any("test_run_state_wiring.py" in t for t in run_routed)
    assert any("test_run_state_resume_precedence.py" in t for t in run_routed)
    assert any("test_run_state_wiring.py" in t for t in tp_routed)
