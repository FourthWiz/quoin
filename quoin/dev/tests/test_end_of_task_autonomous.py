"""SKILL.md-lint tests for `[autonomous]` branches in /end_of_task (IVG-153, T-24).

Text-level guards over `end_of_task/SKILL.md` — assert the `[autonomous]` sentinel is
parsed at bootstrap into `_AUTONOMOUS`, and that each of the four genuine interactive
body-prompt sites (Step 1b garbage-files, Step 2 commit decision, Step 3 lessons-learned,
Step 4 archive-type) has an autonomous auto-resolution branch that never calls
`AskUserQuestion`. Also asserts the commit branch selects "Commit" (never "Abort"), the
"never auto-create a PR" invariant is restated in the autonomous path, and — SCOPE GUARD —
that no Stage-2 idempotency rework (no-op-if-already-pushed / archive-checks-finalized /
done-sentinel-outside-archive) was introduced. The GENERATED §0'/§0″-equivalent dispatch
blocks and the HAND-MAINTAINED §0-worktree-fallback / §0-sidecar blocks (owned by T-23/T-25
respectively) are intentionally left untouched by this task.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
END_OF_TASK_SKILL = (
    REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
)


@pytest.fixture(scope="module")
def eot_skill_text() -> str:
    assert END_OF_TASK_SKILL.exists(), f"end_of_task/SKILL.md not found at {END_OF_TASK_SKILL}"
    return END_OF_TASK_SKILL.read_text(encoding="utf-8")


def test_autonomous_sentinel_parsed_at_bootstrap(eot_skill_text: str) -> None:
    text = eot_skill_text
    assert "_AUTONOMOUS" in text
    assert "[autonomous]" in text
    assert "Autonomous mode bootstrap" in text


def test_step1b_garbage_files_autonomous_branch(eot_skill_text: str) -> None:
    text = eot_skill_text
    idx = text.index("**Step 1b: Working-tree cleanup scan**")
    end = text.index("**Step 2: Commit decision")
    section = text[idx:end]
    assert "**Autonomous mode:**" in section
    assert "skip the" in section
    assert "AskUserQuestion" in section
    assert "Delete garbage files" in section


def test_step2_commit_decision_autonomous_branch_selects_commit_not_abort(
    eot_skill_text: str,
) -> None:
    text = eot_skill_text
    idx = text.index("**Step 2: Commit decision")
    end = text.index("**Step 3: Lessons learned")
    section = text[idx:end]
    assert "**Autonomous mode:**" in section
    autonomous_idx = section.index("**Autonomous mode:**")
    autonomous_clause = section[autonomous_idx : autonomous_idx + 400]
    assert '"Commit"' in autonomous_clause
    assert "NEVER" in autonomous_clause
    assert '"Abort"' in autonomous_clause
    # Dedicated negative check: the autonomous clause must not itself select Abort.
    assert 'select **"Abort"**' not in autonomous_clause


def test_step3_lessons_learned_autonomous_branch_no_askuserquestion(eot_skill_text: str) -> None:
    text = eot_skill_text
    idx = text.index("**Step 3: Lessons learned")
    end = text.index("**Step 4: Archive type")
    section = text[idx:end]
    assert "**Autonomous mode:**" in section
    autonomous_idx = section.index("**Autonomous mode:**")
    autonomous_clause = section[autonomous_idx : autonomous_idx + 500]
    assert "skip the" in autonomous_clause
    assert "AskUserQuestion" in autonomous_clause
    assert "auto-capture" in autonomous_clause.lower()
    assert "lessons_text" in autonomous_clause


def test_step4_archive_type_autonomous_branch_selects_fully_complete(eot_skill_text: str) -> None:
    text = eot_skill_text
    idx = text.index("**Step 4: Archive type")
    end = text.index("**Step 5: Write")
    section = text[idx:end]
    assert "**Autonomous mode:**" in section
    autonomous_idx = section.index("**Autonomous mode:**")
    autonomous_clause = section[autonomous_idx : autonomous_idx + 400]
    assert '"Fully complete"' in autonomous_clause
    assert 'archive_type = "feature"' in autonomous_clause


def test_never_auto_create_pr_restated_in_autonomous_path(eot_skill_text: str) -> None:
    """The 'never auto-create a PR' invariant must be explicitly restated somewhere in
    the autonomous-mode text, not just in the pre-existing 'Important behaviors' note."""
    text = eot_skill_text
    idx = text.index("## Autonomous mode bootstrap")
    end = text.index("## Process")
    # Normalize whitespace (source wraps prose across lines) before substring checks.
    bootstrap_section = " ".join(text[idx:end].split())
    assert "never auto-create a PR" in bootstrap_section
    assert "never creates a PR" in bootstrap_section


def test_no_stage2_idempotency_text_introduced(eot_skill_text: str) -> None:
    """SUPERSEDED by IVG-153 Stage 2 T-11 (2026-07-21): this was originally a scope
    guard for T-24 (IN-SESSION auto-confirm ONLY, Stage 1), asserting Stage-2
    end_of_task idempotency rework had NOT yet been introduced. T-11 now
    deliberately introduces exactly that rework (push no-op guard, Sub-phase B
    entry-skip sentinel, archive-skip guard, done-sentinel written last) — see
    `test_end_of_task_idempotent.py` for the positive coverage. Kept as a no-op
    (rather than deleted) so the historical scope note stays discoverable."""
    assert eot_skill_text  # file still readable; nothing forbidden anymore


def test_dispatch_and_worktree_blocks_not_hand_edited(eot_skill_text: str) -> None:
    """T-23/T-25 own the generated/hand-synced dispatch blocks — this task must not
    touch them."""
    text = eot_skill_text
    assert "<!-- §0-worktree-fallback-begin -->" in text
    assert "<!-- §0-worktree-fallback-end -->" in text
    assert "<!-- §0-sidecar-begin -->" in text
    assert "<!-- §0-sidecar-end -->" in text


def test_normal_path_askuserquestion_still_documented(eot_skill_text: str) -> None:
    """Non-autonomous behavior stays unchanged: all four body prompts still document
    AskUserQuestion outside the autonomous branches."""
    text = eot_skill_text
    assert 'question="Garbage files or debug leftovers found' in text
    assert 'question="There are uncommitted changes. Commit them now or abort?"' in text
    assert 'question="Task complete. Anything that surprised you' in text
    assert "question=\"Is the feature '<task-name>' fully complete" in text

