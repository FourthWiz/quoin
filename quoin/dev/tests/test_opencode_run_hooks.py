"""Hook points around a single-phase run: cost rows, supersession and phase-run entries."""
from __future__ import annotations

import pytest

from quoin.opencode_adapter import cost, evidence, gate, run_hooks, runstore

import _opencode_cost_helpers as ch
import _opencode_gate_helpers as gh

SOURCE = ch.SOURCE_DIR
CRITIC2 = "stage-1/critic-response-2.md"
CRITIC3 = "stage-1/critic-response-3.md"
REL = ".workflow_artifacts/t1/stage-1/"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return ch.HookWorld(tmp_path, monkeypatch)


def _rewrite(w):
    w.synth.ledger.write_text("# Cost Ledger \u2014 t1\nrewritten | x | plan | m | task | n | 0\n", encoding="utf-8")


def reasons(result):
    return set(result.reasons)


def warnings(result):
    return set(result.warnings)


# -- plan, critic and review entries ------------------------------------------


def test_a_completed_plan_run_records_one_entry_that_ignores_older_critic_files(w):
    run = w.exec_run("plan")
    out = w.hook(run)
    assert out.entry_recorded and not out.violation
    entries = w.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["origin"] == "phase-run" and entry["phase"] == "plan" and entry["stage"] == 1
    assert entry["runs"] == [run] and entry["outputs_recorded"] is True
    assert entry["critic_responses"] == [] and entry["boundary"] is None
    assert run in entry["ledger_uuids"]
    assert "critic-missing" in reasons(w.evaluate("plan"))
    assert len(w.synth.rows_for(run)) == 1


def test_plan_run_then_critic_run_lists_both_and_only_the_new_response(w):
    plan = w.exec_run("plan")
    w.hook(plan)
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(critic)
    entry = w.live("plan")
    assert entry["runs"] == [plan, critic]
    assert entry["critic_responses"] == [REL + "critic-response-2.md"]
    result = w.evaluate("plan")
    assert result.verdict == "PASS", reasons(result)
    assert "boundary-unverified" in warnings(result)


def test_a_critic_run_after_an_adopted_plan_starts_alone(w):
    w.fx.record("plan", origin="adopted", boundary="ok")
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(critic)
    entry = w.live("plan")
    assert entry["origin"] == "phase-run" and entry["runs"] == [critic]
    assert "run-not-completed" in reasons(w.evaluate("plan"))


def test_a_thorough_plan_run_records_every_critic_file_in_order(w):
    run = w.exec_run("thorough_plan", writes={CRITIC3: gh.CRITIC_PASS, CRITIC2: gh.CRITIC_REVISE})
    w.hook(run)
    assert w.live("plan")["critic_responses"] == [REL + "critic-response-2.md", REL + "critic-response-3.md"]


def test_a_review_run_records_the_file_it_produced_not_an_older_one(w):
    run = w.exec_run("review", writes={"stage-1/review-2.md": gh.REVIEW})
    w.hook(run)
    entry = w.live("review")
    assert entry["phase"] == "review" and entry["harvested"][0]["path"] == REL + "review-2.md"
    assert len(entry["harvested"][0]["sha256"]) == 64
    assert w.evaluate("review").verdict == "PASS"


def test_a_review_run_that_produced_nothing_leaves_the_gate_refusing(w):
    run = w.exec_run("review")
    w.hook(run)
    entry = w.live("review")
    assert entry["harvested"] == [] and entry["outputs_recorded"] is True
    assert "artifact-missing" in reasons(w.evaluate("review"))


# -- open runs, blocks and ledger rows ----------------------------------------


def test_an_interrupted_resumable_run_records_nothing_but_the_mark(w):
    run = w.exec_run("plan", state="interrupted")
    out = w.hook(run, outcome="INTERRUPTED")
    assert not out.entry_recorded and out.reason == "run-open"
    assert w.entries() == [] and w.synth.rows_for(run) == []
    assert w.synth.record(run)["ledger_mark"]["exists"] is True


def test_a_checkpoint_invalid_block_in_the_result_is_costed_and_recorded(w):
    run = w.exec_run("plan", state="interrupted")
    w.hook(run, outcome="INTERRUPTED", blocked="checkpoint-invalid")
    assert len(w.synth.rows_for(run)) == 1 and "outcome=interrupted" in w.synth.rows_for(run)[0]
    stored = w.synth.record(run)
    assert stored["resume_blocked"] == "checkpoint-invalid" and stored["telemetry"]["final"] is True
    assert w.live("plan")["runs"] == [run]
    again = w.exec_run("plan", state="running")
    w.hook(again, candidate=run, outcome="INTERRUPTED")
    assert len(w.synth.rows_for(run)) == 1
    assert "superseded_by" not in w.synth.record(run)


def test_non_gated_runs_record_a_row_and_no_entry(w):
    for phase in ("gate", "checkpoint", "continue_work", "end_of_task"):
        run = w.exec_run(phase, stage=None)
        out = w.hook(run)
        assert not out.entry_recorded
        assert len(w.synth.rows_for(run)) == 1
        assert w.synth.record(run)["telemetry"]["evidence"]["reason"] == "phase-not-gated"
    assert w.entries() == []


def test_repeated_hooks_add_no_row_and_no_second_entry(w):
    run = w.exec_run("plan")
    w.hook(run)
    out = w.hook(run)
    assert out.entry_recorded
    assert len(w.entries()) == 1 and len(w.synth.rows_for(run)) == 1


# -- unresolved stage folders --------------------------------------------------


def test_a_staged_run_without_a_source_dir_fails_closed_without_a_hook_error(w):
    run = w.exec_run("plan", writes={CRITIC2: gh.CRITIC_PASS})
    out = w.hook(run, source_dir=None)
    assert out.entry_recorded
    entry = w.live("plan")
    assert entry["outputs_error"] == "stage-unresolved" and entry["critic_responses"] == []
    assert "hook_error" not in w.synth.record(run)["telemetry"]


def test_any_stage_resolver_error_becomes_stage_unresolved(w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(gate, "stage_dir", boom)
    run = w.exec_run("plan")
    w.hook(run)
    assert w.live("plan")["outputs_error"] == "stage-unresolved"
    assert "hook_error" not in w.synth.record(run)["telemetry"]


# -- incomplete hashes ----------------------------------------------------------


def test_truncated_input_hashes_record_no_file(w):
    before = runstore.hash_inputs(w.root, w.task)
    w.gh.write(w.path(CRITIC2), gh.CRITIC_PASS)
    after = dict(runstore.hash_inputs(w.root, w.task), **{"<truncated>": {"skipped": "file-cap"}})
    run = w.exec_run("critic", attempts=[ch.attempt(before=before, after=after)])
    w.hook(run)
    entry = w.live("plan")
    assert entry["outputs_error"] == "hashes-incomplete" and entry["critic_responses"] == []


def test_an_after_map_of_none_is_incomplete_even_when_the_record_holds_the_before_map(w):
    before = runstore.hash_inputs(w.root, w.task)
    run = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS},
                     attempts=[ch.attempt(before=before, after=None)], extra={"input_hashes": before})
    w.hook(run)
    assert w.live("plan")["outputs_error"] == "hashes-incomplete"


def test_a_skip_marker_on_a_stage_file_is_incomplete(w):
    before = runstore.hash_inputs(w.root, w.task)
    after = dict(before)
    after[REL + "critic-response-2.md"] = {"skipped": "too-large"}
    run = w.exec_run("critic", attempts=[ch.attempt(before=before, after=after)])
    w.hook(run)
    assert w.live("plan")["outputs_error"] == "hashes-incomplete"


def test_an_exception_in_the_snapshot_is_stored_as_a_hook_error(w, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(evidence, "take_snapshot", boom)
    run = w.exec_run("plan")
    out = w.hook(run)
    assert not out.entry_recorded
    assert "hook_error" in w.synth.record(run)["telemetry"]
    assert len(w.synth.rows_for(run)) == 1


# -- supersession ---------------------------------------------------------------


def test_a_superseded_candidate_gets_a_row_no_entry_and_is_closed(w):
    old = w.exec_run("plan", state="interrupted")
    w.hook(old, outcome="INTERRUPTED")
    new = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(new, candidate=old)
    stored = w.synth.record(old)
    assert stored["superseded_by"] == new and cost.is_open(stored) is False
    rows = w.synth.rows_for(old)
    assert len(rows) == 1 and "outcome=superseded" in rows[0]
    assert all(old not in e["runs"] for e in w.entries())
    w.hook(new, candidate=old)
    assert len(w.synth.rows_for(old)) == 1 and len(w.synth.rows_for(new)) == 1


def test_a_budget_stopped_prepared_candidate_is_closed_without_a_row(w):
    old = w.exec_run("plan", state="prepared", attempts=[])
    new = w.exec_run("plan")
    w.hook(new, candidate=old)
    assert w.synth.rows_for(old) == []
    stored = w.synth.record(old)
    assert stored["superseded_by"] == new and stored["telemetry"]["ledger"]["row_reason"] == "never-spawned"


def test_a_new_run_that_failed_to_start_still_costs_the_old_run_once(w):
    old = w.exec_run("plan", state="interrupted")
    w.hook(old, outcome="INTERRUPTED")
    failed = w.exec_run("plan", state="interrupted", attempts=[ch.attempt(1, pid=None, state="interrupted")])
    out = w.hook(failed, candidate=old, outcome="INTERRUPTED")
    assert len(w.synth.rows_for(old)) == 1 and "outcome=superseded" in w.synth.rows_for(old)[0]
    assert w.synth.rows_for(failed) == [] and not out.entry_recorded
    assert w.synth.record(failed)["ledger_mark"]["exists"] is True
    assert cost.is_open(w.synth.record(failed)) is True


def test_a_candidate_the_pointer_still_names_is_not_costed(w):
    old = w.exec_run("plan", state="interrupted")
    other = w.exec_run("plan", state="completed", pointer=False)
    w.synth.set_pointer(old)
    w.hook(other, candidate=old)
    assert w.synth.rows_for(old) == [] and "superseded_by" not in w.synth.record(old)


def test_a_candidates_own_rewrite_does_not_fail_the_new_run(w):
    old = w.exec_run("plan", state="interrupted")
    cost.store_mark(w.root, old, w.mark)
    _rewrite(w)
    mark = cost.ledger_mark(w.root, w.task, ch.clock)
    w.mark = mark
    new = w.exec_run("plan")
    out = w.hook(new, candidate=old)
    assert out.violation is False
    assert w.synth.record(old)["telemetry"]["ledger"]["prefix"] == "changed"


# -- ledger boundary -------------------------------------------------------------


def _rewrite(w):
    w.synth.ledger.write_text("# Cost Ledger — t1\nrewritten | x | plan | m | task | n | 0\n", encoding="utf-8")


def test_a_prefix_change_marks_the_entry_boundary_a_violation(w):
    run = w.exec_run("plan", edit=lambda: _rewrite(w))
    out = w.hook(run)
    assert out.violation and w.live("plan")["boundary"] == "violation"
    assert "boundary-violation" in reasons(w.evaluate("plan"))
    assert len(w.synth.rows_for(run)) == 1


def test_a_clean_critic_cannot_launder_an_earlier_rewrite(w):
    plan = w.exec_run("plan", edit=lambda: _rewrite(w))
    w.hook(plan)
    w.mark = cost.ledger_mark(w.root, w.task, ch.clock)
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    out = w.hook(critic)
    assert out.violation is False
    entry = w.live("plan")
    assert entry["runs"] == [plan, critic] and entry["boundary"] == "violation"
    assert "boundary-violation" in reasons(w.evaluate("plan"))


def test_the_sticky_boundary_is_rederived_when_the_copied_field_is_cleared(w):
    plan = w.exec_run("plan", edit=lambda: _rewrite(w))
    w.hook(plan)
    directory = runstore.store_dir(w.root)
    state = runstore.load_workflow_state(directory, w.task)
    state["entries"][0]["boundary"] = None
    runstore.write_workflow_state(directory, state)
    w.mark = cost.ledger_mark(w.root, w.task, ch.clock)
    critic = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(critic)
    assert w.live("plan")["boundary"] == "violation"


def test_a_line_appended_during_the_run_is_listed_and_warned(w):
    def agent():
        with open(w.synth.ledger, "a", encoding="utf-8") as fh:
            fh.write("agent-row-1 | 2026-01-01 | plan | m | task | agent | 0\n")

    run = w.exec_run("plan", edit=agent)
    w.hook(run)
    entry = w.live("plan")
    assert entry["ledger_lines_appended_during_run"] == [{"uuid": "agent-row-1", "phase": "plan"}]
    assert "ledger-appended-during-run" in warnings(w.evaluate("plan"))


# -- status of a closed run --------------------------------------------------


def _status(w, **kw):
    from quoin.opencode_adapter import status

    return status.collect(w.root, **kw)


def _text(report):
    from quoin.opencode_adapter import status

    return status.render_text(report)


def _supersede(w, old_state="interrupted", **old_kw):
    old = w.exec_run("plan", state=old_state, **old_kw)
    w.hook(old, outcome="INTERRUPTED")
    new = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(new, candidate=old)
    return old, new


def test_status_of_a_closed_superseded_run_names_the_successor_and_no_resume_advice(w):
    old, new = _supersede(w)
    text = _text(_status(w, run_id=old))
    assert "superseded: this run is closed and costed; nothing to resume" in text
    assert new in text and "start the phase over with --new-run" not in text
    assert "resume is blocked" not in text and "quoin run" not in text


def test_a_closed_run_that_still_reads_running_keeps_kill_hints_but_not_the_start_over_line(w):
    old = w.exec_run("plan", state="running", attempts=[ch.attempt(1, state="running", ended=None, pid=4242)],
                     extra={})
    new = w.exec_run("critic", writes={CRITIC2: gh.CRITIC_PASS})
    w.hook(new, candidate=old)
    stored = w.synth.record(old)
    assert stored["state"] == "running" and stored["superseded_by"] == new
    direct = _status(w, run_id=old)
    listed = _status(w, run_id=new)
    item = next(i for i in listed["superseded_running"] if i["run_id"] == old)
    assert item["closed"] is True
    text_listed = _text(listed)
    assert "older run %s still reads running" % old in text_listed
    assert "kill -TERM" in text_listed and "then start over with --new-run" not in text_listed
    assert "superseded: this run is closed" in _text(direct)
    assert "then start over with --new-run" not in _text(direct)


def test_an_unclosed_older_running_record_keeps_todays_lines(w):
    old = w.exec_run("plan", state="running", attempts=[ch.attempt(1, state="running", ended=None, pid=4242)])
    w.exec_run("plan")
    listed = _status(w, task=w.task)
    item = next(i for i in listed["superseded_running"] if i["run_id"] == old)
    assert item["closed"] is False
    assert "then start over with --new-run" in _text(listed)
