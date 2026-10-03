"""Role boundaries for single-phase `quoin run`, through `cli.main` against the
real driver and the fake executable, in a real git repository."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import _opencode_boundary_helpers as bh
import _opencode_driver_helpers as dh
import _opencode_gate_helpers as gh
from quoin import cli
from quoin.opencode_adapter import run_hooks, runstore, testrun

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

TASK = bh.TASK
SRC = str(Path(__file__).resolve().parents[3] / "src")
PLAN_REL = ".workflow_artifacts/demo/stage-1/current-plan.md"


class World(bh.SnapshotProject):
    def __init__(self, tmp_path, monkeypatch, scenario):
        super().__init__(tmp_path, monkeypatch, scenario)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(cli, "_make_opencode_driver", self.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)

    def set_scenario(self, scenario):
        dh.fake.write_scenario(self.tmp / "s.json", dh.scenario_dict(scenario))

    def run_cli(self, capsys, phase="plan", *extra, stage="default"):
        stage = ("1" if phase in ("plan", "implement", "review", "critic") else None) if stage == "default" else stage
        argv = ["run", TASK, "--runtime", "opencode", "--profile", "work", "--phase", phase,
                "--project-root", str(self.root)]
        if stage is not None:
            argv += ["--stage", stage]
        code = cli.main(argv + list(extra))
        lines = capsys.readouterr().out.strip().splitlines()
        return code, json.loads(lines[-1])

    def entries(self):
        state = runstore.load_workflow_state(runstore.store_dir(self.root), TASK)
        return list((state or {}).get("entries") or [])

    def gate(self, capsys, phase="plan", stage="1"):
        argv = ["opencode", "gate", "--task", TASK, "--phase", phase, "--project-root", str(self.root)]
        if stage is not None:
            argv += ["--stage", stage]
        code = cli.main(argv)
        return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    @property
    def state_root(self):
        return self.world.tmp / "state" / "quoin" / "opencode"

    def telemetry(self, run_id):
        return self.record(run_id)["telemetry"]


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario="record_only"):
        proj = World(tmp_path, monkeypatch, scenario)
        holder.append(proj)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def writes(*items):
    return ("writes_paths", {"paths": [list(i) for i in items]})


def last_entry(proj):
    return proj.entries()[-1]


# -- violations ---------------------------------------------------------------


def test_an_architect_that_edits_source_fails_and_the_gate_refuses(make, capsys):
    proj = make(writes(("src/app.py", "x = 2\n")))
    code, summary = proj.run_cli(capsys, "architect")
    assert code == 2 and summary["outcome"] == "FAILED" and summary["reason"] == "boundary-violation"
    entry = last_entry(proj)
    assert entry["boundary"] == "violation"
    telemetry = proj.telemetry(summary["run_id"])["boundary"]
    assert telemetry["status"] == "violation" and telemetry["violations"]
    code, data = proj.gate(capsys, "architect", None)
    assert "boundary-violation" in data["reasons"]


def test_an_implementer_that_edits_an_installed_command_is_a_violation(make, capsys):
    proj = make(("edits_path", {"path": ".opencode/commands/quoin-plan.md"}))
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 2 and summary["reason"] == "boundary-violation"
    assert last_entry(proj)["boundary"] == "violation"


def test_a_planner_that_writes_into_the_coordinator_store_is_a_violation(make, capsys):
    proj = make(writes((".workflow_artifacts/memory/runtime/opencode/stray-note.json", "{}")))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 2 and summary["reason"] == "boundary-violation"
    assert last_entry(proj)["boundary"] == "violation"


# -- allowed writes ---------------------------------------------------------


def test_launch_time_managed_files_are_not_violations(make, capsys):
    plan = gh.PLAN + "\nmore\n"
    proj = make(("opencode_managed_writes", {"extra": [[PLAN_REL, plan]]}))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 0 and summary["outcome"] == "COMPLETED"
    entry = last_entry(proj)
    assert entry["boundary"] == "ok"
    assert proj.telemetry(summary["run_id"])["boundary"]["status"] == "ok"


def test_a_stage_plan_and_a_session_file_are_ok_without_a_warning(make, capsys):
    proj = make(writes(
        (PLAN_REL, gh.PLAN + "\nrevised\n"),
        (".workflow_artifacts/memory/sessions/2026-01-01-demo.md", "state\n")))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 0
    assert last_entry(proj)["boundary"] == "ok"
    code, data = proj.gate(capsys, "plan")
    assert "boundary-unverified" not in data.get("warnings", [])


def test_another_tasks_folder_without_a_live_lock_is_unverified_for_a_single_phase(make, capsys):
    proj = make(writes((".workflow_artifacts/other/notes.md", "x\n")))
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 0 and summary["outcome"] != "FAILED"
    entry = last_entry(proj)
    assert entry["boundary"] is None and entry["boundary_reason"] == "concurrent-writer-unlocked"


def test_a_live_lock_on_another_task_downgrades_its_writes(make, capsys):
    proj = make(writes((".workflow_artifacts/other/notes.md", "x\n")))
    lock = proj.root / ".workflow_artifacts" / "memory" / "run-supervisor-other.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 0 and summary["outcome"] != "FAILED"
    entry = last_entry(proj)
    assert entry["boundary"] is None and entry["boundary_reason"] == "concurrent-task-run"


def test_a_stale_open_run_and_new_run_are_reconciled_inside_the_window(make, capsys):
    proj = make("record_only")
    directory = runstore.store_dir(proj.root, create=True)
    old, _ = runstore.reserve_run_id(directory)
    record = runstore.new_run_record(
        old, TASK, {"task": TASK, "stage": "1", "phase": "plan", "profile": "work"}, {})
    record["state"] = "running"
    runstore.write_record(directory, record)
    runstore.write_pointer(directory, runstore.new_pointer(TASK, old))
    code, summary = proj.run_cli(capsys, "plan", "--new-run")
    assert code == 0 and summary["run_id"] != old
    assert proj.record(old).get("superseded_by") == summary["run_id"]
    assert last_entry(proj)["boundary"] == "ok"


def test_a_concurrent_task_writing_its_own_folder_during_the_run_is_unverified(make, capsys):
    proj = make(writes((".workflow_artifacts/other/notes.md", "x\n")))
    lock = proj.root / ".workflow_artifacts" / "memory" / "run-supervisor-other.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    code, summary = proj.run_cli(capsys, "implement")
    assert summary["outcome"] != "FAILED"
    assert last_entry(proj)["boundary_reason"] == "concurrent-task-run"


def test_a_fake_git_world_keeps_an_unverified_source(tmp_path, monkeypatch, capsys):
    from _opencode_cost_helpers import make_cost_project

    proj = make_cost_project(tmp_path, monkeypatch, "record_only")
    try:
        code, summary = proj.run_cli(capsys)
        assert code == 0
        entry = proj.entries()[-1]
        assert entry["boundary"] is None
    finally:
        proj.cleanup()


# -- windows and clocks -----------------------------------------------------


def _interrupted_then_resumed(make, capsys, *, blocked):
    first = [{"do": "write_file", "path": "src/app.py", "content": "x = 2\n"},
             dh.fake._step_start("prt_s1")]
    if not blocked:
        first.append(dh.fake._step_finish("prt_f1", "tool-calls"))
    first.append({"do": "crash", "signal": "SIGKILL"})
    proj = make(dh.fake._continuation(first, dh.fake._clean_second_attempt("2")))
    code, one = proj.run_cli(capsys, "architect", "--max-relaunch", "0")
    assert code == 5
    return proj, one


def test_a_resumed_run_reports_a_partial_window_never_ok(make, capsys):
    proj, one = _interrupted_then_resumed(make, capsys, blocked=False)
    assert proj.entries() == []
    code, two = proj.run_cli(capsys, "architect", "--max-relaunch", "0")
    assert code == 0 and two["run_id"] == one["run_id"]
    entry = last_entry(proj)
    assert entry["boundary"] is None and entry["boundary_reason"] == "boundary-window-partial"


def test_a_blocked_close_of_an_earlier_run_is_partial_too(make, capsys):
    proj, one = _interrupted_then_resumed(make, capsys, blocked=True)
    code, two = proj.run_cli(capsys, "architect", "--max-relaunch", "0")
    assert two["run_id"] == one["run_id"] and two.get("resume_blocked") == "effect-uncertain"
    entries = proj.entries()
    assert entries and entries[-1]["boundary"] is None
    assert entries[-1]["boundary_reason"] == "boundary-window-partial"


def test_a_fresh_run_under_a_fixed_driver_clock_is_not_partial(make, capsys):
    # the driver stamps attempts from a fixed 2026 clock while the listings use
    # wall time; nothing compares the two
    proj = make(writes((PLAN_REL, gh.PLAN + "\nnew\n")))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 0
    assert last_entry(proj)["boundary"] == "ok"
    assert proj.telemetry(summary["run_id"])["boundary"]["reason"] is None


# -- test results -----------------------------------------------------------


def configure(proj, command=("sh", "-c", "true")):
    testrun.configure(proj.root, TASK, command=list(command))


BOOT = (
    "import sys; sys.path.insert(0, %r); from quoin import cli; "
    "raise SystemExit(cli.main(['opencode', 'test-run', '--task', 'demo', '--stage', '1']))" % SRC
)


def test_the_child_of_a_run_writes_where_the_hook_reads(make, capsys, monkeypatch):
    proj = make("record_only")
    monkeypatch.setenv("XDG_STATE_HOME", str(proj.world.tmp / "elsewhere"))
    configure(proj)
    proj.set_scenario(dh.fake._scenario(
        {"do": "run_cmd", "argv": [sys.executable, "-c", BOOT]}, *dh.fake._clean_finish()))
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 0, summary
    assert any(line.startswith("ran 0 ") for line in proj.effects()), proj.effects()
    entry = last_entry(proj)
    assert entry["tests"]["exit_code"] == 0 and entry["tests"]["run_id"] == summary["run_id"]
    code, data = proj.gate(capsys, "implement")
    assert "tests-failed" not in data["reasons"]


def test_without_the_launcher_variable_the_result_lands_elsewhere(make, capsys):
    proj = make("record_only")
    configure(proj)
    proj.set_scenario(dh.fake._scenario(
        {"do": "run_cmd", "argv": ["env", "-u", "QUOIN_OPENCODE_STATE_DIR", sys.executable, "-c", BOOT]},
        *dh.fake._clean_finish()))
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 0
    assert any(line.startswith("ran 0 ") for line in proj.effects()), proj.effects()
    assert "tests" not in last_entry(proj) or last_entry(proj)["tests"] is None


def test_a_result_forged_inside_the_project_is_ignored_and_flagged(make, capsys):
    forged = {"outcome": "PASSED", "exit_code": 0, "run_id": "x", "attempt": 1}
    proj = make(writes((".workflow_artifacts/memory/runtime/opencode/tests/demo/1-latest.json",
                        json.dumps(forged))))
    configure(proj)
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 2 and summary["reason"] == "boundary-violation"
    entry = last_entry(proj)
    assert entry["boundary"] == "violation" and not entry.get("tests")


def _record(run_id, attempts=(1,)):
    return {"run_id": run_id, "attempts": [{"attempt": n, "state": "completed"} for n in attempts]}


@pytest.mark.parametrize("payload, accepted", [
    ({"outcome": "PASSED", "run_id": "r1", "attempt": 2, "exit_code": 0}, True),
    ({"outcome": "PASSED", "run_id": "other", "attempt": 2, "exit_code": 0}, False),
    ({"outcome": "PASSED", "run_id": "r1", "attempt": 1, "exit_code": 0}, False),
    ({"outcome": "FAILED", "run_id": "r1", "attempt": 2, "exit_code": 0,
      "reason": "tests-real-tree-changed"}, False),
])
def test_only_a_passed_result_of_this_run_and_its_last_attempt_is_copied(tmp_path, payload, accepted):
    root = tmp_path / "proj"
    root.mkdir()
    state = tmp_path / "state"
    path = testrun.result_path(state, root, "demo", 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    got = run_hooks._read_test_result(state, root, "demo", 1, _record("r1", (1, 2)))
    assert (got is not None) is accepted
