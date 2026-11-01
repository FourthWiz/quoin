"""Cells must not run git in the agent's clone after a Verified session.

A plain `git diff HEAD` there reads agent-writable `.git/config`, so an
fsmonitor hook planted during the session would run under the operator's
environment. Verified rows are scored from the harness's own patch capture,
so the cells skip that diff for them. Each cell runs against a fake CLI on
PATH; a non-Verified source shows the same setup does fire the hook.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quoin.benchmarks.harness.cells import quoin_claude, quoin_codex, simple_claude, simple_codex
from quoin.benchmarks.harness.config import BudgetSpec
from verified_standin import git

_RESULT_EVENT = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0, "num_turns": 1})


def _fake_cli(bin_dir: Path, name: str, stdout_line: str) -> None:
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\necho 'agent edit' >> a.txt\nprintf '%s\\n' '{stdout_line}'\n")
    script.chmod(0o755)


@pytest.fixture
def clone(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_cli(bin_dir, "claude", _RESULT_EVENT)
    _fake_cli(bin_dir, "codex", "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)

    work = tmp_path / "clone"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    (work / "a.txt").write_text("base\n")
    git(["add", "a.txt"], work)
    git(["commit", "-q", "-m", "base"], work)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "mon.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(["config", "core.fsmonitor", str(hook)], work)

    arm = tmp_path / "arm"
    arm.mkdir()
    git(["init", "-q", "-b", "main"], arm)
    git(["commit", "-q", "--allow-empty", "-m", "arm"], arm)

    monkeypatch.setattr(
        quoin_claude, "_initialize_workflow_artifacts",
        lambda *a, **k: {"ok": True, "reason": None, "returncode": 0},
    )
    monkeypatch.setattr(quoin_codex, "_bootstrap_quoin_codex_environment", lambda *a, **k: None)
    return {"path": work, "marker": marker, "arm": arm}


def _invoke(cell, source: str, c: dict) -> dict:
    task = {"id": "t1", "source": source, "source_id": "org__repo-1", "description": "Fix it.",
            "repo": "org/repo", "base_commit": "0" * 40}
    kwargs = {}
    if cell is quoin_claude:
        kwargs["arm_root"] = c["arm"]
    return cell.invoke(task_spec=task, workdir=c["path"], budget=BudgetSpec(wall_clock_seconds=60),
                       run_id="r1", **kwargs)


_CELLS = [simple_claude, quoin_claude, simple_codex, quoin_codex]


@pytest.mark.parametrize("cell", _CELLS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_verified_session_runs_no_git_in_agent_clone(cell, clone):
    result = _invoke(cell, "swebench_verified", clone)
    assert (clone["path"] / "a.txt").read_text() == "base\nagent edit\n"
    assert not clone["marker"].exists()
    assert result["diff_patch"] == ""


@pytest.mark.parametrize("cell", _CELLS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_non_verified_session_still_records_diff(cell, clone):
    result = _invoke(cell, "swebench_lite", clone)
    assert "+agent edit" in result["diff_patch"]
    assert clone["marker"].exists()
