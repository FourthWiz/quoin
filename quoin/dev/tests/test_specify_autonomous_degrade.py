"""SKILL.md-lint tests for the /specify non-interactive autonomous degrade (IVG-153, T-07).

Text-level guards over `specify/SKILL.md` (and its runtime-neutral core mirror) — assert
the autonomous branch does NOT call AskUserQuestion for elicitation, writes a `confidence`
frontmatter field, and auto-rejects the repo-spec update gate. The GENERATED
`<!-- §0doubleprime-begin/end -->` block (owned by T-23) is intentionally left untouched.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SPECIFY_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "specify" / "SKILL.md"
SPECIFY_CORE = REPO_ROOT / "quoin" / "core" / "skills" / "specify.md"


@pytest.fixture(scope="module")
def specify_skill_text() -> str:
    assert SPECIFY_SKILL.exists(), f"specify/SKILL.md not found at {SPECIFY_SKILL}"
    return SPECIFY_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def specify_core_text() -> str:
    assert SPECIFY_CORE.exists(), f"core/skills/specify.md not found at {SPECIFY_CORE}"
    return SPECIFY_CORE.read_text(encoding="utf-8")


def test_core_mirror_documents_autonomous_degrade(specify_core_text: str) -> None:
    text = specify_core_text
    assert "Autonomous non-interactive degrade" in text
    assert "confidence" in text
    assert "auto-reject" in text.lower()


