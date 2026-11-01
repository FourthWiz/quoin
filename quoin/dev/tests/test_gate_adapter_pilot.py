"""Phase 11 adapter pilot tests for gate.

Parametrized over the single skill migrated in Phase 11. Follows the
pattern established by test_architecture_planning_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - gate: sonnet, has §0
"""
import re
from pathlib import Path

import pytest
import yaml
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("gate", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–10 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/gate.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'gate-2026-05-09.md')
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


# ── Parametrized tests (run over the gate skill) ───────────────────────────


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


def test_preamble_present_for_gate():
    """R-02 guard: quoin/adapters/claude/skills/gate/preamble.md must remain at this path.

    The installer copies it to ~/.claude/skills/gate/preamble.md for
    cache-warming. Deleting or moving it would break the §0 model dispatch
    cost-guardrail and audit-log preamble deploy.
    """
    preamble = PKG_DIR / "adapters" / "claude" / "skills" / "gate" / "preamble.md"
    assert preamble.is_file(), (
        "CONSTRAINT VIOLATION: quoin/adapters/claude/skills/gate/preamble.md was deleted or moved. "
        "This file MUST remain at this path — install.sh deploys it for cache-warming."
    )


# ── Regression-guard tests (Phase 10 skills must still be covered) ────────


def test_phase_10_skills_still_covered():
    """Guard against accidental regressions in Phase 11 work.

    Asserts that each Phase 6–10 migrated skill still has:
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


# ── MAJ-2 regression guards (T-08 edits must stay intact) ─────────────────


def test_detection_rule_consistency_lists_gate_at_adapter_path():
    """Guard that test_detection_rule_consistency.py lists gate at the adapter path.

    If a future maintainer reverts the T-08 Group D edit, this pilot test
    fires before the sibling test does.
    """
    target = PKG_DIR / "dev" / "tests" / "test_detection_rule_consistency.py"
    text = target.read_text(encoding="utf-8")
    assert 'ADAPTER_SKILLS_DIR / "gate" / "SKILL.md"' in text, (
        "test_detection_rule_consistency.py must list gate at the adapter path "
        "(T-08 Group D). The entry was reverted — restore it."
    )
    # Check that the bare (non-adapter) form is absent. Use regex to distinguish
    # SKILLS_DIR from ADAPTER_SKILLS_DIR (the latter contains the former as substring).
    assert not re.search(r'(?<!ADAPTER_)SKILLS_DIR\s*/\s*"gate"\s*/\s*"SKILL\.md"', text), (
        "test_detection_rule_consistency.py must NOT list gate at the bare "
        "SKILLS_DIR path. The T-08 Group D repoint was reverted — restore it."
    )


def test_install_fresh_clone_lists_gate_in_migrated_skills():
    """Guard that test_install_fresh_clone.py includes gate in MIGRATED_SKILLS.

    If a future maintainer reverts the T-08 Group E edit, this pilot test
    fires before the sibling test does.
    """
    target = PKG_DIR / "dev" / "tests" / "test_install_fresh_clone.py"
    text = target.read_text(encoding="utf-8")
    # Find the MIGRATED_SKILLS tuple region and check gate is inside it.
    tuple_start = text.find('MIGRATED_SKILLS = (')
    assert tuple_start != -1, (
        "test_install_fresh_clone.py missing MIGRATED_SKILLS tuple — "
        "unexpected test structure change"
    )
    tuple_end = text.find(')', tuple_start)
    tuple_region = text[tuple_start:tuple_end + 1]
    assert '"gate"' in tuple_region, (
        "test_install_fresh_clone.py MIGRATED_SKILLS tuple must include \"gate\" "
        "(T-08 Group E). The entry was reverted — restore it."
    )


# ── IVG-70: Branch-hygiene gate check assertions ──────────────────────────


def test_gate_adapter_standard_gate_has_branch_hygiene():
    """IVG-70 T-08 AC-1: Standard gate checklist must contain the branch hygiene check."""
    text = _adapter_skill("gate").read_text(encoding="utf-8")
    # Both Standard and Full gate sections must reference branch_hygiene.py
    assert "branch_hygiene.py" in text, (
        "gate adapter SKILL.md must reference 'branch_hygiene.py'. "
        "IVG-70 T-05 edit is missing or was reverted."
    )
    # has_task_commits or commits-ahead FAIL wording must be present
    assert "has_task_commits" in text, (
        "gate adapter SKILL.md must contain 'has_task_commits' (the commits-ahead "
        "FAIL signal, NOT bare on-protected). IVG-70 T-05 AC-2 guard failed."
    )
    # $(pwd) root resolution must be present (not path_resolve.py --project-root)
    assert "$(pwd)" in text, (
        "gate adapter SKILL.md must use '$(pwd)' for project root resolution "
        "(gate runs inline at project-root cwd — no walk-up needed). "
        "IVG-70 T-05 AC-1 guard failed."
    )


def test_gate_adapter_audit_enumeration_has_branch_hygiene():
    """IVG-70 T-08 AC-4 (MIN-4): 'Branch hygiene' must appear in the ## Automated checks audit enumeration.

    This is a distinct location from the Standard/Full gate checklists.
    The literal string 'Branch hygiene' must appear in the audit-log body
    description near '## Automated checks', not just in the Standard/Full sections.
    """
    text = _adapter_skill("gate").read_text(encoding="utf-8")
    # The audit enumeration description (Step 5 format-kit compose block) must reference Branch hygiene
    assert "Branch hygiene" in text, (
        "gate adapter SKILL.md must contain the literal string 'Branch hygiene'. "
        "IVG-70 T-05 MIN-4 guard: check name must be identical in both the "
        "Standard/Full gate checklist AND the ## Automated checks audit enumeration."
    )
    # Confirm the string appears near the ## Automated checks audit context
    # (not only in the Standard/Full sections)
    automated_checks_idx = text.find("## Automated checks")
    branch_hygiene_near_audit = text.find("Branch hygiene", automated_checks_idx)
    assert branch_hygiene_near_audit != -1, (
        "gate adapter SKILL.md must contain 'Branch hygiene' in or after the "
        "'## Automated checks' audit enumeration section. "
        "IVG-70 T-05 MIN-4 audit-location guard failed."
    )


def test_gate_core_doc_has_branch_hygiene():
    """IVG-70 T-08 AC-3: core gate.md must contain the portable branch hygiene rule."""
    text = _core_doc("gate").read_text(encoding="utf-8")
    assert "protected branch" in text, (
        "core/skills/gate.md must mention 'protected branch' in the gate level rules. "
        "IVG-70 T-05 core doc edit is missing or was reverted."
    )
    # Core must NOT contain branch_hygiene.py or __QUOIN_HOME__ (adapter-specific)
    assert "branch_hygiene.py" not in text, (
        "core/skills/gate.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )
    assert "__QUOIN_HOME__" not in text, (
        "core/skills/gate.md must NOT contain '__QUOIN_HOME__' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )


def test_form_b_c_allowlist_lists_gate_at_adapter_path():
    """Guard that test_path_resolve_e2e.py lists gate at the adapter path.

    Covers both EXPLICIT_FORM_B_C_FILES (line ~97) and
    FORM_B_C_RESIDUAL_CANARIES (line ~159). If a future maintainer
    reverts the T-08 Group F edits, this pilot test fires.
    """
    target = PKG_DIR / "dev" / "tests" / "test_path_resolve_e2e.py"
    text = target.read_text(encoding="utf-8")
    assert 'ADAPTER_SKILLS_DIR / "gate" / "SKILL.md"' in text, (
        "test_path_resolve_e2e.py must list gate at the adapter path "
        "(T-08 Group F). The entry was reverted — restore it."
    )
    assert 'PROJECT_ROOT / "quoin" / "skills" / "gate" / "SKILL.md"' not in text, (
        "test_path_resolve_e2e.py must NOT list gate at the legacy PROJECT_ROOT "
        "quoin/skills path. The T-08 Group F repoint was reverted — restore it."
    )
