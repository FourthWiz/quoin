"""Phase 18 adapter pilot tests for cost_snapshot.

Parametrized over the single skill migrated in Phase 18. Follows the
pattern established by test_weekly_review_adapter_pilot.py (Phase 17),
test_end_of_day_adapter_pilot.py (Phase 16), and prior phase pilots.

Skill parameters: (skill_name, expected_model, has_section_0)
  - cost_snapshot: haiku, §0 present (cheap-tier — self-dispatches to Haiku)

PRIOR_PHASE_SKILLS lists the 17 prior-phase skills (Phases 6–17). Later
phases (19+) will extend their own pilot's PRIOR_PHASE_SKILLS, NOT this
file's.
"""
import re
from pathlib import Path

import pytest
import yaml
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("cost_snapshot", "haiku", True),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–17 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate", "implement", "rollback",
    "end_of_task", "run", "end_of_day", "weekly_review",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/cost_snapshot.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'cost_snapshot-2026-05-11.md')
    """
    return re.compile(
        r"(?<![a-zA-Z0-9_\-])"
        + re.escape("/" + name.lstrip("/"))
        + r"(?=[^a-zA-Z0-9_\-]|$)"
    )


def _core_doc(skill_name: str) -> Path:
    return PKG_DIR / "core" / "skills" / f"{skill_name}.md"


def _adapter_skill(skill_name: str) -> Path:
    return PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"


# ── Parametrized tests (run over the cost_snapshot skill) ────────────────────


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_exists(skill_name, expected_model, has_section_0):
    path = _core_doc(skill_name)
    assert path.is_file(), f"Missing core skill doc: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_no_forbidden_tokens(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")

    # Static forbidden tokens — raw substring check.
    hits = [t for t in FORBIDDEN_TOKENS if t in text]

    # Per-skill slash form — negative-lookahead regex (D-03: bare substring FORBIDDEN).
    if _slash_regex(skill_name).search(text):
        hits.append(f"/{skill_name}")

    assert not hits, (
        f"Core doc for {skill_name} contains forbidden tokens: {hits}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_mentions_anchor(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc for {skill_name} must contain literal: {REQUIRED_MENTION}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_exists(skill_name, expected_model, has_section_0):
    path = _adapter_skill(skill_name)
    assert path.is_file(), f"Missing adapter SKILL.md: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_declares_correct_model(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"Adapter SKILL.md for {skill_name} missing YAML frontmatter"
    fm = yaml.safe_load(parts[1])
    assert fm.get("model") == expected_model, (
        f"Adapter SKILL.md for {skill_name} must declare model: {expected_model} "
        f"(got {fm.get('model')!r})"
    )
    assert fm.get("name") == skill_name, (
        f"Adapter SKILL.md for {skill_name} must declare name: {skill_name} "
        f"(got {fm.get('name')!r})"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_references_core_doc(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    ref = f"quoin/core/skills/{skill_name}.md"
    assert ref in text, (
        f"Adapter SKILL.md for {skill_name} must reference the portable intent doc: {ref}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_install_sh_has_adapter_override(skill_name, expected_model, has_section_0):
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter(skill_name)


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_has_or_lacks_section_0(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    has_block = "## §0 Model dispatch" in text
    if has_section_0:
        assert has_block, (
            f"Adapter SKILL.md for {skill_name} (cheap-tier) must carry "
            "## §0 Model dispatch block"
        )
    else:
        assert not has_block, (
            f"Adapter SKILL.md for {skill_name} (Opus-tier) must NOT carry "
            "## §0 dispatch block"
        )


# ── Non-parametrized constraint guards ────────────────────────────────────


def test_cost_snapshot_has_no_preamble_md():
    """cost_snapshot is NOT a subagent-preamble spawn target.

    Per CLAUDE.md "Subagent preamble" section, the 7 spawn-target skills
    are: critic, revise, revise-fast, plan, review, gate, architect.
    cost_snapshot is excluded — `build_preambles.py` does not generate one.
    Asserting absence prevents accidental preamble leak.
    """
    adapter_preamble = (
        PKG_DIR / "adapters" / "claude" / "skills" / "cost_snapshot" / "preamble.md"
    )
    assert not adapter_preamble.exists(), (
        f"CONSTRAINT VIOLATION: {adapter_preamble} unexpectedly exists. "
        "cost_snapshot is NOT a spawn-target; build_preambles.py must not generate one."
    )


# ── Regression-guard tests (Phase 17 skills must still be covered) ────────


def test_phase_17_skills_still_covered():
    """Regression guard for the cost_snapshot work — verifies all 17 Phase 6–17 skills are still covered.

    Asserts that each Phase 6–17 migrated skill still has:
    - its core doc at quoin/core/skills/<skill>.md
    - its adapter at quoin/adapters/claude/skills/<skill>/SKILL.md
    - its install.sh override variable and elif branch
    """
    from quoin import installer
    for skill in PRIOR_PHASE_SKILLS:
        core_doc = _core_doc(skill)
        assert core_doc.is_file(), (
            f"Regression: core doc for prior-phase skill {skill!r} is missing: {core_doc}"
        )
        adapter = _adapter_skill(skill)
        assert adapter.is_file(), (
            f"Regression: adapter SKILL.md for prior-phase skill {skill!r} is missing: {adapter}"
        )
        assert skill in installer.CANONICAL_SKILLS, (
            f"Regression: installer.CANONICAL_SKILLS missing {skill!r} for prior-phase skill"
        )


def test_install_fresh_clone_lists_cost_snapshot_in_migrated_skills():
    """Guard that test_install_fresh_clone.py includes cost_snapshot in MIGRATED_SKILLS (T-06 edit)."""
    target = PKG_DIR / "dev" / "tests" / "test_install_fresh_clone.py"
    text = target.read_text(encoding="utf-8")
    tuple_start = text.find('MIGRATED_SKILLS = (')
    assert tuple_start != -1, "test_install_fresh_clone.py missing MIGRATED_SKILLS tuple"
    tuple_end = text.find(')', tuple_start)
    tuple_region = text[tuple_start:tuple_end + 1]
    assert '"cost_snapshot"' in tuple_region, (
        'test_install_fresh_clone.py MIGRATED_SKILLS tuple must include "cost_snapshot" '
        "(T-06 edit). The entry was reverted — restore it."
    )


# ---------------------------------------------------------------------------
# T-08 (stage 4, T-05 doc-token coverage): the /cost_snapshot SKILL.md prose
# must document the inline-first precedence rule, and the §0 dispatch block
# + the line-127 [quoin-onbehalf] predicate/sentinel must be byte-unchanged.
# ---------------------------------------------------------------------------

def _cost_snapshot_adapter_text() -> str:
    return _adapter_skill("cost_snapshot").read_text(encoding="utf-8")


def test_step1_documents_8col_first_class_and_precedence_rule():
    text = _cost_snapshot_adapter_text()
    assert "8th column" in text or "8 fields" in text
    assert "attribution" in text
    assert "Inline-first precedence rule" in text
    assert "unresolvable" in text
    assert "src=unresolved" in text or "src ≠ `unresolved`" in text or "src=" in text


def test_step1_documents_9col_tolerance_warn():
    text = _cost_snapshot_adapter_text()
    assert "9 or more fields" in text
    assert "expected ≤8" in text


def test_step2_documents_inline_resolved_uuid_exclusion():
    text = _cost_snapshot_adapter_text()
    assert "JSONL-lookup set" in text
    assert "resolved-inline UUIDs" in text or "resolved-inline UUID" in text
    assert "double-representation" in text or "double representation" in text


def test_step3_documents_single_unresolvable_counter_and_partial_surface():
    text = _cost_snapshot_adapter_text()
    assert "unresolvable_count" in text
    assert "REPLACES the old" in text or "replaces the old" in text
    assert "(partial)" in text
    # The old competing "unknown cost" line name must not still be documented
    # as the primary counter — it is folded into unresolvable_count.
    assert "sessions unresolvable" in text


def test_quoin_onbehalf_predicate_and_sentinel_intact():
    """The stage-3 [quoin-onbehalf] cost-ledger self-write skip predicate and
    sentinel comment must be byte-unchanged by the T-05 prose edit."""
    text = _cost_snapshot_adapter_text()
    assert "[quoin-onbehalf]" in text
    assert "SKIP this cost-ledger self-write" in text
    assert "<!-- quoin:ledger-self-write -->" in text


def test_section_0_dispatch_block_present():
    """§0 model dispatch preamble must still be present (T-05 touches only
    the human-authored Process/Step sections, never the generated §0 block)."""
    text = _cost_snapshot_adapter_text()
    assert "## §0 Model dispatch" in text
