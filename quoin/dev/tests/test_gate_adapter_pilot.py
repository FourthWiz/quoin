"""Phase 11 adapter pilot tests for gate.

Parametrized over the single skill migrated in Phase 11. Follows the
pattern established by test_architecture_planning_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - gate: sonnet, has §0
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
def test_install_sh_has_adapter_override(skill_name, expected_model, has_section_0):
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter(skill_name)


# ── Non-parametrized constraint guards ────────────────────────────────────


# ── Regression-guard tests (Phase 10 skills must still be covered) ────────


# ── MAJ-2 regression guards (T-08 edits must stay intact) ─────────────────


# ── IVG-70: Branch-hygiene gate check assertions ──────────────────────────


def test_gate_core_doc_has_branch_hygiene():
    """IVG-70 T-08 AC-3: core gate.md must contain the portable branch hygiene rule."""
    text = _core_doc("gate").read_text(encoding="utf-8")
    assert "protected branch" in text, (
        "core/skills/gate.md must mention 'protected branch' in the gate level rules. "
        "IVG-70 T-05 core doc edit is missing or was reverted."
    )
    # Core must NOT contain branch_hygiene.py or ${CLAUDE_PLUGIN_ROOT} (adapter-specific)
    assert "branch_hygiene.py" not in text, (
        "core/skills/gate.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )
    assert "${CLAUDE_PLUGIN_ROOT}" not in text, (
        "core/skills/gate.md must NOT contain '${CLAUDE_PLUGIN_ROOT}' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )


