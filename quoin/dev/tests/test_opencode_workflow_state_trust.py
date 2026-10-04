"""An agent edit to the workflow state file during an interrupted attempt must
not be absorbed as the coordinator's own write when the run is continued."""
from __future__ import annotations

import json
import os
import shutil

import pytest

import _opencode_gate_helpers as gh
import _opencode_workflow_e2e_helpers as eh
from quoin.opencode_adapter import runstore
from test_opencode_workflow_resume import FLOW, NO_RELAUNCH, PLAN1, make, scenario_with  # noqa: F401

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]


def test_a_state_edit_during_an_interrupted_attempt_is_a_boundary_violation_on_continue(make, capsys):
    hung = eh.crash_attempt(eh._write(PLAN1, gh.PLAN + "\nimplemented task one\n"))
    proj = make(scenario_with(hung))
    code, summary = proj.run_workflow(capsys, *FLOW, *NO_RELAUNCH)
    assert code == 5, json.dumps(summary)
    path = runstore.workflow_state_path(runstore.store_dir(proj.root), eh.TASK)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agent_edit"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    text = json.dumps(summary)
    assert code == 2 and "boundary-violation" in text, text[:3000]
