"""The ledger layer: mark, scan, exactly-once append, recording a run, the pre-launch row."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from quoin.opencode_adapter import cost, phase_loop, runstore

import _opencode_cost_helpers as ch

SOURCE = ch.SOURCE_DIR


@pytest.fixture
def synth(tmp_path):
    return ch.Synth(tmp_path)


def core():
    return cost.load_cost_event(SOURCE)


def mark_of(s):
    return cost.ledger_mark(s.root, s.task, ch.clock)


def record(s, run_id, *, mark=None, ended_as="completed", source_dir=SOURCE, **kw):
    return cost.record_run(
        s.root, run_id, source_dir=source_dir, mark=mark, ended_as=ended_as, clock=ch.clock, **kw)


def test_first_call_writes_one_eight_column_row(synth):
    run = synth.seed()
    out = record(synth, run, mark=mark_of(synth))
    assert out.row == "written" and out.closed and not out.violation
    rows = synth.rows_for(run)
    assert len(rows) == 1 and len(rows[0].split("|")) == 8
    event = core().parse_row(rows[0])
    assert event.uuid == run and event.phase == "plan" and event.model_or_effort == "quoin-p/some-model"
    assert event.date == "2027-01-15"
    assert event.attribution == "tok=12;src=unresolved"
    assert "command=quoin-plan outcome=completed attempts=1" in event.note
    assert core().classify_attribution(event.attribution)[0] == "unresolvable"
    assert synth.record(run)["telemetry"]["final"] is True


def test_second_call_appends_nothing(synth):
    run = synth.seed()
    mark = mark_of(synth)
    first = record(synth, run, mark=mark)
    second = record(synth, run, mark=mark)
    assert len(synth.rows_for(run)) == 1
    assert second.row == first.row and second.closed


def test_a_crash_after_the_append_appends_nothing_on_the_next_call(synth):
    run = synth.seed()
    mark = mark_of(synth)
    record(synth, run, mark=mark)
    stored = synth.record(run)
    del stored["telemetry"]
    runstore.write_record(synth.directory, stored)
    out = record(synth, run, mark=mark)
    assert len(synth.rows_for(run)) == 1
    assert out.row == "present" and synth.record(run)["telemetry"]["final"] is True


def test_a_missing_ledger_is_created_with_the_standard_header(tmp_path):
    s = ch.Synth(tmp_path, ledger=False)
    run = s.seed()
    out = record(s, run, mark=mark_of(s))
    assert out.row == "written" and out.prefix == "unchanged"
    text = s.ledger.read_text(encoding="utf-8")
    assert text.startswith(ch.HEADER) and len(s.rows_for(run)) == 1


def test_a_ledger_without_a_trailing_newline_is_not_glued(synth):
    synth.ledger.write_text(ch.HEADER + ch.SEED_ROW.rstrip("\n"), encoding="utf-8")
    run = synth.seed()
    record(synth, run, mark=mark_of(synth))
    lines = synth.ledger.read_text(encoding="utf-8").splitlines()
    assert lines[1] == ch.SEED_ROW.rstrip("\n")
    assert lines[2].startswith(run)


def test_a_symlinked_ledger_is_refused_and_the_target_is_unchanged(tmp_path):
    s = ch.Synth(tmp_path, ledger=False)
    target = tmp_path / "elsewhere.md"
    target.write_text("original\n")
    os.symlink(str(target), str(s.ledger))
    run = s.seed()
    out = record(s, run, mark=mark_of(s))
    assert out.row == "skipped" and out.row_reason == "ledger-unsafe"
    assert target.read_text() == "original\n"


def test_an_agent_line_after_the_mark_is_kept_and_reported(synth):
    mark = mark_of(synth)
    with open(synth.ledger, "a", encoding="utf-8") as fh:
        fh.write("agent-row-1 | 2026-01-01 | plan | m | task | agent | 0\n")
        fh.write("sibling-row | 2026-01-01 | critic | m | task | sibling | 0\n")
    run = synth.seed()
    out = record(synth, run, mark=mark, own_uuids=("sibling-row",))
    assert out.prefix == "unchanged" and not out.violation
    assert out.appended == [{"uuid": "agent-row-1", "phase": "plan"}]
    assert "agent-row-1" in synth.ledger.read_text()
    tel = synth.record(run)["telemetry"]["ledger"]
    assert tel["ledger_lines_appended_during_run"] == out.appended and tel["appended_total"] == 1


@pytest.mark.parametrize("damage", ["flip", "truncate", "delete"])
def test_a_changed_prefix_is_a_violation_but_the_row_is_still_written_once(synth, damage):
    mark = mark_of(synth)
    if damage == "flip":
        synth.ledger.write_text(ch.HEADER + ch.SEED_ROW.replace("seed-model", "seed-modal"), encoding="utf-8")
    elif damage == "truncate":
        synth.ledger.write_text("# Cost", encoding="utf-8")
    else:
        synth.ledger.unlink()
    run = synth.seed()
    out = record(synth, run, mark=mark)
    assert out.prefix == "changed" and out.violation and out.row == "written"
    assert len(synth.rows_for(run)) == 1
    assert synth.record(run)["telemetry"]["ledger"]["prefix"] == "changed"


@pytest.mark.parametrize("swap", ["symlink", "directory", "fifo", "oversize"])
def test_a_ledger_replaced_after_the_mark_is_a_violation(synth, tmp_path, monkeypatch, swap):
    mark = mark_of(synth)
    if swap == "oversize":
        monkeypatch.setattr(cost, "MAX_LEDGER_BYTES", 10)
    else:
        forged = tmp_path / "forged.md"
        forged.write_text(synth.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        synth.ledger.unlink()
        if swap == "symlink":
            synth.ledger.symlink_to(forged)
        elif swap == "directory":
            synth.ledger.mkdir()
        else:
            os.mkfifo(str(synth.ledger))
    result = cost.scan(synth.ledger, mark, "run-x")
    assert result.prefix == "changed" and result.prefix_reason.endswith("-after-run")
    run = synth.seed()
    out = record(synth, run, mark=mark)
    assert out.prefix == "changed" and out.violation
    assert synth.record(run)["telemetry"]["ledger"]["prefix"] == "changed"


def test_a_ledger_that_appears_unsafe_when_the_mark_said_absent_is_a_violation(tmp_path):
    s = ch.Synth(tmp_path, ledger=False)
    mark = mark_of(s)
    assert mark["exists"] is False
    forged = tmp_path / "forged.md"
    forged.write_text("x\n", encoding="utf-8")
    s.ledger.symlink_to(forged)
    assert cost.scan(s.ledger, mark, "run-x").prefix == "changed"


def test_an_unsafe_ledger_without_a_usable_mark_stays_unavailable(synth, tmp_path):
    forged = tmp_path / "forged.md"
    forged.write_text("x\n", encoding="utf-8")
    synth.ledger.unlink()
    synth.ledger.symlink_to(forged)
    assert cost.scan(synth.ledger, None, "run-x").prefix == "unavailable"


def test_a_fifo_ledger_is_refused_without_blocking(synth):
    synth.ledger.unlink()
    os.mkfifo(str(synth.ledger))
    assert cost.append_row(synth.root, synth.task, "r | d | plan | m | task | n | 0") == "ledger-unsafe"


def test_no_mark_means_no_prefix_verdict_and_no_violation(synth):
    run = synth.seed()
    out = record(synth, run, mark=None)
    assert out.prefix == "unavailable" and not out.violation and out.row == "written"


def test_an_over_cap_ledger_skips_the_row(synth, monkeypatch):
    mark = mark_of(synth)
    monkeypatch.setattr(cost, "MAX_LEDGER_BYTES", 10)
    run = synth.seed()
    out = record(synth, run, mark=mark)
    assert out.row == "skipped" and out.row_reason == "ledger-too-large"
    assert synth.rows_for(run) == []


def test_an_absent_task_folder_creates_no_file_anywhere(tmp_path):
    s = ch.Synth(tmp_path, task_folder=False)
    run = s.seed()
    out = record(s, run, mark=cost.ledger_mark(s.root, s.task, ch.clock))
    assert out.row == "skipped" and out.row_reason == "task-folder-missing"
    assert not (s.root / ".workflow_artifacts" / ch.TASK).exists()
    assert s.record(run)["telemetry"]["final"] is True


@pytest.mark.parametrize("state", ["prepared", "running", "interrupted"])
@pytest.mark.parametrize("ended_as", ["completed", "failed", "interrupted", "cancelled"])
def test_open_records_write_nothing(synth, state, ended_as):
    run = synth.seed(state)
    out = record(synth, run, mark=mark_of(synth), ended_as=ended_as)
    assert out.row == "skipped" and out.row_reason == "run-open" and not out.closed
    assert synth.rows_for(run) == [] and "telemetry" not in synth.record(run)


def test_checkpoint_invalid_block_is_costed_and_stored_in_one_write(synth):
    run = synth.seed("interrupted", attempts=[ch.attempt(1, state="interrupted")])
    out = record(synth, run, mark=mark_of(synth), ended_as="interrupted", resume_blocked="checkpoint-invalid")
    assert out.row == "written"
    rows = synth.rows_for(run)
    assert len(rows) == 1 and "outcome=interrupted" in rows[0]
    stored = synth.record(run)
    assert stored["resume_blocked"] == "checkpoint-invalid" and stored["telemetry"]["final"] is True
    before = stored["telemetry"]
    phase_loop.annotate_record(synth.root, run, "hint", "checkpoint-invalid")
    after = synth.record(run)
    assert after["telemetry"] == before and len(synth.rows_for(run)) == 1
    again = record(synth, run, mark=mark_of(synth), ended_as="interrupted", resume_blocked="checkpoint-invalid")
    assert again.row == "written" and len(synth.rows_for(run)) == 1


def test_close_path_costs_an_open_interrupted_run_once(synth):
    run = synth.seed("interrupted", attempts=[ch.attempt(1, state="interrupted")])
    new = synth.seed("running", pointer=True)
    out = record(synth, run, mark=mark_of(synth), ended_as="superseded", superseded_by=new)
    assert out.row == "written" and out.closed
    rows = synth.rows_for(run)
    assert len(rows) == 1 and "outcome=superseded" in rows[0]
    stored = synth.record(run)
    assert stored["superseded_by"] == new and stored["resume_blocked"] == "superseded"
    assert new in stored["resume_hint"] and "quoin run" not in stored["resume_hint"]
    assert stored["state"] == "interrupted" and len(stored["attempts"]) == 1
    assert cost.is_open(stored) is False
    record(synth, run, mark=mark_of(synth), ended_as="superseded", superseded_by=new)
    assert len(synth.rows_for(run)) == 1


def test_close_path_for_a_budget_stopped_prepared_run_has_no_row(synth):
    run = synth.seed("prepared", attempts=[])
    new = synth.seed("running")
    out = record(synth, run, mark=mark_of(synth), ended_as="superseded", superseded_by=new)
    assert out.row == "skipped" and out.row_reason == "never-spawned" and out.closed
    assert synth.rows_for(run) == []
    stored = synth.record(run)
    assert stored["telemetry"]["final"] is True and stored["superseded_by"] == new


def test_close_path_for_a_stale_running_run_gets_a_row(synth):
    run = synth.seed("running", attempts=[ch.attempt(1, state="running", ended=None)])
    new = synth.seed("running")
    out = record(synth, run, mark=mark_of(synth), ended_as="superseded", superseded_by=new)
    assert out.row == "written" and cost.is_open(synth.record(run)) is False


def test_close_path_without_a_successor_writes_nothing(synth):
    run = synth.seed("interrupted")
    out = record(synth, run, mark=mark_of(synth), ended_as="superseded")
    assert out.row_reason == "superseded-without-successor" and not out.closed
    assert synth.rows_for(run) == [] and "telemetry" not in synth.record(run)
    own = record(synth, run, mark=mark_of(synth), ended_as="superseded", superseded_by=run)
    assert own.row_reason == "superseded-without-successor"


def test_a_spawn_failed_record_counts_as_not_spawned(synth):
    run = synth.seed("failed", attempts=[ch.attempt(1, pid=None, state="interrupted", ended=0)])
    out = record(synth, run, mark=mark_of(synth), ended_as="failed")
    assert out.row == "skipped" and out.row_reason == "never-spawned"
    assert synth.rows_for(run) == [] and synth.record(run)["telemetry"]["final"] is True


def test_without_a_source_dir_the_row_is_skipped_but_the_prefix_still_counts(synth):
    mark = mark_of(synth)
    synth.ledger.write_text(ch.HEADER + "rewritten | x | plan | m | task | n | 0\n", encoding="utf-8")
    run = synth.seed()
    out = record(synth, run, mark=mark, source_dir=None)
    assert out.row == "skipped" and out.row_reason == "source-unavailable"
    assert out.prefix == "changed" and out.violation
    assert synth.record(run)["telemetry"]["ledger"]["prefix"] == "changed"
    assert synth.rows_for(run) == []


def test_store_mark_keeps_the_first_mark(synth):
    run = synth.seed("running")
    first, second = {"exists": True, "size": 1, "sha256": "a"}, {"exists": True, "size": 2, "sha256": "b"}
    cost.store_mark(synth.root, run, first)
    cost.store_mark(synth.root, run, second)
    assert synth.record(run)["ledger_mark"] == first


def test_a_stored_mark_wins_over_the_argument(synth):
    run = synth.seed()
    cost.store_mark(synth.root, run, mark_of(synth))
    with open(synth.ledger, "a", encoding="utf-8") as fh:
        fh.write("agent-x | 2026-01-01 | plan | m | task | a | 0\n")
    out = record(synth, run, mark={"exists": False, "size": 0, "sha256": None})
    assert out.appended == [{"uuid": "agent-x", "phase": "plan"}]


def _prepared(run, phase="end_of_task"):
    return SimpleNamespace(
        run_id=run, effective_model="quoin-p/some-model",
        request=SimpleNamespace(phase=phase, task=ch.TASK))


def test_prelaunch_row_is_written_once_with_unresolved_attribution(synth):
    run = synth.seed("prepared", attempts=[], phase="end_of_task")
    mark = mark_of(synth)
    cost.prelaunch_row(synth.root, _prepared(run), source_dir=SOURCE, mark=mark, clock=ch.clock)
    cost.prelaunch_row(synth.root, _prepared(run), source_dir=SOURCE, mark=mark, clock=ch.clock)
    rows = synth.rows_for(run)
    assert len(rows) == 1
    assert "outcome=launched" in rows[0] and rows[0].split("|")[-1].strip() == "src=unresolved"
    assert "command=quoin-end-of-task" in rows[0] and rows[0].split("|")[2].strip() == "end-of-task"
    stored = synth.record(run)
    assert stored["prelaunch_row"]["row"] in ("written", "present") and stored["ledger_mark"]["exists"] is True


def test_prelaunch_row_is_absent_for_other_phases(synth):
    run = synth.seed("prepared", attempts=[], phase="plan")
    cost.prelaunch_row(synth.root, _prepared(run, "plan"), source_dir=SOURCE, mark=mark_of(synth), clock=ch.clock)
    assert synth.rows_for(run) == [] and "prelaunch_row" not in synth.record(run)


def test_a_later_record_run_finds_the_prelaunch_row(synth):
    run = synth.seed("prepared", attempts=[ch.attempt(1)], phase="end_of_task")
    mark = mark_of(synth)
    cost.prelaunch_row(synth.root, _prepared(run), source_dir=SOURCE, mark=mark, clock=ch.clock)
    synth.ledger.write_text(synth.ledger.read_text(encoding="utf-8"), encoding="utf-8")
    stored = synth.record(run)
    stored["state"] = "completed"
    runstore.write_record(synth.directory, stored)
    out = record(synth, run, mark=mark)
    assert out.row == "present" and len(synth.rows_for(run)) == 1


def test_a_hostile_note_or_model_still_yields_eight_columns(synth):
    run = synth.seed(prepared={"effective_model": "a|b\nc | d"})
    record(synth, run, mark=mark_of(synth))
    rows = synth.rows_for(run)
    assert len(rows) == 1 and len(rows[0].split("|")) == 8
    assert synth.ledger.read_text(encoding="utf-8").count("\n") == 3


def test_ledger_mark_forms(synth, tmp_path):
    mark = mark_of(synth)
    assert mark["exists"] is True and mark["size"] == len((ch.HEADER + ch.SEED_ROW).encode("utf-8"))
    assert len(mark["sha256"]) == 64
    none = ch.Synth(tmp_path / "x", task_folder=False)
    assert cost.ledger_mark(none.root, none.task, ch.clock)["exists"] is False
    nofile = ch.Synth(tmp_path / "y", ledger=False)
    assert cost.ledger_mark(nofile.root, nofile.task, ch.clock)["exists"] is False
    synth.ledger.unlink()
    os.symlink(str(tmp_path / "t"), str(synth.ledger))
    unsafe = cost.ledger_mark(synth.root, synth.task, ch.clock)
    assert unsafe["exists"] is None and unsafe["reason"] == "ledger-unsafe"


def test_a_record_that_is_missing_reports_it(synth):
    out = cost.record_run(synth.root, "oc-20260101T000000Z-aaaaaaaa", source_dir=SOURCE, ended_as="completed")
    assert out.row == "skipped" and out.row_reason == "record-missing"
