"""
Structural validation tests for the /workspace skill.

Verifies that the adapter SKILL.md, core doc, and installer.py
are all consistently configured for the new /workspace skill (IVG-158 S-06).

Per Stage 1 plan D-03: no live LLM calls — deterministic pathlib + string + YAML parsing only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
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

def test_core_doc_exists():
    assert WORKSPACE_CORE_DOC.exists(), "quoin/core/skills/workspace.md must exist"


def test_core_doc_has_purpose_section():
    text = _read(WORKSPACE_CORE_DOC)
    assert "## Purpose" in text, "core doc must have ## Purpose section"


def test_core_doc_has_contract_section():
    text = _read(WORKSPACE_CORE_DOC)
    assert "## Contract" in text, "core doc must have ## Contract section"


# ── T-02: Stub SKILL.md ───────────────────────────────────────────────────────

# ── T-03: Adapter SKILL.md ───────────────────────────────────────────────────

def test_adapter_exists():
    assert WORKSPACE_ADAPTER_SKILL.exists(), "quoin/adapters/claude/skills/workspace/SKILL.md must exist"


def test_adapter_frontmatter_name():
    fm = _frontmatter(WORKSPACE_ADAPTER_SKILL)
    assert fm.get("name") == "workspace", "adapter frontmatter must have name: workspace"


def test_adapter_frontmatter_model():
    fm = _frontmatter(WORKSPACE_ADAPTER_SKILL)
    assert fm.get("model") == "sonnet", "adapter frontmatter must have model: sonnet"


def test_adapter_frontmatter_description_nonempty():
    fm = _frontmatter(WORKSPACE_ADAPTER_SKILL)
    assert fm.get("description"), "adapter frontmatter must have a non-empty description"


def test_adapter_s0_heading_present():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    heading = "## §0 Model dispatch (FIRST STEP — execute before anything else)"
    assert heading in text, "adapter must contain the §0 dispatch heading"


def test_adapter_s0_heading_unique():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    heading = "## §0 Model dispatch (FIRST STEP — execute before anything else)"
    assert text.count(heading) == 1, "§0 dispatch heading must appear exactly once"


def test_adapter_s0_model_sonnet():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert 'model: "sonnet"' in text, "§0 block must contain: model: \"sonnet\""


def test_adapter_s0_dispatched_tier():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "dispatched-tier: sonnet" in text, "§0 block must contain: dispatched-tier: sonnet"


def test_adapter_s0_sidecar_present():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "<!-- §0-sidecar-begin -->" in text, "adapter must contain §0-sidecar-begin comment"


def test_adapter_recursion_tokens():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    tokens = [
        "[no-redispatch]",
        "[no-redispatch:N]",
        "Quoin self-dispatch hard-cap reached at N=",
        "[quoin-stage-1: subagent dispatch unavailable;",
    ]
    for token in tokens:
        assert token in text, f"adapter must contain recursion token: {token!r}"


def test_adapter_s0b_intentionally_omitted():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "§0b: intentionally omitted" in text, (
        "adapter must have comment explaining §0b was intentionally omitted"
    )


def test_adapter_s0tripleprime_heading_present():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    heading = "## §0‴ Minimum-tier guard (execute after §0 — before any §0-sidecar block and the skill body)"
    assert heading in text, (
        "adapter must contain the generator-injected §0‴ heading (T-06 ran "
        "inject_pollution_dispatch.py)"
    )


def test_adapter_mentions_all_four_subcommands():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    for subcommand in ("create", "status", "takeover", "teardown"):
        assert subcommand in text, f"adapter must document the '{subcommand}' subcommand"


def test_adapter_teardown_confirmation_gate_present():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "AskUserQuestion" in text, "adapter must use AskUserQuestion for confirmation gates"
    assert "unsafe" in text.lower(), "adapter must reference the unsafe-worktree teardown gate"


def test_adapter_post_merge_offer_gate_present():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "OFFER" in text or "offer" in text, (
        "adapter must document the post-merge teardown OFFER (never auto-run)"
    )


def test_adapter_fail_closed_noninteractive_wording():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "[no-interactive]" in text, "adapter must reference [no-interactive] fail-closed wording"
    assert "[autonomous]" in text, "adapter must reference [autonomous] fail-closed wording"
    assert "fail CLOSED" in text or "fail closed" in text.lower(), (
        "adapter must explicitly spell out fail-closed behavior for both confirmation gates"
    )


def test_adapter_mechanism_script_reference():
    text = _read(WORKSPACE_ADAPTER_SKILL)
    assert "workspace.py" in text, "adapter must reference the portable workspace.py mechanism"


# ── T-05: installer.py registration ─────────────────────────────────────────

def test_installer_canonical_skills_has_workspace():
    text = _read(INSTALLER_PY)
    assert '"workspace"' in text, 'installer.py must contain "workspace" in CANONICAL_SKILLS'


def test_installer_skill_overrides_workspace_name_only():
    text = _read(INSTALLER_PY)
    assert '"workspace": "name-only"' in text, (
        'SKILL_OVERRIDES must contain "workspace": "name-only"'
    )


def test_installer_deployed_and_core_scripts_have_workspace_py():
    text = _read(INSTALLER_PY)
    assert '"workspace.py"' in text, (
        "installer.py must already list workspace.py in DEPLOYED_SCRIPTS/CORE_SCRIPTS "
        "(landed in S-01..S-05; S-06 verifies only, does not re-add)"
    )


# ── T-09: CLAUDE.md phase value + model-assignments row ─────────────────────

def test_claude_md_phase_value_workspace():
    text = _read(CLAUDE_MD)
    assert "`workspace`" in text or '"workspace"' in text, (
        "CLAUDE.md must include 'workspace' in the Phase values list"
    )


def test_claude_md_workspace_in_model_assignments():
    text = _read(CLAUDE_MD)
    assert "| /workspace |" in text, "CLAUDE.md model assignments table must include /workspace row"
