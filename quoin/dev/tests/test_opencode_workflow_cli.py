"""`quoin run --runtime opencode --workflow` and `quoin opencode workflow next`."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _opencode_handoff_helpers as hh
from quoin import cli
from quoin.opencode_adapter import handoff, paths, runstore, testrun
from _opencode_run_helpers import ScriptedDriver

WORKFLOW_ONLY = [
    ["--continue"], ["--no-pause"], ["--through", "plan"], ["--from-discover"],
    ["--max-critic-rounds", "3"], ["--test-command", "pytest"], ["--test-include", "x"],
    ["--test-timeout", "30"], ["--rerun-from", "plan"], ["--adopt", "plan"],
]


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root.resolve()


@pytest.fixture()
def stub(monkeypatch, project, tmp_path):
    holder = {"built": 0, "driver": None, "state_root": tmp_path / "state-root"}

    def factory(root):
        holder["built"] += 1
        drv = ScriptedDriver(root, [])
        drv.state_root = holder["state_root"]
        holder["driver"] = drv
        return drv

    monkeypatch.setattr(cli, "_make_opencode_driver", factory)
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
    monkeypatch.setattr(cli, "_handoff_evaluator", lambda root: (lambda profile: hh.fixed_scope()))
    return holder


def argv(project, *extra, workflow=True, profile="work", phase=None):
    out = ["run", "demo", "--runtime", "opencode", "--project-root", str(project)]
    if profile:
        out += ["--profile", profile]
    if workflow:
        out.append("--workflow")
    if phase:
        out += ["--phase", phase]
    return out + list(extra)


def run(project, capsys, *extra, **kw):
    code = cli.main(argv(project, *extra, **kw))
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def lock_path(project):
    return project / ".workflow_artifacts" / "memory" / "run-supervisor-demo.pid"


@pytest.mark.parametrize("flag", WORKFLOW_ONLY + [["--workflow"]])
def test_new_flags_need_the_opencode_runtime(project, capsys, flag):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "demo", "--project-root", str(project)] + flag)
    assert exc.value.code == 2
    assert "only valid with --runtime opencode" in capsys.readouterr().err


@pytest.mark.parametrize("flag", WORKFLOW_ONLY)
def test_workflow_only_flags_need_workflow(project, capsys, flag, stub):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv(project, *flag, workflow=False, phase="plan"))
    assert exc.value.code == 2
    assert "only valid with --workflow" in capsys.readouterr().err
    assert stub["built"] == 0


@pytest.mark.parametrize("extra", [["--stage", "1"], ["--new-run"]])
def test_workflow_rejects_stage_and_new_run(project, capsys, stub, extra):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv(project, *extra))
    assert exc.value.code == 2 and stub["built"] == 0


def test_workflow_and_phase_conflict(project, capsys, stub):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv(project, phase="plan"))
    assert exc.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_bare_form_keeps_the_whole_task_refusal(project, capsys, stub):
    code, summary = run(project, capsys, workflow=False)
    assert code == 3 or code == 2
    assert summary["refusal"]["code"] == "whole-task-unavailable"
    assert stub["built"] == 0


@pytest.mark.parametrize("extra", [["--non-interactive"], ["--autonomous"]])
def test_no_effect_flags_are_accepted(project, capsys, stub, extra):
    code, summary = run(project, capsys, "--through", "discover", *extra)
    assert summary["mode"] == "workflow" and summary["outcome"] != "REFUSED"


def test_held_lock_refuses_and_nothing_spawns(project, capsys, stub):
    lock_path(project).write_text(json.dumps({"pid": os.getppid(), "writer": "cli", "task": "demo"}) + "\n")
    code, summary = run(project, capsys)
    assert code == 3 and summary["outcome"] == "REFUSED"
    assert summary["refusal"]["code"] == "lock-held"
    assert stub["built"] == 0 and lock_path(project).exists()


def test_invalid_task_name_refuses(project, capsys, stub):
    code = cli.main(["run", "../x", "--runtime", "opencode", "--profile", "work", "--workflow",
                     "--project-root", str(project)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and out["refusal"]["code"] == "invalid-task-name"


def test_test_command_pin_lives_under_the_driver_state_root(project, capsys, stub, monkeypatch):
    other = paths.state_dir(os.environ, Path.home())
    assert Path(stub["state_root"]) != Path(other)
    code, summary = run(project, capsys, "--test-command", "python -c pass", "--test-timeout", "30")
    assert summary["mode"] == "workflow"
    drv = stub["driver"]
    pin = testrun.result_dir(drv.state_root, project, "demo") / testrun.PIN_NAME
    assert pin.is_file()
    assert not (testrun.result_dir(other, project, "demo") / testrun.PIN_NAME).exists()
    directory = runstore.store_dir(project, create=True)
    state = runstore.load_workflow_state(directory, "demo")
    assert state["settings"]["max_critic_rounds"] == 2 and state["settings"]["critic_required"] is True


def test_a_second_new_run_refuses_workflow_started(project, capsys, stub):
    run(project, capsys, "--through", "discover")
    code, summary = run(project, capsys)
    assert code == 3 and summary["refusal"]["code"] == "workflow-started"


def test_halt_on_abort_writes_the_result_file(project, capsys, stub):
    code, summary = run(project, capsys, "--halt-on-abort")
    result = project / ".workflow_artifacts" / "memory" / "run-supervisor-demo.result"
    assert result.exists()
    assert not lock_path(project).exists()


# -- quoin opencode workflow next ------------------------------------------------


def nxt(project, capsys, *extra):
    code = cli.main(["opencode", "workflow", "next", "--task", "demo", "--project-root", str(project)] + list(extra))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_next_with_no_state_names_the_fresh_command(project, capsys, stub):
    code, out = nxt(project, capsys)
    assert code == 0 and out["outcome"] == "NEXT_STEP" and out["status"] == "not-started"
    assert "--workflow demo" in out["next"] and "--continue" not in out["next"]


def seed_state(project, *, passed=(), failed=()):
    directory = runstore.store_dir(project, create=True)
    state = runstore.new_workflow_state("demo", lambda: 1.0)
    from quoin.opencode_adapter import workflow

    workflow.write_settings(state, workflow.WorkflowOptions(profile="work"), include_architect=False, from_discover=False)
    for phase in passed:
        state["entries"].append({"phase": phase, "stage": None, "runs": [], "origin": "coordinator",
                                 "gate": {"verdict": "PASS", "reasons": []}})
    for phase in failed:
        state["entries"].append({"phase": phase, "stage": None, "runs": [], "origin": "coordinator",
                                 "gate": {"verdict": "FAIL", "reasons": ["x"]}})
    runstore.write_workflow_state(directory, state)


def test_next_after_a_passed_plan_is_the_continue_command(project, capsys, stub):
    (project / ".workflow_artifacts" / "demo").mkdir()
    seed_state(project, passed=("discover", "plan"))
    code, out = nxt(project, capsys)
    assert code == 0 and out["status"] == "paused-at-gate"
    assert "--continue" in out["next"] and "--rerun-from" not in out["next"]


def test_next_for_a_failed_gate_offers_text_only(project, capsys, stub):
    (project / ".workflow_artifacts" / "demo").mkdir()
    seed_state(project, passed=("discover",), failed=("plan",))
    code, out = nxt(project, capsys)
    assert out["status"] == "gate-failed" and out["next"] is None
    assert "--rerun-from plan" in out["hint"]


def test_next_refuses_a_redirected_project_under_the_launcher(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_STATE_DIR, str(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code = cli.main(["opencode", "workflow", "next", "--task", "demo", "--project-root", str(elsewhere)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and out["refusal"]["code"] == "project-root-refused"


def test_next_writes_nothing(project, capsys, stub):
    before = sorted(str(p) for p in project.rglob("*"))
    nxt(project, capsys)
    assert sorted(str(p) for p in project.rglob("*")) == before
