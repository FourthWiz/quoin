"""Core contract tests for the auto_resume.py run-continuation gate (IVG-280).

Covers the shared gate order, the stop/start/arm/handoff/pause/status
subcommands, owner-liveness detection, and the settle_supervisor accounting
rule, against a fake `.workflow_artifacts/memory` tree and a fake
`~/.claude/projects` tree (patched via `HOME`). Does not attempt the full
exhaustive per-branch matrix named in the architecture's stage decomposition
(test_auto_resume_arm.py / _owner_state.py / _settle_supervisor.py /
_handoff.py / _gate.py) — those remain open follow-up work.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("auto_resume_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar(monkeypatch):
    return _load_module()


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    memory = root / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True)
    return root


def _write_marker(memory_dir: Path, task: str, timestamp: str = "2026-09-29T00:00:00+00:00") -> None:
    (memory_dir / f"autonomous-run-{task}.marker").write_text(
        f"task: {task}\ntimestamp: {timestamp}\nautonomous: true\n", encoding="utf-8"
    )


def _write_record(memory_dir: Path, task: str, session_id: str, **overrides) -> None:
    record = {
        "schema": 1,
        "task": task,
        "session_id": session_id,
        "active": True,
        "phase": "implement",
        "phase_index": 3,
        "subphase": "",
        "step": "",
        "at_stage_boundary": False,
        "route": "",
        "profile": "",
        "artifacts": [],
        "next_action": "",
        "resume_command": f"/run --resume {task}",
        "notes_path": str(memory_dir / f"run-notes-{task}.md"),
        "updated_at": overrides.pop("updated_at", "2026-09-29T00:00:00+00:00"),
    }
    record.update(overrides)
    (memory_dir / f"run-state-{task}.json").write_text(json.dumps(record), encoding="utf-8")


def _arm(memory_dir: Path, sid: str) -> None:
    (memory_dir / f"run-continue-arm-{sid}.txt").touch()


def _stop_stdin(monkeypatch, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")

    class _Buf:
        def read(self, n):
            return data[:n]

    class _FakeStdin:
        buffer = _Buf()

    monkeypatch.setattr(sys, "stdin", _FakeStdin())


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# Plain /run (no markers) stays byte-identical: empty stdout, exit 0.
# ---------------------------------------------------------------------------


def test_stop_no_markers_is_silent(ar, project, monkeypatch, capsys):
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "cwd": str(project)})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_start_source_clear_is_silent(ar, project, capsys):
    rc = ar._cmd_start(_Args(project_root=str(project), source="clear", session_id="sid-1"))
    assert rc == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Opt-out.
# ---------------------------------------------------------------------------


def test_opt_out_disables_stop(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    monkeypatch.setenv("QUOIN_AUTO_RESUME", "0")
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert not (memory / "auto-resume-demo.json").exists()


# ---------------------------------------------------------------------------
# Stop happy path.
# ---------------------------------------------------------------------------


def test_stop_continues_owning_session(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})

    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "/run --resume demo" in out["reason"]
    assert "[quoin-auto-resume]" in out["systemMessage"]

    counter = json.loads((memory / "auto-resume-demo.json").read_text())
    assert counter["attempts"] == 1
    assert counter["chain_blocks"] == 1


def test_stop_no_arm_is_silent(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_stop_session_id_mismatch_is_silent(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    _stop_stdin(monkeypatch, {"session_id": "sid-other"})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_stop_background_tasks_allows_without_mutation(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "background_tasks": [{"id": "t1"}]})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert not (memory / "auto-resume-demo.json").exists()


# ---------------------------------------------------------------------------
# Cap and no-progress halts (I-13: empty stdout, halt file written).
# ---------------------------------------------------------------------------


def test_stop_cap_halts_and_stays_silent(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["attempts"] = 10
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})

    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: auto-resume cap" in halt


def test_stop_no_forward_progress_halts(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["consecutive_no_progress"] = 1
    counter["last_done_count"] = 0
    counter["last_phase"] = ["implement", 3]
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})

    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: no forward progress" in halt


def test_stop_a_halted_task_is_skipped(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    (memory / "autonomous-halt-demo.md").write_text("task: demo\nreason: prior halt\n")
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# arm — D-17 reset rules.
# ---------------------------------------------------------------------------


def test_arm_resets_counter_when_not_in_flight(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["attempts"] = 5
    counter["in_flight"] = False
    ar._write_counter(memory, "demo", counter)

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 0
    assert (memory / "run-continue-arm-sid-1.txt").exists()


def test_arm_does_not_reset_while_in_flight(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["attempts"] = 5
    counter["in_flight"] = True
    ar._write_counter(memory, "demo", counter)

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="fresh"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 5
    assert reloaded["in_flight"] is False


def test_arm_does_not_reset_while_supervisor_lock_live(ar, project, monkeypatch):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["attempts"] = 4
    counter["in_flight"] = False
    ar._write_counter(memory, "demo", counter)
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: True)
    ar._write_lock(memory, "demo", pid=999, granted=3, writer="handoff")

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 4


def test_arm_invalid_task_or_sid_does_nothing(ar, project):
    ar._cmd_arm(_Args(project_root=str(project), task="../evil", session_id="sid-1", entry="fresh"))
    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="not/valid", entry="fresh"))
    memory = project / ".workflow_artifacts" / "memory"
    assert list(memory.glob("run-continue-arm-*.txt")) == []


# ---------------------------------------------------------------------------
# owner_state.
# ---------------------------------------------------------------------------


def test_owner_state_ended_marker_is_gone(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    (memory / "session-ended-sid-9.txt").touch()
    assert ar.owner_state("sid-9", memory) == "gone"


def test_owner_state_no_transcript_is_unknown(ar, project, monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    memory = project / ".workflow_artifacts" / "memory"
    assert ar.owner_state("sid-none", memory) == "unknown"


def test_owner_state_fresh_transcript_is_live(ar, project, monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    proj_dir = fake_home / ".claude" / "projects" / "-some-project-"
    proj_dir.mkdir(parents=True)
    (proj_dir / "sid-live.jsonl").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))
    memory = project / ".workflow_artifacts" / "memory"
    assert ar.owner_state("sid-live", memory) == "live"


def test_owner_state_stale_transcript_is_gone(ar, project, monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    proj_dir = fake_home / ".claude" / "projects" / "-some-project-"
    proj_dir.mkdir(parents=True)
    transcript = proj_dir / "sid-stale.jsonl"
    transcript.write_text("{}")
    old = time.time() - 1000
    os.utime(str(transcript), (old, old))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("QUOIN_AUTO_RESUME_IDLE_SECS", "900")
    memory = project / ".workflow_artifacts" / "memory"
    assert ar.owner_state("sid-stale", memory) == "gone"


# ---------------------------------------------------------------------------
# settle_supervisor (D-19).
# ---------------------------------------------------------------------------


def test_settle_supervisor_consumes_result_file(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "run-supervisor-demo.result").write_text(json.dumps({"status": "SUCCESS", "relaunches": 3}))
    counter = ar._default_counter("demo", "ts")
    counter["attempts"] = 1
    counter = ar.settle_supervisor(memory, "demo", counter)
    assert counter["attempts"] == 4
    assert not (memory / "run-supervisor-demo.result").exists()


def test_settle_supervisor_charges_dead_lock_granted(ar, project, monkeypatch):
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: False)
    ar._write_lock(memory, "demo", pid=424242, granted=5, writer="handoff")
    counter = ar._default_counter("demo", "ts")
    counter["attempts"] = 2
    counter = ar.settle_supervisor(memory, "demo", counter)
    assert counter["attempts"] == 7
    assert not (memory / "run-supervisor-demo.pid").exists()


def test_settle_supervisor_leaves_live_lock_alone(ar, project, monkeypatch):
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: True)
    ar._write_lock(memory, "demo", pid=1, granted=5, writer="handoff")
    counter = ar._default_counter("demo", "ts")
    counter["attempts"] = 2
    counter = ar.settle_supervisor(memory, "demo", counter)
    assert counter["attempts"] == 2
    assert (memory / "run-supervisor-demo.pid").exists()


# ---------------------------------------------------------------------------
# handoff.
# ---------------------------------------------------------------------------


def test_handoff_no_cli_when_quoin_absent(ar, project, monkeypatch):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: None)
    monkeypatch.setattr(ar, "_home", lambda: project.parent / "no-such-home")
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt=None,
    ))
    assert rc == 0


def test_handoff_opt_out_denied(ar, project, monkeypatch, capsys):
    monkeypatch.setenv("QUOIN_AUTO_RESUME", "0")
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt=None,
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "DENIED|opt-out"


def test_handoff_startup_refuses_live_owner(ar, project, monkeypatch, tmp_path, capsys):
    fake_home = tmp_path / "fake-home"
    proj_dir = fake_home / ".claude" / "projects" / "-p-"
    proj_dir.mkdir(parents=True)
    (proj_dir / "sid-1.jsonl").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="startup", on_fail_halt=None,
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "OWNER_LIVE|sid-1"


# ---------------------------------------------------------------------------
# pause.
# ---------------------------------------------------------------------------


def test_pause_writes_halt_and_removes_arm(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    rc = ar._cmd_pause(_Args(project_root=str(project), task="demo", session_id="sid-1"))
    assert rc == 0
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: paused by user" in halt
    assert not (memory / "run-continue-arm-sid-1.txt").exists()


# ---------------------------------------------------------------------------
# status.
# ---------------------------------------------------------------------------


def test_status_reports_counter_and_owner_state(ar, project, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    rc = ar._cmd_status(_Args(project_root=str(project), task="demo"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task"] == "demo"
    assert out["max_attempts"] == 10
    assert out["owner_state"] == "unknown"


# ---------------------------------------------------------------------------
# Parity with quoin.supervisor's completion glob (D-07).
# ---------------------------------------------------------------------------


def test_completion_glob_matches_supervisor(ar):
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from quoin.supervisor import COMPLETION_GLOB_TEMPLATE
    finally:
        sys.path.pop(0)
    assert ar.COMPLETION_GLOB_TEMPLATE == COMPLETION_GLOB_TEMPLATE


# ---------------------------------------------------------------------------
# Token-clean guard (I-02): no line in this module pairs "autonomous" with
# "hooks/".
# ---------------------------------------------------------------------------


def test_module_has_no_autonomous_hooks_pairing():
    text = _CORE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "autonomous" in line and "hooks/" in line:
            pytest.fail(f"line pairs 'autonomous' with 'hooks/': {line!r}")


def test_main_always_exits_zero_on_garbage_argv(ar):
    assert ar.main(["--not-a-real-flag"]) == 0
    assert ar.main([]) == 0
    assert ar.main(["bogus-command"]) == 0
