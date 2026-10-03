"""Runs the context-tracker mod's TypeScript tests through `claude plugin test`.

The harness loads the mod's hook modules itself, so a pass also proves the
engine compiles the registration module. The CLI-dependent entry is kept in its
own file so the skip logic stays separate from the installer tests.

Set QUOIN_SKIP_PLUGIN_TS_TESTS=1 to skip (CI without the CLI, nested-session or
auth failures).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parents[3]
PLUGIN_SRC = REPO / "quoin" / "plugins" / "context-tracker"


def _skip_reason() -> Optional[str]:
    if os.environ.get("QUOIN_SKIP_PLUGIN_TS_TESTS") == "1":
        return "QUOIN_SKIP_PLUGIN_TS_TESTS=1"
    if shutil.which("claude") is None:
        return "claude CLI not found on PATH"
    try:
        probe = subprocess.run(
            ["claude", "plugin", "test", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "plugin test harness unavailable in this Claude Code build"
    if probe.returncode != 0:
        return "plugin test harness unavailable in this Claude Code build"
    return None


def _snapshot(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def _ignore_generated_types(directory: str, names: list) -> list:
    # Only the generated types folder under .claude-plugin is dropped; the
    # source tree's own types/ stub stays.
    if Path(directory).name == ".claude-plugin" and "types" in names:
        return ["types"]
    return []


def test_skip_reason_when_claude_absent(monkeypatch):
    monkeypatch.delenv("QUOIN_SKIP_PLUGIN_TS_TESTS", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    assert _skip_reason() is not None


def test_skip_reason_when_env_set(monkeypatch):
    monkeypatch.setenv("QUOIN_SKIP_PLUGIN_TS_TESTS", "1")
    assert "QUOIN_SKIP_PLUGIN_TS_TESTS" in _skip_reason()


def test_register_ts_passes(tmp_path):
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    target = tmp_path / "context-tracker"
    shutil.copytree(PLUGIN_SRC, target, ignore=_ignore_generated_types)
    before = _snapshot(target)
    try:
        result = subprocess.run(
            ["claude", "plugin", "test", str(target)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "claude plugin test timed out after 120s\n"
            f"stdout: {exc.stdout}\nstderr: {exc.stderr}"
        )
    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, f"claude plugin test exited {result.returncode}\n{output}"
    combined = result.stdout + result.stderr
    assert re.search(r"\b0 fail\b", combined), f"no zero-failure summary line\n{output}"
    assert not re.search(r"\b[1-9]\d* fail", combined), f"failures reported\n{output}"
    assert _snapshot(target) == before, "plugin test wrote files into the plugin folder"
