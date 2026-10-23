"""Tests for the paired orchestrator: ordering, spend stops, resume states and interrupts."""
from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from quoin.benchmarks.harness import paired_run as pr
from quoin.benchmarks.harness import runner, session_state
from quoin.benchmarks.harness.config import HarnessConfig
from quoin.benchmarks.harness.result_writer import RunResult, write_run_result
from quoin.benchmarks.harness.session_state import write_session_json
from quoin.benchmarks.scripts import spend_ledger
from test_runner_verified import (  # noqa: F401  (fixtures and helpers shared with the runner tests)
    FakeAdapter, StubJudge, commit_change, env, _events, _json, _t_dir,
)


def _tasks(n):
    return [{"id": f"t{i}", "source": "swebench_verified"} for i in range(1, n + 1)]


def _plan(tmp_path, n=3, cap=1.0, authorised=100.0, suite_ceiling=100.0, **kw):
    configs = {c: HarnessConfig(run_dir=tmp_path / "runs", cells=[c], max_budget_usd=cap)
               for c in pr.PAIR_ORDER}
    return pr.PairedPlan(
        run_id="r1", run_dir=tmp_path / "runs", tasks=_tasks(n), cap=cap,
        ledger=tmp_path / "ledger.jsonl", authorised=authorised,
        suite_ceiling=suite_ceiling, configs=configs, **kw)


class FakeRunTask:
    def __init__(self, tmp_path, cost=0.2, reasons=None, verdict="pass"):
        self.calls, self.cost, self.reasons, self.verdict = [], cost, reasons or {}, verdict
        self.run_dir = tmp_path / "runs"

    def __call__(self, cell, task, run_id, config):
        start = time.monotonic()
        time.sleep(0.002)
        self.calls.append((cell, task["id"], start, None, config))
        reason = self.reasons.get((cell, task["id"]))
        t_dir = self.run_dir / run_id / cell / task["id"]
        result = RunResult(cell=cell, task_id=task["id"], run_id=run_id, verdict="error" if reason else self.verdict,
                           cost_runtime_usd=self.cost, extra={"failure_reason": reason} if reason else {})
        write_run_result(result, self.run_dir)
        write_session_json(t_dir, {"failure_reason": reason, "genuine_assistant_turns": 3})
        self.calls[-1] = self.calls[-1][:3] + (time.monotonic(),) + self.calls[-1][4:]
        return result


def test_order_is_task_major_plain_first_and_never_overlaps(tmp_path):
    fake = FakeRunTask(tmp_path)
    out = pr.run_paired(_plan(tmp_path), run_task=fake, log=lambda *_: None)
    assert [(c, t) for c, t, *_ in fake.calls] == [
        (c, f"t{i}") for i in (1, 2, 3) for c in pr.PAIR_ORDER]
    for a, b in zip(fake.calls, fake.calls[1:]):
        assert a[3] <= b[2]
    assert out.pairs_completed == 3 and out.stop_reason is None


def test_spend_ceiling_stops_on_a_pair_boundary(tmp_path):
    fake = FakeRunTask(tmp_path, cost=1.0)
    plan = _plan(tmp_path, n=9, cap=1.0, suite_ceiling=4.0)
    out = pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "spend-ceiling"
    cells_by_task = {}
    for c, t, *_ in fake.calls:
        cells_by_task.setdefault(t, set()).add(c)
    assert all(len(v) == 2 for v in cells_by_task.values())
    assert len(cells_by_task) == 2


def test_authorised_ceiling_stops_before_any_spawn(tmp_path):
    fake = FakeRunTask(tmp_path)
    out = pr.run_paired(_plan(tmp_path, authorised=1.5), run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "authorised-ceiling" and fake.calls == []


@pytest.mark.parametrize("reason", ["plain-arm-contaminated", "auth-failure"])
def test_abort_reasons_stop_the_run(tmp_path, reason):
    fake = FakeRunTask(tmp_path, reasons={("simple-claude", "t2"): reason})
    out = pr.run_paired(_plan(tmp_path), run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "abort"
    assert [(c, t) for c, t, *_ in fake.calls][-1] == ("simple-claude", "t2")
    assert not (tmp_path / "runs" / "r1" / "quoin-claude" / "t2").exists()
    assert not (tmp_path / "runs" / "r1" / "simple-claude" / "t3").exists()


def test_identity_recheck_problem_aborts_without_a_reservation(tmp_path):
    fake = FakeRunTask(tmp_path)
    plan = _plan(tmp_path, identity_recheck={"simple-claude": lambda: ["drift"]})
    out = pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "abort" and out.abort_detail == ["drift"]
    assert fake.calls == [] and not plan.ledger.exists()


def test_recheck_abort_names_the_cell_task_and_the_prior_session(tmp_path):
    state = {"calls": 0}

    def recheck():
        state["calls"] += 1
        return ["drift"] if state["calls"] >= 2 else []

    fake = FakeRunTask(tmp_path)
    plan = _plan(tmp_path, identity_recheck={"simple-claude": recheck})
    out = pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "abort"
    assert (out.abort_cell, out.abort_task_id, out.abort_kind) == ("simple-claude", "t2", "recheck")
    assert out.abort_prior_task_id == "t1"


def test_final_recheck_examines_the_last_session_of_each_arm(tmp_path):
    state = {"calls": 0}
    session_count = lambda: len(fake.calls)  # noqa: E731

    def recheck():
        state["calls"] += 1
        return ["drift"] if session_count() == 6 else []

    fake = FakeRunTask(tmp_path)
    plan = _plan(tmp_path, identity_recheck={"simple-claude": recheck})
    out = pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    assert len(fake.calls) == 6
    assert out.stop_reason == "abort" and out.abort_detail == ["drift"]
    assert (out.abort_cell, out.abort_task_id, out.abort_kind) == ("simple-claude", None, "recheck")
    assert out.abort_prior_task_id == "t3"


def test_row_abort_names_the_row_that_tripped(tmp_path):
    fake = FakeRunTask(tmp_path, reasons={("quoin-claude", "t2"): "auth-failure"})
    out = pr.run_paired(_plan(tmp_path), run_task=fake, log=lambda *_: None)
    assert (out.abort_cell, out.abort_task_id, out.abort_kind) == ("quoin-claude", "t2", "row")


@pytest.mark.parametrize("reason", ["install-failed", "commit-unresolvable", "commit-mismatch",
                                    "expected-commit-unarmed"])
def test_quoin_pre_spawn_refusals_stop_the_run_and_are_spawned_again(tmp_path, reason):
    runs = tmp_path / "runs"
    fake = FakeRunTask(tmp_path, reasons={("quoin-claude", "t1"): reason})
    out = pr.run_paired(_plan(tmp_path), run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "abort" and out.abort_detail == [reason]
    assert pr.row_state(runs / "r1" / "quoin-claude" / "t1") == "respawn"


def test_usd_cap_stops_at_the_pair_boundary(tmp_path):
    fake = FakeRunTask(tmp_path, cost=0.4)
    out = pr.run_paired(_plan(tmp_path, n=5, usd_cap=1.0), run_task=fake, log=lambda *_: None)
    assert out.stop_reason == "usd-cap" and out.pairs_completed == 2


def test_every_spawn_has_matching_reservation_and_settlement(tmp_path):
    fake = FakeRunTask(tmp_path)
    out = pr.run_paired(_plan(tmp_path, n=2), run_task=fake, log=lambda *_: None)
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines() if l.strip()]
    for row in out.rows:
        kinds = sorted(r["kind"] for r in rows if r["attempt_id"] == row["attempt_id"])
        assert kinds == ["reservation", "settlement"]


def test_raising_run_task_still_settles_at_cap(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("x")

    plan = _plan(tmp_path)
    with pytest.raises(RuntimeError):
        pr.run_paired(plan, run_task=boom, log=lambda *_: None)
    rows = [json.loads(l) for l in plan.ledger.read_text().splitlines() if l.strip()]
    assert [r["kind"] for r in rows] == ["reservation", "settlement"]
    assert rows[1]["actual_usd"] is None
    assert spend_ledger.recorded_total(plan.ledger, "r1") == pytest.approx(1.0)


def test_resume_supersedes_respawn_and_skips_done(tmp_path):
    fake = FakeRunTask(tmp_path, reasons={("simple-claude", "t1"): "budget-cap-unarmed"})
    plan = _plan(tmp_path, n=1)
    pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    fake2 = FakeRunTask(tmp_path)
    pr.run_paired(plan, run_task=fake2, log=lambda *_: None)
    assert [(c, t) for c, t, *_ in fake2.calls] == [("simple-claude", "t1"), ("quoin-claude", "t1")]
    assert (tmp_path / "runs" / ".superseded" / "r1" / "simple-claude" / "t1" / "1" / "metrics.json").exists()
    fake3 = FakeRunTask(tmp_path)
    pr.run_paired(plan, run_task=fake3, log=lambda *_: None)
    assert fake3.calls == []


def test_quoin_only_pair_checks_one_cap(tmp_path):
    fake = FakeRunTask(tmp_path)
    plan = _plan(tmp_path, n=1)
    FakeRunTask(tmp_path)("simple-claude", {"id": "t1"}, "r1", None)
    out = pr.run_paired(plan, run_task=fake, log=lambda *_: None)
    assert [c for c, *_ in fake.calls] == ["quoin-claude"] and out.pairs_completed == 1


def test_resolve_wall_clock():
    assert pr.resolve_wall_clock(30, {"wall_clock_seconds": 90}) == 30.0
    assert pr.resolve_wall_clock(None, {"wall_clock_seconds": 90}) == 90.0
    assert pr.resolve_wall_clock(None, {}) == 600.0
    assert pr.resolve_wall_clock(None, {"wall_clock_seconds": -1}) == 600.0


def _row(path, session=None, metrics=None, transcript=False):
    path.mkdir(parents=True)
    if session is not None:
        (path / "session.json").write_text(json.dumps(session))
    if metrics is not None:
        (path / "metrics.json").write_text(json.dumps(metrics))
    if transcript:
        (path / "transcript.jsonl").write_text("{}\n")
    return path


def test_row_state_rules(tmp_path):
    assert pr.row_state(tmp_path / "none") == "absent"
    assert pr.row_state(_row(tmp_path / "a", session={"failure_reason": "auth-failure", "genuine_assistant_turns": 0})) == "respawn"
    assert pr.row_state(_row(tmp_path / "b", session={"failure_reason": "auth-failure", "genuine_assistant_turns": 1})) == "done"
    assert pr.row_state(_row(tmp_path / "c", session={"failure_reason": "timeout"})) == "done"
    assert pr.row_state(_row(tmp_path / "d", metrics={"pre_spawn_error": True})) == "respawn"
    assert pr.row_state(_row(tmp_path / "e", transcript=True)) == "respawn"
    assert pr.row_state(_row(tmp_path / "f", metrics={"failure_reason": "repo-prep-x"})) == "foreign"
    assert pr.row_state(_row(tmp_path / "g", session={"failure_reason": "quoin-arm-contaminated"})) == "respawn"


def test_repeated_harness_exception_is_kept_as_error(tmp_path):
    sup = tmp_path / "sup"
    _row(sup / "1", metrics={"failure_reason": "harness-exception-OSError"})
    t = _row(tmp_path / "t", metrics={"failure_reason": "harness-exception-OSError"})
    assert pr.row_state(t) == "respawn"
    assert pr.row_state(t, superseded_dir=sup) == "done"


def test_foreign_row_refuses_the_run(tmp_path):
    _row(tmp_path / "runs" / "r1" / "simple-claude" / "t1", metrics={"failure_reason": "repo-prep-x"})
    with pytest.raises(pr.PairedRefusal):
        pr.run_paired(_plan(tmp_path, n=1), run_task=FakeRunTask(tmp_path), log=lambda *_: None)


def test_repeated_harness_exception_logs_and_moves_on(tmp_path):
    runs = tmp_path / "runs"
    _row(runs / ".superseded" / "r1" / "simple-claude" / "t1" / "1", metrics={"failure_reason": "harness-exception-OSError"})
    _row(runs / "r1" / "simple-claude" / "t1", metrics={"failure_reason": "harness-exception-OSError"})
    logs, fake = [], FakeRunTask(tmp_path)
    pr.run_paired(_plan(tmp_path, n=2), run_task=fake, log=logs.append)
    assert any("harness exception repeated for simple-claude/t1" in l for l in logs)
    assert ("simple-claude", "t2") in [(c, t) for c, t, *_ in fake.calls]


def test_interrupt_gate():
    gate = pr.InterruptGate()
    with pytest.raises(pr.PairedInterrupted) as exc:
        gate.handler(signal.SIGTERM, None)
    assert exc.value.signal_name == "SIGTERM"
    gate = pr.InterruptGate()
    with pytest.raises(pr.PairedInterrupted):
        with gate.hold():
            gate.handler(signal.SIGINT, None)
            gate.handler(signal.SIGTERM, None)
    assert gate.pending == "SIGINT"


def _real(env, adapter, judge, hold=None):
    runner._load_cell_adapter = lambda c: adapter
    runner.judge_task = judge
    env["cfg"].defer_judge = True
    if hold is not None:
        env["cfg"].row_write_hold = hold
    return runner.run_one_task("simple-claude", env["task"], "r1", env["cfg"])


@pytest.fixture
def _restore_runner(monkeypatch):
    monkeypatch.setattr(runner, "_load_cell_adapter", runner._load_cell_adapter)
    monkeypatch.setattr(runner, "judge_task", runner.judge_task)


def test_real_runner_identity_trip_row_is_respawn(env, _restore_runner):
    class Trip(FakeAdapter):
        def invoke(self, **kw):
            out = super().invoke(**kw)
            out["verdict"] = "error"
            out["extra"] = {"failure_reason": "plain-arm-contaminated", "identity_trip": True}
            return out

    _real(env, Trip(), StubJudge())
    t = _t_dir(env)
    assert (t / "session.json").exists()
    assert pr.row_state(t) == "respawn"


def test_real_runner_harness_exception_has_no_session_and_respawns(env, _restore_runner):
    class Boom(FakeAdapter):
        def invoke(self, **kw):
            raise OSError("x")

    _real(env, Boom(), StubJudge())
    t = _t_dir(env)
    assert _json(t / "metrics.json")["failure_reason"] == "harness-exception-OSError"
    assert not (t / "session.json").exists()
    assert pr.row_state(t) == "respawn"


def test_interrupt_after_session_write_is_held_until_metrics_exist(env, _restore_runner, monkeypatch):
    gate = pr.InterruptGate()
    real_write = runner.write_session_json

    def write_then_signal(t_dir, data):
        real_write(t_dir, data)
        gate.handler(signal.SIGTERM, None)

    monkeypatch.setattr(runner, "write_session_json", write_then_signal)
    t = _t_dir(env)
    with pytest.raises(pr.PairedInterrupted):
        _real(env, FakeAdapter(act=commit_change), StubJudge(), hold=gate.hold)
    assert (t / "session.json").exists() and (t / "metrics.json").exists()
    assert pr.settlement_actual_from_row(t) == pytest.approx(0.5)


def test_interrupt_during_patch_capture_is_held_until_metrics_exist(env, _restore_runner, monkeypatch):
    gate = pr.InterruptGate()
    real_capture = runner.capture_patch

    def capture_then_signal(*a, **k):
        gate.handler(signal.SIGTERM, None)
        return real_capture(*a, **k)

    monkeypatch.setattr(runner, "capture_patch", capture_then_signal)
    t = _t_dir(env)
    with pytest.raises(pr.PairedInterrupted):
        _real(env, FakeAdapter(act=commit_change), StubJudge(), hold=gate.hold)
    assert (t / "session.json").exists() and (t / "metrics.json").exists()
    assert pr.settlement_actual_from_row(t) == pytest.approx(0.5)


def test_interrupt_through_run_paired_settles_actual_cost(env, _restore_runner, monkeypatch):
    gate = pr.InterruptGate()
    real_write = runner.write_session_json
    monkeypatch.setattr(runner, "write_session_json",
                        lambda t, d: (real_write(t, d), gate.handler(signal.SIGTERM, None))[0])
    runner._load_cell_adapter = lambda c: FakeAdapter(act=commit_change)
    runner.judge_task = StubJudge()
    cfg = env["cfg"]
    cfg.defer_judge, cfg.row_write_hold = True, gate.hold
    plan = pr.PairedPlan(run_id="r1", run_dir=cfg.run_dir, tasks=[env["task"]], cap=1.0,
                         ledger=env["tmp"] / "l.jsonl", authorised=50.0, suite_ceiling=50.0,
                         configs={c: cfg for c in pr.PAIR_ORDER})
    calls = []

    def run_task(cell, task, run_id, config):
        calls.append(cell)
        return runner.run_one_task(cell, task, run_id, config)

    with pytest.raises(pr.PairedInterrupted):
        pr.run_paired(plan, run_task=run_task, log=lambda *_: None)
    rows = [json.loads(l) for l in plan.ledger.read_text().splitlines() if l.strip()]
    assert [r["kind"] for r in rows] == ["reservation", "settlement"]
    assert rows[1]["actual_usd"] == pytest.approx(0.5)
    assert calls == ["simple-claude"]


def test_paired_interrupted_passes_through_the_runner(env, _restore_runner):
    class Stop(FakeAdapter):
        def invoke(self, **kw):
            raise pr.PairedInterrupted("SIGINT")

    with pytest.raises(pr.PairedInterrupted):
        _real(env, Stop(), StubJudge())
    assert not (_t_dir(env) / "metrics.json").exists()
    assert list(env["work"].iterdir()) == []


def test_judge_phase_uses_runner_attribute(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "judge_pending_rows", lambda *a, **k: seen.update(k) or [])
    sentinel = object()
    monkeypatch.setattr(runner, "judge_task", sentinel)
    pr.judge_phase(_plan(tmp_path), [], "rev")
    assert seen["judge"] is sentinel and seen["snapshot_revision"] == "rev"


def _judged(run_path, cells, task_id, verdict="pass"):
    for cell in cells:
        (run_path / cell / task_id / "judge.json").write_text(json.dumps({"verdict": verdict}))


def test_validity_block_is_valid_after_a_clean_rerun(tmp_path):
    from quoin.benchmarks.harness import tiered_report

    suite = {"tiered": {"tiers": {"easy": {}}}, "tasks": [{"id": "t1", "tier": "easy"}]}
    run_path = tmp_path / "runs" / "r1"
    plan = _plan(tmp_path, n=1)
    pr.run_paired(plan, run_task=FakeRunTask(tmp_path, reasons={("simple-claude", "t1"): "plain-arm-contaminated"}),
                  log=lambda *_: None)
    _judged(run_path, ["simple-claude"], "t1", "error")
    tripped = tiered_report.build_tiered_report(run_path, suite, {})
    assert tripped["validity"]["valid"] is False

    pr.run_paired(plan, run_task=FakeRunTask(tmp_path), log=lambda *_: None)
    _judged(run_path, pr.PAIR_ORDER, "t1")
    report = tiered_report.build_tiered_report(run_path, suite, {})
    assert report["validity"]["valid"] is True
    assert report["validity"]["identity_trip_rows"] == []
    assert report["validity"]["final"] is True


def test_run_level_invalid_reasons_from_the_manifest_invalidate_the_report(tmp_path):
    from quoin.benchmarks.harness import tiered_report

    suite = {"tiered": {"tiers": {"easy": {}}}, "tasks": [{"id": "t1", "tier": "easy"}]}
    run_path = tmp_path / "runs" / "r1"
    pr.run_paired(_plan(tmp_path, n=1), run_task=FakeRunTask(tmp_path), log=lambda *_: None)
    _judged(run_path, pr.PAIR_ORDER, "t1")
    clean = tiered_report.build_tiered_report(run_path, suite, {})
    assert clean["validity"]["valid"] is True
    tripped = tiered_report.build_tiered_report(
        run_path, suite, {"run_invalid_reasons": ["arm contamination check failed: x"]})
    assert tripped["validity"]["valid"] is False
    assert "arm contamination check failed: x" in tripped["validity"]["invalid_reasons"]


def test_repeated_harness_exception_row_counts_as_error(tmp_path):
    from quoin.benchmarks.harness import tiered_report

    runs = tmp_path / "runs"
    _row(runs / ".superseded" / "r1" / "simple-claude" / "t1" / "1",
         metrics={"failure_reason": "harness-exception-OSError"})
    kept = _row(runs / "r1" / "simple-claude" / "t1", metrics={"failure_reason": "harness-exception-OSError"})
    pr.run_paired(_plan(tmp_path, n=1), run_task=FakeRunTask(tmp_path), log=lambda *_: None)
    row = tiered_report.load_row(kept, None, {})
    assert row["verdict"] == "error" and row["judge_pending"] is False
    assert not (kept / "session.json").exists()


def test_paired_run_shares_the_report_reason_sets():
    from quoin.benchmarks.harness import tiered_report

    assert pr.ABORT_REASONS is tiered_report.ABORT_REASONS
    assert pr.RESPAWN_REASONS is tiered_report.RESPAWN_REASONS
    assert tiered_report.QUOIN_PRE_SPAWN_REASONS <= tiered_report.ABORT_REASONS
    assert tiered_report.QUOIN_PRE_SPAWN_REASONS <= tiered_report.RESPAWN_REASONS
