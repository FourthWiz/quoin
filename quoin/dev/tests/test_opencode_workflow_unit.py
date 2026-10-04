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


# -- part two: critic loop, review, gates, pause and outcomes --------------------

UNPARSEABLE = gh.CRITIC_PASS + "\n## Verdict\n\nREVISE\n"


def critic_world(w, bodies, **opt):
    w.seed_workflow()
    w.path("stage-1/current-plan.md").unlink()  # the plan round writes it
    w.drv.effects["plan"] = w.write_plan
    w.drv.effects["critic"] = w.finding_effect("critic-response-7.md", bodies)
    return w.coordinator(**opt)


def test_revise_then_pass_converges_in_two_rounds_and_passes_the_context_on(w):
    coord = critic_world(w, [gh.CRITIC_REVISE, gh.CRITIC_PASS])
    steps = coord.plan_item(1)
    assert [s.result.outcome for s in steps] == ["COMPLETED"] * 4
    entry = w.live("plan")
    assert entry["origin"] == "coordinator" and len(entry["critic_responses"]) == 2
    assert len(entry["runs"]) == 4
    plan_requests = [r for r in w.drv.requests if r.phase == "plan"]
    assert plan_requests[0].context_refs == ()
    assert plan_requests[1].context_refs == (entry["critic_responses"][0],)
    assert steps[-1].gate.passed, steps[-1].gate.reasons


def test_revise_at_the_cap_refuses_at_the_gate(w):
    coord = critic_world(w, [gh.CRITIC_REVISE, gh.CRITIC_REVISE])
    steps = coord.plan_item(1)
    assert len(w.live("plan")["critic_responses"]) == 2
    assert steps[-1].gate.verdict == "FAIL" and "critic-not-converged" in steps[-1].gate.reasons


def test_an_unparseable_critic_response_goes_straight_to_the_gate(w):
    coord = critic_world(w, [UNPARSEABLE])
    steps = coord.plan_item(1)
    assert [s.phase for s in steps] == ["plan", "plan"]
    assert len([r for r in w.drv.requests if r.phase == "plan"]) == 1
    assert "verdict-unparseable" in steps[-1].gate.reasons


def test_critic_classes_are_recorded_and_a_failing_classifier_does_not_stop_the_loop(w, monkeypatch):
    coord = critic_world(w, [gh.CRITIC_PASS])
    steps = coord.plan_item(1)
    classes = w.live("plan")["critic_classes"]
    assert len(classes) == 1 and set(classes[0]["counts"]) == {"issues", "structural", "mechanical"}
    assert steps[-1].gate is not None


def test_a_failing_classifier_records_an_error_and_the_loop_goes_on(w, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad")

    monkeypatch.setattr(workflow, "classify_counts", boom)
    steps = critic_world(w, [gh.CRITIC_PASS]).plan_item(1)
    classes = w.live("plan")["critic_classes"]
    assert classes[-1].get("error") == "ValueError" and "counts" not in classes[-1]
    assert steps[-1].gate is not None


def review_world(w, **opt):
    coord = critic_world(w, [gh.CRITIC_PASS], **opt)
    w.drv.effects["review"] = w.finding_effect("review-7.md", [gh.REVIEW, gh.REVIEW])
    w.drv.effects["implement"] = lambda d, r: gh.write(w.root / "src" / "feature.py", "z = 1\n")
    testrun.configure(w.root, w.task, command=["true"], state_root=w.drv.state_root)
    return coord


def test_a_review_copies_the_implement_tests_when_the_source_is_unchanged(w):
    coord = review_world(w)
    impl = coord.run_plain_phase("implement", 1)
    assert impl.completed and w.live("implement")["tests"]["exit_code"] == 0
    step = coord.review_item(1)
    assert step.completed and step.gate is not None
    review = w.live("review")
    assert review["tests_source"] == "implement-entry" and review["tests"] == w.live("implement")["tests"]
    assert review["harvested"] and review["harvested"][0]["path"].endswith(".md")


def test_a_review_reruns_the_tests_after_a_source_change(w):
    coord = review_world(w)
    coord.run_plain_phase("implement", 1)
    gh.write(w.root / "src" / "feature.py", "z = 2\n")
    coord.review_item(1)
    assert w.live("review")["tests_source"] == "coordinator"


def test_a_gate_artifact_conflict_ends_with_exit_8(w, monkeypatch):
    critic_world(w, [gh.CRITIC_PASS])
    coord = w.coordinator(no_pause=True)

    def conflict(*a, **k):
        raise workflow.gate.GateArtifactConflict()

    monkeypatch.setattr(workflow.gate, "write_artifact", conflict)
    summary = coord.run_items([("plan", 1)])
    assert summary["outcome"] == "GATE_ARTIFACT_FAILED" and summary["exit_code"] == 8
    assert summary["workflow_validated"] is False


def test_pause_after_a_plan_gate_and_through_stop_where_specified(w):
    coord = critic_world(w, [gh.CRITIC_PASS])
    summary = coord.run_items([("plan", 1), ("implement", 1)])
    assert summary["outcome"] == "PAUSED_AT_GATE" and summary["exit_code"] == 0
    assert [p["phase"] for p in summary["phases"]] == ["plan"]
    assert summary["workflow_validated"] is True and "--continue" in summary["resume_hint"]
    assert summary["record"].endswith("t1.json")



def test_through_stops_after_the_first_passing_gate_of_that_phase(w):
    critic_world(w, [gh.CRITIC_PASS])
    done = w.coordinator(no_pause=True, through="plan").run_items([("plan", 1), ("implement", 1)])
    assert done["outcome"] == "COMPLETED" and [p["phase"] for p in done["phases"]] == ["plan"]


def test_a_refused_gate_reports_exit_7_and_is_not_validated(w):
    coord = critic_world(w, [gh.CRITIC_REVISE, gh.CRITIC_REVISE], no_pause=True)
    summary = coord.run_items([("plan", 1)])
    assert summary["outcome"] == "GATE_REFUSED" and summary["exit_code"] == 7
    assert "critic-not-converged" in summary["reasons"] and summary["workflow_validated"] is False


def test_a_workflow_refusal_maps_to_exit_3(w):
    w.seed_workflow()
    testrun.configure(w.root, w.task, command=["true"], state_root=w.drv.state_root.parent / "other")
    summary = w.coordinator().run_items([("implement", 1)])
    assert summary["outcome"] == "REFUSED" and summary["exit_code"] == 3
    assert summary["reasons"] == ["tests-settings-changed"]


# -- part three: continuation, resume and recovery --------------------------------

import json

import _opencode_handoff_helpers as hh

PASS_GATE = {
    "verdict": "PASS", "reasons": [], "warnings": [], "artifact": "x", "artifact_sha256": "0" * 64,
    "evaluated_at": "2026-01-01T00:00:00Z",
}


def seed_pass(w, phase, stage=1, origin="adopted", **fields):
    w.fx.record(phase, stage=stage, origin=origin, **fields)
    directory = runstore.store_dir(w.root, create=True)
    state = runstore.load_workflow_state(directory, w.task)
    runstore.update_current_entry(state, stage, phase, gate=dict(PASS_GATE))
    runstore.write_workflow_state(directory, state)


def write_record(w, **overrides):
    record = handoff.build_record(
        w.root, w.task, scope=hh.fixed_scope(), scope_source="workflow-state", source_dir=wh.SOURCE_DIR,
        clock=w.fx.clock,
    )
    record.update(overrides)
    done = {(c["phase"], c["stage"]) for c in record["completed"]}
    record["pending"] = [p for p in record["pending"] if (p["phase"], p["stage"]) not in done]
    if record["pending"]:
        record["phase"] = {"current": record["pending"][0]["phase"], "stage": record["pending"][0]["stage"],
                           "status": "pending"}
    try:
        handoff.write(w.root, w.task, record, source_dir=wh.SOURCE_DIR)
    except handoff.HandoffRefused as exc:
        raise AssertionError((exc.code, exc.reasons)) from None
    return record


def foreign(phases, **extra):
    """Record fields for a record written by another runtime."""
    return dict(
        origin_runtime="claude",
        completed=[{"phase": p, "stage": s, "run_id": None, "gate": "PASS"} for p, s in phases],
        validation=[{"phase": p, "stage": s, "verdict": "PASS", "reasons": []} for p, s in phases],
        **extra,
    )


def refusal(coord):
    with pytest.raises(workflow.WorkflowRefused) as caught:
        coord.begin()
    return caught.value


def test_a_new_run_refuses_when_the_workflow_already_started(w):
    w.seed_workflow()
    err = refusal(w.coordinator())
    assert err.code == "workflow-started" and err.exit_code == 3 and "--continue" in err.message


def test_a_new_run_refuses_when_only_a_record_exists(w):
    write_record(w)
    assert refusal(w.coordinator()).code == "workflow-started"


def test_a_new_run_writes_the_settings(w):
    coord = w.coordinator(max_critic_rounds=3)
    coord.begin()
    state = coord.load_state()
    assert state["settings"]["workflow"]["include_architect"] is True and state["settings"]["max_critic_rounds"] == 3


def test_continue_refusals_pass_through_with_exit_3(w):
    err = refusal(w.coordinator(continue_=True))
    assert err.code == "continuation-missing" and err.exit_code == 3
    path = handoff.record_path(w.root, w.task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert refusal(w.coordinator(continue_=True)).code == "continuation-invalid"


def test_a_record_with_an_artifact_sha_state_never_saw_is_a_mismatch(w):
    seed_pass(w, "plan")
    record = write_record(w)
    plan = ".workflow_artifacts/t1/stage-1/current-plan.md"
    for item in record["artifacts"]:
        if item["path"] == plan:
            item["sha256"] = "f" * 64
    handoff.write(w.root, w.task, record, source_dir=wh.SOURCE_DIR)
    assert refusal(w.coordinator(continue_=True)).code == "continuation-state-mismatch"


def test_a_lagging_record_is_accepted_and_rewritten(w):
    seed_pass(w, "plan")
    old = write_record(w)
    gh.write(w.path("stage-1/current-plan.md"), gh.PLAN + "\nedited\n")
    seed_pass(w, "plan")  # state now holds a newer plan sha; the old one stays in its history
    state = runstore.load_workflow_state(runstore.store_dir(w.root), w.task)
    assert handoff.state_agreement(old, state) != [] and handoff.state_agreement(old, state, allow_lag=True) == []
    w.coordinator(continue_=True).begin()
    plan = ".workflow_artifacts/t1/stage-1/current-plan.md"
    assert handoff.state_agreement(w.record_json(), state) == []
    assert {a["path"]: a["sha256"] for a in w.record_json()["artifacts"]}[plan] == handoff.latest_shas(state)[plan]


def test_a_record_ahead_of_state_still_refuses(w):
    seed_pass(w, "plan")
    write_record(w, completed=[{"phase": "plan", "stage": 1, "run_id": None, "gate": "PASS"},
                               {"phase": "implement", "stage": 1, "run_id": None, "gate": "PASS"}])
    assert refusal(w.coordinator(continue_=True)).code == "continuation-state-mismatch"


def test_a_foreign_record_seeds_continuation_entries_and_regates_them(w):
    write_record(w, **foreign([("plan", 1)]))
    coord = w.coordinator(continue_=True, no_pause=True)
    coord.begin()
    entry = w.live("plan")
    assert entry["origin"] == "continuation" and entry["continuation_validation"] == "PASS"
    assert coord.load_state()["settings"]["workflow"]["include_architect"] is True
    summary = coord.run_items([("plan", 1)])
    assert [p["phase"] for p in summary["phases"]] == ["plan"] and summary["phases"][0]["gate"] is not None
    assert "run" not in [name for name, *_ in w.drv.calls] and not w.drv.requests


def test_a_regated_seeded_plan_does_not_pause(w):
    write_record(w, **foreign([("plan", 1)]))
    coord = w.coordinator(continue_=True)
    coord.begin()
    summary = coord.run_items([("plan", 1), ("implement", 1)])
    assert summary["phases"][0]["gate"] == "PASS" or summary["outcome"] == "GATE_REFUSED"
    assert summary["outcome"] != "PAUSED_AT_GATE"


def test_a_tui_record_continued_with_a_test_command_seeds_and_keeps_the_settings(w):
    write_record(w, **foreign([("implement", 1)]))
    coord = w.coordinator(continue_=True, test_command=["true"])
    coord.begin()
    entry = w.live("implement")
    assert entry["origin"] == "continuation" and entry["tests_source"] == "continuation-seed"
    assert entry["tests"]["exit_code"] == 0
    assert coord.load_state()["settings"]["test_command"] == ["true"]
    assert testrun.settings_status(w.root, w.task, state_root=w.drv.state_root) is None


def test_a_failing_seed_test_makes_the_regate_refuse(w):
    write_record(w, **foreign([("implement", 1)]))
    coord = w.coordinator(continue_=True, test_command=["false"], no_pause=True)
    coord.begin()
    summary = coord.run_items([("implement", 1)])
    assert summary["outcome"] == "GATE_REFUSED" and "tests-failed" in summary["reasons"]


def test_a_record_over_a_state_with_only_test_settings_takes_the_seeding_branch(w):
    testrun.configure(w.root, w.task, command=["true"], state_root=w.drv.state_root)
    write_record(w, **dict(foreign([("plan", 1)]), origin_runtime="opencode"))
    coord = w.coordinator(continue_=True)
    coord.begin()
    state = coord.load_state()
    assert w.live("plan")["origin"] == "continuation" and state["settings"]["test_command"] == ["true"]


def test_changed_artifacts_refuse_seeding(w):
    write_record(w, **foreign([("plan", 1)]))
    gh.write(w.path("stage-1/current-plan.md"), gh.PLAN + "\nedited\n")
    assert refusal(w.coordinator(continue_=True)).code == "continuation-artifact-changed"


def test_continue_writes_the_workflow_block_without_architect_after_a_plan_passed_without_it(w):
    w.path("architecture.md").unlink()
    w.path("stage-1/current-plan.md").rename(w.path("current-plan.md"))
    write_record(w, **foreign([("plan", None)]))
    coord = w.coordinator(continue_=True, max_critic_rounds=3)
    coord.begin()
    state = coord.load_state()
    assert state["settings"]["workflow"]["include_architect"] is False
    assert state["settings"]["max_critic_rounds"] == 3
    assert ("architect", None) not in coord.sequence(state)


def test_an_explicit_cap_on_continue_is_used_by_the_loop_and_the_gate(w):
    coord = critic_world(w, [gh.CRITIC_REVISE, gh.CRITIC_REVISE, gh.CRITIC_PASS], continue_=True, max_critic_rounds=3)
    write_record(w)
    coord.begin()
    assert coord.load_state()["settings"]["max_critic_rounds"] == 3
    steps = coord.plan_item(1)
    assert len(w.live("plan")["critic_responses"]) == 3 and steps[-1].gate.passed, steps[-1].gate.reasons


def implement_world(w):
    w.drv.effects["implement"] = lambda d, r: gh.write(w.root / "src" / "feature.py", "z = 1\n")
    w.drv.effects["review"] = w.finding_effect("review-7.md", [gh.REVIEW, gh.REVIEW])


def test_rerun_from_implement_supersedes_implement_and_review_and_runs_both_again(w):
    implement_world(w)
    review = ".workflow_artifacts/t1/stage-1/review-1.md"
    seed_pass(w, "plan")
    seed_pass(w, "implement")
    w.fx.record("review", harvested=[{"path": review, "sha256": "0" * 64}])  # reviewed, not yet gated
    write_record(w)
    coord = w.coordinator(continue_=True, rerun_from="implement", no_pause=True)
    coord.begin()
    state = coord.load_state()
    assert runstore.current_entry(state, 1, "implement") is None and runstore.current_entry(state, 1, "review") is None
    assert runstore.current_entry(state, 1, "plan") is not None
    summary = coord.run_items([("plan", 1), ("implement", 1), ("review", 1)])
    assert summary["rerun"] == {"phase": "implement", "stage": 1}
    assert [r.phase for r in w.drv.requests] == ["implement", "review"]
    assert w.drv.requests[0].context_refs == (review,)
    assert w.live("implement")["origin"] == "coordinator" and w.live("review") is not None


def test_rerun_from_after_every_item_passed_reruns_the_last_stage(w):
    seed_pass(w, "architect", stage=None)
    for stage in (1, 2):
        for phase in ("plan", "implement", "review"):
            seed_pass(w, phase, stage=stage)
    write_record(w)
    coord = w.coordinator(continue_=True, rerun_from="implement")
    coord.begin()
    assert coord.rerun_info == {"phase": "implement", "stage": 2}
    state = coord.load_state()
    assert runstore.current_entry(state, 2, "implement") is None and runstore.current_entry(state, 1, "implement")


def interrupted_gate_world(w):
    """An implement run that ended awaiting approval, recorded by the coordinator."""
    implement_world(w)
    w.drv.script.append(rh.outcome("awaiting_approval", evidence="partial"))
    coord = w.coordinator(no_pause=True)
    coord.begin()
    return coord.run_items([("implement", 1)])


def test_adopt_after_an_awaiting_approval_stop_gates_with_the_two_warnings(w):
    first = interrupted_gate_world(w)
    assert first["outcome"] == "AWAITING_APPROVAL" and first["exit_code"] == 4
    again = w.coordinator(continue_=True, no_pause=True)
    again.begin()
    assert again.run_items([("implement", 1)])["exit_code"] == 4  # a stored outcome is never silently rerun
    adopting = w.coordinator(continue_=True, adopt="implement", no_pause=True)
    adopting.begin()
    summary = adopting.run_items([("implement", 1)])
    gate_info = w.live("implement")["gate"]
    assert w.live("implement")["origin"] == "adopted" and summary["phases"][0]["gate"] is not None
    assert set(gate_info["warnings"]) >= {"run-evidence-absent", "boundary-unverified"}


# -- the resolution table -----------------------------------------------------------


def test_resolution_of_a_pointer_run_started_by_a_single_phase_command_refuses(w):
    w.drv.seed("interrupted", request={"stage": "1", "phase": "implement", "profile": "personal",
                                       "workspace": None, "non_interactive": False},
               phase="implement", task=w.task, stage="1", profile="personal")
    res = w.coordinator().resolve(None, "implement", 1)
    assert res.kind == "refuse" and res.code == "phase-unrecorded" and "--adopt" in res.message


def test_resolution_of_an_interrupted_coordinator_run_resumes_it(w):
    w.drv.seed("interrupted", request={"stage": "1", "phase": "implement", "profile": "personal",
                                       "workspace": None, "non_interactive": True},
               phase="implement", task=w.task, stage="1", profile="personal")
    assert w.coordinator().resolve(None, "implement", 1).kind == "run"


def test_resolution_of_a_closed_run_without_an_entry_refuses(w):
    w.drv.seed("failed", request={"stage": "1", "phase": "implement", "profile": "personal",
                                  "workspace": None, "non_interactive": True},
               phase="implement", task=w.task, stage="1", profile="personal")
    assert w.coordinator().resolve(None, "implement", 1).code == "phase-unrecorded"


def test_resolution_with_the_output_already_present_refuses_and_with_nothing_present_runs(w):
    coord = w.coordinator()
    assert coord.resolve(None, "plan", 1).code == "phase-unrecorded"
    assert coord.resolve(None, "implement", 1).kind == "run"
    w.path("stage-1/current-plan.md").unlink()
    assert coord.resolve(None, "plan", 1).kind == "run"


def test_resolution_of_stored_gates_and_unfinished_entries(w):
    seed_pass(w, "plan")
    w.fx.record("implement", origin="phase-run")
    directory = runstore.store_dir(w.root, create=True)
    state = runstore.load_workflow_state(directory, w.task)
    runstore.update_current_entry(state, 1, "implement", gate=dict(PASS_GATE, verdict="FAIL", reasons=["tests-failed"]))
    runstore.write_workflow_state(directory, state)
    state = runstore.load_workflow_state(directory, w.task)
    coord = w.coordinator()
    assert coord.resolve(state, "plan", 1).kind == "pass"
    failed = coord.resolve(state, "implement", 1)
    assert failed.kind == "gate-fail" and failed.exit_code == 7 and failed.reasons == ("tests-failed",)
    w.fx.record("review", origin="phase-run")
    state = runstore.load_workflow_state(directory, w.task)
    assert coord.resolve(state, "review", 1).kind == "gate"


def test_a_stored_boundary_violation_is_reported_not_rerun(w):
    w.fx.record("implement", boundary="violation")
    state = runstore.load_workflow_state(runstore.store_dir(w.root), w.task)
    res = w.coordinator().resolve(state, "implement", 1)
    assert res.kind == "report" and res.exit_code == 2 and res.reasons == ("boundary-violation",)
