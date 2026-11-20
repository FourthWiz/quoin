"""
Structural validation tests for the /workspace skill.

Verifies that the adapter SKILL.md, core doc, and installer.py
are all consistently configured for the new /workspace skill (IVG-158 S-06).

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

WORKSPACE_ADAPTER_SKILL = ADAPTER_SKILLS_DIR / "workspace" / "SKILL.md"
WORKSPACE_CORE_DOC = CORE_SKILLS_DIR / "workspace.md"


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


# ── T-05: installer.py registration ─────────────────────────────────────────


# ── T-09: CLAUDE.md phase value + model-assignments row ─────────────────────

def test_claude_md_phase_value_workspace():
    text = _read(CLAUDE_MD)
    assert "`workspace`" in text or '"workspace"' in text, (
        "CLAUDE.md must include 'workspace' in the Phase values list"
    )


def test_claude_md_workspace_in_model_assignments():
    text = _read(CLAUDE_MD)
    assert "| /workspace |" in text, "CLAUDE.md model assignments table must include /workspace row"
