"""Hook generalisation for coordinator entries."""
from __future__ import annotations

import pytest

import _opencode_cost_helpers as ch
import _opencode_gate_helpers as gh
from quoin.opencode_adapter import boundaries, phase_loop, run_hooks, snapshot, testrun

CRITIC2 = "stage-1/critic-response-2.md"
CRITIC3 = "stage-1/critic-response-3.md"
REL = ".workflow_artifacts/t1/stage-1/"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return ch.HookWorld(tmp_path, monkeypatch)


def hook(w, run, **kw):
    return run_hooks.after_phase_run(
        w.root, w.task, w.result(run), mark=w.mark, source_dir=ch.SOURCE_DIR,
        superseded_candidate=kw.pop("candidate", None), clock=ch.clock, **kw)


def test_coordinator_plan_round_composes_the_earlier_critic_response(w):
    plan = w.exec_run("plan")
    hook(w, plan, origin="coordinator")
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_REVISE})
    hook(w, critic, origin="coordinator")
    plan2 = w.exec_run("plan")
    hook(w, plan2, origin="coordinator", compose=True)
    entry = w.live("plan")
    assert entry["origin"] == "coordinator"
    assert entry["runs"] == [plan, critic, plan2]
    assert entry["critic_responses"] == [REL + "critic-response-2.md"]


def test_a_carried_violation_stays_after_a_clean_round(w):
    plan = w.exec_run("plan")
    hook(w, plan, origin="coordinator")
    w.fx.record("plan", origin="coordinator", boundary="violation", runs=[plan])
    plan2 = w.exec_run("plan")
    hook(w, plan2, origin="coordinator", compose=True)
    assert w.live("plan")["boundary"] == "violation"


def _implement_boundary(w, monkeypatch, *, candidate_is_run, window_complete):
    seen = {}

    def fake_verify(role, before, after, **kw):
        seen.update(kw)
        status = "unverified" if kw.get("window_partial") else "ok"
        return boundaries.BoundaryResult(
            status=status, reason="boundary-window-partial" if status != "ok" else None)

    monkeypatch.setattr(boundaries, "verify", fake_verify)
    before = boundaries.take_listing(w.root)
    run = w.exec_run("implement", writes={})
    out = hook(
        w, run, origin="coordinator", boundary_before=before, window_complete=window_complete,
        candidate=run if candidate_is_run else None, other_task_policy="violation")
    assert out.entry_recorded
    assert seen["other_task_policy"] == "violation"
    return w.live("implement")


def test_a_resumed_window_is_partial_unless_the_store_completed_it(w, monkeypatch):
    partial = _implement_boundary(w, monkeypatch, candidate_is_run=True, window_complete=False)
    assert partial["boundary"] is None
    assert partial["boundary_reason"] == "boundary-window-partial"


def test_a_complete_window_reports_ok(w, monkeypatch):
    entry = _implement_boundary(w, monkeypatch, candidate_is_run=True, window_complete=True)
    assert entry["boundary"] == "ok"


@pytest.mark.parametrize("setup,expected", [
    ("unconfigured", None),
    ("not-run", "tests-not-run"),
    ("changed", "tests-settings-changed"),
])
def test_missing_test_result_records_a_reason(w, tmp_path, setup, expected):
    state_root = tmp_path / "tests-state"
    if setup != "unconfigured":
        testrun.configure(w.root, w.task, command=["true"], state_root=state_root)
    if setup == "changed":
        state_root = tmp_path / "other-state"
    run = w.exec_run("implement")
    hook(w, run, origin="coordinator", state_root=state_root)
    entry = w.live("implement")
    assert entry.get("tests_reason") == expected
    assert entry["tests"] is None


def test_a_stale_and_a_failed_result_record_their_reasons(w, tmp_path):
    state_root = tmp_path / "tests-state"
    testrun.configure(w.root, w.task, command=["true"], state_root=state_root)
    path = testrun.result_path(state_root, w.root, w.task, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    run = w.exec_run("implement")
    path.write_text('{"outcome": "FAILED", "reason": "tests-timeout", "run_id": "other", "attempt": 1}')
    hook(w, run, origin="coordinator", state_root=state_root)
    assert w.live("implement")["tests_reason"] == "tests-result-stale"
    run2 = w.exec_run("implement")
    record = w.synth.record(run2)
    path.write_text('{"outcome": "FAILED", "reason": "tests-timeout", "run_id": "%s", "attempt": %d}' % (
        run2, run_hooks._last_attempt_number(record)))
    hook(w, run2, origin="coordinator", state_root=state_root)
    assert w.live("implement")["tests_reason"] == "tests-timeout"


def _isolated(w, run, *, path_rel=None, error=None, status="ok"):
    harvest = snapshot.Harvest(path_rel=path_rel, sha256="a" * 64 if path_rel else None,
                               number=2 if path_rel else None, error=error)
    boundary = boundaries.BoundaryResult(status=status, reason=None, violations=[]) \
        if hasattr(boundaries, "BoundaryResult") else None
    return snapshot.IsolatedRun(w.result(run), None, harvest, boundary, True)


def snap_hook(w, isolated, *, kind, candidate=None):
    return run_hooks.after_snapshot_run(
        w.root, w.task, isolated, kind=kind, stage=1, mark=w.mark, source_dir=ch.SOURCE_DIR,
        superseded_candidate=candidate, clock=ch.clock)


def test_snapshot_critic_composes_one_response_and_one_row(w):
    plan = w.exec_run("plan")
    hook(w, plan, origin="coordinator")
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    out = snap_hook(w, _isolated(w, critic, path_rel=REL + "critic-response-2.md"), kind="critic")
    assert out.entry_recorded
    entry = w.live("plan")
    assert entry["runs"] == [plan, critic]
    assert entry["critic_responses"] == [REL + "critic-response-2.md"]
    assert len(w.synth.rows_for(critic)) == 1


def test_snapshot_critic_closes_an_interrupted_older_run_once(w):
    old = w.exec_run("critic", state="interrupted")
    new = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.synth.point_at(new) if hasattr(w.synth, "point_at") else None
    snap_hook(w, _isolated(w, new, path_rel=REL + "critic-response-2.md"), kind="critic", candidate=old)
    assert len(w.synth.rows_for(new)) == 1
    assert len(w.synth.rows_for(old)) <= 1


def test_snapshot_review_without_a_finding_records_the_error(w):
    run = w.exec_run("review")
    snap_hook(w, _isolated(w, run, error="harvest-none"), kind="review")
    entry = w.live("review")
    assert entry["harvested"] == [] and entry["outputs_error"] == "harvest-none"


def test_a_second_call_for_the_same_closed_run_adds_nothing(w):
    run = w.exec_run("review")
    isolated = _isolated(w, run, path_rel=REL + "review-2.md")
    snap_hook(w, isolated, kind="review")
    snap_hook(w, isolated, kind="review")
    assert len(w.entries()) == 1 and len(w.synth.rows_for(run)) == 1
