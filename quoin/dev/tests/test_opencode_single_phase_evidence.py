"""Single-phase runs whose evidence differs from a plain phase: a thorough plan
that delegates the critic to a subagent, and an end-of-task run that moves the
task folder.

Commands proved here: quoin-thorough-plan, quoin-end-of-task.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

import _opencode_gate_helpers as gh
import _opencode_workflow_e2e_helpers as eh
from quoin import cli
from quoin.opencode_adapter import runstore

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

SDIR = ".workflow_artifacts/demo/stage-1"
SYNC_TASK = {"status": "completed", "input": {}, "title": "critic", "output": "reviewed", "metadata": {}}


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario):
        proj = eh.WorkflowProject(tmp_path, monkeypatch, scenario)
        holder.append(proj)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def single_phase(proj, capsys, phase, *extra):
    code = cli.main(["run", eh.TASK, "--runtime", "opencode", "--project-root", str(proj.root),
                     "--profile", "work", "--phase", phase, *extra])
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def test_thorough_plan_with_a_synchronous_critic_subagent_records_a_plan_entry(make, capsys):
    fake = eh.fake
    steps = [
        fake._step_start("prt_s1"),
        fake._task_part("prt_k1", SYNC_TASK),
        eh._write(SDIR + "/current-plan.md", gh.PLAN),
        eh._try(SDIR + "/critic-response-1.md", gh.CRITIC_PASS),
        fake._step_finish("prt_f1", "stop"),
        {"do": "exit", "code": 0},
    ]
    scenario = {"attempts": [{"steps": steps}]}
    proj = make(scenario)
    gh.write(proj.root / ".workflow_artifacts" / eh.TASK / "architecture.md", gh.ARCHITECTURE)
    code, summary = single_phase(proj, capsys, "thorough_plan", "--stage", "1")
    assert code == 0 and summary["outcome"] == "COMPLETED", summary
    assert "quoin-thorough-plan" in proj.commands()
    state = proj.state()
    entries = [e for e in state.get("entries") or [] if e["phase"] == "plan" and e["stage"] == 1]
    assert entries and entries[-1]["evidence"]["coverage"] == "full"
    assert (proj.root / SDIR / "current-plan.md").is_file()


def end_of_task_scenario(finish=True):
    fake = eh.fake
    move = {"do": "move_path", "from": ".workflow_artifacts/demo", "to": ".workflow_artifacts/finalized/demo"}
    return {"attempts": [{"steps": [fake._step_start("prt_s1"), move, fake._step_finish("prt_f1", "stop"),
                                    {"do": "exit", "code": 0}]}]}


def test_end_of_task_moves_the_folder_only_in_this_explicit_run(make, capsys):
    proj = make(end_of_task_scenario())
    gh.build_task(proj.root, eh.TASK, discover=True)
    assert not (proj.root / ".workflow_artifacts" / "finalized").exists()
    code, summary = single_phase(proj, capsys, "end_of_task")
    assert code == 0, summary
    assert "quoin-end-of-task" in proj.commands()
    assert (proj.root / ".workflow_artifacts" / "finalized" / eh.TASK / "architecture.md").is_file()
    assert not (proj.root / ".workflow_artifacts" / eh.TASK / "cost-ledger.md").exists()
    ledger = proj.root / ".workflow_artifacts" / "finalized" / eh.TASK / "cost-ledger.md"
    assert ledger.is_file()
    rows = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.startswith("oc-")]
    run_id = summary["run_id"]
    mine = [r for r in rows if r.startswith(run_id)]
    assert len(mine) == 1 and "outcome=launched" in mine[0] and "src=unresolved" in mine[0], rows


def test_a_rejected_commit_ask_ends_awaiting_approval_and_moves_nothing(make, capsys):
    approval = eh.fake._fixture_scenario("tool-rejected.jsonl")()
    proj = make(approval)
    gh.build_task(proj.root, eh.TASK, discover=True)
    code, summary = single_phase(proj, capsys, "end_of_task")
    assert code == 4 and summary["outcome"] == "AWAITING_APPROVAL", summary
    assert not (proj.root / ".workflow_artifacts" / "finalized").exists()
    assert (proj.root / ".workflow_artifacts" / eh.TASK / "architecture.md").is_file()
