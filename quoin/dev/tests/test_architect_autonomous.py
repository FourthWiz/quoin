"""SKILL.md-lint tests for `[autonomous]` branches in /architect (IVG-153, T-06).

Text-level guards over `architect/SKILL.md` — assert an autonomous branch exists at
each BODY (non-dispatch) interactive site. The GENERATED `<!-- §0doubleprime-begin/end -->`
block (owned by T-23) is intentionally left untouched by this task and is NOT asserted here.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCH_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "architect" / "SKILL.md"


@pytest.fixture(scope="module")
def arch_skill_text() -> str:
    assert ARCH_SKILL.exists(), f"architect/SKILL.md not found at {ARCH_SKILL}"
    return ARCH_SKILL.read_text(encoding="utf-8")


def test_autonomous_sentinel_parsed_at_bootstrap(arch_skill_text: str) -> None:
    text = arch_skill_text
    assert "_AUTONOMOUS" in text
    assert "[autonomous]" in text


def test_spec_preflight_autonomous_branch(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("## Spec pre-flight")
    section = text[idx: idx + 1500]
    assert "**Under `[autonomous]`:**" in section
    assert "skip the `AskUserQuestion`" in section


def test_scan_ambiguity_autonomous_branch(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("#### Questions before synthesis")
    section = text[idx: idx + 1200]
    assert "**Under `[autonomous]`:**" in section
    assert "skip `AskUserQuestion`" in section
    assert "Open questions" in section


def test_scan_failure_autonomous_branch(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("#### Scan agent errors")
    section = text[idx: idx + 1500]
    assert "**Under `[autonomous]`:**" in section
    assert "proceed best-effort without asking" in section
    assert "flag the gap explicitly" in section


def test_phase4_round2_cost_guard_autonomous_branch(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("if round == 2:")
    section = text[idx: idx + 900]
    assert "_AUTONOMOUS" in section
    assert 'confirm = "Yes, proceed"' in section


def test_same_class_escalation_autonomous_branch(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("if prior_family and this_family and prior_family == this_family:")
    section = text[idx: idx + 900]
    assert "_AUTONOMOUS" in section
    assert 'decision = "Continue revising"' in section


def test_loop_detected_accept_path_documents_autonomous(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("**Loop detected (strict mode only).**")
    section = text[idx: idx + 500]
    assert "**Under `[autonomous]`:**" in section
    assert "never via a user" in section


def test_dispatch_sites_fail_open_without_askuserquestion(arch_skill_text: str) -> None:
    """§0'/§0″ generated-block dispatch-failure paths proceed fail-OPEN — asserted only
    via the untouched generated-block markers, never hand-edited by this task."""
    text = arch_skill_text
    assert "<!-- §0doubleprime-begin -->" in text
    assert "<!-- §0doubleprime-end -->" in text
    # This task does not hand-edit the generated block; its autonomous fail-OPEN clause
    # is added uniformly by the T-23 generator-template change, not asserted here.


def test_phase4_critic_spawn_failure_retries_once(arch_skill_text: str) -> None:
    text = arch_skill_text
    idx = text.index("if critic_spawn_failed(round):")
    comment = text[text.rindex("# Spawn failure", 0, idx): idx]
    block = text[idx: text.index("# Read from TASK ROOT", idx)]
    assert "Retry ONCE" in comment
    assert "never retry it" in comment
    # One retry with the same prompt, then a nested second-failure check that stops
    # the loop without an AskUserQuestion, so it also holds under [autonomous].
    assert block.count("if critic_spawn_failed(round):") == 2
    assert 'spawn_critic_subagent(model="opus", prompt=spawn_prompt)' in block
    assert "including one retry" in block
    assert "break" in block
    assert "AskUserQuestion(" not in block
    assert "**Critic failed.**" in text
