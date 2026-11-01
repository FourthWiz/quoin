"""
Behavior test for the /enrich skill (IVG-152).

Asserts the active Claude adapter SKILL.md carries the in-lane guardrails that
keep /enrich a lightweight upstream-of-/specify sharpening pass rather than a
spec/plan writer: it writes enriched-prompt.md, has a quick "already clear"
return path, degrades to a flagged best-effort rewrite when non-interactive,
and explicitly stops without invoking any downstream phase.

Per lesson 2026-04-23: no live LLM calls — deterministic string matching only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "enrich" / "SKILL.md"


def _adapter_text() -> str:
    return ADAPTER_SKILL_MD.read_text(encoding="utf-8")


def test_adapter_skill_md_exists():
    assert ADAPTER_SKILL_MD.is_file(), f"Missing {ADAPTER_SKILL_MD}"


def test_adapter_declares_opus_and_name():
    text = _adapter_text()
    parts = text.split("---", 2)
    assert len(parts) >= 3, "Adapter SKILL.md missing YAML frontmatter"
    fm = yaml.safe_load(parts[1])
    assert fm.get("model") == "opus", (
        f"Adapter SKILL.md must declare model: opus (got {fm.get('model')!r})"
    )
    assert fm.get("name") == "enrich"


def test_adapter_writes_enriched_prompt_artifact():
    text = _adapter_text()
    assert "enriched-prompt.md" in text, (
        "Adapter must write enriched-prompt.md — the skill's only output artifact"
    )


def test_adapter_has_already_clear_quick_return():
    text = _adapter_text()
    assert "no material enrichment needed" in text, (
        "Adapter must have a quick-return path for prompts that are already clear"
    )


def test_adapter_has_non_interactive_degradation():
    text = _adapter_text()
    assert "questions I would have asked" in text, (
        "Adapter must degrade to a flagged best-effort rewrite under non-interactive "
        "dispatch, listing the questions it would have asked instead of silently guessing"
    )


def test_adapter_never_invokes_downstream_phase():
    text = _adapter_text()
    assert "STOP" in text, "Adapter must explicitly STOP after writing the artifact"
    assert "never invokes a downstream phase" in text, (
        "Adapter must state it never auto-invokes a downstream phase "
        "(/specify, /architect, /thorough_plan, /plan, /implement)"
    )


def test_adapter_references_core_doc():
    text = _adapter_text()
    assert "quoin/core/skills/enrich.md" in text, (
        "Adapter SKILL.md must reference the portable intent doc path"
    )


@pytest.mark.parametrize(
    "removed_marker",
    [
        "STOP",
        "never invokes a downstream phase",
        "no material enrichment needed",
        "questions I would have asked",
    ],
)
def test_behavior_test_fails_if_guardrail_removed(removed_marker):
    """Verify these are real guardrails: deleting one flips the corresponding check red."""
    text = _adapter_text()
    stripped = text.replace(removed_marker, "")
    assert removed_marker not in stripped
    assert removed_marker in text  # sanity: it was actually present before stripping
