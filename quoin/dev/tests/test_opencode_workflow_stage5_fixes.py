"""Recovery semantics of the whole-task coordinator: critic harvest failures and resumed windows."""
from __future__ import annotations

import hashlib
import json

import pytest

import _opencode_gate_helpers as gh
import _opencode_workflow_helpers as wh
from quoin.opencode_adapter import run_hooks, runstore


@pytest.fixture
def w(tmp_path, monkeypatch):
    return wh.CoordWorld(tmp_path, monkeypatch)


def test_a_failed_critic_harvest_is_reported_on_continue_and_plan_is_not_rerun(w):
    w.seed_workflow()
    w.path("stage-1/current-plan.md").unlink()
    w.drv.effects["plan"] = w.write_plan
    good = w.finding_effect("critic-response-7.md", [gh.CRITIC_REVISE])
    seen = {"n": 0}

    def critic(driver, request):
        seen["n"] += 1
        if seen["n"] == 1:
            good(driver, request)  # round one: a REVISE response is harvested

    w.drv.effects["critic"] = critic
    coord = w.coordinator()
    steps = coord.plan_item(1)
    # round two plan completes, then its critic produces nothing to harvest
    last = steps[-1]
    assert last.result.outcome == "FAILED" and last.result.reason

    entry = w.live("plan")
    assert entry["critic_errors"] and entry["critic_errors"][-1]["run"] == entry["runs"][-1]
    plan_runs = [r for r in w.drv.requests if r.phase == "plan"]

    # a later invocation resolves to a stored failure, not a new plan round
    again = w.coordinator()
    state = again.load_state()
    res = again.resolve(state, "plan", 1)
    assert res.kind == "report" and res.outcome == "FAILED" and res.exit_code == 2
    assert res.reasons == (last.result.reason,)
    assert [r for r in w.drv.requests if r.phase == "plan"] == plan_runs


def _state_rel(w):
    path = runstore.workflow_state_path(runstore.store_dir(w.root), w.task)
    return path, str(path.relative_to(w.root)).replace("\\", "/")


def test_an_edit_to_the_workflow_state_during_an_interrupted_attempt_is_not_the_resume_baseline(w):
    w.seed_workflow()
    coord = w.coordinator()
    path, rel = _state_rel(w)
    run_id, _ = runstore.reserve_run_id(runstore.store_dir(w.root, create=True))
    listing = run_hooks.before_boundary(w.root, w.task)
    assert listing is not None and rel in listing.entries
    coord.window.save_pending(1, "implement", listing)
    assert coord.window.bind(run_id)

    # the interrupted attempt changes the coordinator-owned file; nobody noted it
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agent_edit"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    got, complete = coord._pre_run_listing(1, "implement", {"run_id": run_id})
    assert complete
    assert got.entries[rel] == listing.entries[rel]  # still the pre-run entry, so the edit differs from it

    # a write the coordinator itself noted for that run is accepted
    coord.window.note_write(run_id, rel, hashlib.sha256(path.read_bytes()).hexdigest())
    got, _ = coord._pre_run_listing(1, "implement", {"run_id": run_id})
    assert got.entries[rel] != listing.entries[rel]
