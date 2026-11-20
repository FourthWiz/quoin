"""Phase 16 adapter pilot tests for end_of_day.

Parametrized over the single skill migrated in Phase 16. Follows the
pattern established by test_end_of_task_adapter_pilot.py and
test_run_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - end_of_day: sonnet, §0 present (cheap-tier — self-dispatches to Sonnet)
"""
import re
import subprocess
from pathlib import Path

import pytest
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("end_of_day", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–15 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate", "implement", "rollback",
    "end_of_task", "run",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/end_of_day.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'end_of_day-2026-05-11.md')
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


# ── Parametrized tests (run over the end_of_day skill) ────────────────────


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


# ── Regression-guard tests (Phase 15 skills must still be covered) ────────


def test_sleep_chaining_sh_passes():
    """Regression guard: the shell regression test itself must exit 0.

    Runs quoin/dev/tests/test_sleep_chaining.sh via subprocess so a future
    edit that silently breaks the adapter chaining (or repoints the shell
    test at the wrong file) cannot pass CI unnoticed — the shell test alone
    is not wired into pytest and can rot silently, which is exactly how an
    earlier drop of Step 6 went undetected.
    """
    repo_root = PKG_DIR.parent
    script = repo_root / "quoin" / "dev" / "tests" / "test_sleep_chaining.sh"
    assert script.is_file(), f"Missing shell test: {script}"

    result = subprocess.run(
        ["bash", "quoin/dev/tests/test_sleep_chaining.sh"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"test_sleep_chaining.sh exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
