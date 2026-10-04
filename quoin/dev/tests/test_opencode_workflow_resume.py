"""Interruption and resume of the headless coordinator, each case compared with
an uninterrupted run of the same scenario in a second project, plus the
closed-run rerun refusal.

Commands proved here: quoin-implement, quoin-critic, quoin-plan (resume of the
coordinator's phases), and the whole-task form `quoin run --workflow`.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import pytest

import _opencode_gate_helpers as gh
import _opencode_workflow_e2e_helpers as eh
from quoin.opencode_adapter import run_hooks, runstore, workflow

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

FLOW = ("--from-discover", "--no-pause")
NO_RELAUNCH = ("--max-relaunch", "0")
PLAN1 = ".workflow_artifacts/demo/stage-1/current-plan.md"


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario=None, name=None):
        base = tmp_path / name if name else tmp_path
        base.mkdir(parents=True, exist_ok=True)
        proj = eh.WorkflowProject(base, monkeypatch, scenario)
        holder.append(proj)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def implement_attempt(stage=1, plan_marker=True):
    items = []
    if plan_marker:
        items.append(eh._write(PLAN1, gh.PLAN + "\nimplemented task one\n"))
    items.append(eh._write("src/stage%d.py" % stage, "VALUE = %d\n" % stage))
    return eh._step(*items)


def scenario_with(first_implement=None, **kw):
    """The standard scenario whose stage-1 implementer rewrites a plan marker;
    `first_implement` (an interrupted attempt) goes before the finishing one."""
    attempts = ([first_implement] if first_implement else []) + [implement_attempt()]
    return eh.workflow_scenario(extra={"quoin-implement@1": attempts}, **kw)


def reference_prints(make, capsys, scenario, *extra):
    ref = make(scenario, name="reference")
    code, summary = ref.run_workflow(capsys, *FLOW, *extra)
    assert code == 0, json.dumps(summary)
    return eh.prints_of(ref)


def test_case_a_interrupt_mid_implement_after_a_write(make, capsys):
    ref = reference_prints(make, capsys, scenario_with())
    hung = eh.crash_attempt(eh._write(PLAN1, gh.PLAN + "\nimplemented task one\n"))
    proj = make(scenario_with(hung), name="interrupted")
    code, summary = proj.run_workflow(capsys, *FLOW, *NO_RELAUNCH)
    assert code == 5 and summary["outcome"] == "INTERRUPTED", summary
    state = proj.state()
    plan_entry = [e for e in state["entries"] if e["phase"] == "plan" and e["stage"] == 1][-1]
    assert plan_entry["evidence"]["task_hashes"][PLAN1]
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, json.dumps(summary)
    assert "continuation-state-mismatch" not in json.dumps(summary)
    fresh = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-implement"
             and "stage 1 of" in " ".join(i["parsed"]["message"])]
    resumed = [i for i in proj.runs() if "--session" in i["argv"] and "quoin-implementer" in i["argv"]]
    assert len(fresh) == 1 and len(resumed) == 1
    assert resumed[0]["session_id"] == fresh[0]["session_id"]
    entry = [e for e in proj.state()["entries"] if e["phase"] == "implement" and e["stage"] == 1][-1]
    assert len(entry["runs"]) == 1 and entry["boundary"] == "ok"
    assert [c for c in proj.commands() if c == "quoin-review"]
    eh.assert_same_outcome(proj, ref)


def _crash_once(monkeypatch, target, name, *, when):
    original = getattr(target, name)
    fired = {"n": 0}

    def wrapper(*args, **kwargs):
        if fired["n"] == 0 and when(args, kwargs):
            fired["n"] = 1
            raise eh.Crash(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return fired


def test_case_b_crash_after_the_hook_before_the_gate(make, capsys, monkeypatch):
    ref = reference_prints(make, capsys, scenario_with())
    proj = make(scenario_with(), name="interrupted")
    fired = _crash_once(monkeypatch, workflow.Coordinator, "gate",
                        when=lambda a, k: a[2] == "implement" and a[1] == 1)
    code, summary = proj.run_workflow(capsys, *FLOW)
    assert fired["n"] == 1 and code == 1, summary
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, json.dumps(summary)
    stage1 = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-implement"
              and "stage 1 of" in " ".join(i["parsed"]["message"])]
    assert len(stage1) == 1
    eh.assert_same_outcome(proj, ref)


def test_case_c_crash_between_the_entry_write_and_the_refresh(make, capsys, monkeypatch):
    ref = reference_prints(make, capsys, scenario_with())
    proj = make(scenario_with(), name="interrupted")
    armed = {"on": False, "fired": 0}
    original_hook = run_hooks.after_phase_run
    original_refresh = workflow.Coordinator.refresh_record

    def hook(root, task, result, **kw):
        out = original_hook(root, task, result, **kw)
        if getattr(result, "outcome", None) == "COMPLETED" and kw.get("origin") == "coordinator":
            request = (runstore.load_record(runstore.store_dir(root), result.run_id) or {}).get("request") or {}
            if request.get("phase") == "implement" and request.get("stage") == "1" and not armed["fired"]:
                armed["on"] = True
        return out

    def refresh(self):
        if armed["on"]:
            armed["on"] = False
            armed["fired"] += 1
            raise eh.Crash("refresh")
        return original_refresh(self)

    monkeypatch.setattr(run_hooks, "after_phase_run", hook)
    monkeypatch.setattr(workflow.Coordinator, "refresh_record", refresh)
    code, summary = proj.run_workflow(capsys, *FLOW)
    assert armed["fired"] == 1 and code == 1, summary
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, json.dumps(summary)
    assert "continuation-state-mismatch" not in json.dumps(summary)
    stage1 = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-implement"
              and "stage 1 of" in " ".join(i["parsed"]["message"])]
    assert len(stage1) == 1
    eh.assert_same_outcome(proj, ref)


def test_case_d_interrupted_critic_restarts_fresh(make, capsys):
    ref = reference_prints(make, capsys, scenario_with())
    base = scenario_with()
    attempts = base["commands"]["quoin-critic@1"]["attempts"]
    base["commands"]["quoin-critic@1"]["attempts"] = [eh.crash_attempt()] + attempts
    proj = make(base, name="interrupted")
    code, summary = proj.run_workflow(capsys, *FLOW, *NO_RELAUNCH)
    assert code == 5, summary
    critic_runs_before = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-critic"]
    assert len(critic_runs_before) == 1
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, json.dumps(summary)
    critic_launches = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-critic"]
    assert all("--session" not in i["argv"] for i in critic_launches)
    rows = [r for r in eh.ledger_rows(proj) if "| critic |" in r]
    abandoned = [r for r in rows if "outcome=superseded" in r]
    assert len(abandoned) == 1, rows
    eh.assert_same_outcome(proj, ref)


def test_case_e_interrupted_rerun_resumes_with_its_context(make, capsys):
    rejected = gh.REVIEW.replace("APPROVED", "CHANGES_REQUESTED")
    sdir = ".workflow_artifacts/demo/stage-1"
    scenario = eh.workflow_scenario(review_text=[rejected])
    proj = make(scenario, name="interrupted")
    code, summary = proj.run_workflow(capsys, *FLOW, "--through", "review")
    assert code == 7, summary
    first_implement = scenario["commands"]["quoin-implement@1"]["attempts"][0]
    first_review = scenario["commands"]["quoin-review@1"]["attempts"][0]
    scenario["commands"]["quoin-implement@1"] = {"attempts": [
        first_implement, eh.crash_attempt(eh._write("src/stage1.py", "VALUE = 11\n")),
        eh._step(eh._write("src/stage1.py", "VALUE = 11\n"))]}
    scenario["commands"]["quoin-review@1"] = {"attempts": [
        first_review, eh._step(eh._try(sdir + "/review-2.md", gh.REVIEW))]}
    proj.set_scenario(scenario)
    code, summary = proj.run_workflow(
        capsys, "--continue", "--rerun-from", "implement", "--through", "review", *NO_RELAUNCH)
    assert code == 5, json.dumps(summary)
    reruns = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-implement"][-1]
    argument = " ".join(reruns["parsed"]["message"])
    assert "review-1.md" in argument and argument.endswith(eh.MARKER)
    interrupted_id = summary["phases"][-1]["run_ids"][-1]
    code, summary = proj.run_workflow(capsys, "--continue", "--through", "review")
    assert code == 0, json.dumps(summary)[:3000]
    launches = [i for i in proj.runs() if i["parsed"].get("command") == "quoin-implement"]
    assert launches[-1] is not None and launches[-1]["session_id"] == reruns["session_id"]
    resumed = [i for i in proj.runs() if "--session" in i["argv"] and "quoin-implementer" in i["argv"]][-1]
    assert resumed["session_id"] == reruns["session_id"]
    record = runstore.load_record(runstore.store_dir(proj.root), interrupted_id)
    assert "review-1.md" in " ".join(record["request"]["context_refs"])
    entries = [e for e in proj.state()["entries"] if e["phase"] == "implement" and e["stage"] == 1 and not e["superseded"]]
    assert len(entries) == 1 and entries[0]["runs"] == [interrupted_id]
    review = [e for e in proj.state()["entries"] if e["phase"] == "review" and e["stage"] == 1 and not e["superseded"]]
    assert review and review[0]["gate"]["verdict"] == "PASS"


def test_sigterm_cancels_and_the_next_invocation_is_not_locked_out(make, capsys):
    hung = eh.hang_attempt(eh._write(PLAN1, gh.PLAN + "\nimplemented task one\n"))
    proj = make(scenario_with(hung))
    code, summary = eh.interrupted_workflow(proj, capsys, "quoin-implement", *FLOW)
    assert code == 143 and summary["outcome"] == "CANCELLED", summary
    # a cancelled run is final: continuing re-reports it, without a record mismatch
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 143 and "continuation-state-mismatch" not in json.dumps(summary), summary
    code, summary = proj.run_workflow(capsys, "--continue", "--rerun-from", "implement", *FLOW)
    assert code == 0, json.dumps(summary)[:3000]
    assert "continuation-state-mismatch" not in json.dumps(summary)


def test_a_state_rewrite_between_attempts_is_not_a_boundary_violation(make, capsys):
    hung = eh.crash_attempt(eh._write(PLAN1, gh.PLAN + "\nimplemented task one\n"))
    proj = make(scenario_with(hung))
    code, summary = proj.run_workflow(capsys, *FLOW, *NO_RELAUNCH)
    assert code == 5, json.dumps(summary)
    time.sleep(1.2)  # the state file carries a one-second timestamp the continuation rewrites
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, json.dumps(summary)
    entry = [e for e in proj.state()["entries"] if e["phase"] == "implement" and e["stage"] == 1][-1]
    assert entry["boundary"] == "ok"


# -- closed runs with a stored boundary violation -------------------------------


def seed_closed_violation(proj, *, non_interactive, run_id=None):
    directory = runstore.store_dir(proj.root, create=True)
    if run_id is None:
        run_id, _ = runstore.reserve_run_id(directory)
        request = {"task": eh.TASK, "stage": "1", "phase": "plan", "profile": "work",
                   "workspace": None, "non_interactive": non_interactive}
        record = runstore.new_run_record(run_id, eh.TASK, request, {})
    else:
        record = runstore.load_record(directory, run_id)
    record["state"] = "interrupted"
    record["telemetry"] = {"final": True, "boundary": {
        "status": "violation", "reason": "boundary-violation",
        "violations": [{"path": "src/app.py", "change": "changed", "rule": "outside-scope"}]}}
    runstore.write_record(directory, record)
    runstore.write_pointer(directory, runstore.new_pointer(eh.TASK, run_id))
    return run_id


def ledger_text(proj):
    path = proj.root / ".workflow_artifacts" / eh.TASK / "cost-ledger.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_closed_violation_run_is_re_reported_by_a_single_phase_run(make, capsys):
    from quoin import cli
    proj = make(name="closed")
    seed_closed_violation(proj, non_interactive=False)
    before = ledger_text(proj)
    code = cli.main(["run", eh.TASK, "--runtime", "opencode", "--project-root", str(proj.root),
                     "--profile", "work", "--phase", "plan", "--stage", "1"])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and summary["outcome"] == "FAILED", summary
    assert summary["reason"] == "boundary-violation"
    assert ledger_text(proj) == before
    assert proj.runs() == []
    assert not proj.state().get("entries")


def test_closed_violation_run_in_the_coordinator_is_reported_then_rerun(make, capsys):
    base = eh.workflow_scenario()
    plan = base["commands"]["quoin-plan@1"]["attempts"]
    sdir = ".workflow_artifacts/demo/stage-1"
    # the planner also edits a source file, which a planner may not do
    base["commands"]["quoin-plan@1"]["attempts"] = [
        eh._step(eh._write(sdir + "/current-plan.md", gh.PLAN), eh._write("src/app.py", "x = 2\n"))] + plan
    proj = make(base, name="closed")
    code, summary = proj.run_workflow(capsys, *FLOW, "--through", "plan")
    assert code == 2 and summary["reasons"] == ["boundary-violation"], json.dumps(summary)
    launches = len(proj.runs())
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW, "--through", "plan")
    assert code == 2 and summary["reasons"] == ["boundary-violation"], json.dumps(summary)
    assert len(proj.runs()) == launches
    code, summary = proj.run_workflow(capsys, "--continue", "--rerun-from", "plan", *FLOW, "--through", "plan")
    assert code == 99, json.dumps(summary)
    assert len(proj.runs()) > launches
