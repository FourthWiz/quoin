"""Tests for IVG-153 Stage 2 T-11: `/end_of_task` step-idempotency under
autonomous resume.

Verified terminal ordering: push (Sub-phase A) -> Sub-phase B (lessons +
session-state + cost) -> archive (Sub-phase C) -> done-sentinel (LAST, after
the archive move AND the final report). A kill at ANY boundary and re-run must
not duplicate work:

(a) push is a no-op if HEAD == origin/branch after fetch.
(b) Sub-phase B is gated behind an `end_of_task.subphaseB.done` entry-skip
    sentinel (autonomous only) AND a lessons-append idempotency guard keyed
    on task+stage (not task alone, MINOR fix) AND re-runnable cost
    aggregation (recompute/overwrite, never a second total row).
(c) archive skips the move if the `finalized/` target already exists.
(d) the done-sentinel is written LAST — after the archive move and the report
    print — atomically, outside the archived folder.
(e) the kill-after-push-before-done case is explicitly named safe with no
    duplicate lessons entry.
(f) "never auto-create a PR" is restated.
(g) flag-flip idempotency (Step 3a) is preserved (pre-existing, MINOR fix
    acceptance — assert it is still documented, not removed by this task's
    edits).

This is a SKILL.md-lint test (grep/slice the source), matching the repo's
existing style for autonomous-mode text-level guards.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
END_OF_TASK_SKILL = (
    REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
)


@pytest.fixture(scope="module")
def eot_text() -> str:
    assert END_OF_TASK_SKILL.exists(), f"missing: {END_OF_TASK_SKILL}"
    return END_OF_TASK_SKILL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) push no-op if already pushed
# ---------------------------------------------------------------------------


def test_push_is_noop_if_head_equals_origin(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 6: Dispatch Sub-phase A")
    end = text.index("**Step 7: Dispatch Sub-phase B")
    section = text[idx:end]
    assert "idempotent, T-11" in section
    assert "git fetch origin" in section
    assert "SKIP the push as a no-op" in section
    assert '"push_skipped": true' in section


# ---------------------------------------------------------------------------
# (b) Sub-phase B: entry-skip sentinel + task+stage lessons guard + re-runnable cost
# ---------------------------------------------------------------------------


def test_subphase_b_entry_skip_sentinel(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 7: Dispatch Sub-phase B")
    end = text.index("**Step 8: Dispatch Sub-phase C")
    section = text[idx:end]

    assert "end_of_task.subphaseB.done" in section
    assert "Entry-skip guard" in section
    assert "_AUTONOMOUS" in section
    assert "run BEFORE step 1" in section
    assert "SKIP everything below as a" in section
    # Counts toward the T-06 forward-progress glob.
    assert "autonomous-progress-{task}/*.done" in section


def test_lessons_append_idempotency_keyed_on_task_and_stage(eot_text: str) -> None:
    """MINOR fix: the grep marker must key on task+stage, not task alone, so a
    stage-2 lessons entry is not false-skipped by a stage-1 entry's heading."""
    text = eot_text
    idx = text.index("**Step 7: Dispatch Sub-phase B")
    end = text.index("**Step 8: Dispatch Sub-phase C")
    section = " ".join(text[idx:end].split())

    assert "keyed on task+stage" in section
    assert "stage-1 entry's" in section or "stage-1" in section
    assert "SKIP the append" in section
    assert "and `stage`" in section  # reads the stage field from eot-preflights.json
    assert "[stage-" in section  # stage-suffixed heading form


def test_cost_aggregation_re_runnable(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 7: Dispatch Sub-phase B")
    end = text.index("**Step 8: Dispatch Sub-phase C")
    section = " ".join(text[idx:end].split())

    assert "Re-runnable (T-11)" in section
    assert "never blind-appends a second total row" in section
    assert "OVERWRITES cost-summary.json" in section


def test_subphase_b_sentinel_written_last_and_gated(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 7: Dispatch Sub-phase B")
    end = text.index("**Step 8: Dispatch Sub-phase C")
    section = " ".join(text[idx:end].split())

    assert "Write the Sub-phase B entry-skip sentinel (T-11, `_AUTONOMOUS` only) — LAST" in section
    assert "Inert when `_AUTONOMOUS` is false" in section


# ---------------------------------------------------------------------------
# (c) archive skip-if-already-archived
# ---------------------------------------------------------------------------


def test_archive_skips_if_target_already_exists(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 8: Dispatch Sub-phase C")
    end = text.index("## Important behaviors")
    section = text[idx:end]

    assert "Archive (idempotent, T-11)" in section
    assert "before the move, check whether the target directory" in section
    assert "already archived" in section
    assert "SKIP the move as a" in section
    assert "fsops.py mv --parents" in section
    assert "create the target dir with `mkdir -p`" not in section


# ---------------------------------------------------------------------------
# (d) done-sentinel written LAST, after archive + report, outside archive
# ---------------------------------------------------------------------------


def test_done_sentinel_written_last_after_archive_and_report(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 8: Dispatch Sub-phase C")
    end = text.index("## Important behaviors")
    section = " ".join(text[idx:end].split())

    assert "autonomous-done-<task_name>.md" in section
    assert "Write the done-sentinel (T-11, `_AUTONOMOUS` only) — LAST" in section
    assert "after the archive move (step 4) AND the report print (step 5)" in section
    assert "OUTSIDE the just-archived task folder" in section
    # Atomic write through the helper.
    assert "fsops.py write-atomic" in section


# ---------------------------------------------------------------------------
# (e) kill-after-push-before-done named safe, no duplicate lessons entry
# ---------------------------------------------------------------------------


def test_kill_after_push_before_done_named_safe(eot_text: str) -> None:
    text = eot_text
    idx = text.index("**Step 8: Dispatch Sub-phase C")
    end = text.index("## Important behaviors")
    section = " ".join(text[idx:end].split())

    assert "after-push-before-Sub-phase-B" in section
    assert "after-Sub-phase-B-before-archive" in section
    assert "after-archive-before-done" in section
    assert "no duplicated work" in section


# ---------------------------------------------------------------------------
# (f) "never auto-create a PR" restated
# ---------------------------------------------------------------------------


def test_never_auto_create_pr_restated(eot_text: str) -> None:
    normalized = " ".join(eot_text.split())
    assert "never auto-create a PR" in normalized or "never creates a PR" in normalized


# ---------------------------------------------------------------------------
# (g) flag-flip idempotency preserved (pre-existing; T-11 must not remove it)
# ---------------------------------------------------------------------------


def test_flag_flip_idempotency_preserved(eot_text: str) -> None:
    text = eot_text
    assert "Flip finalized-task session flags" in text
    assert "idempotent — safe to re-run" in text


# ---------------------------------------------------------------------------
# Ordering sanity: the four sub-phase-boundary markers appear in source order
# ---------------------------------------------------------------------------


def test_step_ordering_push_then_b_then_archive_then_done(eot_text: str) -> None:
    text = eot_text
    push_idx = text.index("**Step 6: Dispatch Sub-phase A")
    b_idx = text.index("**Step 7: Dispatch Sub-phase B")
    archive_idx = text.index("**Step 8: Dispatch Sub-phase C")
    done_idx = text.index("Write the done-sentinel (T-11")
    assert push_idx < b_idx < archive_idx < done_idx
