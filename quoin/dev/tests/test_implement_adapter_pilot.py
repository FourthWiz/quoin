"""Phase 12 adapter pilot tests for implement.

Parametrized over the single skill migrated in Phase 12. Follows the
pattern established by test_architecture_planning_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - implement: sonnet, has §0
"""
import re
from pathlib import Path

import pytest
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("implement", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–11 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/implement.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'implement-2026-05-09.md')
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


# ── Parametrized tests (run over the implement skill) ─────────────────────


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
def test_install_sh_has_adapter_override(skill_name, expected_model, has_section_0):
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter(skill_name)


# ── Non-parametrized constraint guards ────────────────────────────────────


# ── Regression-guard tests (Phase 11 skills must still be covered) ────────


# ── MAJ/MIN regression guards (T-09, T-11, T-12 edits must stay intact) ──


# ── IVG-70: Branch-hygiene precheck assertions ────────────────────────────


def test_implement_core_doc_has_branch_hygiene_section():
    """IVG-70 T-08: core implement.md must contain the portable Branch hygiene section."""
    text = _core_doc("implement").read_text(encoding="utf-8")
    assert "## Branch hygiene" in text, (
        "core/skills/implement.md must contain '## Branch hygiene' section. "
        "IVG-70 T-04 core doc edit is missing or was reverted."
    )


def test_implement_core_doc_no_branch_hygiene_script_refs():
    """IVG-70 T-08: core implement.md must NOT reference branch_hygiene.py or ${CLAUDE_PLUGIN_ROOT}."""
    text = _core_doc("implement").read_text(encoding="utf-8")
    assert "branch_hygiene.py" not in text, (
        "core/skills/implement.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific; keep core portable). IVG-70 adapter/core boundary violated."
    )
    assert "${CLAUDE_PLUGIN_ROOT}" not in text, (
        "core/skills/implement.md must NOT contain '${CLAUDE_PLUGIN_ROOT}' "
        "(adapter-specific; keep core portable). IVG-70 adapter/core boundary violated."
    )


