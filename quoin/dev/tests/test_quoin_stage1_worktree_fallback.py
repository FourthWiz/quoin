"""
Quoin Stage 1 — worktree-fallback structural tests for the §0 self-dispatch preamble.

This module enforces the static-structural invariants of the worktree-error
triage block added to the §0 fail-graceful path in all 15 cheap-tier SKILL.md
files (T-02 / T-03 of task subagent-dispatch-worktree-fallback).

Per Stage 1 plan D-03 and lesson 2026-04-23 LLM-replay non-determinism: this
file contains NO live LLM calls — only deterministic pathlib + string + regex
+ YAML parsing.

See also:
  - test_quoin_stage1_preamble.py      — 15-skill §0 structural invariants
  - test_quoin_stage1_recursion_abort.py — 15-skill abort-branch invariants
  - quoin/dev/verify_subagent_dispatch.md — manual verification procedures
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
# .workflow_artifacts/ivg-118-registration-manifest/current-plan.md. "pr"'s
# authoritative SKILL.md is (and remains) the Claude adapter copy, so it
# resolves correctly under the derived set exactly as it did under the prior
# hand-maintained literal.
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


# All 15 cheap-tier skills that carry the worktree-fallback block.
# 12 adapter skills + checkpoint + next_steps + sleep.
WORKTREE_FALLBACK_SKILLS = [
    "capture_insight",
    "cost_snapshot",
    "end_of_day",
    "end_of_task",
    "expand",
    "gate",
    "implement",
    "revise-fast",
    "rollback",
    "start_of_day",
    "triage",
    "weekly_review",
    "next_steps",
    "sleep",
]

SENTINEL_BEGIN = "<!-- §0-worktree-fallback-begin -->"
SENTINEL_END = "<!-- §0-worktree-fallback-end -->"

# Source-mutating skills requiring the §0-sidecar block (D-08 scope).
# Defined here (near the fallback-skill list) as the SINGLE source of truth so the
# artifact-only exclusion below and Tests 7-11 all reference the same set — no stale
# hand-copied literal. `pr` carries a sidecar (T-05) and so is included here; it is
# therefore excluded from the artifact-only fallback byte-equal set below.
# `workspace` (IVG-158 S-06) also byte-copies the §0-sidecar block from `pr` (D-02).
SOURCE_MUTATING_WORKTREE_SKILLS = {"implement", "rollback", "end_of_task", "pr", "workspace"}


def extract_worktree_block(skill_path: Path) -> str:
    """Extract the worktree-fallback block anchored by sentinel HTML comments."""
    text = skill_path.read_text(encoding="utf-8")
    begin_pos = text.find(SENTINEL_BEGIN)
    end_pos = text.find(SENTINEL_END)
    if begin_pos == -1 or end_pos == -1:
        return ""
    return text[begin_pos : end_pos + len(SENTINEL_END)]


# -------------------------------------------------------------------------
# Test 1 — error classification section present in every §0 slice.
# Silent-deletion regression catcher.
# -------------------------------------------------------------------------
@pytest.mark.parametrize("skill", WORKTREE_FALLBACK_SKILLS)
def test_error_class_triage_section_present(skill):
    """§0 slice must contain both 'Error classification:' and 'Worktree-class:'."""
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert "Error classification:" in slice_text, (
        f"{skill}/SKILL.md §0 missing 'Error classification:' section — "
        "worktree-fallback triage block was deleted or not applied."
    )
    assert "Worktree-class:" in slice_text, (
        f"{skill}/SKILL.md §0 missing 'Worktree-class:' classification entry — "
        "worktree error class was removed from the triage block."
    )


# -------------------------------------------------------------------------
# Test 2 — option labels present per T-01 variant.
# Uses the T-01 YAML stub from insights-2026-05-15.md at collection time.
# -------------------------------------------------------------------------
def _load_t01_stub() -> dict:
    """Load the T-01 YAML stub from the daily insights file.

    Returns a dict with keys worktree_a_skippable and worktree_b_available,
    or an empty dict if the stub is not present (pre-T-01 run).
    """
    insights_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / ".workflow_artifacts"
        / "memory"
        / "daily"
        / "insights-2026-05-15.md"
    )
    if not insights_path.exists():
        return {}
    text = insights_path.read_text(encoding="utf-8")
    # Find the YAML block in the T-01 spike note
    m = re.search(
        r"worktree_a_skippable:\s*(true|false)\s*\n"
        r"worktree_b_available:\s*(true|false)",
        text,
    )
    if not m:
        return {}
    return {
        "worktree_a_skippable": m.group(1) == "true",
        "worktree_b_available": m.group(2) == "true",
    }


_T01_STUB = _load_t01_stub()


@pytest.mark.parametrize("skill", WORKTREE_FALLBACK_SKILLS)
def test_option_labels_present(skill):
    """§0 slice must contain the option labels matching the T-01 variant."""
    slice_text = extract_preamble_block(skill_md_path(skill))

    # All variants must contain option (c)
    assert "proceed-current-tier" in slice_text, (
        f"{skill}/SKILL.md §0 missing 'proceed-current-tier' option label — "
        "required in all worktree-fallback variants."
    )

    if not _T01_STUB:
        pytest.skip("T-01 YAML stub not found in insights-2026-05-15.md; skipping variant-conditional assertions.")

    worktree_a_skippable = _T01_STUB.get("worktree_a_skippable", False)
    worktree_b_available = _T01_STUB.get("worktree_b_available", False)

    # Variant A-2 and B: assert retry-no-isolation
    if worktree_a_skippable:
        assert "retry-no-isolation" in slice_text, (
            f"{skill}/SKILL.md §0 missing 'retry-no-isolation' option label — "
            "required when worktree_a_skippable=true (Variant A-2 or B)."
        )

    # Variant B only: assert retry-with-base
    if worktree_b_available:
        assert "retry-with-base" in slice_text, (
            f"{skill}/SKILL.md §0 missing 'retry-with-base' option label — "
            "required when worktree_b_available=true (Variant B only)."
        )


# -------------------------------------------------------------------------
# Test 3 — extended warning string documented in every §0 slice.
# Guarantees the error-class classification line survives future edits.
# -------------------------------------------------------------------------
@pytest.mark.parametrize("skill", WORKTREE_FALLBACK_SKILLS)
def test_extended_warning_string_documented(skill):
    """§0 slice must contain the classification-line template 'error-class=worktree'."""
    slice_text = extract_preamble_block(skill_md_path(skill))
    assert "error-class=worktree" in slice_text, (
        f"{skill}/SKILL.md §0 missing 'error-class=worktree' in the extended "
        "warning classification line template."
    )


# -------------------------------------------------------------------------
# Test 4 — bare warning string present in all 15 skills.
# Extends coverage from the existing 12-skill subset in recursion_abort.py
# to the full 15-skill patched set (including checkpoint, sleep, next_steps).
# -------------------------------------------------------------------------
@pytest.mark.parametrize("skill", WORKTREE_FALLBACK_SKILLS)
def test_bare_warning_present_in_wider_skillset(skill):
    """§0 slice must contain the bare I-01 fail-OPEN warning verbatim."""
    slice_text = extract_preamble_block(skill_md_path(skill))
    expected = "[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]"
    assert expected in slice_text, (
        f"{skill}/SKILL.md §0 missing bare fail-OPEN warning: {expected!r}. "
        "This warning must appear verbatim in the Other-class path so downstream "
        "string-matching consumers are unaffected by the worktree-fallback expansion."
    )


# -------------------------------------------------------------------------
# Test 5 — inserted block byte-equal across the 12 artifact-only skills.
# CI-enforced byte-equality guardrail per lesson-2026-05-11 and D-09.
# Sentinel comments are the SOLE extraction anchor; prose-anchor fallback removed.
#
# NOTE: The 4 source-mutating skills (implement, rollback, end_of_task, pr) now carry
# an additional §0-sidecar block inside the worktree-fallback sentinel (D-08,
# nested-git-worktree-dispatch task). They are intentionally distinct and are
# checked separately by test_sidecar_block_byte_equal_across_source_mutating_skills.
# (pr is not in WORKTREE_FALLBACK_SKILLS, so it never appears in this list regardless;
# referencing the shared constant keeps the exclusion in lock-step with Tests 7-11.)
# -------------------------------------------------------------------------
_ARTIFACT_ONLY_WORKTREE_FALLBACK_SKILLS = [
    s for s in WORKTREE_FALLBACK_SKILLS
    if s not in SOURCE_MUTATING_WORKTREE_SKILLS
]


def test_inserted_block_byte_equal_across_skills():
    """Worktree-fallback block must be byte-identical across the 12 artifact-only SKILL.md files."""
    blocks = {}
    missing = []
    for skill in _ARTIFACT_ONLY_WORKTREE_FALLBACK_SKILLS:
        path = skill_md_path(skill)
        block = extract_worktree_block(path)
        if not block:
            missing.append(skill)
        else:
            blocks[skill] = block

    assert not missing, (
        f"Sentinel comments not found in: {missing}. "
        f"Expected '{SENTINEL_BEGIN}' ... '{SENTINEL_END}' in §0 block."
    )

    canonical_skill = _ARTIFACT_ONLY_WORKTREE_FALLBACK_SKILLS[0]
    canonical_block = blocks[canonical_skill]
    mismatches = []
    for skill, block in blocks.items():
        if block != canonical_block:
            mismatches.append(skill)

    assert not mismatches, (
        f"Worktree-fallback block is NOT byte-identical in: {mismatches}. "
        f"Canonical block from '{canonical_skill}'. "
        "Re-run the T-03 byte-copy patcher to restore byte-equality."
    )


# -------------------------------------------------------------------------
# Test 6 — worktree-retry sentinel grammar pinned to first-line position.
# Only asserts for skills where 'retry-no-isolation' is present (Variant A-2 or B).
# -------------------------------------------------------------------------
@pytest.mark.parametrize("skill", WORKTREE_FALLBACK_SKILLS)
def test_worktree_retry_grammar_pinned(skill):
    """For skills with retry-no-isolation, §0 must describe [worktree-retry] as FIRST LINE."""
    slice_text = extract_preamble_block(skill_md_path(skill))

    # Only assert for skills carrying the retry-no-isolation option
    if "retry-no-isolation" not in slice_text:
        pytest.skip(f"{skill}: 'retry-no-isolation' not present (Variant A-1); skip grammar check.")

    # Find [worktree-retry] and assert FIRST LINE / first line appears within 200 chars
    wt_pos = slice_text.find("[worktree-retry]")
    assert wt_pos != -1, (
        f"{skill}/SKILL.md §0 has 'retry-no-isolation' but missing '[worktree-retry]' sentinel."
    )

    # Check that "FIRST LINE" or "first line" appears within 200 chars of [worktree-retry]
    window = slice_text[max(0, wt_pos - 100) : wt_pos + 300]
    has_first_line = re.search(r"first line", window, re.IGNORECASE) is not None
    assert has_first_line, (
        f"{skill}/SKILL.md §0: '[worktree-retry]' sentinel exists but its position-anchor "
        "rule ('first line' / 'FIRST LINE') was not found within 200 characters. "
        "The grammar must describe [worktree-retry] as a first-line sentinel, not substring-anywhere."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests 7-11 — D-08 WorktreeCreate hook: sidecar block invariants (nested-git-worktree-dispatch)
# ─────────────────────────────────────────────────────────────────────────────

# SOURCE_MUTATING_WORKTREE_SKILLS defined once near the top (single source of truth;
# now includes "pr" per T-05). Tests 7-11 below parametrize over it.
SIDECAR_SENTINEL_BEGIN = "<!-- §0-sidecar-begin -->"
SIDECAR_SENTINEL_END = "<!-- §0-sidecar-end -->"


def extract_sidecar_block(skill_path: Path) -> str:
    """Extract the §0-sidecar block anchored by sidecar sentinel comments."""
    text = skill_path.read_text(encoding="utf-8")
    begin_pos = text.find(SIDECAR_SENTINEL_BEGIN)
    end_pos = text.find(SIDECAR_SENTINEL_END)
    if begin_pos == -1 or end_pos == -1:
        return ""
    return text[begin_pos : end_pos + len(SIDECAR_SENTINEL_END)]


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — sidecar block byte-identical across source-mutating skills
# ─────────────────────────────────────────────────────────────────────────────
def test_sidecar_block_byte_equal_across_source_mutating_skills():
    """Sidecar block must be byte-identical across implement, rollback, end_of_task."""
    blocks = {}
    missing = []
    for skill in sorted(SOURCE_MUTATING_WORKTREE_SKILLS):
        path = skill_md_path(skill)
        block = extract_sidecar_block(path)
        if not block:
            missing.append(skill)
        else:
            blocks[skill] = block

    assert not missing, (
        f"Sidecar sentinel comments not found in: {missing}. "
        f"Expected '{SIDECAR_SENTINEL_BEGIN}' ... '{SIDECAR_SENTINEL_END}' in §0 block."
    )

    skills_list = sorted(SOURCE_MUTATING_WORKTREE_SKILLS)
    canonical_skill = skills_list[0]
    canonical_block = blocks[canonical_skill]
    mismatches = []
    for skill in skills_list[1:]:
        if blocks[skill] != canonical_block:
            mismatches.append(skill)

    assert not mismatches, (
        f"Sidecar block is NOT byte-identical in: {mismatches}. "
        f"Canonical block from '{canonical_skill}'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — sidecar block absent from artifact-only skills
# ─────────────────────────────────────────────────────────────────────────────
ARTIFACT_ONLY_SKILLS = [s for s in WORKTREE_FALLBACK_SKILLS if s not in SOURCE_MUTATING_WORKTREE_SKILLS]


@pytest.mark.parametrize("skill", ARTIFACT_ONLY_SKILLS)
def test_sidecar_block_absent_from_artifact_only_skills(skill):
    """Artifact-only skills must NOT have the §0-sidecar block."""
    block = extract_sidecar_block(skill_md_path(skill))
    assert block == "", (
        f"{skill}/SKILL.md contains §0-sidecar sentinel block but is an artifact-only skill. "
        "Only implement/rollback/end_of_task should have this block."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — sidecar invocation present in source-mutating skills
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("skill", sorted(SOURCE_MUTATING_WORKTREE_SKILLS))
def test_sidecar_invocation_present(skill):
    """Source-mutating §0 must contain dispatch_sidecar.py invocation."""
    block = extract_sidecar_block(skill_md_path(skill))
    assert "dispatch_sidecar.py" in block, (
        f"{skill}/SKILL.md §0-sidecar block missing 'dispatch_sidecar.py' invocation. "
        "The block must call the sidecar writer before Agent dispatch."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — Phase 2 retry at cheap tier documented in source-mutating skills
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("skill", sorted(SOURCE_MUTATING_WORKTREE_SKILLS))
def test_phase2_retry_documented(skill):
    """Source-mutating §0 must document the cheap-tier retry without isolation."""
    block = extract_sidecar_block(skill_md_path(skill))
    assert "Phase 2" in block or "phase 2" in block.lower(), (
        f"{skill}/SKILL.md §0-sidecar block missing Phase 2 retry documentation."
    )
    assert "without isolation" in block or "WITHOUT isolation" in block, (
        f"{skill}/SKILL.md §0-sidecar block missing 'without isolation' language for Phase 2."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — no parent-cd artifacts remain in source-mutating §0 blocks
# ─────────────────────────────────────────────────────────────────────────────
_RETIRED_ARTIFACTS = [
    "ORIG_DIR",
    "/tmp/quoin_orig_dir_",
    "[dispatch-cwd-pivot=",
    "DISPATCH_CWD",
]


@pytest.mark.parametrize("skill", sorted(SOURCE_MUTATING_WORKTREE_SKILLS))
def test_no_parent_cd_artifacts_remain(skill):
    """Source-mutating §0 must not contain retired parent-cd design artifacts."""
    # Check the full §0 block for retired artifacts
    slice_text = extract_preamble_block(skill_md_path(skill))
    for artifact in _RETIRED_ARTIFACTS:
        assert artifact not in slice_text, (
            f"{skill}/SKILL.md §0 contains retired parent-cd artifact: {artifact!r}. "
            "The parent-cd design was retired in round-4 pivot. Remove this carryover."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 12 — T-25 (autonomous-run-mode, M-3): autonomous fail-OPEN clause present
# on the worktree-class path, for both byte-identity classes. The clause is
# conditional on the `[autonomous]` sentinel — non-autonomous behavior (the
# AskUserQuestion prompt / the Phase 2 retry) is preserved unchanged; see D-08.
# Reachable-under-autonomous skills per the Stage-1 plan: revise-fast, gate
# (artifact-only class) and implement, end_of_task (source-mutating class).
# The clause is propagated to every member of each byte-identity class (not
# only the reachable ones) so Tests 5 and 7 above stay green.
# ─────────────────────────────────────────────────────────────────────────────
_AUTONOMOUS_CLAUSE_MARKER = "Autonomous fail-OPEN"
_REACHABLE_ARTIFACT_ONLY_SKILLS = ["revise-fast", "gate"]
_REACHABLE_SOURCE_MUTATING_SKILLS = ["implement", "end_of_task"]


@pytest.mark.parametrize("skill", _REACHABLE_ARTIFACT_ONLY_SKILLS)
def test_autonomous_clause_present_artifact_only_reachable(skill):
    """Worktree-class branch must carry the [autonomous]-conditional fail-OPEN clause."""
    block = extract_worktree_block(skill_md_path(skill))
    assert _AUTONOMOUS_CLAUSE_MARKER in block, (
        f"{skill}/SKILL.md worktree-fallback block missing the T-25 autonomous "
        "fail-OPEN clause on the Worktree-class branch."
    )
    assert "do NOT call\n      AskUserQuestion" in block, (
        f"{skill}/SKILL.md worktree-fallback block must document skipping "
        "AskUserQuestion under the autonomous branch."
    )
    assert "`[autonomous]` sentinel" in block, (
        f"{skill}/SKILL.md worktree-fallback block clause must be conditional on the "
        "`[autonomous]` sentinel."
    )


@pytest.mark.parametrize("skill", _REACHABLE_SOURCE_MUTATING_SKILLS)
def test_autonomous_clause_present_source_mutating_reachable(skill):
    """Sidecar block's Phase 2 retry must document the [autonomous] fail-OPEN guarantee."""
    block = extract_sidecar_block(skill_md_path(skill))
    assert _AUTONOMOUS_CLAUSE_MARKER in block, (
        f"{skill}/SKILL.md §0-sidecar block missing the T-25 autonomous fail-OPEN "
        "clause on the Phase 2 retry (STEP C)."
    )
    assert "`[autonomous]`" in block and "sentinel" in block, (
        f"{skill}/SKILL.md §0-sidecar block clause must be conditional on the "
        "`[autonomous]` sentinel."
    )


def test_autonomous_clause_does_not_alter_nonautonomous_option_labels():
    """Non-autonomous path must still be reachable: proceed-current-tier option
    label is preserved verbatim in the artifact-only worktree-fallback block
    (the autonomous clause only adds a preceding conditional branch)."""
    block = extract_worktree_block(skill_md_path("gate"))
    assert "(c) `proceed-current-tier`" in block, (
        "gate/SKILL.md: non-autonomous AskUserQuestion option (c) proceed-current-tier "
        "must remain byte-present after the T-25 autonomous clause insertion."
    )
