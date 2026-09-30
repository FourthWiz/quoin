"""SKILL.md-lint tests for `[autonomous]` branches in /implement (IVG-153, T-09).

Text-level guards over `implement/SKILL.md` — assert (a) an `[autonomous]` branch at the
§0b branch-hygiene precheck that auto-creates `feat/{task}` on a protected branch WITHOUT
`AskUserQuestion`; (b) an `[autonomous]` branch at the "Confirm the task" step that
auto-selects all pending tasks; (c) the autonomous branch is a NEW path independent of
(does not require) `QUOIN_GATE_AUTO_APPROVE`/`QUOIN_BENCHMARK_RUN`. The GENERATED
`<!-- §0-...  -->` dispatch/worktree blocks are intentionally left untouched by this task.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
IMPLEMENT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "implement" / "SKILL.md"


@pytest.fixture(scope="module")
def implement_skill_text() -> str:
    assert IMPLEMENT_SKILL.exists(), f"implement/SKILL.md not found at {IMPLEMENT_SKILL}"
    return IMPLEMENT_SKILL.read_text(encoding="utf-8")


def _branch_hygiene_section(text: str) -> str:
    idx = text.index("## §0b Branch-hygiene precheck")
    end = text.index("## Explicit invocation only")
    return text[idx:end]


def _confirm_task_section(text: str) -> str:
    idx = text.index('4. **Confirm the task.**')
    end = text.index("## Implementation rules")
    return text[idx:end]


def test_autonomous_sentinel_parsed_at_bootstrap(implement_skill_text: str) -> None:
    text = implement_skill_text
    assert "_AUTONOMOUS" in text
    assert "[autonomous]" in text


def test_branch_hygiene_autonomous_auto_creates_branch_without_askuserquestion(
    implement_skill_text: str,
) -> None:
    section = _branch_hygiene_section(implement_skill_text)
    assert "`[autonomous]` bypass" in section
    assert "skip `AskUserQuestion`" in section
    assert "feat/{task-name}" in section
    # Reuses the benchmark bypass's git switch -c logic.
    assert "git -C {repo} switch -c {branch}" in section


def test_branch_hygiene_autonomous_independent_of_benchmark_dual_guard(
    implement_skill_text: str,
) -> None:
    section = _branch_hygiene_section(implement_skill_text)
    autonomous_idx = section.index("`[autonomous]` bypass")
    autonomous_para = section[autonomous_idx : autonomous_idx + 800]
    assert "INDEPENDENT of" in autonomous_para or "independent of" in autonomous_para
    assert (
        "does NOT require either env var" in autonomous_para
        or "does NOT depend on either env var" in autonomous_para
        or "keyed solely on the sentinel" in autonomous_para
    )
    # The autonomous branch explicitly documents that it fires REGARDLESS of the
    # benchmark dual-guard vars (mentioning them for clarity is fine; gating on
    # them is not — assert the "regardless of" framing, not their absence).
    assert "regardless of" in autonomous_para


def test_confirm_task_autonomous_auto_selects_all_remaining(implement_skill_text: str) -> None:
    section = _confirm_task_section(implement_skill_text)
    assert "`[autonomous]` branch" in section
    assert "skip `AskUserQuestion` entirely" in section
    assert "All remaining tasks" in section
    assert "no wait for user input" in section


def test_normal_path_askuserquestion_still_documented(implement_skill_text: str) -> None:
    """Non-autonomous behavior is unchanged: the normal task-confirm path still uses
    AskUserQuestion outside the autonomous branch."""
    text = implement_skill_text
    assert "Use AskUserQuestion to ask the user which task(s)" in text


def test_dispatch_sites_not_hand_edited(implement_skill_text: str) -> None:
    text = implement_skill_text
    assert "<!-- §0-worktree-fallback-begin -->" in text
    assert "<!-- §0-worktree-fallback-end -->" in text


def _after_implementation_section(text: str) -> str:
    return text[text.index("## After implementation"):]


def test_confirm_task_autonomous_branch_checks_plan_and_writes_marker(implement_skill_text: str) -> None:
    section = _confirm_task_section(implement_skill_text)
    assert "plan_tasks.py" in section
    assert "stdout prefix" in section
    assert "$PROJECT_ROOT/.workflow_artifacts/memory/autonomous-progress-{task}/implement.tasks.done" in section
    assert "all plan tasks complete" in section
    assert "no selectable tasks" in section
    assert "All tasks already implemented." in section
    # only the orchestrator writes implement.done: it appears in the negative clause only
    assert "do NOT write `implement.done` (only the orchestrator writes it, after its gate)" in section
    assert section.replace(
        "do NOT write `implement.done` (only the orchestrator writes it, after its gate)", ""
    ).count("implement.done") == 0
    assert "write-atomic" in section and "implement.done`" not in section.split("write-atomic")[1][:60]


def test_confirm_task_fix_scope_precedes_plan_check(implement_skill_text: str) -> None:
    section = _confirm_task_section(implement_skill_text)
    fix = section.index("Fix scope")
    assert fix < section.index("plan_tasks.py")
    fix_para = section[fix : section.index("**`[autonomous]` branch:**")]
    assert "`Fix scope:`" in fix_para
    assert "gate <gate audit path>" in fix_para
    assert "review <review file path>" in fix_para
    assert "gate fix applied" in fix_para and "review fix applied" in fix_para


def test_after_implementation_writes_marker_only_on_clean_autonomous_finish(implement_skill_text: str) -> None:
    section = _after_implementation_section(implement_skill_text)
    marker = section[section.index("**Completion marker.**"):section.index("**Final-message branch.**")]
    assert "plan_tasks.py" in marker
    assert "never on the PARTIAL path" in marker
    assert "never on a scoped dispatch" in marker
    assert "all plan tasks complete" in marker
