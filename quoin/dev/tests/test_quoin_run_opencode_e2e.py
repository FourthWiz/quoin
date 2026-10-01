"""`quoin run --runtime opencode` against the fake opencode executable."""
from __future__ import annotations

import json
import os
import signal
import threading
import time

import pytest

from quoin import cli

from _opencode_run_helpers import SEEDED_SECRET, InstalledProject

REPLAY = ("replay", {"fixture": "plain-complete.jsonl"})

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def world(tmp_path, monkeypatch):
    holder = {}

    def make(scenario, **kw):
        project = InstalledProject(tmp_path, monkeypatch, scenario, **kw)
        holder["project"] = project
        monkeypatch.setattr(cli, "_make_opencode_driver", project.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        return project

    yield make
    if "project" in holder:
        holder["project"].cleanup()


def run(project, capsys, *extra):
    argv = ["run", "demo", "--runtime", "opencode", "--profile", "work", "--phase", "plan",
            "--project-root", str(project.root)] + list(extra)
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def lock_file(project):
    return project.root / ".workflow_artifacts" / "memory" / "run-supervisor-demo.pid"


def test_plain_completion(world, capsys):
    project = world(REPLAY)
    code, summary = run(project, capsys)
    assert code == 0 and summary["outcome"] == "COMPLETED" and summary["evidence"] == "full"
    assert project.record(summary["run_id"])["state"] == "completed"
    assert os.path.exists(summary["sidecar"]) and not lock_file(project).exists()


def test_approval_prompt_stops_the_phase(world, capsys):
    project = world("approval_tool_error")
    code, summary = run(project, capsys)
    assert code == 4 and summary["outcome"] == "AWAITING_APPROVAL"
    assert project.stray() == []


def test_background_task_is_unverified(world, capsys):
    code, summary = run(world("task_background"), capsys)
    assert code == 6 and summary["outcome"] == "COMPLETED_UNVERIFIED"


def test_denied_tool_then_clean_finish(world, capsys):
    code, summary = run(world("denied_tool_then_clean_finish"), capsys)
    assert code == 0


def test_agent_fallback_fails_without_a_retry(world, capsys):
    project = world("agent_fallback")
    code, summary = run(project, capsys)
    assert code == 2 and summary["outcome"] == "FAILED" and summary["attempts"] == 1
    assert len(project.invocations()) == 1


def test_native_error_with_default_limits(world, capsys):
    code, summary = run(world("native_error"), capsys)
    assert code == 2


def test_crash_in_an_open_step_blocks_the_resume(world, capsys):
    project = world("crash_in_open_step")
    code, summary = run(project, capsys)
    assert code == 5 and summary["resume_blocked"] == "effect-uncertain"
    assert len(project.invocations()) <= 2
    assert summary["resume_hint"].endswith(" --new-run")


def test_exit_early_stops_at_the_relaunch_cap(world, capsys):
    code, summary = run(world("exit_early"), capsys, "--max-relaunch", "0")
    assert code == 5 and summary["reason"] == "relaunch cap"


def test_exit_early_without_progress_aborts(world, capsys):
    project = world("exit_early")
    code, summary = run(project, capsys, "--max-relaunch", "3")
    assert code == 2 and summary["reason"] == "no forward progress"
    assert summary["attempts"] == 2


def test_version_mismatch_is_refused_before_any_spawn(world, capsys):
    project = world("version_mismatch", version="0.0.1")
    code, summary = run(project, capsys)
    assert code == 3 and summary["refusal"]["category"] == "unsupported-version"
    assert project.invocations() == []


def test_resume_across_invocations(world, capsys):
    project = world("session_continuation")
    code, first = run(project, capsys, "--max-relaunch", "0")
    assert code == 5 and first["run_id"]
    code, second = run(project, capsys, "--max-relaunch", "0")
    assert code == 0 and second["run_id"] == first["run_id"]
    record = project.record(first["run_id"])
    assert len(record["attempts"]) == 2
    runs = project.invocations()
    assert len(runs) == 2
    assert "--session" in runs[1]["argv"] and "--command" not in runs[1]["argv"]
    assert project.effects() == ["effect.txt"]
    from quoin.opencode_adapter import runstore

    sequences = [e.sequence for e in runstore.read_sidecar(
        project.store() / (first["run_id"] + ".jsonl")).events]
    assert len(sequences) == len(set(sequences))
    code, third = run(project, capsys, "--new-run")
    assert third["run_id"] != first["run_id"]


def test_sigterm_during_an_attempt(world, capsys):
    project = world("hang")
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        code, summary = run(project, capsys)
    finally:
        timer.join()
    assert code == 143 and summary["outcome"] == "CANCELLED"
    assert project.record(summary["run_id"])["state"] == "cancelled"
    assert project.stray() == [] and not lock_file(project).exists()
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_live_lock_refuses_and_nothing_spawns(world, capsys):
    project = world(REPLAY)
    lock = lock_file(project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getppid(), "writer": "cli", "task": "demo"}) + "\n")
    code, summary = run(project, capsys)
    assert code == 3 and summary["refusal"]["code"] == "lock-held"
    assert project.invocations() == []


def test_a_seeded_secret_never_reaches_any_output(world, capsys):
    project = world("secret_echo")
    code, summary = run(project, capsys, "--halt-on-abort")
    memory = project.root / ".workflow_artifacts" / "memory"
    texts = [json.dumps(summary)]
    texts.append((project.store() / (summary["run_id"] + ".run.json")).read_text())
    result = memory / "run-supervisor-demo.result"
    if result.exists():
        texts.append(result.read_text())
    assert all(SEEDED_SECRET not in text for text in texts)
