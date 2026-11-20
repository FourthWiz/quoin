"""Phase 9 adapter pilot tests for plan, critic, revise, revise-fast.

Parametrized over the four skills migrated in Phase 9. Follows the
pattern established by test_review_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - plan: opus, no §0
  - critic: opus, no §0
  - revise: opus, no §0
  - revise-fast: sonnet, HAS §0
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
    ("plan", "opus", False),
    ("critic", "opus", False),
    ("revise", "opus", False),
    ("revise-fast", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]


def _core_doc(skill_name: str) -> Path:
    return PKG_DIR / "core" / "skills" / f"{skill_name}.md"


def _adapter_skill(skill_name: str) -> Path:
    return PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_exists(skill_name, expected_model, has_section_0):
    path = _core_doc(skill_name)
    assert path.is_file(), f"Missing core skill doc: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_no_forbidden_tokens(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    # Forbid slash-command invocation form (e.g. `/plan`, `/critic`) — match the
    # token only when followed by a space, end-of-string, or punctuation (not a
    # path separator like in `critic-response-*.md`).
    slash_cmd = f"/{skill_name}"
    if re.search(re.escape(slash_cmd) + r"(?=[^a-zA-Z0-9_\-]|$)", text):
        hits.append(slash_cmd)
    assert not hits, f"Core doc for {skill_name} contains forbidden tokens: {hits}"


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


