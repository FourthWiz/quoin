"""Tests for IVG-106 T-04: discovery-refresh-routine.md memory file and init_workflow wiring."""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"

_ROUTINE_DOC = _SOURCE_ROOT / "memory" / "discovery-refresh-routine.md"
_INSTALLER = _REPO_ROOT / "src" / "quoin" / "installer.py"
_INIT_WORKFLOW = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "init_workflow" / "SKILL.md"


def test_routine_doc_exists() -> None:
    assert _ROUTINE_DOC.exists(), f"discovery-refresh-routine.md not found: {_ROUTINE_DOC}"


def test_routine_doc_starts_with_h1() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    assert text.strip().startswith("# "), (
        "discovery-refresh-routine.md must start with an H1 heading (Tier-1)"
    )


def test_routine_doc_mentions_discover() -> None:
    assert "/discover" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "discovery-refresh-routine.md must reference /discover"
    )


def test_routine_doc_mentions_schedule() -> None:
    assert "/schedule" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "discovery-refresh-routine.md must reference /schedule"
    )


def test_routine_doc_has_cron_constant() -> None:
    assert "QUOIN_DISCOVERY_REFRESH_CRON" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "discovery-refresh-routine.md must define QUOIN_DISCOVERY_REFRESH_CRON constant"
    )


def test_routine_doc_has_graceful_absence() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    has_toolsearch = "ToolSearch" in text
    has_do_nothing = "do nothing" in text.lower() or "skip silently" in text.lower()
    assert has_toolsearch or has_do_nothing, (
        "discovery-refresh-routine.md must describe Graceful Absence (ToolSearch probe + do nothing)"
    )


def test_routine_doc_has_execution_environment_caveat() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    has_cloud = "cloud" in text.lower()
    has_drive = "Drive" in text or "local" in text.lower()
    assert has_cloud and has_drive, (
        "discovery-refresh-routine.md must include execution-environment caveat mentioning cloud and Drive/local"
    )


def test_routine_doc_has_no_literal_home_claude() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    assert "~/.claude/" not in text, (
        "discovery-refresh-routine.md must use ${CLAUDE_PLUGIN_ROOT} token, not literal ~/.claude/"
    )


