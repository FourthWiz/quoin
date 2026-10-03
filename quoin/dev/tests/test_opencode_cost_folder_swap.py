"""A task folder swapped during a run, and the entry of a run that is already closed."""
from __future__ import annotations

import os
import shutil

import pytest

from quoin import cli
from quoin.opencode_adapter import cost, run_hooks, runstore

import _opencode_cost_helpers as ch

pytestmark = pytest.mark.skipif(os.name != "posix", reason="symlinks and process groups are POSIX-only")


@pytest.fixture
def w(tmp_path, monkeypatch):
    return ch.HookWorld(tmp_path, monkeypatch)


def _swap_for_link(root, task):
    folder = root / ".workflow_artifacts" / task
    copy = root / ".workflow_artifacts" / (task + "-copy")
    shutil.copytree(str(folder), str(copy), symlinks=True)
    shutil.rmtree(str(folder))
    os.symlink(str(copy), str(folder))


def test_folder_state_tells_present_absent_and_unsafe_apart(tmp_path):
    synth = ch.Synth(tmp_path)
    assert cost.folder_state(synth.root, synth.task) == "present"
    assert cost.folder_state(synth.root, "other") == "absent"
    _swap_for_link(synth.root, synth.task)
    assert cost.folder_state(synth.root, synth.task) == "unsafe"
    assert cost.task_folder_present(synth.root, synth.task) is False


def test_a_folder_replaced_by_a_link_after_the_mark_is_a_violation_with_no_row(w):
    run = w.exec_run("plan", edit=lambda: _swap_for_link(w.root, w.task))
    out = w.hook(run)
    ledger = w.synth.record(run)["telemetry"]["ledger"]
    assert out.violation is True
    assert ledger["prefix"] == "changed" and ledger["prefix_reason"] == "task-folder-unsafe-after-run"
    assert ledger["row"] == "skipped"
    rows = (w.root / ".workflow_artifacts" / (w.task + "-copy") / "cost-ledger.md").read_text(encoding="utf-8")
    assert run not in rows


def test_an_unsafe_folder_without_a_recorded_ledger_is_no_violation_and_no_row(w):
    run = w.exec_run("plan", edit=lambda: _swap_for_link(w.root, w.task))
    out = w.hook(run, mark={"exists": False, "size": 0, "sha256": None, "taken_at": "x"})
    ledger = w.synth.record(run)["telemetry"]["ledger"]
    assert out.violation is False
    assert ledger["row_reason"] == "task-folder-unsafe" and ledger["row"] == "skipped"


def test_a_moved_away_folder_is_missing_not_a_violation(w):
    folder = w.root / ".workflow_artifacts" / w.task
    run = w.exec_run("plan", edit=lambda: shutil.move(str(folder), str(w.root / "archived")))
    out = w.hook(run)
    ledger = w.synth.record(run)["telemetry"]["ledger"]
    assert out.violation is False
    assert ledger["row_reason"] == "task-folder-missing" and ledger["prefix"] != "changed"


def test_the_cli_outcome_is_failed_when_the_folder_is_swapped_after_the_mark(tmp_path, monkeypatch, capsys):
    project = ch.make_cost_project(tmp_path, monkeypatch, "record_only")
    try:
        real = run_hooks.after_phase_run

        def swapping(project_root, task, result, **kw):
            _swap_for_link(project.root, task)
            return real(project_root, task, result, **kw)

        monkeypatch.setattr(run_hooks, "after_phase_run", swapping)
        code, summary = project.run_cli(capsys)
        assert code == 2 and summary["outcome"] == "FAILED" and summary["reason"] == "boundary-violation"
        ledger = project.telemetry(summary["run_id"])["ledger"]
        assert ledger["prefix_reason"] == "task-folder-unsafe-after-run" and ledger["row"] == "skipped"
    finally:
        project.cleanup()


def test_a_plain_rerun_of_a_closed_run_records_no_second_entry(w):
    run = w.exec_run("plan")
    w.hook(run)
    assert len(w.entries()) == 1
    directory = runstore.store_dir(w.root)
    record = runstore.load_record(directory, run)
    record["telemetry"]["evidence"] = {"recorded": False, "reason": "stage-unresolved"}
    runstore.write_record(directory, record)
    out = w.hook(run)
    assert len(w.entries()) == 1 and len(w.synth.rows_for(run)) == 1
    assert out.entry_recorded is False and out.reason == "stage-unresolved"


def test_a_blocked_resume_of_a_run_that_spawned_earlier_records_one_entry(w):
    run = w.exec_run("plan", state="interrupted")
    first = w.hook(run, outcome="INTERRUPTED")
    assert first.reason == "run-open" and w.entries() == []
    out = w.hook(run, outcome="INTERRUPTED", blocked="effect-uncertain")
    assert out.entry_recorded is True
    assert len(w.entries()) == 1 and w.entries()[0]["resume_blocked"] == "effect-uncertain"
    w.hook(run, outcome="INTERRUPTED", blocked="effect-uncertain")
    assert len(w.entries()) == 1 and len(w.synth.rows_for(run)) == 1
