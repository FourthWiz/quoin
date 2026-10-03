"""Cost rows and run telemetry through `quoin run` against the fake opencode executable."""
from __future__ import annotations

import json
import os
import signal
import threading

import pytest

import _opencode_cost_helpers as ch
import _opencode_gate_helpers as gh
from _opencode_run_helpers import SEEDED_SECRET
from test_quoin_run_opencode_cli import KEYS

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

ROOT_CRITIC = ".workflow_artifacts/demo/critic-response-1.md"


@pytest.fixture
def world(tmp_path, monkeypatch):
    holder = {}

    def make(scenario, **kw):
        project = ch.make_cost_project(tmp_path, monkeypatch, scenario, **kw)
        holder["project"] = project
        return project

    yield make
    if "project" in holder:
        holder["project"].cleanup()


def no_usd_zero(project):
    return "usd=0;" not in project.ledger.read_text(encoding="utf-8")


def test_a_completed_plan_run_writes_one_row_and_telemetry(world, capsys):
    project = world("record_only")
    code, summary = project.run_cli(capsys)
    assert code == 0 and sorted(summary) == KEYS
    run = summary["run_id"]
    rows = project.rows_for(run)
    assert len(rows) == 1 and len(project.rows()) == 2
    row = rows[0]
    core = ch.load_core_cost_event()
    assert row.phase == "plan" and row.category == "task" and row.fallback_fires == 0
    assert row.model_or_effort and row.model_or_effort != "unknown"
    assert "command=quoin-plan outcome=completed attempts=1" in row.note
    assert row.attribution == "tok=12;src=unresolved"
    assert core.classify_attribution(row.attribution)[0] == "unresolvable"
    line = [ln for ln in project.ledger.read_text().splitlines() if ln.startswith(run)][0]
    assert len(line.split("|")) == 8
    t = project.telemetry(run)
    assert t["final"] is True and t["provider"]["id"] and t["provider"]["native_id"]
    assert t["model"]["effective"] == row.model_or_effort
    assert t["effort"]["requested"] is None and t["effort"]["requested_reason"]
    assert t["effort"]["effective"] == "unknown"
    assert t["retries"]["attempts"] == 1 and t["elapsed"]["total_seconds"] is not None
    assert t["native"]["step_finish_part_ids"] == ["prt_f1"]
    assert t["cost"]["usd_reason"] == "cost-unpriced-model" and t["cost"]["usd"] is None
    assert t["unavailable"] == ["child-session-usage", "effective-effort"]
    assert t["usage_provenance"]["parent_cost_includes_children"] is False


def test_resume_across_invocations_counts_the_replayed_part_once(world, capsys):
    project = world("session_continuation_replay")
    code, first = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 5
    run = first["run_id"]
    assert project.rows_for(run) == [] and project.entries() == []
    assert project.record(run)["ledger_mark"]["exists"] is True
    code, second = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 0 and second["run_id"] == run
    rows = project.rows_for(run)
    assert len(rows) == 1
    assert rows[0].attribution == "tok=24;src=unresolved"
    assert "attempts=2" in rows[0].note


def test_a_revised_usage_part_is_counted_at_its_latest_revision(world, capsys):
    project = world("usage_revised")
    code, summary = project.run_cli(capsys)
    assert code == 0
    t = project.telemetry(summary["run_id"])
    assert t["usage"]["input_tokens"] == 50 and t["usage"]["output_tokens"] == 7
    assert t["native"]["revised_parts"] == 1
    assert project.rows_for(summary["run_id"])[0].attribution == "tok=57;src=unresolved"


def test_unknown_tokens_are_never_written_as_zero(world, capsys):
    project = world("usage_unknown_tokens")
    code, summary = project.run_cli(capsys)
    row = project.rows_for(summary["run_id"])[0]
    assert row.attribution == "src=unresolved"
    t = project.telemetry(summary["run_id"])
    assert t["usage"]["output_tokens"] is None and t["usage"]["tokens"] is None
    assert t["usage"]["reasons"]["output_tokens"] and no_usd_zero(project)


def test_a_reported_zero_cost_stays_unpriced(world, capsys):
    project = world("usage_zero_cost")
    code, summary = project.run_cli(capsys)
    row = project.rows_for(summary["run_id"])[0]
    assert row.attribution == "tok=120;src=unresolved"
    assert project.telemetry(summary["run_id"])["cost"]["usd_reason"] == "cost-unpriced-model"
    assert no_usd_zero(project)


def test_a_line_the_agent_appended_is_kept_and_reported(world, capsys):
    project = world("ledger_append")
    code, summary = project.run_cli(capsys)
    assert code == 0
    run = summary["run_id"]
    text = project.ledger.read_text()
    assert "agent-row-1" in text and len(project.rows_for(run)) == 1
    t = project.telemetry(run)
    assert t["ledger"]["ledger_lines_appended_during_run"] == [{"uuid": "agent-row-1", "phase": "plan"}]
    entry = project.entries()[-1]
    assert entry["ledger_lines_appended_during_run"] == [{"uuid": "agent-row-1", "phase": "plan"}]
    code, data = project.gate(capsys)
    assert "ledger-appended-during-run" in data["warnings"]


def test_a_ledger_rewrite_fails_the_run_and_the_gate_keeps_refusing(world, capsys):
    project = world("ledger_rewrite")
    code, summary = project.run_cli(capsys)
    assert code == 2 and summary["outcome"] == "FAILED" and summary["reason"] == "boundary-violation"
    assert summary["resume_hint"] and summary["resume_hint"].endswith("--new-run")
    run = summary["run_id"]
    assert project.record(run)["state"] == "completed" and len(project.rows_for(run)) == 1
    assert project.entries()[-1]["boundary"] == "violation"
    code, data = project.gate(capsys)
    assert "boundary-violation" in data["reasons"]
    fake = project.dh.fake
    project.set_scenario(fake._scenario(
        {"do": "write_file", "path": ROOT_CRITIC, "content": gh.CRITIC_PASS},
        fake._step_start("prt_s1"), fake._step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}))
    code, second = project.run_cli(capsys, phase="critic")
    assert second["outcome"] in ("COMPLETED", "COMPLETED_UNVERIFIED")
    entry = project.entries()[-1]
    assert entry["boundary"] == "violation" and run in entry["runs"]
    code, data = project.gate(capsys)
    assert "boundary-violation" in data["reasons"]


def test_a_cancelled_run_and_a_blocked_run_are_each_costed(world, capsys):
    project = world("hang")
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        code, summary = project.run_cli(capsys)
    finally:
        timer.join()
    assert code == 143 and summary["outcome"] == "CANCELLED"
    assert len(project.rows_for(summary["run_id"])) == 1
    assert "outcome=cancelled" in project.rows_for(summary["run_id"])[0].note
    project.set_scenario("crash_in_open_step")
    code, blocked = project.run_cli(capsys, "--new-run")
    assert code == 5 and blocked["resume_blocked"] == "effect-uncertain"
    assert len(project.rows_for(blocked["run_id"])) == 1
    assert len(project.rows()) == 3


def test_a_new_run_supersedes_an_interrupted_resumable_run(world, capsys):
    project = world("session_continuation")
    code, first = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 5
    old = first["run_id"]
    project.set_scenario(("replay", {"fixture": "plain-complete.jsonl"}))
    code, second = project.run_cli(capsys, "--new-run")
    assert code == 0 and second["run_id"] != old
    rows = project.rows_for(old)
    assert len(rows) == 1 and "outcome=superseded" in rows[0].note
    assert len(project.rows_for(second["run_id"])) == 1
    stored = project.record(old)
    assert stored["superseded_by"] == second["run_id"]
    text = _status_text(project, capsys, old)
    assert "superseded: this run is closed and costed" in text
    assert "start the phase over" not in text and second["run_id"] in text
    project.run_cli(capsys, phase="critic")
    assert len(project.rows_for(old)) == 1


def _status_text(project, capsys, run_id):
    from quoin import cli

    cli.main(["opencode", "status", "--run", run_id, "--project-root", str(project.root)])
    return capsys.readouterr().out


def test_a_different_phase_supersedes_the_open_run_without_new_run(world, capsys):
    project = world("session_continuation")
    code, first = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 5
    project.set_scenario(("replay", {"fixture": "plain-complete.jsonl"}))
    code, second = project.run_cli(capsys, phase="critic")
    old = first["run_id"]
    rows = project.rows_for(old)
    assert len(rows) == 1 and "outcome=superseded" in rows[0].note
    assert project.record(old)["superseded_by"] == second["run_id"]


def test_a_checkpoint_that_is_unreadable_still_costs_the_run_once(world, capsys):
    project = world("session_continuation")
    code, first = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 5
    old = first["run_id"]
    from quoin.opencode_adapter import runstore

    checkpoint = runstore.run_paths(project.store(), old).checkpoint
    checkpoint.write_text("{not json", encoding="utf-8")
    code, again = project.run_cli(capsys, "--max-relaunch", "0")
    assert code == 5 and again["reason"] == "checkpoint-invalid" and again["run_id"] == old
    rows = project.rows_for(old)
    assert len(rows) == 1 and "outcome=interrupted" in rows[0].note
    assert project.record(old)["telemetry"]["final"] is True
    project.set_scenario(("replay", {"fixture": "plain-complete.jsonl"}))
    code, third = project.run_cli(capsys, "--new-run")
    assert len(project.rows_for(old)) == 1 and len(project.rows_for(third["run_id"])) == 1


def test_end_of_task_row_is_written_before_launch_and_survives_the_archive(world, capsys):
    scenario = {"attempts": [{"steps": [
        {"do": "append_file", "path": ".workflow_artifacts/demo/cost-ledger.md",
         "content": "agent-row-1 | 2026-01-01 | end-of-task | m | task | agent | 0\n"},
        {"do": "move_path", "from": ".workflow_artifacts/demo", "to": ".workflow_artifacts/finalized/demo"},
        {"do": "emit", "event": {"type": "step_start", "sessionID": "$session",
                                 "part": {"id": "prt_s1", "messageID": "m", "type": "step-start"}}},
        {"do": "emit", "event": {"type": "step_finish", "sessionID": "$session", "part": {
            "id": "prt_f1", "reason": "stop", "messageID": "m", "type": "step-finish",
            "tokens": {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0.001}}},
        {"do": "exit", "code": 0}]}]}
    project = world(scenario)
    code, summary = project.run_cli(capsys, phase="end_of_task")
    run = summary["run_id"]
    archived = project.root / ".workflow_artifacts" / "finalized" / "demo" / "cost-ledger.md"
    text = archived.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines[0].startswith("seed-row-1") and lines[1].startswith(run) and lines[2].startswith("agent-row-1")
    assert "outcome=launched" in lines[1] and lines[1].endswith("src=unresolved")
    assert not (project.task_dir / "cost-ledger.md").exists()
    t = project.telemetry(run)
    assert t["final"] is True and t["usage"]["tokens"] == 12
    assert t["ledger"]["row_reason"] == "task-folder-missing"


def test_a_version_mismatch_refusal_writes_no_row(world, capsys):
    project = world("version_mismatch", version="0.0.1")
    code, summary = project.run_cli(capsys, phase="end_of_task")
    assert code == 3
    assert len(project.rows()) == 1


def test_without_a_task_folder_there_is_telemetry_but_no_ledger_or_state(world, capsys):
    project = world("record_only", task_folder=False)
    code, summary = project.run_cli(capsys)
    assert code == 0
    assert not (project.root / ".workflow_artifacts" / "demo" / "cost-ledger.md").exists()
    assert not project.task_dir.exists()
    assert project.telemetry(summary["run_id"])["final"] is True
    assert project.entries() == []


def test_a_seeded_secret_never_reaches_the_ledger_record_or_state(world, capsys):
    project = world("secret_echo")
    code, summary = project.run_cli(capsys)
    texts = [project.ledger.read_text(), json.dumps(summary)]
    texts.append((project.store() / (summary["run_id"] + ".run.json")).read_text())
    state = project.store() / "workflow-demo.json"
    if state.exists():
        texts.append(state.read_text())
    assert all(SEEDED_SECRET not in text for text in texts)


def test_a_gate_write_after_a_run_keeps_one_row_per_run_id(world, capsys):
    project = world("record_only")
    code, summary = project.run_cli(capsys)
    before = project.ledger.read_text()
    project.gate(capsys, "plan", "--write")
    after = project.ledger.read_text()
    assert [r.uuid for r in project.rows()].count(summary["run_id"]) == 1
    assert after.startswith(before)
    code, again = project.run_cli(capsys)
    ids = [r.uuid for r in project.rows()]
    assert ids.count(summary["run_id"]) == 1 and ids.count(again["run_id"]) == 1
