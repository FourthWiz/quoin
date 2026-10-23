"""The Claude cells' sandbox parameters: explicit environment, process groups, identity trips."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_claude import PLATFORM_INJECTED_ENV, make_fake_claude, read_log  # noqa: E402

from quoin.benchmarks.harness import arm_identity as ai  # noqa: E402
from quoin.benchmarks.harness.cells import quoin_claude, simple_claude  # noqa: E402
from quoin.benchmarks.harness.config import BudgetSpec  # noqa: E402
from quoin.benchmarks.harness.result_writer import RunResult  # noqa: E402
from quoin.benchmarks.harness.paired_spend import settlement_actual  # noqa: E402
from quoin.benchmarks.scripts import run_three_arm_gate as gate  # noqa: E402
from verified_standin import git  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "benchmark_streams"
REPO_ROOT = Path(__file__).resolve().parents[3]
CELLS = [simple_claude, quoin_claude]
_ID = lambda m: m.__name__.rsplit(".", 1)[-1]  # noqa: E731
RESULT = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5}
INIT = {"type": "system", "subtype": "init", "skills": ["a"], "apiKeySource": "none"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
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


def _invoke(cell, tmp_path, bin_dir, *, wall=60, cap=5.0, **kwargs):
    work, arm = _clone(tmp_path)
    task = {"id": "t1", "source": "swebench_verified", "source_id": "org__repo-1",
            "description": "Fix it.", "repo": "org/repo", "base_commit": "0" * 40}
    if cell is quoin_claude:
        kwargs["arm_root"] = arm
        kwargs.setdefault("expected_quoin_commit", gate.worktree_head(arm))
    return cell.invoke(task_spec=task, workdir=work, budget=BudgetSpec(wall_clock_seconds=wall),
                       run_id="r1", max_budget_usd=cap, **kwargs)


def _session_env(bin_dir):
    return {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"]), "HOME": "/nonexistent-home",
            "CLAUDE_CONFIG_DIR": "/nonexistent-home/.claude"}


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_session_env_is_the_whole_child_environment(cell, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_FOO", "leak")
    monkeypatch.setenv("QUOIN_FOO", "leak")
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    env = _session_env(bin_dir)
    _invoke(cell, tmp_path, bin_dir, session_env=env)
    child = read_log(bin_dir)[0]
    expected = set(env)
    if cell is quoin_claude:
        expected |= {quoin_claude.ENV_GATE_AUTO_APPROVE, quoin_claude.ENV_BENCHMARK_RUN}
    assert set(child["env"]) - PLATFORM_INJECTED_ENV == expected - PLATFORM_INJECTED_ENV
    assert child["pgid"] != os.getpgid(0)
    assert child["pgid"] == child["pid"]


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_legacy_call_keeps_the_operator_session(cell, tmp_path, monkeypatch):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, os.environ["PATH"]))
    monkeypatch.setenv("CLAUDE_FOO", "kept")
    result = _invoke(cell, tmp_path, bin_dir)
    child = read_log(bin_dir)[0]
    assert child["env"].get("CLAUDE_FOO") == "kept"
    assert child["pgid"] == os.getpgid(0)
    assert "arm_identity" not in result["extra"]
    assert "identity_trip" not in result["extra"]
    assert "pre_spawn_error" not in result["extra"]
    assert result["verdict"] != "error"


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_detached_background_child_is_reaped(cell, tmp_path):
    marker = tmp_path / "late.marker"
    bin_dir = make_fake_claude(tmp_path, {"steps": [
        {"emit": INIT},
        {"bg_devnull": {"marker": str(marker), "delay": 2}},
        {"emit": RESULT},
    ]})
    result = _invoke(cell, tmp_path, bin_dir, session_env=_session_env(bin_dir))
    assert result["verdict"] != "timeout"
    time.sleep(3)
    assert not marker.exists()


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_background_child_holding_stdout_does_not_stall(cell, tmp_path):
    bin_dir = make_fake_claude(tmp_path, {"steps": [
        {"emit": INIT},
        {"bg_inherit": {"seconds": 30}},
        {"emit": RESULT},
    ]})
    start = time.monotonic()
    result = _invoke(cell, tmp_path, bin_dir, wall=60, session_env=_session_env(bin_dir))
    assert time.monotonic() - start < 10
    assert result["verdict"] != "timeout"
    assert result["cost_runtime_usd"] == 0.5


def _identity(mode, tmp_path):
    canonical = tuple(gate._load_arm_installer(REPO_ROOT).CANONICAL_SKILLS)
    return ai.ArmIdentity(
        mode=mode, config_dir=tmp_path / ".claude", canonical=canonical,
        distinctive=ai.distinctive_quoin_skills(canonical), arm_config_mode="symmetric",
    )


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_contaminated_init_kills_the_session(cell, tmp_path):
    steps = [{"emit_file": str(FIX / "init_contaminated_plain.jsonl")},
             {"sleep": 5}, {"emit": {"type": "assistant", "message": {"content": []}}}]
    bin_dir = make_fake_claude(tmp_path, {"steps": steps})
    out = tmp_path / "out"
    start = time.monotonic()
    result = _invoke(cell, tmp_path, bin_dir, session_env=_session_env(bin_dir),
                     arm_identity=_identity(ai.QUOIN_FREE, tmp_path), run_output_dir=out)
    assert time.monotonic() - start < 5
    assert result["verdict"] == "error"
    assert result["extra"]["failure_reason"] == "plain-arm-contaminated"
    assert result["extra"]["identity_trip"] is True
    assert result["extra"]["arm_identity"]["failure_detail"].startswith("hook-event:")
    assert '"assistant"' not in (out / "transcript.jsonl").read_text()


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_unarmed_cap_with_session_env_refuses_without_spawning(cell, tmp_path):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    result = _invoke(cell, tmp_path, bin_dir, cap=None, session_env=_session_env(bin_dir))
    assert result["verdict"] == "error"
    assert result["extra"]["failure_reason"] == "budget-cap-unarmed"
    assert result["extra"]["pre_spawn_error"] is True
    assert read_log(bin_dir) == []
    assert settlement_actual(RunResult(cell="c", task_id="t1", run_id="r1", verdict="error", extra=result["extra"])) == 0.0


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_gate_mode_refusal_without_session_env_has_no_pre_spawn_flag(cell, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")
    bin_dir = make_fake_claude(tmp_path, {"steps": []})
    result = _invoke(cell, tmp_path, bin_dir, cap=None)
    assert result["extra"]["failure_reason"] == "budget-cap-unarmed"
    assert "pre_spawn_error" not in result["extra"]


@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize("failure,setup", [
    ("install-failed", "install"), ("commit-unresolvable", "nogit"), ("commit-mismatch", "mismatch"),
])
def test_quoin_pre_spawn_refusals_are_flagged_only_in_paired_mode(tmp_path, monkeypatch, paired,
                                                                  failure, setup):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    kwargs = {"session_env": _session_env(bin_dir)} if paired else {}
    if setup == "install":
        monkeypatch.setattr(
            quoin_claude, "_initialize_workflow_artifacts",
            lambda *a, **k: {"ok": False, "reason": "x", "returncode": 1},
        )
    elif setup == "mismatch":
        kwargs["expected_quoin_commit"] = "1" * 40
    else:
        kwargs["arm_root"] = tmp_path / "does-not-exist"
        kwargs["expected_quoin_commit"] = "1" * 40
    work, arm = _clone(tmp_path)
    kwargs.setdefault("arm_root", arm)
    kwargs.setdefault("expected_quoin_commit", gate.worktree_head(arm))
    task = {"id": "t1", "source": "swebench_verified", "source_id": "org__repo-1",
            "description": "Fix it.", "repo": "org/repo", "base_commit": "0" * 40}
    result = quoin_claude.invoke(task_spec=task, workdir=work,
                                 budget=BudgetSpec(wall_clock_seconds=60), run_id="r1",
                                 max_budget_usd=5.0, **kwargs)
    assert result["extra"]["failure_reason"] == failure
    assert result["extra"].get("pre_spawn_error", False) is paired
    assert read_log(bin_dir) == []
    if paired:
        run = RunResult(cell="c", task_id="t1", run_id="r1", verdict="error", extra=result["extra"])
        assert settlement_actual(run) == 0.0


@pytest.mark.parametrize("cell", CELLS, ids=_ID)
def test_interrupt_while_setting_up_the_stream_does_not_orphan_the_session(cell, tmp_path, monkeypatch):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"sleep": 60}]})

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise KeyboardInterrupt

    import subprocess

    spawned = []
    real_popen = subprocess.Popen

    class Recording(real_popen):
        def __init__(self, argv, *a, **k):
            super().__init__(argv, *a, **k)
            if argv and os.path.basename(str(argv[0])) == "claude":
                spawned.append(self.pid)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    monkeypatch.setattr(cell.threading, "Thread", Boom)
    with pytest.raises(KeyboardInterrupt):
        _invoke(cell, tmp_path, bin_dir, session_env=_session_env(bin_dir))
    assert len(spawned) == 1
    pgid = spawned[0]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    pytest.fail("the spawned session is still running")
