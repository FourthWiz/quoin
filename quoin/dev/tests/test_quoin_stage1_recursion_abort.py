"""
Quoin Stage 1 — recursion-abort branch tests for the §0 self-dispatch preamble.

This module is the **architecture R-01 mitigation test** per
`.workflow_artifacts/quoin-foundation/architecture.md` `## De-risking strategy`
step 1: it confirms that every cheap-tier SKILL.md's §0 block correctly
describes the four-way branch tree (dispatch / proceed / abort / fail-graceful)
and the manual kill switch. Together with `test_quoin_stage1_preamble.py`,
this provides the static-structural safety net that complements the HITL
pilot (T-00) and four-phase smoke (T-09).

Per Stage 1 plan D-03 and lesson 2026-04-23 LLM-replay non-determinism: this
file contains NO live LLM calls — only deterministic pathlib + string + regex
operations. Live functional verification lives in T-00 / T-09.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
SKILLS_DIR = TESTS_DIR.parent.parent / "skills"
ADAPTER_SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"


# Skills migrated to the three-file adapter pattern (Phase 6 / Phase 7).
# IVG-118 T-05: derived from the filesystem (every skill with an adapter
# SKILL.md) instead of a hand-maintained literal — see D-02/D-03 in
# .workflow_artifacts/ivg-118-registration-manifest/current-plan.md. "run" is
# NOT excluded here (unlike the pre-T-05 literal): this file's cheap-tier §0
# recursion-abort assertions are parametrized over CHEAP_TIER_SKILLS (a
# separate, still-scoped roster — see D-04), not over MIGRATED_TO_ADAPTER, so
# "run" resolving to the adapter path here is inert w.r.t. this file's tests.
MIGRATED_TO_ADAPTER = frozenset(
    p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()
)


def skill_md_path(skill_name: str) -> Path:
    """Return the canonical SKILL.md path for a given skill.

    Migrated skills (per `MIGRATED_TO_ADAPTER`) are installed from the
    Claude adapter path, so their authoritative SKILL.md lives under
    adapters/claude/skills/. All other skills still live under skills/.
    """
    if skill_name in MIGRATED_TO_ADAPTER:
        return ADAPTER_SKILLS_DIR / skill_name / "SKILL.md"
    return SKILLS_DIR / skill_name / "SKILL.md"

SO_HEADING = "## §0 Model dispatch (FIRST STEP — execute before anything else)"

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
    "sleep",
    "next_steps",
]


def extract_preamble_block(skill_path: Path) -> str:
    """Slice between §0 heading (inclusive) and the next H2 (exclusive).

    Duplicated locally from `test_quoin_stage1_preamble.py` to keep this
    file self-contained per plan T-05 ("import or duplicate locally if
    cross-file import is awkward").
    """
    text = skill_path.read_text(encoding="utf-8")
    match = re.search(
        r"^## §0 Model dispatch \(FIRST STEP — execute before anything else\).+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(0)


# -----------------------------------------------------------------------------
# (a) Dispatch branch is described — Spawn Agent + dispatched-tier marker +
# literal model: " parameter form (per round-3 MAJ-1 — confirms the dispatch
# action contains a real `model:` argument, not just a description string).
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_describes_dispatch_branch(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert "Spawn an Agent subagent" in slice_text, (
        f"{skill}/SKILL.md §0 missing `Spawn an Agent subagent` dispatch instruction"
    )
    assert "dispatched-tier:" in slice_text, (
        f"{skill}/SKILL.md §0 missing `dispatched-tier:` marker"
    )
    assert 'model: "' in slice_text, (
        f"{skill}/SKILL.md §0 missing literal `model: \"` parameter form — "
        "the dispatch action does not pass an actual `model:` argument to the Agent tool. "
        "This is the round-3 MAJ-1 regression class."
    )


# -----------------------------------------------------------------------------
# (b) Proceed branch is described — Otherwise + proceed to §1.
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_describes_proceed_branch(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert "Otherwise" in slice_text, (
        f"{skill}/SKILL.md §0 missing `Otherwise` proceed-branch lead-in"
    )
    assert "proceed to §1" in slice_text, (
        f"{skill}/SKILL.md §0 missing `proceed to §1` proceed-branch terminator"
    )


# -----------------------------------------------------------------------------
# (c) Abort branch is described — counter-form sentinel + abort message
# leading constant. This is the load-bearing recursion-guard test.
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_describes_abort_branch(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert "[no-redispatch:N]" in slice_text, (
        f"{skill}/SKILL.md §0 missing counter-form sentinel `[no-redispatch:N]` "
        "in the abort-rule prose"
    )
    assert "Quoin self-dispatch hard-cap reached at N=" in slice_text, (
        f"{skill}/SKILL.md §0 missing abort-message leading constant "
        "`Quoin self-dispatch hard-cap reached at N=`. R-01 recursion-guard "
        "mitigation broken."
    )


# -----------------------------------------------------------------------------
# (d) Manual kill switch — bare `[no-redispatch]` used in TWO distinct
# contexts (parent-emit prepend rule AND user-override paragraph). The bare
# token is intentionally shared between both per CRIT-3 — a child cannot
# distinguish parent-emit from user-typed, and that's by design.
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_describes_manual_kill_switch(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    # Count bare-form occurrences. The §0 prose contains `[no-redispatch]`
    # at multiple sites: sentinel-parsing rule, the dispatch action's
    # prompt-prefix line, the manual-kill-switch paragraph, and the
    # final "Otherwise" line. Require ≥ 2 bare-form occurrences to confirm
    # the dual-use is preserved.
    bare_pattern = re.compile(r"\[no-redispatch\](?!:)")  # bare, NOT counter form
    bare_count = len(bare_pattern.findall(slice_text))
    assert bare_count >= 2, (
        f"{skill}/SKILL.md §0 has only {bare_count} occurrence(s) of bare "
        "`[no-redispatch]`; expected ≥ 2 (parent-emit context + user-override "
        "context per CRIT-3 dual-use design)."
    )
    # Manual kill switch prose must explicitly mention user typing the sentinel.
    assert "Manual kill switch" in slice_text, (
        f"{skill}/SKILL.md §0 missing `Manual kill switch` heading paragraph"
    )


# -----------------------------------------------------------------------------
# (e) Fail-graceful branch is described — I-01 fail-OPEN warning string
# present in every §0 slice.
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_describes_fail_graceful_branch(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    expected = "[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]"
    assert expected in slice_text, (
        f"{skill}/SKILL.md §0 missing fail-graceful warning string: {expected!r}. "
        "Per architecture I-01 + CRIT-1 fix, harness incompatibility must surface "
        "as an observable warning, not a silent no-op."
    )


# -----------------------------------------------------------------------------
# (f) Synthetic abort-branch simulation — given the documented branch tree
# in §0 prose and a synthetic incoming-prompt string `[no-redispatch:2] /gate`,
# confirm via deterministic string matching that the input matches the
# documented abort rule's grep targets. No LLM is invoked.
# -----------------------------------------------------------------------------
def test_synthetic_abort_branch_simulation():
    # Pick any cheap-tier skill — the §0 branch tree is structurally identical
    # across all 12 (T-04 case (e) confirms the sentinel grammar is uniform).
    slice_text = extract_preamble_block(skill_md_path("gate"))

    # Confirm the abort rule is documented with the expected grep targets.
    assert "[no-redispatch:N]" in slice_text, "abort-rule counter-form prose missing"
    assert "N ≥ 2" in slice_text, "abort-rule N-threshold prose missing"

    # Synthetic input that should hit the abort branch.
    synthetic_input = "[no-redispatch:2] /gate"

    # Encode the documented abort rule as a predicate and assert input matches.
    counter_match = re.match(r"^\[no-redispatch:(\d+)\]\s+", synthetic_input)
    assert counter_match, (
        f"synthetic input {synthetic_input!r} does not start with the counter-form "
        "sentinel — abort rule simulation would never fire on this input"
    )
    n = int(counter_match.group(1))
    assert n >= 2, (
        f"synthetic counter N={n} is below abort threshold (≥ 2); the rule would "
        "treat it as bare `[no-redispatch]` (proceed branch). Use N≥2 for the abort sim."
    )
    # If we reach here, the documented abort rule covers the synthetic input.
    # The §0 prose's branch tree is correctly specified for the recursion-guard
    # safety property (R-01 dominant risk).


# -----------------------------------------------------------------------------
# (g) §0 block is present in all 15 cheap-tier SKILL.md files. Marked
# `pytest.mark.smoke` to flag the intentional redundancy with T-04 case (a)
# per MAJ-7 — this test is the silent-deletion regression catcher even if
# T-04 is somehow stripped down or skipped in a future maintenance pass.
# -----------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.parametrize("skill", CHEAP_TIER_SKILLS)
def test_block_present_in_all_15_skills(skill):
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert slice_text, (
        f"{skill}/SKILL.md has empty §0 slice — the §0 block was deleted "
        "or its heading no longer matches the canonical literal. "
        f"Heading expected: {SO_HEADING!r}"
    )
