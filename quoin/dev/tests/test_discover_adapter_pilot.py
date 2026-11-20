"""Phase 20 adapter pilot tests for discover.

Parametrized over the single skill migrated in Phase 20. Follows the
pattern established by test_expand_adapter_pilot.py (Phase 19),
test_cost_snapshot_adapter_pilot.py (Phase 18), and prior phase pilots.

Skill parameters: (skill_name, expected_model, has_section_0)
  - discover: opus, §0 absent (Opus-tier — NOT a cheap-tier self-dispatcher).

NOTE: has_section_0=False for discover. This is the most likely copy-paste
error when adapting from Phase 19 (expand uses has_section_0=True). Discover
is Opus-tier and does NOT carry the §0 block. See CLAUDE.md "§0 Model
dispatch preamble": the 12 cheap-tier skills carry §0; the 9 Opus-tier
skills (including discover) do NOT.

discover characteristics:
  - model: opus (NOT sonnet, NOT haiku — differs from Phase 18 and 19 templates)
  - §0 Model dispatch block: absent (Opus-tier)
  - NOT a subagent-preamble spawn target (no preamble.md). The 7 spawn
    targets are: critic, revise, revise-fast, plan, review, gate, architect.
    Discover is excluded — build_preambles.py must not generate a preamble.md.
  - Seventh Opus-tier migration (after review, plan, critic, revise,
    architect, thorough_plan).

PRIOR_PHASE_SKILLS lists the 19 Phase 6–19 skills. Later phases (21+) will
extend their own pilot's PRIOR_PHASE_SKILLS, NOT this file's.
"""
import re
from pathlib import Path

import pytest
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("discover", "opus", False),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–19 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate", "implement", "rollback",
    "end_of_task", "run", "end_of_day", "weekly_review", "cost_snapshot",
    "expand",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/discover.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'discover-2026-05-11.md')
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


# ── Parametrized tests (run over the discover skill) ──────────────────────────


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


# ── Regression-guard tests (Phase 19 skills must still be covered) ────────


