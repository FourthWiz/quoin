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
from datetime import datetime, timedelta, timezone
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
def ar(monkeypatch, tmp_path):
    module = _load_module()
    # Hermeticity (R-05): no test should read a real install record or a
    # real HOME, and none should spawn a real process via _popen.
    monkeypatch.setattr(module, "_runtime_record_path", lambda: tmp_path / "absent-quoin-runtime.json")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    popen_calls = []

    def _guard_popen(argv, **kw):
        popen_calls.append(argv)
        raise AssertionError(f"unexpected real _popen call: {argv!r}")

    monkeypatch.setattr(module, "_popen", _guard_popen)
    yield module
    assert popen_calls == [], f"_popen was called without a fake override: {popen_calls!r}"


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    memory = root / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True)
    return root


# Fixture timestamps are derived from one clock reading taken at import so
# armed records never age past the run-state staleness window, and equal
# offsets stay equal within a test run.
_NOW = datetime.now(timezone.utc)


def _recent_iso(offset_seconds: int = 0) -> str:
    """ISO timestamp `offset_seconds` before the module's clock reading."""
    return (_NOW - timedelta(seconds=offset_seconds)).isoformat()


def _write_marker(memory_dir: Path, task: str, timestamp: str | None = None) -> None:
    if timestamp is None:
        timestamp = _recent_iso()
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
        "updated_at": overrides.pop("updated_at", _recent_iso()),
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
    counter = ar._default_counter("demo", _recent_iso())
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
    counter = ar._default_counter("demo", _recent_iso())
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


def test_stop_no_forward_progress_halts_even_when_cli_is_genuinely_missing(ar, project, monkeypatch, capsys):
    """The no-progress check runs, and can halt, before `_do_handoff` ever
    resolves a CLI — the old code additionally gated it behind a
    `_which('quoin')` pre-check that no longer exists now that CLI
    resolution goes through the shared install-record resolver. This pins
    that a genuinely missing CLI can't mask or replace the no-progress
    halt: it still wins, matching the code's own predicate order."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: None)
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


def test_stop_counter_survives_marker_rewrite_across_reentries(ar, project, monkeypatch, capsys):
    """CRIT reproduction: run SKILL.md rewrites the marker's timestamp on
    every autonomous entry, including every supervisor child's own
    `/run --resume --autonomous`. That rewrite alone must never reset the
    budget — only `arm`'s consumed consent stamp may (see the arm tests
    above). Three re-entries with the marker rewritten each time must
    accumulate `attempts` to 3, not reset it to 1 each time."""
    memory = project / ".workflow_artifacts" / "memory"
    _arm(memory, "sid-1")
    for i in range(3):
        _write_marker(memory, "demo", timestamp=_recent_iso((3 - i) * 3600))
        _write_record(memory, "demo", "sid-1", phase_index=i)
        _stop_stdin(monkeypatch, {"session_id": "sid-1"})
        rc = ar._cmd_stop(_Args(project_root=str(project)))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block"

    counter = json.loads((memory / "auto-resume-demo.json").read_text())
    assert counter["attempts"] == 3
    assert counter["consecutive_no_progress"] == 0


def test_no_progress_streak_increments_and_eventually_halts(ar, project, monkeypatch, capsys):
    """CRIT reproduction: a continuation that makes no progress must
    increment `consecutive_no_progress`; before the fix it never moved off
    0, so the two-in-a-row halt could never fire (reproduced upstream with
    10+ non-progressing continuations and no halt)."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase_index=3)
    _arm(memory, "sid-1")

    # Continuation 1: first-ever evaluation always counts as progressed
    # (last_phase starts at None), so it must not touch the streak.
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    assert ar._cmd_stop(_Args(project_root=str(project))) == 0
    out1 = json.loads(capsys.readouterr().out)
    assert out1["decision"] == "block"
    counter = json.loads((memory / "auto-resume-demo.json").read_text())
    assert counter["consecutive_no_progress"] == 0

    # Continuation 2: same phase, no new .done files -> no progress. Must
    # increment the streak.
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    assert ar._cmd_stop(_Args(project_root=str(project))) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["decision"] == "block"
    counter = json.loads((memory / "auto-resume-demo.json").read_text())
    assert counter["consecutive_no_progress"] == 1

    # Continuation 3: still no progress -> the streak must now halt.
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    assert ar._cmd_stop(_Args(project_root=str(project))) == 0
    assert capsys.readouterr().out == ""
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: no forward progress" in halt


def test_stop_cap_handoff_denied_falls_through_to_in_session_block(ar, project, monkeypatch, capsys):
    """MAJ: at attempts == MAX-1 with chain >= HANDOFF_AT, a hand-off is one
    unit too expensive (needs attempts+2<=MAX) but the cheaper in-session
    block (attempts+1<=MAX) still fits — the Stop path must fall through to
    it, with no halt and no spawn, rather than ending the turn one unit
    early."""
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase_index=9)
    _arm(memory, "sid-1")
    monkeypatch.setenv("QUOIN_AUTO_RESUME_MAX", "10")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 9  # MAX - 1
    counter["chain_blocks"] = ar._handoff_at() - 1
    counter["last_phase"] = ["implement", 3]
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "stop_hook_active": True})

    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert not (memory / "autonomous-halt-demo.md").exists()
    written = ar._load_counter(memory, "demo")
    assert written["attempts"] == 10


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
# arm — D-04 consent-stamp reset rules (never on marker rewrite / in_flight).
# ---------------------------------------------------------------------------


def test_arm_without_consent_keeps_counter_despite_marker_rewrite(ar, project):
    """CRIT reproduction: `/run` rewrites the marker's timestamp on every
    autonomous entry, including a resumed `/run --resume --autonomous`. A
    bare re-arm with no consent stamp on file must never reset the budget
    on that rewrite alone."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo", timestamp=_recent_iso(-3600))
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 5
    ar._write_counter(memory, "demo", counter)

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 5
    assert reloaded["in_flight"] is False
    assert (memory / "run-continue-arm-sid-1.txt").exists()


def test_arm_with_consent_and_no_live_lock_resets_counter(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 5
    counter["consecutive_no_progress"] = 1
    counter["chain_blocks"] = 3
    ar._write_counter(memory, "demo", counter)
    (memory / "run-continue-consent-sid-1.txt").touch()

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 0
    assert reloaded["consecutive_no_progress"] == 0
    assert reloaded["chain_blocks"] == 0
    # CRIT reproduction (review round 2): a consent-honored reset used to
    # write `last_done_count: None`, which crashes every later gate/
    # hand-off comparison (`done_now > None`) with a TypeError. It must
    # always land back on a valid int, matching `_default_counter`.
    assert reloaded["last_done_count"] == 0
    # the stamp is consumed exactly once
    assert not (memory / "run-continue-consent-sid-1.txt").exists()


def test_arm_with_consent_then_stop_survives_the_reset_through_main(ar, project, monkeypatch, capsys):
    """CRIT reproduction (review round 2), exercised through `main()` the
    same way the real CLI is invoked: a consent stamp, `arm`, then `stop`
    must still evaluate the gate and block. Before the fix, the arm's
    reset wrote `last_done_count: None` and the following `stop` crashed
    inside `_evaluate_gate` (`done_now > None`), which `main()`'s fail-open
    wrapper swallowed — empty stdout, exit 0, auto-resume silently dead."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 5
    counter["last_done_count"] = 3
    ar._write_counter(memory, "demo", counter)
    (memory / "run-continue-consent-sid-1.txt").touch()

    rc = ar.main([
        "arm", "--project-root", str(project), "--task", "demo",
        "--session-id", "sid-1", "--entry", "resume",
    ])
    assert rc == 0
    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 0
    assert reloaded["last_done_count"] == 0

    _stop_stdin(monkeypatch, {"session_id": "sid-1"})
    rc = ar.main(["stop", "--project-root", str(project)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert out_text != "", "stop produced no output — the gate crashed and was swallowed"
    out = json.loads(out_text)
    assert out["decision"] == "block"

    reloaded2 = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded2["attempts"] == 1
    assert not (memory / "auto-resume-errors.log").exists()


def test_unexpected_gate_error_is_logged_to_observable_file(ar, project, monkeypatch, capsys):
    """The fail-open wrapper in `main()` prints to stderr, but both hook
    call sites (`stop.sh`, `sessionstart.sh`) invoke this helper with
    `2>/dev/null` — so that print alone is invisible in real use. A
    swallowed exception must also land in a durable file under the memory
    dir so a silent fail-open death can be found after the fact."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _arm(memory, "sid-1")
    _stop_stdin(monkeypatch, {"session_id": "sid-1"})

    def _boom(*a, **kw):
        raise TypeError("'>' not supported between instances of 'int' and 'NoneType'")

    monkeypatch.setattr(ar, "_evaluate_gate", _boom)

    rc = ar.main(["stop", "--project-root", str(project)])
    assert rc == 0
    assert capsys.readouterr().out == ""

    log_path = memory / "auto-resume-errors.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "command=stop" in content
    assert "TypeError" in content


def test_arm_with_consent_but_live_lock_keeps_counter_and_consumes_consent(ar, project, monkeypatch):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 4
    ar._write_counter(memory, "demo", counter)
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: True)
    ar._write_lock(memory, "demo", pid=999, granted=3, writer="handoff")
    (memory / "run-continue-consent-sid-1.txt").touch()

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 4
    assert not (memory / "run-continue-consent-sid-1.txt").exists()


def test_arm_stale_consent_older_than_prior_arm_is_ignored(ar, project):
    """A consent stamp left over from an earlier span (before this
    session's previous arm) must not reset a budget it was never meant
    for."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 5
    ar._write_counter(memory, "demo", counter)

    arm_path = memory / "run-continue-arm-sid-1.txt"
    arm_path.write_text(f"task: demo\narmed_at: {ar._iso_now()}\n")
    consent_path = memory / "run-continue-consent-sid-1.txt"
    consent_path.touch()
    old_time = time.time() - 3600
    os.utime(consent_path, (old_time, old_time))

    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))

    reloaded = json.loads((memory / "auto-resume-demo.json").read_text())
    assert reloaded["attempts"] == 5
    assert not consent_path.exists()


def test_arm_without_marker_is_noop(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="fresh"))
    assert not (memory / "run-continue-arm-sid-1.txt").exists()
    assert not (memory / "auto-resume-demo.json").exists()


def test_arm_invalid_task_or_sid_does_nothing(ar, project):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    ar._cmd_arm(_Args(project_root=str(project), task="../evil", session_id="sid-1", entry="fresh"))
    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="not/valid", entry="fresh"))
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


def _mock_successful_spawn(ar, monkeypatch, pid=4242):
    """Route `_which`/`_popen` to a fake CLI + fake subprocess so a handoff
    can run to completion without spawning anything real. Returns the list
    the fake `_popen` call's argv gets appended to."""
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")
    captured_argv = []

    class _FakeProc:
        def __init__(self):
            self.pid = pid

    def _fake_popen(argv, **kw):
        captured_argv.append(argv)
        return _FakeProc()

    monkeypatch.setattr(ar, "_popen", _fake_popen)
    return captured_argv


def test_handoff_grant_is_cap_minus_attempts_minus_one(ar, project, monkeypatch, capsys):
    """D-01: the hand-off's own charge plus its grant must together stay
    within the cap — grant = cap - attempts_before - 1, not cap - attempts_before
    (the round-2 off-by-one this pins is the 'edge' row of the worked table)."""
    captured_argv = _mock_successful_spawn(ar, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setenv("QUOIN_AUTO_RESUME_MAX", "10")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 8
    ar._write_counter(memory, "demo", counter)

    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt=None,
    ))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("HANDOFF|4242|9/10"), out
    written = ar._load_counter(memory, "demo")
    assert written["attempts"] == 9
    argv = captured_argv[0]
    assert argv[argv.index("--max-relaunch") + 1] == "1", argv


def test_handoff_refused_at_cap_minus_one_remaining(ar, project, monkeypatch, capsys):
    """D-01 'refused' row: attempts_before=9 leaves only 1 unit of budget —
    not enough for the hand-off's own charge plus a launch grant — so it must
    refuse (halt `auto-resume cap`) rather than spawn a supervisor that could
    overshoot the cap."""
    _mock_successful_spawn(ar, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setenv("QUOIN_AUTO_RESUME_MAX", "10")
    counter = ar._default_counter("demo", _recent_iso())
    counter["attempts"] = 9
    ar._write_counter(memory, "demo", counter)

    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt=None,
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "DENIED|cap"
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: auto-resume cap" in halt


def test_handoff_lock_create_race_refuses_instead_of_overwriting(ar, project, monkeypatch):
    """MAJ reproduction: two concurrent hand-offs can both pass a stale
    liveness read before either creates the lock. The O_CREAT|O_EXCL
    create must let only one winner through — the loser must refuse
    (`LOCKED|`) rather than overwrite the winner's lock with its own pid."""
    _mock_successful_spawn(ar, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    record = {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
              "resume_command": "/run --resume demo"}
    counter = ar._default_counter("demo", _recent_iso())

    winner_pid = os.getpid()  # our own pid is always alive
    winner_lock = {"pid": winner_pid, "started_at": "x", "granted": 3, "writer": "handoff", "token": "winner-token"}
    (memory / "run-supervisor-demo.pid").write_text(json.dumps(winner_lock))

    real_lock_live = ar._supervisor_lock_live
    calls = {"n": 0}

    def fake_lock_live(mem, task):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # simulate the stale read before the winner's create
        return real_lock_live(mem, task)

    monkeypatch.setattr(ar, "_supervisor_lock_live", fake_lock_live)

    result = ar._do_handoff(memory, project, "demo", "budget", counter, record)

    assert result == f"LOCKED|{winner_pid}"
    assert json.loads((memory / "run-supervisor-demo.pid").read_text()) == winner_lock


# ---------------------------------------------------------------------------
# MAJ round-2 reproduction: lock creation atomicity + conditional stale
# removal (review-2 issue 2).
# ---------------------------------------------------------------------------


def test_create_lock_exclusive_never_publishes_a_half_written_file(ar, project):
    """The atomic creator must never let a reader observe `lock_path`
    existing but empty — the old O_CREAT|O_EXCL-then-`os.write` split left
    exactly that window. Once it returns True, the file is already fully
    populated, and it leaves no temp file behind."""
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    lock_path = memory / "run-supervisor-demo.pid"
    payload = json.dumps({"pid": 123, "granted": 3, "writer": "handoff"}).encode("utf-8") + b"\n"

    assert ar._create_lock_exclusive(lock_path, payload) is True
    assert lock_path.read_bytes() == payload
    leftovers = [p for p in memory.iterdir() if p.name != lock_path.name]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"

    # A second creator loses the race against the now-fully-populated file.
    assert ar._create_lock_exclusive(lock_path, b'{"pid": 456}\n') is False
    assert lock_path.read_bytes() == payload


def test_do_handoff_refuses_fresh_empty_lock_instead_of_clobbering(ar, project, monkeypatch):
    """Empty-lock-window reproduction: a lock file that exists but fails to
    parse (as an in-flight winner's create would look for a moment under
    the old split-write) must never be treated as stale on emptiness alone
    while it's still fresh — a racing loser must refuse, not clobber a
    winner's lock that simply hasn't been observed as valid yet."""
    _mock_successful_spawn(ar, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    record = {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
              "resume_command": "/run --resume demo"}
    counter = ar._default_counter("demo", _recent_iso())
    monkeypatch.setattr(ar, "_supervisor_lock_live", lambda mem, task: False)
    lock_path = memory / "run-supervisor-demo.pid"
    lock_path.write_text("")  # empty — unparseable, and freshly written

    result = ar._do_handoff(memory, project, "demo", "budget", counter, record)

    assert result == "LOCKED|"
    assert lock_path.read_text() == "", "a fresh empty lock must not be clobbered"


def test_do_handoff_reclaims_old_empty_lock_and_spawns(ar, project, monkeypatch):
    """An empty/unparseable lock old enough that it cannot still be
    mid-write (well past the atomic creator's own latency) is genuinely
    abandoned — e.g. a crash before this fix shipped — and must be
    reclaimable so `quoin run --autonomous` doesn't refuse forever."""
    _mock_successful_spawn(ar, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    record = {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
              "resume_command": "/run --resume demo"}
    counter = ar._default_counter("demo", _recent_iso())
    monkeypatch.setattr(ar, "_supervisor_lock_live", lambda mem, task: False)
    lock_path = memory / "run-supervisor-demo.pid"
    lock_path.write_text("")
    old_time = time.time() - (ar._STALE_UNPARSEABLE_LOCK_SECS + 5)
    os.utime(lock_path, (old_time, old_time))

    result = ar._do_handoff(memory, project, "demo", "budget", counter, record)

    assert result.startswith("HANDOFF|")
    written = json.loads(lock_path.read_text())
    assert written["pid"] == 4242


def test_claim_lock_for_removal_lets_only_one_racer_win(ar, project):
    """Concurrent double-spawn reproduction, at the primitive level:
    `settle_supervisor`'s old read-then-unlink let two racers both read the
    same dead-pid lock before either removed it, so both could charge its
    grant (and, via the retry path this backs, both could go on to spawn a
    supervisor). `_claim_lock_for_removal`'s atomic rename means only one
    of two racing claims against the same lock can ever return content —
    the second always sees the (already-renamed-away) source gone."""
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    lock_path = memory / "run-supervisor-demo.pid"
    lock_path.write_text(json.dumps({"pid": 999, "granted": 3, "writer": "handoff"}))

    first = ar._claim_lock_for_removal(lock_path)
    second = ar._claim_lock_for_removal(lock_path)

    assert first is not None and first["granted"] == 3
    assert second is None
    assert not lock_path.exists()


def test_settle_supervisor_charges_a_dead_lock_exactly_once(ar, project, monkeypatch):
    """Invariant (not itself the race, which is covered at the primitive
    level above): calling `settle_supervisor` again after it has already
    consumed a dead-pid lock must never re-charge — the lock is gone, so
    there is nothing left to read."""
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: False)
    ar._write_lock(memory, "demo", pid=999, granted=3, writer="handoff")

    counter_a = ar._default_counter("demo", _recent_iso())
    counter_a = ar.settle_supervisor(memory, "demo", counter_a)
    assert counter_a["attempts"] == 3
    assert not (memory / "run-supervisor-demo.pid").exists()

    counter_b = ar._default_counter("demo", _recent_iso())
    counter_b = ar.settle_supervisor(memory, "demo", counter_b)
    assert counter_b["attempts"] == 0


def test_do_handoff_live_lock_is_never_reclaimed(ar, project, monkeypatch):
    """Stale-vs-live reproduction: a lock naming a pid that is still alive
    must never be reclaimed, regardless of the create race."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    record = {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
              "resume_command": "/run --resume demo"}
    counter = ar._default_counter("demo", _recent_iso())
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ar, "_supervisor_lock_live", lambda mem, task: False)  # force past the early check
    live_lock = {"pid": 999, "started_at": "x", "granted": 3, "writer": "handoff"}
    lock_path = memory / "run-supervisor-demo.pid"
    lock_path.write_text(json.dumps(live_lock))

    result = ar._do_handoff(memory, project, "demo", "budget", counter, record)

    assert result == "LOCKED|999"
    assert json.loads(lock_path.read_text()) == live_lock


def test_handoff_releases_reservation_on_non_oserror_spawn_failure(ar, project, monkeypatch):
    """MIN reproduction: a spawn failure that isn't an `OSError` (e.g. a
    malformed argv raising `ValueError`, or `subprocess.SubprocessError`)
    used to skip the reservation cleanup entirely, leaving the lock behind
    under this process's own (still-alive) pid until a later settle
    mistakenly charged it as a crashed supervisor's grant."""
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")

    def _boom(argv, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(ar, "_popen", _boom)
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    record = {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
              "resume_command": "/run --resume demo"}
    counter = ar._default_counter("demo", _recent_iso())

    result = ar._do_handoff(memory, project, "demo", "budget", counter, record)

    assert result == "DENIED|spawn"
    assert not (memory / "run-supervisor-demo.pid").exists()


def test_handoff_on_fail_halt_skips_halt_when_locked(ar, project, monkeypatch, capsys):
    """MAJ: `--on-fail-halt` must never halt a supervised child just
    because a live supervisor already holds the lock — that is the normal
    case of a run in progress, not a failure to hand off."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setattr(ar, "_pid_alive", lambda pid: True)
    ar._write_lock(memory, "demo", pid=999, granted=3, writer="cli")

    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="context", on_fail_halt="context exhaustion",
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "LOCKED|999"
    assert not (memory / "autonomous-halt-demo.md").exists()


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


def test_main_resolves_relative_project_root_before_dispatch(ar, tmp_path, monkeypatch):
    """A relative --project-root must resolve against the caller's actual
    cwd before any handler runs — not against whatever cwd a later
    probe/spawn switches into (issue 4/round 2)."""
    captured = {}

    def _capture(args):
        captured["project_root"] = args.project_root
        return 0

    monkeypatch.setattr(ar, "_HANDLERS", {**ar._HANDLERS, "status": _capture})
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)

    rc = ar.main(["status", "--project-root", "sub", "--task", "demo"])

    assert rc == 0
    assert captured["project_root"] == str((tmp_path / "sub").resolve())


def test_main_resets_cli_memo_before_dispatch(ar, tmp_path, monkeypatch):
    """The resolver memo is process-lifetime, not call-lifetime — a stale
    entry from an earlier `main()` invocation in the same process must
    never leak into a later, unrelated one."""
    ar._CLI_MEMO[("stale", "start")] = {"status": "usable"}
    monkeypatch.setattr(ar, "_HANDLERS", {**ar._HANDLERS, "status": lambda args: 0})

    rc = ar.main(["status", "--project-root", str(tmp_path), "--task", "demo"])

    assert rc == 0
    assert ar._CLI_MEMO == {}
