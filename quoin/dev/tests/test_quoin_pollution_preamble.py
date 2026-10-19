"""
Quoin Stage 1 — structural consistency tests for the §0' pollution dispatch block.

The 9 Opus-tier target skills (architect, plan, critic, revise, review,
init_workflow, discover, specify, security_review) carry a `## §0' Pollution dispatch ...` block.
Cheap-tier skills and orchestrators must NOT carry it.

Per lesson 2026-04-23 LLM-replay non-determinism: NO live LLM calls —
only deterministic pathlib + string matching.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
# Phase-10 adapter migration moved active SKILL.md content from quoin/skills/ (stubs)
# to quoin/adapters/claude/skills/ (active adapter files). §0' was restored into the
# adapter dir by inject_pollution_dispatch.py (IVG-69 Stage A). Tests must read adapters.
SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"

POLLUTION_HEADING = "## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)"
SESSION_BOOTSTRAP_HEADING = "## Session bootstrap"
MODEL_REQ_HEADING = "## Model requirement"

# 9 Opus-tier target skills — must carry §0'.
POLLUTION_TARGET_SKILLS = [
    "architect",
    "plan",
    "critic",
    "revise",
    "review",
    "init_workflow",
    "discover",
    "specify",
    "security_review",
    "enrich",
]

# 12 cheap-tier skills — must NOT carry §0'.
CHEAP_TIER_SKILLS = [
    "gate",
    "end_of_day",
    "start_of_day",
    "capture_insight",
    "cost_snapshot",
    "weekly_review",
    "end_of_task",
    "implement",
    "rollback",
    "expand",
    "revise-fast",
    "triage",
]

# Orchestrators — must NOT carry §0'.
ORCHESTRATOR_SKILLS = ["run", "thorough_plan"]

# Required structural tokens in every §0' block.
REQUIRED_TOKENS = [
    "[no-redispatch]",
    "[quoin-S-1: cannot extract per-skill dispatch contract; running in main]",
    "[quoin-S-1: pollution dispatch unavailable; proceeding in current session]",
    "pollution_guard.py",
    "OVER|",
    "POLLUTION_THRESHOLD",
    'model: "opus"',
    "[autonomous]",
    "[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]",
]

# Per-skill distinctive token that proves the dispatch contract is skill-specific.
SKILL_DISTINCTIVE_TOKENS = {
    "architect": "repos-inventory.md",
    "plan": "architecture.md",
    "critic": "Target:",
    "revise": "critic-response",
    "review": "Branch:",
    "init_workflow": "project root",
    "discover": "project root",
    "specify": "spec.md",
    "security_review": "OWASP",
    "enrich": "enriched-prompt.md",
}


def _read_skill(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _extract_pollution_block(text: str) -> str:
    """Return the §0' block content (heading through last line before next H2)."""
    match = re.search(
        r"^## §0' Pollution dispatch \(execute after §0 / §0c if present — before skill body\).+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(0)


# ─── (a) Each target skill has exactly one §0' heading ───────────────────────

@pytest.mark.parametrize("skill", POLLUTION_TARGET_SKILLS)
def test_each_target_skill_has_pollution_heading(skill):
    text = _read_skill(skill)
    count = text.count(POLLUTION_HEADING)
    assert count == 1, (
        f"{skill}/SKILL.md contains the §0' heading {count} times (expected exactly 1). "
        f"Heading literal: {POLLUTION_HEADING!r}"
    )


# ─── (b) §0' appears BEFORE ## Session bootstrap (or ## Model requirement for architect) ─

@pytest.mark.parametrize("skill", POLLUTION_TARGET_SKILLS)
def test_pollution_heading_before_session_bootstrap(skill):
    text = _read_skill(skill)
    assert POLLUTION_HEADING in text, f"{skill}/SKILL.md missing §0' heading"
    p_idx = text.index(POLLUTION_HEADING)
    # discover uses ## Model requirement before ## Session bootstrap
    if skill == "discover":
        assert MODEL_REQ_HEADING in text, f"{skill}/SKILL.md missing ## Model requirement"
        mr_idx = text.index(MODEL_REQ_HEADING)
        assert p_idx < mr_idx, (
            f"{skill}/SKILL.md: §0' (pos={p_idx}) appears AFTER ## Model requirement (pos={mr_idx})"
        )
    else:
        assert SESSION_BOOTSTRAP_HEADING in text, f"{skill}/SKILL.md missing ## Session bootstrap"
        sb_idx = text.index(SESSION_BOOTSTRAP_HEADING)
        assert p_idx < sb_idx, (
            f"{skill}/SKILL.md: §0' (pos={p_idx}) appears AFTER ## Session bootstrap (pos={sb_idx})"
        )


# ─── (d) None of the 12 cheap-tier skills carry §0' ─────────────────────────

@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_no_cheap_tier_skill_has_pollution(skill):
    text = _read_skill(skill)
    assert POLLUTION_HEADING not in text, (
        f"{skill}/SKILL.md is cheap-tier but contains the §0' heading. "
        "S-1 architecture limits §0' to the 9 Opus-tier non-orchestrator skills."
    )


# ─── (e) Orchestrators do NOT carry §0' ──────────────────────────────────────

@pytest.mark.parametrize("skill", ORCHESTRATOR_SKILLS)
def test_no_orchestrator_has_pollution(skill):
    text = _read_skill(skill)
    assert POLLUTION_HEADING not in text, (
        f"{skill}/SKILL.md is an orchestrator but contains the §0' heading. "
        "Orchestrators spawn phases as subagents — §0' is excluded (per plan)."
    )


# ─── (f) Each §0' block contains required structural tokens ──────────────────

@pytest.mark.parametrize("skill", POLLUTION_TARGET_SKILLS)
@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_pollution_block_required_token(skill, token):
    text = _read_skill(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}/SKILL.md §0' block is empty (heading present but block not extracted)"
    assert token in block, (
        f"{skill}/SKILL.md §0' block missing required token: {token!r}"
    )


# ─── (g) Per-skill dispatch contract token present ───────────────────────────

@pytest.mark.parametrize("skill,token", sorted(SKILL_DISTINCTIVE_TOKENS.items()))
def test_pollution_block_per_skill_dispatch_token(skill, token):
    text = _read_skill(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}/SKILL.md §0' block is empty"
    assert token in block, (
        f"{skill}/SKILL.md §0' block missing per-skill dispatch contract token: {token!r}. "
        "This token proves the dispatch contract is skill-specific (not a copy-paste generic block)."
    )


# ─── (h) revise/SKILL.md SYNC: §0' is an allowed diff (revise has it, revise-fast does not) ─

def test_revise_pollution_sync_allowed():
    """revise has §0'; revise-fast does NOT — this is intentional per D-05."""
    revise_text = _read_skill("revise")
    revise_fast_text = _read_skill("revise-fast")
    assert POLLUTION_HEADING in revise_text, (
        "revise/SKILL.md missing §0' — T-04 insertion failed for revise"
    )
    assert POLLUTION_HEADING not in revise_fast_text, (
        "revise-fast/SKILL.md has §0' — it should NOT (revise-fast is cheap-tier with §0 model dispatch). "
        "If §0' was intentionally added to revise-fast, update this test and D-05 in the plan."
    )
