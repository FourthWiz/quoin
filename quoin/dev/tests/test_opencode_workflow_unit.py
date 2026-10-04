"""Whole-task coordinator: settings, sequence, record refresh, context and plain phases."""
from __future__ import annotations

import pytest

import _opencode_gate_helpers as gh
import _opencode_run_helpers as rh
import _opencode_workflow_helpers as wh
from quoin.opencode_adapter import driver, handoff, phase_loop, runstore, testrun, workflow


@pytest.fixture
def w(tmp_path, monkeypatch):
    return wh.CoordWorld(tmp_path, monkeypatch)


def calls(world):
    return [name for name, *_ in world.drv.calls]


# -- sequence ------------------------------------------------------------------


def test_sequence_without_discover_and_with_two_stages(w):
    opts = workflow.WorkflowOptions()  # the fixture already holds the three discover files
    assert workflow.sequence(w.root, w.task, None, wh.SOURCE_DIR, opts) == [
        ("architect", None), ("plan", 1), ("implement", 1), ("review", 1),
        ("plan", 2), ("implement", 2), ("review", 2),
    ]


def test_sequence_starts_with_discover_when_asked_or_when_files_are_missing(w):
    from_flag = workflow.WorkflowOptions(from_discover=True)
    assert workflow.sequence(w.root, w.task, None, wh.SOURCE_DIR, from_flag)[0] == ("discover", None)
    assert workflow.sequence(w.root, w.task, None, wh.SOURCE_DIR, workflow.WorkflowOptions())[0] == (
        "architect", None)
    (w.root / ".workflow_artifacts" / "memory" / "dependencies-map.md").unlink()
    assert workflow.sequence(w.root, w.task, None, wh.SOURCE_DIR, workflow.WorkflowOptions())[0] == (
        "discover", None)


def test_sequence_without_architect_or_stage_decomposition(w, tmp_path):
    state = runstore.new_workflow_state(w.task)
    workflow.write_settings(state, workflow.WorkflowOptions(), include_architect=False, from_discover=False)
    (w.path("architecture.md")).unlink()
    items = workflow.sequence(w.root, w.task, state, wh.SOURCE_DIR, workflow.WorkflowOptions())
    assert items == [("plan", None), ("implement", None), ("review", None)]


def test_settings_block_and_critic_cap():
    state = runstore.new_workflow_state("t1")
    opts = workflow.WorkflowOptions(profile="work", no_pause=True, max_critic_rounds=9)
    workflow.write_settings(state, opts, include_architect=True, from_discover=True)
    block = state["settings"]["workflow"]
    assert block["profile"] == "work" and block["no_pause"] is True and block["from_discover"] is True
    assert state["settings"]["max_critic_rounds"] == workflow.MAX_CRITIC_ROUNDS
    assert state["settings"]["critic_required"] is True
    assert workflow.stored_cap(state, workflow.WorkflowOptions(max_critic_rounds=1)) == 5
    assert workflow.stored_cap(None, workflow.WorkflowOptions()) == workflow.DEFAULT_MAX_CRITIC_ROUNDS


def test_handoff_sequence_names_architect_and_discover_for_coordinator_state(w):
    (w.path("architecture.md")).unlink()
    state = runstore.new_workflow_state(w.task)
    workflow.write_settings(state, workflow.WorkflowOptions(), include_architect=True, from_discover=True)
    items = handoff.sequence(w.root, w.task, state, wh.SOURCE_DIR)
    assert items[:2] == [("discover", None), ("architect", None)]
    plain = runstore.new_workflow_state(w.task)
    assert ("architect", None) not in handoff.sequence(w.root, w.task, plain, wh.SOURCE_DIR)


# -- the record ----------------------------------------------------------------


def test_the_record_written_at_phase_start_names_the_phase_about_to_run(w):
    w.seed_workflow()
    seen = {}

    def spy(drv, request):
        seen["phase"] = w.record_json()["phase"]

    w.drv.effects["architect"] = spy
    coord = w.coordinator()
    step = coord.run_plain_phase("architect", None, gate_after=False)
    assert step.completed and coord.errors == []
    assert seen["phase"]["current"] == "architect"


def test_pin_to_state_keeps_the_state_sha_after_the_plan_changed_on_disk(w):
    w.fx.record("plan", origin="adopted")
    directory = runstore.store_dir(w.root)
    state = runstore.load_workflow_state(directory, w.task)
    plan = "%s/stage-1/current-plan.md" % ".workflow_artifacts/t1"
    recorded = handoff.latest_shas(state)[plan]
    gh.write(w.path("stage-1/current-plan.md"), gh.PLAN + "\nedited\n")
    from _opencode_handoff_helpers import fixed_scope

    kw = dict(scope=fixed_scope(), scope_source="workflow-state", source_dir=wh.SOURCE_DIR)
    pinned = handoff.build_record(w.root, w.task, pin_to_state=True, **kw)
    sha = {a["path"]: a["sha256"] for a in pinned["artifacts"]}[plan]
    assert sha == recorded
    assert handoff.state_agreement(pinned, state) == []
    free = handoff.build_record(w.root, w.task, **kw)
    assert {a["path"]: a["sha256"] for a in free["artifacts"]}[plan] != recorded


# -- executor ------------------------------------------------------------------


def test_a_clean_plan_run_records_a_coordinator_entry_with_an_ok_boundary(w):
    w.seed_workflow()
    w.drv.effects["plan"] = w.write_plan
    step = w.coordinator().run_plain_phase("plan", 1, gate_after=False)
    assert step.completed and step.hook.entry_recorded
    entry = w.live("plan")
    assert entry["origin"] == "coordinator" and entry["boundary"] == "ok"
    request = w.drv.requests[-1]
    assert request.stage == "1" and request.non_interactive is True


def test_implement_refuses_task_commits_on_a_protected_branch(w, monkeypatch):
    w.seed_workflow()
    remote = w.root.parent / "remote.git"
    gh.git(w.root.parent, "init", "-q", "--bare", str(remote))
    branch = gh.git(w.root, "branch", "--show-current").strip()
    gh.git(w.root, "remote", "add", "origin", str(remote))
    gh.git(w.root, "push", "-q", "-u", "origin", branch)
    gh.write(w.root / "src" / "more.py", "y = 2\n")
    gh.git(w.root, "add", "-A")
    gh.git(w.root, "commit", "-q", "-m", "ahead")
    monkeypatch.setenv("QUOIN_PROTECTED_BRANCHES", branch)
    coord = w.coordinator()
    with pytest.raises(workflow.WorkflowRefused) as caught:
        coord.run_plain_phase("implement", 1, gate_after=False)
    assert caught.value.code == "protected-branch" and caught.value.exit_code == 3
    assert "prepare" not in calls(w)
    monkeypatch.setenv("QUOIN_DISABLE_BRANCH_HYGIENE", "1")
    assert coord.run_plain_phase("implement", 1, gate_after=False).completed


def test_implement_refuses_changed_test_settings_before_any_spawn(w, tmp_path):
    w.seed_workflow()
    testrun.configure(w.root, w.task, command=["true"], state_root=tmp_path / "another-root")
    coord = w.coordinator()
    with pytest.raises(workflow.WorkflowRefused) as caught:
        coord.run_plain_phase("implement", 1, gate_after=False)
    assert caught.value.code == "tests-settings-changed"
    assert "prepare" not in calls(w) and "start" not in calls(w)


def test_a_failed_implement_stops_and_never_launches_the_tests(w, monkeypatch):
    w.seed_workflow()
    testrun.configure(w.root, w.task, command=["true"], state_root=w.drv.state_root)
    launched = []
    monkeypatch.setattr(testrun, "run_tests", lambda *a, **k: launched.append(a))
    w.drv.script.append(rh.outcome("failed", reason="boom", evidence="none"))
    step = w.coordinator().run_plain_phase("implement", 1)
    assert step.result.outcome in ("FAILED", "ABORTED") and not step.completed
    assert step.gate is None and not launched and not step.tests_ran
    assert step.exit_code == 2


def test_the_coordinator_runs_the_tests_itself_after_a_completed_implement(w):
    w.seed_workflow()
    testrun.configure(w.root, w.task, command=["true"], state_root=w.drv.state_root)
    step = w.coordinator().run_plain_phase("implement", 1, gate_after=False)
    assert step.completed and step.tests_ran
    entry = w.live("implement")
    assert entry["tests_source"] == "coordinator"
    assert entry["tests"]["exit_code"] == 0 and "tests_reason" not in entry


# -- context -------------------------------------------------------------------


@pytest.fixture
def installed(tmp_path, monkeypatch):
    project = rh.InstalledProject(tmp_path, monkeypatch, "record_only")
    yield project
    project.cleanup()


@pytest.mark.parametrize("stage,head", [("1", "stage 1 of demo"), (None, "demo")])
def test_prepare_places_the_context_suffix_before_the_marker(installed, stage, head):
    import pathlib

    drv = installed.driver_factory()(installed.root)
    (installed.root / ".workflow_artifacts" / "demo").mkdir(parents=True, exist_ok=True)
    ref = ".workflow_artifacts/demo/critic-response-1.md"
    (installed.root / ref).write_text("x\n")

    def build(**kw):
        return drv.prepare(driver.RunRequest(
            project_root=pathlib.Path(installed.root), task="demo", stage=stage, phase="plan",
            profile="work", **kw)).argv[-1]

    assert build(non_interactive=True, context_refs=(ref,)) == "%s (context: %s) (non-interactive run)" % (head, ref)
    assert build(context_refs=(ref,)) == "%s (context: %s)" % (head, ref)
    assert build() == head
    assert build(non_interactive=True) == head + " (non-interactive run)"


def test_a_coordinator_request_matches_the_single_phase_record_except_for_the_mode(w):
    w.seed_workflow()
    run_id = w.drv.seed("interrupted", request={"stage": "1", "phase": "implement", "profile": "personal",
                                                "workspace": None, "non_interactive": False},
                        phase="implement", task=w.task, stage="1", profile="personal")
    record = w.drv.record(run_id)
    request = driver.RunRequest(project_root=w.root, task=w.task, stage="1", phase="implement",
                                profile="personal")
    assert phase_loop._same_request(record, request)
    assert not phase_loop._same_request(record, driver.RunRequest(
        project_root=w.root, task=w.task, stage="1", phase="implement", profile="personal",
        non_interactive=True))
