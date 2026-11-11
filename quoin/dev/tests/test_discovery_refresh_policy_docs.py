"""Tests for IVG-106 T-06: discovery refresh policy documented in CLAUDE.md + discover files."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"

_CLAUDE_MD = _SOURCE_ROOT / "CLAUDE.md"
_ADAPTER_DISCOVER = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "discover" / "SKILL.md"
_CORE_DISCOVER = _SOURCE_ROOT / "core" / "skills" / "discover.md"
_BUILD_PREAMBLES = _REPO_ROOT / "quoin" / "scripts" / "build_preambles.py"


def test_claude_md_has_discovery_stale_days() -> None:
    assert "QUOIN_DISCOVERY_STALE_DAYS" in _CLAUDE_MD.read_text(encoding="utf-8"), (
        "CLAUDE.md must reference QUOIN_DISCOVERY_STALE_DAYS"
    )


def test_claude_md_has_discovery_refresh_disable() -> None:
    assert "QUOIN_DISCOVERY_REFRESH_DISABLE" in _CLAUDE_MD.read_text(encoding="utf-8"), (
        "CLAUDE.md must reference QUOIN_DISCOVERY_REFRESH_DISABLE (master off switch)"
    )


def test_claude_md_has_refresh_policy_phrase() -> None:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    assert "refresh policy" in text.lower() or "Discovery/Serena refresh" in text, (
        "CLAUDE.md must contain 'refresh policy' or 'Discovery/Serena refresh' phrase"
    )


def test_claude_md_serena_block_has_refresh_pointer() -> None:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    assert "§Refresh" in text or "Refresh / Re-onboarding" in text, (
        "CLAUDE.md Serena block must reference serena-activation.md §Refresh section"
    )


def test_adapter_discover_has_clock_reset_clause() -> None:
    text = _ADAPTER_DISCOVER.read_text(encoding="utf-8")
    assert "Updated" in text, "Adapter discover SKILL.md must mention Updated column"
    has_clock_reset = "all repos" in text.lower() or "including" in text.lower() or "skipped" in text.lower()
    assert has_clock_reset, (
        "Adapter discover SKILL.md must state Updated column is written for ALL repos including skipped"
    )


def test_core_discover_has_clock_reset_clause() -> None:
    text = _CORE_DISCOVER.read_text(encoding="utf-8")
    assert "Updated" in text, "Core discover.md must mention Updated column"
    has_clock_reset = "all repos" in text.lower() or "including" in text.lower()
    assert has_clock_reset, (
        "Core discover.md must state Updated column is written for ALL repos including skipped"
    )


def test_adapter_discover_mentions_staleness_trigger() -> None:
    text = _ADAPTER_DISCOVER.read_text(encoding="utf-8")
    assert "S-5" in text or "staleness" in text.lower() or "session-start" in text.lower(), (
        "Adapter discover SKILL.md must mention the session-start staleness trigger"
    )


def test_core_discover_mentions_staleness_trigger() -> None:
    text = _CORE_DISCOVER.read_text(encoding="utf-8")
    assert "S-5" in text or "staleness" in text.lower() or "session-start" in text.lower(), (
        "Core discover.md must mention the session-start staleness trigger"
    )


def test_build_preambles_check_exits_0() -> None:
    """build_preambles.py --check must exit 0 (preambles unaffected by T-06 edits)."""
    if not _BUILD_PREAMBLES.exists():
        import pytest
        pytest.skip(f"build_preambles.py not found: {_BUILD_PREAMBLES}")
    result = subprocess.run(
        [sys.executable, str(_BUILD_PREAMBLES), "--check"],
        capture_output=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"build_preambles.py --check failed (exit {result.returncode}): {result.stderr.decode()}"
    )


def test_discovery_dirty_marker_contract() -> None:
    """/run writes and reads the marker, guards lessons, and /discover clears it."""
    run_dir = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run"
    run = (run_dir / "SKILL.md").read_text(encoding="utf-8")
    # Step 2 is a light-route step, kept in the reference file /run reads on that route.
    light = (run_dir / "light-pipeline.md").read_text(encoding="utf-8")
    step2 = light.split("## Step 2 — Discover", 1)[1].split("## Step 3", 1)[0]
    step8 = run.split("## Step 8 — Commit", 1)[1].split("## Escalation", 1)[0]
    assert "discovery-dirty" in step2
    assert "discovery-dirty" in step8
    assert "lessons_guard.py" in step8 and "--no-strict-attrib" in step8
    discover = _ADAPTER_DISCOVER.read_text(encoding="utf-8")
    assert "rm -f" in discover and "discovery-dirty" in discover
