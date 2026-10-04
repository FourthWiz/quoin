"""State and test-settings foundations for the whole-task coordinator."""
from __future__ import annotations

import json

import pytest

from quoin.opencode_adapter import runstore, testrun


def clock():
    return 1_700_000_000.0


def entry(phase="plan", stage=1, **extra):
    return dict(phase=phase, origin="coordinator", stage=stage, **extra)


def test_supersede_marks_only_named_phases_of_named_stage():
    state = runstore.new_workflow_state("t1", clock)
    for stage in (1, 2):
        for phase in ("plan", "implement", "review"):
            runstore.record_phase_entry(state, entry(phase, stage), clock)
    assert runstore.supersede_items(state, 1, ["implement", "review"], clock) == 2
    live = {(e["stage"], e["phase"]) for e in state["entries"] if not e["superseded"]}
    assert live == {(1, "plan"), (2, "plan"), (2, "implement"), (2, "review")}
    assert runstore.supersede_items(state, 1, ["implement"], clock) == 0


def test_eleventh_superseded_entry_drops_the_oldest_and_keeps_order():
    state = runstore.new_workflow_state("t1", clock)
    runstore.record_phase_entry(state, entry("review", 1, tag="other"), clock)
    for i in range(12):
        runstore.record_phase_entry(state, entry("plan", 1, tag=i), clock)
    plans = [e for e in state["entries"] if e["phase"] == "plan"]
    superseded = [e for e in plans if e["superseded"]]
    assert len(superseded) == runstore.MAX_SUPERSEDED_PER_ITEM
    assert [e["tag"] for e in superseded] == list(range(1, 11))
    live = [e for e in plans if not e["superseded"]]
    assert [e["tag"] for e in live] == [11]
    assert [e["phase"] for e in state["entries"]][0] == "review"


@pytest.fixture()
def configured(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    return project, tmp_path / "state"


def test_settings_status_values(configured, tmp_path):
    project, root = configured
    assert testrun.settings_status(project, "t1", state_root=root) == "tests-not-configured"
    testrun.configure(project, "t1", command=["true"], state_root=root)
    assert testrun.settings_status(project, "t1", state_root=root) is None
    other = tmp_path / "other-state"
    assert testrun.settings_status(project, "t1", state_root=other) == "tests-settings-changed"


def test_read_result_guards(configured):
    project, root = configured
    assert testrun.read_result(root, project, "t1", 1) is None
    path = testrun.result_path(root, project, "t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"outcome": "FAILED", "reason": "tests-failed"}))
    assert testrun.read_result(root, project, "t1", 1)["reason"] == "tests-failed"
    path.write_text("{nope")
    assert testrun.read_result(root, project, "t1", 1) is None
    path.write_text(" " * (testrun.MAX_RESULT_BYTES + 1))
    assert testrun.read_result(root, project, "t1", 1) is None
    path.unlink()
    target = path.parent / "elsewhere.json"
    target.write_text("{}")
    path.symlink_to(target)
    assert testrun.read_result(root, project, "t1", 1) is None
