"""Both Claude cells record tool-error counts and per-model usage in metrics.json.

Each cell runs against a fake `claude` that replays a recorded stream. The
counter is contained: a defect in it must leave the session's verdict alone.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from quoin.benchmarks.harness import runner
from quoin.benchmarks.harness.cells import quoin_claude, simple_claude
from quoin.benchmarks.harness.config import BudgetSpec, HarnessConfig
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.harness.result_writer import RunResult, write_run_result
from quoin.benchmarks.harness.stream_metrics import StreamMetrics
from verified_standin import build_upstream, configure_env, git

FIX = Path(__file__).parent / "fixtures" / "benchmark_streams"
SUITE = Path(runner.__file__).resolve().parent.parent / "suite-tiered-small.json"
QUOIN_FIXTURE = "quoin_subagent_tool_errors.jsonl"
PLAIN_FIXTURE = "plain_no_tool_errors.jsonl"
CELLS = [simple_claude, quoin_claude]
_ID = lambda m: m.__name__.rsplit(".", 1)[-1]  # noqa: E731


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _install_fake_claude(tmp_path, monkeypatch, fixture):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\ncat '%s'\n" % (FIX / fixture))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, os.environ["PATH"]))
    monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)
    monkeypatch.setattr(
        quoin_claude, "_initialize_workflow_artifacts",
        lambda *a, **k: {"ok": True, "reason": None, "returncode": 0},
    )


def _clone(tmp_path):
    work = tmp_path / "clone"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    (work / "a.txt").write_text("base\n")
    git(["add", "a.txt"], work)
    git(["commit", "-q", "-m", "base"], work)
    arm = tmp_path / "arm"
    arm.mkdir()
    git(["init", "-q", "-b", "main"], arm)
    git(["commit", "-q", "--allow-empty", "-m", "arm"], arm)
    return work, arm


def _invoke(cell, work, arm):
    task = {"id": "t1", "source": "swebench_verified", "source_id": "org__repo-1",
            "description": "Fix it.", "repo": "org/repo", "base_commit": "0" * 40}
    kwargs = {"arm_root": arm} if cell is quoin_claude else {}
    return cell.invoke(task_spec=task, workdir=work, budget=BudgetSpec(wall_clock_seconds=60),
                       run_id="r1", **kwargs)


def _metrics(cell, result, tmp_path):
    run_result = RunResult(
        cell=cell.__name__.rsplit(".", 1)[-1].replace("_", "-"), task_id="t1", run_id="r1",
        verdict=result.get("verdict", "unknown"), extra=result["extra"],
        tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"),
        tokens_cache_read=result.get("tokens_cache_read"),
        tokens_cache_write=result.get("tokens_cache_write"),
    )
    out_dir = write_run_result(run_result, tmp_path / "out")
    return json.loads((out_dir / "metrics.json").read_text())


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_recorded_quoin_stream_counts(cell, tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, QUOIN_FIXTURE)
    work, arm = _clone(tmp_path)
    metrics = _metrics(cell, _invoke(cell, work, arm), tmp_path)
    assert metrics["tool_error_count"] == 2
    assert metrics["subagent_tool_error_count"] == 1
    assert metrics["permission_denial_count"] == 1
    assert metrics["tool_error_count_available"] is True
    assert metrics["model_usage_totals"]["output"] == 12424
    assert metrics["tokens_out"] == 8905


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_recorded_plain_stream_counts(cell, tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, PLAIN_FIXTURE)
    work, arm = _clone(tmp_path)
    metrics = _metrics(cell, _invoke(cell, work, arm), tmp_path)
    assert metrics["tool_error_count"] == 0
    assert metrics["permission_denial_count"] == 0
    assert metrics["tool_error_count_available"] is True


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_a_counter_failure_leaves_the_verdict_alone(cell, tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, PLAIN_FIXTURE)
    work, arm = _clone(tmp_path)
    baseline = _invoke(cell, work, arm)

    def boom(self, event):
        raise RuntimeError("schema drift")

    monkeypatch.setattr(StreamMetrics, "_observe", boom)
    second = tmp_path / "second"
    second.mkdir()
    work2, arm2 = _clone(second)
    patched = _invoke(cell, work2, arm2)
    assert patched["verdict"] == baseline["verdict"]
    assert patched["verdict"] != "error"
    metrics = _metrics(cell, patched, tmp_path)
    assert metrics["tool_error_count_available"] is False
    assert metrics["stream_metrics_error"].startswith("RuntimeError")


def test_counters_reach_metrics_through_the_verified_runner(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch, QUOIN_FIXTURE)
    standin = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, standin)
    work_root = tmp_path / "work"
    work_root.mkdir()
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    cfg = HarnessConfig(run_dir=tmp_path / "runs", suite_path=SUITE,
                        repo_cache_root=cache, repo_work_root=work_root)
    monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: simple_claude)

    class StubJudge:
        def __call__(self, task_id, task_dir, run_id, **kwargs):
            from quoin.benchmarks.harness.judge import JudgeResult
            return JudgeResult(task_id, "swebench_verified", "fail", "ev", 0.1,
                               extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", StubJudge())
    runner.run_one_task("simple-claude", standin["task"], "r1", cfg)
    t_dir = cfg.run_dir / "r1" / "simple-claude" / standin["task"]["id"]
    metrics = json.loads((t_dir / "metrics.json").read_text())
    assert metrics["tool_error_count"] == 2
    assert metrics["permission_denial_count"] == 1
    assert metrics["model_usage_totals"]["output"] == 12424
    assert metrics["tool_error_count_available"] is True


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_a_missing_cli_writes_no_counter_keys(cell, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)
    monkeypatch.setattr(
        quoin_claude, "_initialize_workflow_artifacts",
        lambda *a, **k: {"ok": True, "reason": None, "returncode": 0},
    )
    work, arm = _clone(tmp_path)
    result = _invoke(cell, work, arm)
    assert result["verdict"] == "error"
    assert "tool_error_count" not in result["extra"]
    assert "tool_error_count_available" not in result["extra"]
