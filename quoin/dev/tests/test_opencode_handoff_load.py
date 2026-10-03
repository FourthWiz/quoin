"""Loading continuation records and the advice printed from them."""
from __future__ import annotations

import builtins
import json
import os
import shlex
import shutil
from pathlib import Path

import pytest

import _opencode_handoff_helpers as hh
from quoin import cli
from quoin.opencode_adapter import driver, handoff, phase_loop, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PLAN1 = ".workflow_artifacts/t1/stage-1/current-plan.md"


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    return hh.Project(tmp_path, monkeypatch)


@pytest.fixture()
def open_plan(tmp_path, monkeypatch):
    """Stage-1 plan present, no entry for it, architect passed: current item is plan 1."""
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    project.seed_gate("architect", None, "PASS")
    return project


@pytest.fixture()
def at_implement(tmp_path, monkeypatch):
    """Architect and stage-1 plan passed: current item is implement 1."""
    project = hh.Project(tmp_path, monkeypatch)
    project.seed_gate("architect", None, "PASS")
    return project


def load(fx, **kw):
    return handoff.load_for_continuation(fx.root, fx.task, source_dir=fx.source, **kw)


def refusal(fx, **kw):
    with pytest.raises(handoff.HandoffRefused) as info:
        load(fx, **kw)
    return info.value


def store(fx, record=None):
    handoff.write(fx.root, fx.task, record or fx.build(), source_dir=fx.source)


def rpath(fx):
    return handoff.record_path(fx.root, fx.task)


def advise(fx, record=None):
    record = record or fx.build()
    directory = runstore.inspect_store(fx.root)
    state = None if directory is None else runstore.load_workflow_state(directory, fx.task)
    return handoff.next_step(fx.root, record, state, handoff.run_facts(directory, fx.task), fx.source)


def q(value):
    return shlex.quote(str(value))


def run_cmd(fx, phase, *, stage="1", profile="personal", new_run=False):
    return "quoin run --runtime opencode --profile %s --phase %s%s t1 --project-root %s%s" % (
        profile, phase, "" if stage is None else " --stage " + stage, q(fx.root), " --new-run" if new_run else "")


def adopt_cmd(fx, phase, stage=1):
    return "quoin opencode adopt --task t1%s --phase %s --project-root %s" % (
        "" if stage is None else " --stage %d" % stage, phase, q(fx.root))


def gate_cmd(fx, phase, stage=1):
    return "quoin opencode gate --task t1%s --phase %s --write --project-root %s" % (
        "" if stage is None else " --stage %d" % stage, phase, q(fx.root))


def write_cmd(fx):
    return "quoin opencode handoff write --task t1 --project-root %s" % q(fx.root)


def all_text(advice):
    return " ".join(advice["steps"] + advice["candidates"])


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_missing_record_names_the_write_command(fx):
    exc = refusal(fx)
    assert exc.code == "continuation-missing"
    assert "quoin opencode handoff write --task t1" in exc.message


MARKERS = [
    "memory/checkpoints/2026-01-01T0900-t1.md",
    "memory/sessions/2026-01-01-t1-codex.md",
    "memory/sessions/2026-01-01-t1-orchestrator.md",
    "memory/run-state-t1.json",
    "memory/run-notes-t1.md",
]


@pytest.mark.parametrize("marker", MARKERS)
def test_legacy_marker_alone_is_refused_without_being_opened(fx, monkeypatch, marker):
    path = fx.root / ".workflow_artifacts" / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret legacy text")
    path.chmod(0)
    real_open, real_os_open = builtins.open, os.open

    def guard(target, *a, **k):
        assert Path(str(target)).name != path.name, "a legacy marker was opened"
        return real_open(target, *a, **k)

    def guard_os(target, *a, **k):
        assert Path(str(target)).name != path.name, "a legacy marker was opened"
        return real_os_open(target, *a, **k)

    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(os, "open", guard_os)
    try:
        exc = refusal(fx)
    finally:
        path.chmod(0o600)
    assert exc.code == "continuation-legacy-format"
    assert exc.reasons == (".workflow_artifacts/" + marker,) or list(exc.reasons) == [".workflow_artifacts/" + marker]


def test_markers_for_another_task_or_plain_session_files_do_not_count(fx):
    memory = fx.root / ".workflow_artifacts" / "memory"
    for rel in ("checkpoints/2026-01-01T0900-t2.md", "sessions/2026-01-01-t1.md", "sessions/2026-01-01-t12-codex.md",
                "run-state-t2.json"):
        (memory / rel).parent.mkdir(parents=True, exist_ok=True)
        (memory / rel).write_text("x")
    assert handoff.legacy_markers(fx.root, "t1") == []
    assert refusal(fx).code == "continuation-missing"


@pytest.mark.parametrize("where", ["finalized/t1", "PARENT/finalized/t1"])
@pytest.mark.parametrize("with_record", [False, True])
def test_finalized_task_is_refused(fx, where, with_record):
    if with_record:
        store(fx)
    (fx.root / ".workflow_artifacts" / where).mkdir(parents=True)
    exc = refusal(fx)
    assert exc.code == "task-finalized"
    assert "finalized" in exc.message
    assert handoff.finalized_location(fx.root, "t1") == ".workflow_artifacts/" + where


def edit_file(fx, change):
    data = json.loads(rpath(fx).read_text())
    change(data)
    rpath(fx).write_text(json.dumps(data))


@pytest.mark.parametrize("change,expected", [
    (lambda d: d.pop("decisions"), "field-missing:decisions"),
    (lambda d: d["provenance"].update(transcripts_imported=True), "transcripts-imported"),
    (lambda d: d["artifacts"][0].update(path="../x"), "path-invalid"),
], ids=["missing-field", "transcripts", "traversal"])
def test_invalid_record_is_refused_with_the_reason(fx, change, expected):
    store(fx)
    edit_file(fx, change)
    exc = refusal(fx)
    assert exc.code == "continuation-invalid"
    assert any(str(r).startswith(expected) for r in exc.reasons), exc.reasons


def test_bad_json_is_refused(fx):
    store(fx)
    rpath(fx).write_text("{not json")
    exc = refusal(fx)
    assert exc.code == "continuation-invalid"
    assert "json-invalid" in exc.reasons


def test_task_mismatch(fx):
    store(fx)
    edit_file(fx, lambda d: d.update(task="other"))
    exc = refusal(fx)
    assert exc.code == "continuation-invalid"
    assert "task-mismatch" in exc.reasons


def test_unsafe_continuation_location_is_refused(tmp_path, monkeypatch):
    fx = hh.Project(tmp_path, monkeypatch)
    memory = fx.root / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (memory / "continuation").symlink_to(elsewhere)
    exc = refusal(fx)
    assert exc.code == "continuation-invalid"
    assert "unsafe-path" in exc.reasons


# ---------------------------------------------------------------------------
# requested scope
# ---------------------------------------------------------------------------


def test_scope_comparison(fx):
    store(fx, fx.build(scope=hh.fixed_scope("personal", "personal")))
    assert refusal(fx, requested_scope=hh.fixed_scope("work", "personal")).code == "profile-mismatch"
    assert refusal(fx, requested_scope=hh.fixed_scope("personal", "work")).code == "classification-mismatch"
    wider = hh.fixed_scope(enabled_providers=["alpha", "beta"])
    exc = refusal(fx, requested_scope=wider)
    assert exc.code == "policy-widened"
    assert any("enabled_providers" in r for r in exc.reasons)
    narrower = hh.fixed_scope(enabled_providers=[], provider_allowlist=[])
    assert load(fx, requested_scope=narrower)["task"] == "t1"
    assert load(fx, requested_scope=hh.fixed_scope())["task"] == "t1"


def test_work_record_continued_as_personal_is_refused(fx):
    store(fx, fx.build(scope=hh.fixed_scope("personal", "work")))
    assert refusal(fx, requested_scope=hh.fixed_scope("personal", "personal")).code == "classification-mismatch"


def test_profile_mismatch_wins_over_other_findings(fx):
    store(fx)
    exc = refusal(fx, requested_scope=hh.fixed_scope("work", "work", enabled_providers=["alpha", "beta"]))
    assert exc.code == "profile-mismatch"
    assert len(exc.reasons) == 3


# ---------------------------------------------------------------------------
# artifact changes and state agreement
# ---------------------------------------------------------------------------


def test_artifact_changes(fx, tmp_path):
    record = fx.build()
    assert handoff.artifact_changes(fx.root, record) == []
    base = fx.root / ".workflow_artifacts" / "t1"
    (base / "stage-1" / "current-plan.md").write_text("edited\n")
    (base / "architecture.md").unlink()
    review = base / "stage-1" / "review-1.md"
    other = tmp_path / "decoy.md"
    other.write_text(review.read_text())
    review.unlink()
    review.symlink_to(other)
    changed = handoff.artifact_changes(fx.root, record)
    assert sorted(changed) == sorted([
        ".workflow_artifacts/t1/architecture.md", PLAN1, ".workflow_artifacts/t1/stage-1/review-1.md",
    ])


def test_state_agreement(fx):
    record = fx.build()
    assert handoff.state_agreement(record, fx.state()) == []
    assert handoff.state_agreement(record, None) == ["completed"]
    fx.seed_gate("architect", None, "PASS")
    assert handoff.state_agreement(record, fx.state()) == ["completed"]


def test_rewritten_plan_after_the_plan_entry(fx):
    stale = fx.build()
    (fx.root / PLAN1).write_text(hh.g.PLAN + "\nmore\n")
    fx.record("implement", stage=1, origin="adopted")
    assert handoff.state_agreement(fx.build(), fx.state()) == []
    assert handoff.state_agreement(stale, fx.state()) == ["artifact:" + PLAN1]


def test_state_for_another_task_disagrees(fx):
    record = fx.build()
    state = fx.state()
    state["task"] = "other"
    assert "task" in handoff.state_agreement(record, state)


# ---------------------------------------------------------------------------
# next_step basics
# ---------------------------------------------------------------------------


def test_done_task_has_no_steps(fx):
    for phase, stage in (("architect", None), ("implement", 1), ("review", 1), ("plan", 2),
                         ("implement", 2), ("review", 2)):
        fx.seed_gate(phase, stage, "PASS")
    advice = advise(fx)
    assert advice["status"] == "done"
    assert advice["steps"] == [] and advice["candidates"] == []


def test_staged_implement_without_any_trace_offers_a_fresh_run(at_implement):
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["steps"] == [run_cmd(at_implement, "implement")]
    assert "quoin opencode adopt --phase implement" in advice["hint"]


def test_unstaged_task_commands_carry_no_stage(tmp_path, monkeypatch):
    hh.g.isolate_git(monkeypatch, tmp_path / "home")
    root = hh.g.make_repo(tmp_path / "proj")
    hh.g.write(root / ".workflow_artifacts" / "t1" / "current-plan.md", hh.g.PLAN)
    record = handoff.build_record(
        root, "t1", scope=hh.fixed_scope(), scope_source="operator-flag", source_dir=hh.SOURCE_DIR,
    )
    advice = handoff.next_step(root, record, None, None, hh.SOURCE_DIR)
    assert (advice["phase"], advice["stage"], advice["status"]) == ("plan", None, "unrecorded")
    assert advice["steps"] == [
        "quoin opencode adopt --task t1 --phase plan --project-root %s" % q(root),
        "quoin opencode gate --task t1 --phase plan --write --project-root %s" % q(root),
    ]


def test_existing_task_without_workflow_state(tmp_path, monkeypatch):
    fx = hh.Project(tmp_path, monkeypatch, passed=False)
    assert fx.state() is None
    advice = advise(fx)
    kinds = {(i["phase"], i["stage"]): i["kind"] for i in advice["items"]}
    assert kinds[("architect", None)] == "unrecorded"
    assert kinds[("plan", 1)] == "unrecorded"
    assert advice["status"] == "unrecorded"
    assert advice["steps"] == [handoff.gate.adopt_command("t1", None, "architect", fx.root),
                               gate_cmd(fx, "architect", None)]
    assert all(s.endswith("--project-root " + q(fx.root)) for s in advice["steps"])
    assert "quoin run" not in all_text(advice)


# ---------------------------------------------------------------------------
# live entries
# ---------------------------------------------------------------------------


def test_adopted_entry_without_gate_awaits_the_gate(open_plan):
    open_plan.record("plan", stage=1, origin="adopted")
    advice = advise(open_plan)
    assert advice["status"] == "awaiting-gate"
    assert advice["steps"] == [gate_cmd(open_plan, "plan")]
    assert "opencode adopt" not in all_text(advice)


def test_adopted_entry_with_failed_gate(open_plan):
    open_plan.seed_gate("plan", 1, "FAIL", reasons=("missing-section",))
    advice = advise(open_plan)
    assert advice["status"] == "gate-failed"
    assert advice["reasons"] == ["missing-section"]
    assert advice["steps"] == [handoff.gate.adopt_command("t1", 1, "plan", open_plan.root), gate_cmd(open_plan, "plan")]


def test_run_entry_without_gate_awaits_the_gate(open_plan):
    open_plan.record("plan", stage=1, origin="phase-run")
    advice = advise(open_plan)
    assert advice["status"] == "awaiting-gate"
    assert advice["steps"] == [gate_cmd(open_plan, "plan")]
    assert "opencode adopt" not in all_text(advice)


@pytest.mark.parametrize("origin", ["phase-run", "continuation"])
def test_failed_gate_on_run_evidence_never_offers_adopt(open_plan, origin):
    open_plan.seed_gate("plan", 1, "FAIL", reasons=("x",), origin=origin)
    advice = advise(open_plan)
    assert advice["status"] == "gate-failed"
    assert advice["steps"] == []
    assert advice["candidates"] == [run_cmd(open_plan, "plan")]
    assert "opencode adopt" not in all_text(advice)


def test_gate_passed_after_the_record(open_plan):
    record = open_plan.build()
    open_plan.seed_gate("plan", 1, "PASS")
    advice = advise(open_plan, record)
    assert advice["status"] == "recorded-since"
    assert advice["steps"] == [write_cmd(open_plan)]


# ---------------------------------------------------------------------------
# open runs
# ---------------------------------------------------------------------------


def test_interrupted_thorough_plan_run_is_resumed(open_plan):
    open_plan.seed_run("thorough_plan", "1", "interrupted", checkpoint=True)
    advice = advise(open_plan)
    assert advice["status"] == "run-open"
    assert advice["steps"] == [run_cmd(open_plan, "thorough_plan")]
    assert "--new-run" not in advice["steps"][0]
    assert "opencode adopt" not in all_text(advice)


def test_open_run_with_no_stored_profile_offers_only_a_restart(open_plan):
    open_plan.seed_run("thorough_plan", "1", "interrupted", profile=None)
    advice = advise(open_plan)
    assert advice["status"] == "run-open"
    assert advice["steps"] == []
    assert advice["candidates"] == [run_cmd(open_plan, "thorough_plan", new_run=True)]
    assert "opencode adopt" not in all_text(advice)


@pytest.mark.parametrize("variant", ["running", "session-lost", "driver-lost"])
def test_unsafe_open_runs_only_offer_candidates(open_plan, variant):
    kw = {"state": "interrupted"}
    if variant == "running":
        kw = {"state": "running"}
    elif variant == "session-lost":
        kw["resume_blocked"] = "session-lost"
    else:
        kw["driver_lost"] = True
    open_plan.seed_run("thorough_plan", "1", kw.pop("state"), **kw)
    advice = advise(open_plan)
    assert advice["steps"] == []
    assert advice["candidates"] == [
        run_cmd(open_plan, "thorough_plan"), run_cmd(open_plan, "thorough_plan", new_run=True),
    ]
    assert "quoin opencode status" in advice["hint"]
    if variant == "running":
        assert "run-in-progress" in advice["hint"] and "lock-held" in advice["hint"]
    else:
        assert "run-in-progress" not in advice["hint"]
    assert "opencode adopt" not in all_text(advice)


def test_critic_run_maps_to_the_plan_item(open_plan):
    open_plan.seed_run("critic", "1", "interrupted")
    advice = advise(open_plan)
    assert (advice["phase"], advice["status"]) == ("plan", "run-open")
    assert advice["steps"] == [run_cmd(open_plan, "critic")]


def capture(monkeypatch, name):
    seen = []

    def fake(args):
        seen.append(args)
        return 0

    monkeypatch.setattr(cli, name, fake)
    return seen


def request_from(args):
    return driver.RunRequest(
        project_root=Path(args.project_root).resolve(), task=args.task, stage=args.stage, phase=args.phase,
        profile=args.profile, budget=args.budget,
    )


@pytest.mark.parametrize("seed,build_phase", [
    (("thorough_plan", "interrupted", {}), None),
    (("critic", "interrupted", {}), None),
    (("thorough_plan", "running", {}), None),
    (("thorough_plan", "interrupted", {"resume_blocked": "session-lost"}), None),
    (("thorough_plan", "interrupted", {"driver_lost": True}), None),
])
def test_printed_run_commands_match_the_real_parser_and_request(open_plan, monkeypatch, seed, build_phase):
    phase, state, extra = seed
    run_id = open_plan.seed_run(phase, "1", state, **extra)
    advice = advise(open_plan)
    seeded = runstore.load_record(open_plan.directory(), run_id)
    seen = capture(monkeypatch, "_cmd_run_opencode")
    commands = advice["steps"] + advice["candidates"]
    assert commands
    for command in commands:
        cli.main(shlex.split(command)[1:])
        args = seen[-1]
        assert phase_loop._same_request(seeded, request_from(args))
        assert bool(args.new_run) == command.endswith("--new-run")


def test_restart_command_for_a_profileless_run_parses(open_plan, monkeypatch):
    open_plan.seed_run("thorough_plan", "1", "interrupted", profile=None)
    record = open_plan.build()
    advice = advise(open_plan, record)
    seen = capture(monkeypatch, "_cmd_run_opencode")
    cli.main(shlex.split(advice["candidates"][0])[1:])
    assert seen[-1].profile == record["scope"]["profile"]
    assert seen[-1].new_run is True


def test_adopt_and_gate_steps_parse(open_plan, monkeypatch):
    open_plan.seed_run("thorough_plan", "1", "completed")
    advice = advise(open_plan)
    adopt = capture(monkeypatch, "_cmd_opencode_adopt")
    gate = capture(monkeypatch, "_cmd_opencode_gate")
    cli.main(shlex.split(advice["steps"][0])[1:])
    cli.main(shlex.split(advice["steps"][1])[1:])
    for args in (adopt[-1], gate[-1]):
        assert (args.task, args.stage, args.phase) == ("t1", 1, "plan")
        assert Path(args.project_root).resolve() == Path(open_plan.root).resolve()


# ---------------------------------------------------------------------------
# stale record, completed runs, failed runs
# ---------------------------------------------------------------------------


def test_record_stale_when_the_open_run_is_gone(at_implement):
    at_implement.seed_run("implement", "1", "interrupted")
    record = at_implement.build()
    assert record["phase"]["status"] == "interrupted"
    at_implement.seed_run("review", "1", "completed")
    advice = advise(at_implement, record)
    assert advice["status"] == "record-stale"
    assert advice["steps"] == [write_cmd(at_implement)]
    assert "opencode adopt" not in all_text(advice)


def test_completed_implement_run_is_adopted_not_rerun(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    advice = advise(at_implement)
    assert advice["status"] == "run-completed"
    assert advice["steps"] == [handoff.gate.adopt_command("t1", 1, "implement", at_implement.root),
                               gate_cmd(at_implement, "implement")]
    assert "quoin run" not in all_text(advice)


def test_completed_thorough_plan_run_is_not_unrecorded(open_plan):
    open_plan.seed_run("thorough_plan", "1", "completed")
    advice = advise(open_plan)
    assert advice["status"] == "run-completed"
    assert "quoin run" not in all_text(advice)


def test_completed_run_with_an_adopted_entry_awaits_the_gate(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    at_implement.record("implement", stage=1, origin="adopted")
    advice = advise(at_implement)
    assert advice["status"] == "awaiting-gate"
    assert "opencode adopt" not in all_text(advice)


def test_completed_run_for_another_item_leaves_this_one_pending(at_implement):
    at_implement.seed_run("review", "1", "completed")
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["steps"] == [run_cmd(at_implement, "implement")]


def test_completed_critic_run_hint(open_plan):
    open_plan.seed_run("critic", "1", "completed")
    advice = advise(open_plan)
    assert advice["status"] == "run-completed"
    assert "after that run" in advice["hint"]


def test_pointer_moving_to_a_later_completed_run_keeps_the_earlier_one(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    at_implement.seed_run("review", "1", "completed")
    advice = advise(at_implement)
    assert advice["status"] == "run-completed"
    assert advice["steps"] == [handoff.gate.adopt_command("t1", 1, "implement", at_implement.root),
                               gate_cmd(at_implement, "implement")]
    assert "quoin run" not in all_text(advice)


def test_pointer_moving_to_an_interrupted_run_of_the_next_phase(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    at_implement.seed_run("review", "1", "interrupted", checkpoint=True)
    record = at_implement.build()
    advice = advise(at_implement, record)
    assert (advice["phase"], advice["status"]) == ("review", "run-open")
    assert advice["steps"] == [run_cmd(at_implement, "review")]
    kinds = {(i["phase"], i["stage"]): i["kind"] for i in advice["items"]}
    assert kinds[("implement", 1)] == "run-completed"


def test_later_failed_run_wins_over_an_earlier_completed_one(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    at_implement.seed_run("implement", "1", "failed")
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["facts"]["run_state"] == "failed"
    assert advice["steps"] == [run_cmd(at_implement, "implement")]
    assert "partial changes" in advice["hint"]


def test_failed_implement_run(at_implement):
    run_id = at_implement.seed_run("implement", "1", "failed")
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["facts"]["run_state"] == "failed" and advice["facts"]["run_id"] == run_id
    assert advice["steps"] == [run_cmd(at_implement, "implement")]
    assert "partial changes" in advice["hint"]


def test_failed_thorough_plan_with_a_plan_is_unrecorded_with_a_warning(open_plan):
    open_plan.seed_run("thorough_plan", "1", "failed")
    advice = advise(open_plan)
    assert advice["status"] == "unrecorded"
    assert "did not complete" in advice["hint"]


def test_unreadable_run_record_blocks_a_fresh_run_step(at_implement):
    directory = at_implement.directory()
    (directory / "oc-20200101T000000Z-deadbeef.run.json").write_text("not json")
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["facts"]["run_records_skipped"] == 1
    assert advice["steps"] == []
    assert advice["candidates"] == [run_cmd(at_implement, "implement")]
    assert "unreadable" in advice["hint"]


def test_orphaned_running_run_is_a_candidate_only(at_implement):
    at_implement.seed_run("implement", "1", "running", pointer=False)
    at_implement.seed_run("review", "1", "completed")
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["steps"] == []
    assert advice["candidates"] == [run_cmd(at_implement, "implement")]
    assert "quoin opencode status" in advice["hint"]


def test_missing_state_and_run_store_classify_every_item(fx):
    record = fx.build()
    advice = handoff.next_step(fx.root, record, None, None, fx.source)
    assert len(advice["items"]) == len(record["pending"])
    assert advice["status"] in {"unrecorded", "pending"}


def test_run_facts_without_a_pointer(at_implement):
    run_id = at_implement.seed_run("implement", "1", "completed", pointer=False)
    facts = handoff.run_facts(at_implement.directory(), "t1")
    assert facts["pointer"] is None
    assert facts["by_item"][("implement", 1)]["run_id"] == run_id
    assert handoff.run_facts(None, "t1") is None


def test_session_ids_the_resume_rule_refuses_are_dropped(at_implement):
    for bad in ("ses_ok bad", "bad id!"):
        at_implement.seed_run("implement", "1", "interrupted", checkpoint=True, session=bad)
        assert at_implement.build()["native"]["session_id"] is None


# ---------------------------------------------------------------------------
# run facts: never-launched and hidden runs
# ---------------------------------------------------------------------------


def corrupt_record(fx, name="oc-20990101T000000Z-deadbeef.run.json"):
    (fx.directory() / name).write_text("not json")


def test_run_refused_at_prepare_does_not_hide_a_completed_run(at_implement):
    done = at_implement.seed_run("implement", "1", "completed")
    at_implement.seed_run("implement", "1", "prepared", pointer=False)
    at_implement.seed_run("review", "1", "completed")
    advice = advise(at_implement)
    kinds = {(i["phase"], i["stage"]): i for i in advice["items"]}
    assert kinds[("implement", 1)]["kind"] == "run-completed"
    assert kinds[("implement", 1)]["facts"]["run_id"] == done
    assert run_cmd(at_implement, "implement") not in all_text(advice)


def test_prepared_only_run_is_not_a_run_fact(at_implement):
    at_implement.seed_run("implement", "1", "prepared", pointer=False)
    facts = handoff.run_facts(at_implement.directory(), "t1")
    assert ("implement", 1) not in facts["by_item"]


def test_hidden_newer_run_demotes_the_fresh_run_even_with_a_visible_failed_run(at_implement):
    at_implement.seed_run("implement", "1", "failed")
    corrupt_record(at_implement)
    advice = advise(at_implement)
    assert advice["status"] == "pending"
    assert advice["steps"] == []
    assert advice["candidates"] == [run_cmd(at_implement, "implement")]
    assert "could not be read" in advice["hint"]


def test_hidden_newer_run_demotes_the_adopt_of_an_older_completed_run(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    corrupt_record(at_implement)
    advice = advise(at_implement)
    assert advice["status"] == "run-completed"
    assert advice["steps"] == []
    assert advice["candidates"] == [adopt_cmd(at_implement, "implement"), gate_cmd(at_implement, "implement")]
    assert "may be hidden" in advice["hint"]


def test_completed_run_with_nothing_hidden_still_gets_steps(at_implement):
    at_implement.seed_run("implement", "1", "completed")
    advice = advise(at_implement)
    assert advice["steps"] == [adopt_cmd(at_implement, "implement"), gate_cmd(at_implement, "implement")]
    assert advice["candidates"] == []


def test_list_records_hides_the_oldest_runs_past_the_cap(at_implement):
    ids = [at_implement.seed_run("implement", "1", "completed", pointer=False) for _ in range(3)]
    records, skipped = runstore.list_records(at_implement.directory(), "t1", limit=2)
    assert sorted(r["run_id"] for r in records) == sorted(ids[1:])
    assert skipped == 1


@pytest.mark.parametrize("bad", [{"request": "nope"}, {"attempts": {"a": 1}}])
def test_malformed_run_record_shapes_are_skipped_not_crashed(at_implement, bad):
    run_id = at_implement.seed_run("implement", "1", "failed", pointer=False)
    record = runstore.load_record(at_implement.directory(), run_id)
    record.update(bad)
    runstore.write_record(at_implement.directory(), record)
    facts = handoff.run_facts(at_implement.directory(), "t1")
    assert ("implement", 1) not in facts["by_item"]
    assert facts["records_skipped"] == 1


def test_cancelled_latest_run_carries_the_partial_changes_hint(at_implement):
    at_implement.seed_run("implement", "1", "cancelled")
    assert "partial changes" in advise(at_implement)["hint"]
