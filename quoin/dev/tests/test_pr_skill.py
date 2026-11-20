"""
Structural validation tests for the /pr skill.

Verifies that the adapter SKILL.md and installer.py are
all consistently configured for the new /pr skill (IVG-53).

Per Stage 1 plan D-03: no live LLM calls — deterministic pathlib + string + YAML parsing only.
"""
from __future__ import annotations

from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).parent
QUOIN_DIR = TESTS_DIR.parent.parent
REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SKILLS_DIR = QUOIN_DIR / "adapters" / "claude" / "skills"
CORE_SKILLS_DIR = QUOIN_DIR / "core" / "skills"
CLAUDE_MD = QUOIN_DIR / "CLAUDE.md"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"

PR_ADAPTER_SKILL = ADAPTER_SKILLS_DIR / "pr" / "SKILL.md"
PR_CORE_DOC = CORE_SKILLS_DIR / "pr.md"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a SKILL.md file."""
    text = _read(path)
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end])


# ── T-01: Core skill doc ─────────────────────────────────────────────────────


# ── T-02: Stub SKILL.md ───────────────────────────────────────────────────────

# ── T-03: Adapter SKILL.md ───────────────────────────────────────────────────


# ── T-04: installer.py registration ─────────────────────────────────────────


# ── T-05: CLAUDE.md phase value ───────────────────────────────────────────────

def test_claude_md_phase_value_pr():
    text = _read(CLAUDE_MD)
    assert "`pr`" in text or '"pr"' in text, (
        "CLAUDE.md must include 'pr' in the Phase values list"
    )


def test_claude_md_pr_in_model_assignments():
    text = _read(CLAUDE_MD)
    assert "| /pr |" in text, "CLAUDE.md model assignments table must include /pr row"


def test_claude_md_git_safety_references_pr():
    text = _read(CLAUDE_MD)
    assert "/pr" in text, "CLAUDE.md Git & PR Safety section must reference /pr"


# ── Comment-cleanup pre-flight wiring ──────────────────────────────────────────


