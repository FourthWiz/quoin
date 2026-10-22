"""CLI tests for --prepare-repos and --judge-only, run in-process with no model call."""
from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.harness import runner
from quoin.benchmarks.harness.config import HarnessConfig
from quoin.benchmarks.harness.judge import JudgeResult
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.scripts import run_benchmark
from verified_standin import build_upstream, configure_env

REQUIRED = "error: the following arguments are required: --run-id"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def shim(tmp_path, monkeypatch):
    """A claude executable that records any spawn; PATH lookups resolve only here."""
    d = tmp_path / "shim"
    d.mkdir()
    marker = tmp_path / "claude-was-run"
    exe = d / "claude"
    exe.write_text(f"#!/bin/sh\ntouch {marker}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{Path(shutil.which('git')).parent}")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: str(exe) if name == "claude" else None)
    return marker


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", *argv])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    return exc.value.code


@pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--verify-model"]])
def test_missing_run_id_error_is_unchanged(monkeypatch, capsys, shim, extra):
    code = _main(monkeypatch, "--suite", "tiered-small", *extra)
    assert code == 2
    assert capsys.readouterr().err.rstrip().endswith(REQUIRED)
    assert not shim.exists()


@pytest.mark.parametrize("flags,message", [
    (["--judge-only", "--verify-model"], "--judge-only cannot be combined with --verify-model"),
    (["--judge-only", "--dry-run"], "--judge-only cannot be combined with --dry-run"),
    (["--prepare-repos", "--dry-run"], "--prepare-repos cannot be combined with --dry-run"),
    (["--prepare-repos", "--judge-only"], "--prepare-repos cannot be combined with --judge-only"),
])
def test_flag_conflicts_exit_two(monkeypatch, capsys, shim, flags, message):
    code = _main(monkeypatch, "--suite", "tiered-small", "--run-id", "R", *flags)
    assert code == 2
    assert message in capsys.readouterr().err
    assert not shim.exists()


def test_dry_run_with_verify_model_is_still_accepted_together(monkeypatch, capsys, tmp_path, shim):
    code = _main(monkeypatch, "--suite", "tiered-small", "--run-id", "R", "--dry-run", "--verify-model")
    assert code == 2
    assert "--spend-ledger" in capsys.readouterr().err
    assert not shim.exists()


@pytest.fixture
def standin(tmp_path, monkeypatch):
    up = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, up)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"tiered": {"dataset_revision": "rev"}, "tasks": [up["task"]]}))
    return {**up, "cache": cache, "suite": suite, "tmp": tmp_path}


def test_prepare_repos_succeeds_without_run_id(monkeypatch, capsys, standin, shim):
    code = _main(monkeypatch, "--suite", str(standin["suite"]), "--prepare-repos")
    out = capsys.readouterr().out
    assert code == 0 and "verified_easy_000" in out and " OK" in out
    assert not shim.exists()


def test_prepare_repos_fails_for_missing_commit(monkeypatch, capsys, standin):
    bad = dict(standin["task"], base_commit="b" * 40)
    standin["suite"].write_text(json.dumps({"tasks": [bad]}))
    assert _main(monkeypatch, "--suite", str(standin["suite"]), "--prepare-repos") == 1


def test_prepare_repos_needs_verified_tasks(monkeypatch, capsys):
    assert _main(monkeypatch, "--suite", str(Path(runner.__file__).resolve().parent.parent / "suite-v1.json"), "--prepare-repos") == 2


def test_judge_only_rejudges_pending_row(monkeypatch, capsys, standin, shim):
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    cfg = HarnessConfig(run_dir=standin["tmp"] / "runs", suite_path=standin["suite"],
                        repo_cache_root=standin["cache"], repo_work_root=standin["tmp"])

    class Adapter:
        def invoke(self, task_spec, workdir, budget, run_id):
            (Path(workdir) / "new.py").write_text("x = 1\n")
            events = [json.loads(l) for l in (Path(__file__).parent / "fixtures" / "benchmark_streams"
                                              / "clean_success.jsonl").read_text().splitlines()]
            return {"transcript_events": events, "prompt": "p", "extra": {}}

    def failing(*a, **k):
        raise RuntimeError("docker down")

    monkeypatch.setattr(runner, "_load_cell_adapter", lambda c: Adapter())
    monkeypatch.setattr(runner, "judge_task", failing)
    runner.run_one_task("simple-claude", standin["task"], "R", cfg)

    def stub(task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
             task_spec=None, judge_context=None):
        return JudgeResult(task_id, "swebench_verified", "pass", "ev", 0.0, extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", stub)
    code = _main(monkeypatch, "--suite", str(standin["suite"]), "--run-dir", str(cfg.run_dir),
                 "--judge-only", "--run-id", "R")
    assert code == 0
    assert (cfg.run_dir / "R" / "summary.md").exists()
    judge = json.loads((cfg.run_dir / "R" / "simple-claude" / standin["task"]["id"] / "judge.json").read_text())
    assert judge["verdict"] == "pass" and judge["judge_final"] is True
    assert not shim.exists()


def test_judge_only_missing_run_dir(monkeypatch, standin):
    code = _main(monkeypatch, "--suite", str(standin["suite"]), "--run-dir",
                 str(standin["tmp"] / "nope"), "--judge-only", "--run-id", "R")
    assert code == 2
