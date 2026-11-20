"""Tests for the Serena code-intelligence integration (serena-integration task)."""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # quoin/
SOURCE_ROOT = REPO_ROOT / "quoin"

SERENA_MEMORY = SOURCE_ROOT / "memory" / "serena-activation.md"
INSTALLER = REPO_ROOT / "src" / "quoin" / "installer.py"
CLAUDE_MD = SOURCE_ROOT / "CLAUDE.md"
ADAPTER_INIT_WORKFLOW = SOURCE_ROOT / "adapters" / "claude" / "skills" / "init_workflow" / "SKILL.md"


def test_serena_memory_file_exists_and_tier1() -> None:
    """serena-activation.md must exist, be non-empty, start with an English heading,
    and contain the required key terms (Tier-1 guard)."""
    assert SERENA_MEMORY.exists(), f"Missing: {SERENA_MEMORY}"
    text = SERENA_MEMORY.read_text(encoding="utf-8")
    assert text.strip(), "serena-activation.md is empty"
    assert text.lstrip().startswith("# "), "Must start with a Markdown H1 heading (Tier-1)"
    for term in ("ToolSearch", "activate_project", "initial_instructions", "onboarding"):
        assert term in text, f"Missing required term '{term}' in serena-activation.md"
    assert "do nothing" in text, (
        "Graceful-absence rule must include 'do nothing' phrase in serena-activation.md"
    )


def test_serena_memory_uses_quoin_home_token() -> None:
    """SOURCE memory file must use ${CLAUDE_PLUGIN_ROOT} token, never the literal ~/.claude/ path."""
    text = SERENA_MEMORY.read_text(encoding="utf-8")
    assert "~/.claude/" not in text, (
        "serena-activation.md must use ${CLAUDE_PLUGIN_ROOT} token, not literal ~/.claude/"
    )


def test_claude_md_has_conditional_serena_pointer() -> None:
    """CLAUDE.md must have a conditional Serena block with both the present/absent branches
    and a pointer to serena-activation.md."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "serena-activation.md" in text, (
        "CLAUDE.md must reference serena-activation.md"
    )
    # Must have the ToolSearch probe (marks it as conditional, not unconditional)
    assert "ToolSearch" in text or "mcp__serena__" in text, (
        "CLAUDE.md Serena block must include a conditional probe (ToolSearch or mcp__serena__)"
    )
    # Must have the graceful-absence branch — "do nothing" or "don't exist" or "absent"
    has_absence = any(phrase in text for phrase in ("do nothing", "don't exist", "no schema"))
    assert has_absence, (
        "CLAUDE.md Serena block must include a graceful-absence branch"
    )


# ── IVG-106 T-05: serena-activation.md §Refresh section ──────────────────────

def test_serena_activation_has_refresh_section() -> None:
    """serena-activation.md must have a ## Refresh / Re-onboarding section with required content."""
    text = SERENA_MEMORY.read_text(encoding="utf-8")
    assert "## Refresh" in text or "Re-onboarding" in text, (
        "serena-activation.md must contain ## Refresh section"
    )
    assert "QUOIN_SERENA_STALE_DAYS" in text, (
        "serena-activation.md must reference QUOIN_SERENA_STALE_DAYS"
    )
    assert "serena-onboarded.md" in text, (
        "serena-activation.md must reference serena-onboarded.md marker"
    )
    assert "onboarding" in text.lower(), (
        "serena-activation.md must reference onboarding procedure"
    )
    # Graceful Absence
    assert "do nothing" in text.lower() or "Graceful Absence" in text, (
        "serena-activation.md must contain Graceful Absence 'do nothing' phrase"
    )
    # Present-but-stale language
    assert "present" in text.lower() and "stale" in text.lower(), (
        "serena-activation.md must describe the present-but-stale condition"
    )
    # First-time onboarding path
    assert "first-time" in text.lower() or "absent marker" in text.lower() or "first time" in text.lower(), (
        "serena-activation.md must describe the first-time onboarding path"
    )
    # Hook does NOT emit exit-12 for absent marker (important invariant)
    assert "hook" in text.lower() and "absent" in text.lower(), (
        "serena-activation.md must document that hook does not emit Serena banner for absent marker"
    )


