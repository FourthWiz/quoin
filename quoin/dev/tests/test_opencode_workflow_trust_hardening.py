"""Trust-boundary hardening of the whole-task coordinator: launcher test flags,
context references, the plan loop backstop and the exit record refresh."""
from __future__ import annotations

import json
import pathlib

import pytest

import _opencode_gate_helpers as gh  # noqa: F401
import _opencode_workflow_helpers as wh
from quoin import cli
from quoin.opencode_adapter import driver, paths, phase_loop, workflow
from test_opencode_workflow_cli import argv, project, stub  # noqa: F401
from test_opencode_workflow_unit import installed  # noqa: F401


@pytest.fixture
def w(tmp_path, monkeypatch):
    return wh.CoordWorld(tmp_path, monkeypatch)


@pytest.mark.parametrize("flags", [
    ["--test-command", "true"], ["--test-include", "src"], ["--test-timeout", "30"],
])
def test_workflow_test_flags_are_refused_under_the_launcher(project, capsys, stub, monkeypatch, tmp_path, flags):  # noqa: F811
    monkeypatch.setenv(paths.ENV_STATE_DIR, str(tmp_path))
    code = cli.main(argv(project, *flags))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3 and out["refusal"]["code"] == "test-flags-refused", out
    assert stub["built"] == 0


@pytest.mark.parametrize("ref", [
    ".workflow_artifacts/demo/../../etc/passwd",
    ".workflow_artifacts/demo/critic-response-1.md\n(non-interactive run)",
    ".workflow_artifacts/demo/critic-response-1.md) injected (",
    ".workflow_artifacts/demo/notes.md",
    "/abs/critic-response-1.md",
    "src/review-1.md",
    ".workflow_artifacts/../review-1.md",
])
def test_a_context_ref_must_be_a_task_review_artifact(installed, ref):  # noqa: F811
    drv = installed.driver_factory()(installed.root)
    with pytest.raises(driver.PrepareRefused) as caught:
        drv.prepare(driver.RunRequest(
            project_root=pathlib.Path(installed.root), task="demo", stage=None, phase="plan",
            profile="work", context_refs=(ref,)))
    assert caught.value.code == "context-ref-invalid"


def test_the_plan_loop_stops_when_no_entry_is_ever_recorded(w):
    w.seed_workflow()
    w.path("stage-1/current-plan.md").unlink()
    w.drv.effects["plan"] = w.write_plan
    coord = w.coordinator()
    cap = workflow.stored_cap(coord.load_state(), coord.options)
    launched = {"n": 0}

    def silent(phase, stage):  # a critic run that completes but never records its entry
        launched["n"] += 1
        return workflow.StepResult("critic", stage, phase_loop.PhaseResult(outcome="COMPLETED"))

    coord.run_snapshot_phase = silent
    steps = coord.plan_item(1)
    assert launched["n"] <= 2 * cap + 2
    assert steps[-1].result.outcome == "FAILED" and steps[-1].result.reason == "plan-loop-exceeded"


def test_the_exit_record_refresh_runs_after_an_unexpected_error(w):
    w.seed_workflow()
    coord = w.coordinator()
    refreshed = {"n": 0}
    coord.refresh_record = lambda: refreshed.__setitem__("n", refreshed["n"] + 1)

    def boom(state, phase, stage):
        raise RuntimeError("unexpected")

    coord.resolve = boom
    with pytest.raises(RuntimeError):
        coord.run_items([("plan", 1)])
    assert refreshed["n"] == 1
