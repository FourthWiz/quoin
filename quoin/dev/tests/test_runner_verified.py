"""Offline tests for the runner's Verified branch: fake adapters, stub judges, a stand-in upstream."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from quoin.benchmarks.harness import repo_prep, runner
from quoin.benchmarks.harness.config import HarnessConfig
from quoin.benchmarks.harness.judge import JudgeResult
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.harness.runner import judge_pending_rows, run_cell, run_one_task
from quoin.benchmarks.harness.session_state import is_judge_pending
from verified_standin import build_upstream, configure_env, git

FIX = Path(__file__).parent / "fixtures" / "benchmark_streams"
SUITE = Path(runner.__file__).resolve().parent.parent / "suite-tiered-small.json"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _events(name):
    return [json.loads(l) for l in (FIX / name).read_text().splitlines() if l.strip()]


class FakeAdapter:
    def __init__(self, act=None, events=None, verdict=None):
        self.act, self.events, self.verdict = act, events if events is not None else _events("clean_success.jsonl"), verdict
        self.calls = []

    def invoke(self, task_spec, workdir, budget, run_id, run_output_dir=None):
        self.calls.append(Path(workdir))
        if self.act:
            self.act(Path(workdir))
        out = {"transcript_events": self.events, "prompt": "p", "diff_patch": "IGNORED",
               "cost_runtime_usd": 0.5, "cost_available": True, "extra": {"cell_flag": 1}}
        if self.verdict:
            out["verdict"] = self.verdict
        return out


def commit_change(workdir: Path):
    (workdir / "a.txt").write_text("base\nfixed\n")
    git(["commit", "-q", "-am", "agent"], workdir)


class StubJudge:
    def __init__(self, verdict="pass", final=True, raises=None):
        self.verdict, self.final, self.raises = verdict, final, raises
        self.calls = []

    def __call__(self, task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
                 task_spec=None, judge_context=None):
        self.calls.append({"task_dir": Path(task_dir), "spec": task_spec, "ctx": judge_context,
                           "patch": (Path(task_dir) / "diff.patch").read_bytes(),
                           "session": (Path(task_dir) / "session.json").exists()})
        if self.raises:
            raise self.raises
        return JudgeResult(task_id, "swebench_verified", self.verdict, "ev", 0.1,
                           extra={"judge_final": self.final})


@pytest.fixture
def env(tmp_path, monkeypatch):
    standin = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, standin)
    work = tmp_path / "work"
    work.mkdir()
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    cfg = HarnessConfig(run_dir=tmp_path / "runs", suite_path=SUITE,
                        repo_cache_root=cache, repo_work_root=work)
    return {**standin, "cfg": cfg, "work": work, "cache": cache, "tmp": tmp_path}


def _t_dir(env, cell="simple-claude"):
    return env["cfg"].run_dir / "r1" / cell / env["task"]["id"]


def _run(env, adapter, judge, cell="simple-claude", monkeypatch=None, spec=None):
    runner_mod = runner
    runner_mod._load_cell_adapter = lambda c: adapter
    runner_mod.judge_task = judge
    return run_one_task(cell, spec or env["task"], "r1", env["cfg"])


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.setattr(runner, "_load_cell_adapter", runner._load_cell_adapter)
    monkeypatch.setattr(runner, "judge_task", runner.judge_task)


def _json(path):
    return json.loads(path.read_text())


def test_repo_prep_failure_never_spawns(env):
    spec = dict(env["task"], source_id="org__repo-9")
    adapter = FakeAdapter()
    res = _run(env, adapter, StubJudge(), spec=spec)
    t = env["cfg"].run_dir / "r1" / "simple-claude" / spec["id"]
    assert adapter.calls == [] and res.verdict == "error"
    lines = [json.loads(l) for l in (t / "transcript.jsonl").read_text().splitlines()]
    assert lines and lines[0]["type"] == "harness_note"
    assert not (t / "session.json").exists()
    assert _json(t / "metrics.json")["pre_spawn_error"] is True


def test_unexpected_prep_exception_is_contained(env, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(runner, "prepare_task_repo", boom)
    res = _run(env, FakeAdapter(), StubJudge())
    assert res.extra["failure_reason"] == "repo-prep-unexpected-FileNotFoundError"


def test_happy_path_writes_session_before_judging(env):
    judge = StubJudge()
    res = _run(env, FakeAdapter(act=commit_change), judge)
    t = _t_dir(env)
    assert res.verdict == "pass"
    assert b"+fixed" in (t / "diff.patch").read_bytes()
    assert judge.calls[0]["session"] is True and judge.calls[0]["spec"]["id"] == env["task"]["id"]
    j = _json(t / "judge.json")
    assert j["source_benchmark"] == "swebench_verified" and j["judge_final"] is True
    m = _json(t / "metrics.json")
    assert m["tree_sha"] and m["terminal_state"] == "completed"
    assert not [p for p in env["work"].iterdir()]
    ctx = judge.calls[0]["ctx"]
    assert ctx["cell"] == "simple-claude"
    assert ctx["snapshot_revision"] == json.loads(SUITE.read_text())["tiered"]["dataset_revision"]


def test_budget_halt_is_not_judged(env):
    judge = StubJudge()
    res = _run(env, FakeAdapter(events=_events("budget_halt.jsonl")), judge)
    assert res.verdict == "budget_stopped" and judge.calls == []


def test_auth_failure_is_not_judged(env):
    judge = StubJudge()
    res = _run(env, FakeAdapter(events=_events("auth_failure.jsonl")), judge)
    assert res.verdict == "error" and res.extra["failure_reason"] == "auth-failure"
    assert judge.calls == []


def test_errored_after_turns_is_judged(env):
    judge = StubJudge()
    res = _run(env, FakeAdapter(events=_events("errored_after_turns.jsonl")), judge)
    assert judge.calls and _json(_t_dir(env) / "metrics.json")["session_errored"] is True
    assert res.verdict == "pass"


def test_latin1_patch_is_neither_judged_nor_lost(env):
    judge = StubJudge()

    def act(w):
        (w / "latin.txt").write_bytes(b"caf\xe9\n")

    res = _run(env, FakeAdapter(act=act), judge)
    t = _t_dir(env)
    assert res.verdict == "error" and res.extra["failure_reason"] == "patch-not-utf8"
    assert judge.calls == []
    assert b"caf\xe9\n" in (t / "diff.patch").read_bytes()
    assert _json(t / "judge.json")["judged"] is False


def test_raising_judge_keeps_patch_and_row_stays_pending(env):
    res = _run(env, FakeAdapter(act=commit_change), StubJudge(raises=RuntimeError("boom")))
    t = _t_dir(env)
    captured = (t / "diff.patch").read_bytes()
    assert b"+fixed" in captured
    j = _json(t / "judge.json")
    assert j["verdict"] == "error" and j["source_benchmark"] == "swebench_verified"
    assert j.get("judge_final") is not True
    assert is_judge_pending(t)
    m = _json(t / "metrics.json")
    assert m["tree_sha"] and m["terminal_state"] == "completed"
    assert m["failure_reason"] == "harness-exception-RuntimeError"
    assert m["wall_clock_seconds"] > 0
    assert _json(t / "cost.json")["cost_runtime_usd"] == 0.5
    assert (t / "prompt.txt").read_text() == "p"

    before = {p.name: p.read_bytes() for p in t.iterdir() if p.name != "judge.json"}
    runner._load_cell_adapter = lambda c: (_ for _ in ()).throw(AssertionError("adapter loaded"))
    good = StubJudge()
    rows = judge_pending_rows(env["cfg"].run_dir, "r1", [env["task"]],
                              snapshot_revision="rev", judge=good)
    assert rows == [{"cell": "simple-claude", "task_id": env["task"]["id"],
                     "verdict": "pass", "judge_final": True}]
    assert good.calls[0]["patch"] == captured
    assert {p.name: p.read_bytes() for p in t.iterdir() if p.name != "judge.json"} == before
    assert not is_judge_pending(t)
    again = judge_pending_rows(env["cfg"].run_dir, "r1", [env["task"]], judge=good)
    assert again == [] and len(good.calls) == 1


def test_judge_pending_rows_uses_runner_attribute(env, monkeypatch):
    _run(env, FakeAdapter(act=commit_change), StubJudge(raises=RuntimeError("x")))
    late = StubJudge()
    monkeypatch.setattr(runner, "judge_task", late)
    judge_pending_rows(env["cfg"].run_dir, "r1", [env["task"]])
    assert len(late.calls) == 1


def test_cleanup_failure_is_recorded_and_next_task_runs(env, monkeypatch):
    t2 = dict(env["task"], id="verified_easy_001", source_id="org__repo-2")
    prepare_mirrors([env["task"], t2], log=lambda *_: None)
    monkeypatch.setattr(runner, "cleanup_task_repo", lambda p: "stuck")
    runner._load_cell_adapter = lambda c: FakeAdapter(act=commit_change)
    runner.judge_task = StubJudge()
    env["cfg"].cells = ["simple-claude"]
    out = run_cell("simple-claude", [env["task"], t2], "r1", env["cfg"])
    assert len(out.task_results) == 2
    assert _json(_t_dir(env) / "metrics.json")["clone_cleanup_failed"] == "stuck"
    assert _json(_t_dir(env) / "session.json")["clone_cleanup_failed"] == "stuck"


def test_both_arms_get_equal_trees(env):
    _run(env, FakeAdapter(), StubJudge(), cell="simple-claude")
    _run(env, FakeAdapter(), StubJudge(), cell="quoin-claude")
    a = _json(_t_dir(env, "simple-claude") / "metrics.json")["tree_sha"]
    b = _json(_t_dir(env, "quoin-claude") / "metrics.json")["tree_sha"]
    assert a == b


def test_non_verified_specs_use_legacy_path(env):
    lite = {"id": "swebench_lite_001", "source": "swebench_lite", "source_id": "a__b-1", "description": "d"}
    seen = {}

    def legacy_judge(task_id, task_dir, run_id, invocation_extra=None):
        seen["called"] = True
        return JudgeResult(task_id, "swebench_lite", "fail", None, 0.0)

    res = _run(env, FakeAdapter(), legacy_judge, spec=lite)
    assert seen["called"] and res.verdict == "fail"
    assert not (env["cfg"].run_dir / "r1" / "simple-claude" / "swebench_lite_001" / "session.json").exists()


def test_wall_time_kept_when_invoke_raises(env):
    class Boom(FakeAdapter):
        def invoke(self, **kw):
            time.sleep(0.02)
            raise RuntimeError("spawn died")

    _run(env, Boom(), StubJudge())
    assert _json(_t_dir(env) / "metrics.json")["wall_clock_seconds"] > 0


def test_respawn_removes_stale_judge_json(env, monkeypatch):
    t = _t_dir(env)
    t.mkdir(parents=True)
    (t / "judge.json").write_text(json.dumps({"verdict": "pass", "judge_final": True}))

    seen = {}
    real = runner.write_session_json

    def spy(t_dir, data):
        seen["stale_present"] = (Path(t_dir) / "judge.json").exists()
        return real(t_dir, data)

    monkeypatch.setattr(runner, "write_session_json", spy)
    _run(env, FakeAdapter(act=commit_change), StubJudge(raises=RuntimeError("die")))
    assert seen["stale_present"] is False


def test_capture_failure_reason_is_recorded(env, monkeypatch):
    def too_big(*a, **k):
        raise repo_prep.RepoPrepError("patch-too-large", "diff exceeds 10 bytes")

    monkeypatch.setattr(runner, "capture_patch", too_big)
    judge = StubJudge()
    res = _run(env, FakeAdapter(act=commit_change), judge)
    metrics = _json(_t_dir(env) / "metrics.json")
    assert judge.calls == [] and res.verdict == "error"
    assert metrics["capture_error"] == "patch-too-large"
    assert metrics["capture_error_detail"] == "diff exceeds 10 bytes"
    assert metrics["failure_reason"] == "patch-capture-failed"
