"""Tests for quoin/core/scripts/discovery_staleness.py (IVG-106 T-01)."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── Script paths ─────────────────────────────────────────────────────────────

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[2]         # quoin/ repo root
_CORE_SCRIPT = _REPO_ROOT / "quoin" / "core" / "scripts" / "discovery_staleness.py"
_WRAPPER_SCRIPT = _REPO_ROOT / "quoin" / "scripts" / "discovery_staleness.py"


def _load_module():
    """Load the core script via importlib (per lessons-learned importlib-loader pattern)."""
    spec = importlib.util.spec_from_file_location("_ds_mod", _CORE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_staleness_md(path: Path, updated: datetime) -> None:
    """Write a minimal _staleness.md with the given Updated timestamp."""
    ts = updated.strftime("%Y-%m-%dT%H:%M:%SZ")  # trailing-Z UTC form (real /discover format)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "| Repo | HEAD | Updated |\n"
        "|------|------|--------|\n"
        f"| testrepo | abc1234 | {ts} |\n",
        encoding="utf-8",
    )


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project root with .workflow_artifacts structure."""
    wa = tmp_path / ".workflow_artifacts"
    wa.mkdir()
    (wa / "cache").mkdir()
    (wa / "memory").mkdir()
    return tmp_path


NOW_UTC = datetime.now(tz=timezone.utc)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_verdict_fresh_recent_staleness_file(tmp_path, monkeypatch):
    """Updated = now-1d → verdict fresh."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=1))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "fresh", f"Expected fresh, got: {report}"


def test_verdict_stale_old_staleness_file(tmp_path, monkeypatch):
    """Updated = now-10d, default threshold 7 → verdict stale."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=10))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "stale", f"Expected stale, got: {report}"
    assert report["age_days"] is not None and report["age_days"] > 7


def test_verdict_absent_no_artifacts(tmp_path, monkeypatch):
    """Empty project root → verdict absent, age_days None."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    # No .workflow_artifacts at all
    mod = _load_module()
    report = mod.staleness_report(tmp_path, now=NOW_UTC)
    assert report["verdict"] == "absent", f"Expected absent, got: {report}"
    assert report["age_days"] is None


def test_dirty_marker_forces_stale(tmp_path, monkeypatch):
    """A discovery-dirty marker makes recent discovery stale (exit 10)."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    _make_staleness_md(root / ".workflow_artifacts" / "cache" / "_staleness.md", NOW_UTC - timedelta(days=1))
    (root / ".workflow_artifacts" / "memory" / "discovery-dirty").write_text("2026-11-11 task-x\n", encoding="utf-8")

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "stale", report
    assert report["signal_source"] == "discovery_dirty_marker"


def test_threshold_env_override(tmp_path, monkeypatch):
    """Updated = now-4d with QUOIN_DISCOVERY_STALE_DAYS=3 → stale."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.setenv("QUOIN_DISCOVERY_STALE_DAYS", "3")
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=4))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "stale", f"Expected stale with threshold=3, got: {report}"


def test_disable_knob_forces_fresh(tmp_path, monkeypatch):
    """QUOIN_DISCOVERY_REFRESH_DISABLE=1 → fresh + disabled=True even for 99d-old signal."""
    monkeypatch.setenv("QUOIN_DISCOVERY_REFRESH_DISABLE", "1")
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=99))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "fresh", f"Expected fresh (disabled), got: {report}"
    assert report["disabled"] is True


def test_signal_precedence_prefers_updated_column(tmp_path, monkeypatch):
    """Updated column fresh (now-1d) but file mtime older → verdict fresh, source is column."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"

    # Write with recent Updated column
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=1))
    # Backdate the file mtime to 15 days ago (would signal stale if used)
    old_ts = (NOW_UTC - timedelta(days=15)).timestamp()
    os.utime(staleness_path, (old_ts, old_ts))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "fresh", f"Expected fresh (column wins), got: {report}"
    assert report["signal_source"] == "staleness_updated_column"


def test_directory_mtime_not_used(tmp_path, monkeypatch):
    """Touching only the directory (not files inside) should NOT be used as freshness signal."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    memory_dir = root / ".workflow_artifacts" / "memory"

    # Place one memory artifact with old mtime (would signal stale)
    artifact = memory_dir / "repos-inventory.md"
    artifact.write_text("# Repos\n", encoding="utf-8")
    old_ts = (NOW_UTC - timedelta(days=10)).timestamp()
    os.utime(artifact, (old_ts, old_ts))

    # Touch the directory itself to "now" — should NOT influence result
    os.utime(memory_dir, None)  # sets to now

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    # The signal must come from file mtime, not directory mtime
    assert report["signal_source"] in (
        "memory_artifact_mtime",
        "repo_heads_mtime",
        "staleness_file_mtime",
        "staleness_updated_column",
    ), f"Unexpected source: {report['signal_source']}"
    # With a 10-day-old artifact and default threshold 7, should be stale
    assert report["verdict"] == "stale"


def test_z_suffix_timestamp_parsed_correctly(tmp_path, monkeypatch):
    """_staleness.md with Z-suffix Updated → fresh, source is staleness_updated_column.

    This test specifically fails if Z-parsing raises ValueError on Python 3.10.
    The real /discover always emits trailing-Z timestamps.
    """
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"

    # Write exactly the Z-suffix form /discover emits (now minus 1 day)
    ts = (NOW_UTC - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    staleness_path.write_text(
        "| Repo | HEAD | Updated |\n"
        "|------|------|--------|\n"
        f"| myrepo | deadbeef | {ts} |\n",
        encoding="utf-8",
    )
    # Backdate file mtime so if Z-parsing fails and falls back to mtime it would be stale
    old_ts = (NOW_UTC - timedelta(days=15)).timestamp()
    os.utime(staleness_path, (old_ts, old_ts))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["verdict"] == "fresh", (
        f"Z-suffix parse failed or fell through to mtime; report={report}"
    )
    assert report["signal_source"] == "staleness_updated_column"


def test_serena_marker_age_reported(tmp_path, monkeypatch):
    """serena-onboarded.md at now-40d → stale=True, present_marker=True."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    monkeypatch.delenv("QUOIN_SERENA_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, NOW_UTC - timedelta(days=1))  # discovery fresh

    # Write serena marker with old mtime
    marker_path = root / ".workflow_artifacts" / "memory" / "serena-onboarded.md"
    marker_path.write_text("Serena onboarded.\n", encoding="utf-8")
    old_ts = (NOW_UTC - timedelta(days=40)).timestamp()
    os.utime(marker_path, (old_ts, old_ts))

    mod = _load_module()
    report = mod.staleness_report(root, now=NOW_UTC)
    assert report["serena"]["present_marker"] is True
    assert report["serena"]["stale"] is True  # 40d > default 30d threshold

    # Also test absent marker case
    marker_path.unlink()
    report2 = mod.staleness_report(root, now=NOW_UTC)
    assert report2["serena"]["present_marker"] is False
    assert report2["serena"]["stale"] is False


def test_serena_only_stale_exit_code(tmp_path, monkeypatch):
    """CLI: discovery fresh + serena marker present at 40d → exit code 12."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    monkeypatch.delenv("QUOIN_SERENA_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, datetime.now(tz=timezone.utc) - timedelta(days=1))

    marker_path = root / ".workflow_artifacts" / "memory" / "serena-onboarded.md"
    marker_path.write_text("Serena onboarded.\n", encoding="utf-8")
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(marker_path, (old_ts, old_ts))

    result = subprocess.run(
        [sys.executable, str(_CORE_SCRIPT), str(root), "--quiet"],
        capture_output=True,
    )
    assert result.returncode == 12, (
        f"Expected exit 12 (serena present-but-stale), got: {result.returncode}"
    )


def test_absent_serena_marker_does_not_emit_exit_12(tmp_path, monkeypatch):
    """CLI: discovery fresh + absent serena marker → exit code 0 (NOT 12).

    Graceful Absence: absent marker is not a Serena banner trigger.
    """
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, datetime.now(tz=timezone.utc) - timedelta(days=1))
    # No serena-onboarded.md

    result = subprocess.run(
        [sys.executable, str(_CORE_SCRIPT), str(root), "--quiet"],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (absent marker, no Serena banner), got: {result.returncode}"
    )


def test_cli_exit_codes(tmp_path, monkeypatch):
    """CLI exit codes: 10 stale, 0 fresh, 11 absent."""
    monkeypatch.delenv("QUOIN_DISCOVERY_REFRESH_DISABLE", raising=False)
    monkeypatch.delenv("QUOIN_DISCOVERY_STALE_DAYS", raising=False)

    # --- exit 11: absent ---
    result = subprocess.run(
        [sys.executable, str(_CORE_SCRIPT), str(tmp_path), "--quiet"],
        capture_output=True,
    )
    assert result.returncode == 11, f"Expected 11 (absent), got: {result.returncode}"

    # --- exit 10: stale ---
    root = _make_project(tmp_path)
    staleness_path = root / ".workflow_artifacts" / "cache" / "_staleness.md"
    _make_staleness_md(staleness_path, datetime.now(tz=timezone.utc) - timedelta(days=10))

    result = subprocess.run(
        [sys.executable, str(_CORE_SCRIPT), str(root), "--quiet"],
        capture_output=True,
    )
    assert result.returncode == 10, f"Expected 10 (stale), got: {result.returncode}"

    # --- exit 0: fresh ---
    _make_staleness_md(staleness_path, datetime.now(tz=timezone.utc) - timedelta(days=1))
    result = subprocess.run(
        [sys.executable, str(_CORE_SCRIPT), str(root), "--quiet"],
        capture_output=True,
    )
    assert result.returncode == 0, f"Expected 0 (fresh), got: {result.returncode}"


def test_wrapper_script_resolves_core(monkeypatch):
    """The wrapper script must resolve and load the core script via parents[1]."""
    assert _WRAPPER_SCRIPT.exists(), f"Wrapper not found: {_WRAPPER_SCRIPT}"
    text = _WRAPPER_SCRIPT.read_text(encoding="utf-8")
    assert "parents[1]" in text, "Wrapper must use parents[1] for location-relative resolution"
    # Path may be constructed as / "core" / "scripts" / "discovery_staleness.py" (separate parts)
    assert "core" in text and "scripts" in text and "discovery_staleness.py" in text, (
        "Wrapper must reference core/scripts/discovery_staleness.py components"
    )
    # No __QUOIN_HOME__ literal in the wrapper (must use parents[1] pattern)
    assert "__QUOIN_HOME__" not in text, (
        "Wrapper must NOT use __QUOIN_HOME__ literal; use Path(__file__).resolve().parents[1]"
    )
