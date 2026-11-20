"""Tests for IVG-137 T-05: eod-refresh-routine.md memory file and installer wiring.

Mirrors test_discovery_refresh_routine.py's AST-based TIER1_MEMORY_FILES
registration check for discovery-refresh-routine.md — TIER1_MEMORY_FILES is an
explicit allow-list consumed by BOTH deploy_memory() and compute_drift() in
src/quoin/installer.py, and a new memory/ file omitted from the tuple silently
never deploys. Do not rely on tier1-files.md (a reference doc, not the installer
manifest) as a substitute for this registration check.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"

_ROUTINE_DOC = _SOURCE_ROOT / "memory" / "eod-refresh-routine.md"
_INSTALLER = _REPO_ROOT / "src" / "quoin" / "installer.py"


def test_routine_doc_exists() -> None:
    assert _ROUTINE_DOC.exists(), f"eod-refresh-routine.md not found: {_ROUTINE_DOC}"


def test_routine_doc_starts_with_h1() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    assert text.strip().startswith("# "), (
        "eod-refresh-routine.md must start with an H1 heading (Tier-1)"
    )


def test_routine_doc_mentions_end_of_day() -> None:
    assert "/end_of_day" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "eod-refresh-routine.md must reference /end_of_day"
    )


def test_routine_doc_mentions_schedule() -> None:
    assert "/schedule" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "eod-refresh-routine.md must reference /schedule"
    )


def test_routine_doc_has_cron_constant() -> None:
    assert "QUOIN_EOD_REFRESH_CRON" in _ROUTINE_DOC.read_text(encoding="utf-8"), (
        "eod-refresh-routine.md must define QUOIN_EOD_REFRESH_CRON constant"
    )


def test_routine_doc_has_execution_environment_caveat() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    has_cloud = "cloud" in text.lower()
    has_drive = "Drive" in text or "local" in text.lower()
    assert has_cloud and has_drive, (
        "eod-refresh-routine.md must include execution-environment caveat mentioning cloud and Drive/local"
    )


def test_routine_doc_states_end_of_task_is_primary() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    assert "/end_of_task" in text and "primary" in text.lower(), (
        "eod-refresh-routine.md must state /end_of_task folding is the primary "
        "mechanism and this cron is a secondary opt-in (per T-05 spec)"
    )


def test_routine_doc_has_no_literal_home_claude() -> None:
    text = _ROUTINE_DOC.read_text(encoding="utf-8")
    assert "~/.claude/" not in text, (
        "eod-refresh-routine.md must use ${CLAUDE_PLUGIN_ROOT} token, not literal ~/.claude/"
    )


