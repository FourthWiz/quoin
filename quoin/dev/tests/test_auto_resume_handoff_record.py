"""Tests for the call-site integration between the shared interpreter
resolver (`resolve_cli`) and the hand-off callers — `handoff`, `start`, and
`stop`. Uses the same fake `.workflow_artifacts/memory` tree and
fixture style as `test_auto_resume_core.py`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


def _fresh_updated_at() -> str:
    """A run-state record is only considered live for a limited window, so
    a fixed timestamp would silently expire and turn these tests red."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("auto_resume_handoff_record_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    module = _load_module()
    monkeypatch.setattr(module, "_runtime_record_path", lambda: tmp_path / "absent-quoin-runtime.json")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    popen_calls = []

    def _guard_popen(argv, **kw):
        popen_calls.append(argv)
        raise AssertionError(f"unexpected real _popen call: {argv!r}")

    monkeypatch.setattr(module, "_popen", _guard_popen)
    yield module
    assert popen_calls == []


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    memory = root / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True)
    return root


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _write_marker(memory_dir: Path, task: str, timestamp: str = "2026-09-29T00:00:00+00:00") -> None:
    (memory_dir / f"autonomous-run-{task}.marker").write_text(
        f"task: {task}\ntimestamp: {timestamp}\nautonomous: true\n", encoding="utf-8"
    )


def _write_record(memory_dir: Path, task: str, session_id: str, **overrides) -> None:
    record = {
        "schema": 1, "task": task, "session_id": session_id, "active": True,
        "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
        "at_stage_boundary": False, "route": "", "profile": "", "artifacts": [],
        "next_action": "", "resume_command": f"/run --resume {task}",
        "notes_path": str(memory_dir / f"run-notes-{task}.md"),
        "updated_at": overrides.pop("updated_at", _fresh_updated_at()),
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


def _write_runtime_record(path: Path, python: str, **overrides) -> None:
    record = {
        "schema": 1, "python": python, "version": "1.0.0", "pythonpath": None,
        "quoin_file": "/fake/quoin/__init__.py", "source_dir": "/fake/source",
        "source_version": None, "installed_at": "2026-09-29T00:00:00Z",
    }
    record.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _fake_interpreter(dir_path: Path, mode="fail") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "python"
    path.write_text(
        "#!/bin/sh\n"
        f"echo 'ImportError: no module named quoin' 1>&2\nexit 1\n"
        if mode == "fail" else "#!/bin/sh\necho QUOIN_VERSION=9.9.9\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _stale_record(ar, memory_dir: Path) -> None:
    """A record with a missing interpreter: cheap, deterministic 'stale'."""
    _write_runtime_record(
        ar._runtime_record_path(), python=str(memory_dir / "does-not-exist" / "python"),
    )


# ── handoff subcommand ───────────────────────────────────────────────────────


def test_handoff_stale_record_writes_halt_with_remedy(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _stale_record(ar, memory)
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt="context exhaustion",
    ))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("STALE_CLI|interpreter-missing|")
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "context exhaustion: " in halt
    assert "quoin install" in halt
    assert not (memory / "run-supervisor-demo.pid").exists()
    notes = (memory / "run-notes-demo.md").read_text()
    assert "cli=stale kind=interpreter-missing" in notes


def test_handoff_no_record_no_cli_halt_reason_unchanged(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: None)
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt="context exhaustion",
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "NO_CLI|"
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: context exhaustion" in halt
    assert "context exhaustion:" not in halt  # unchanged, no appended message


def test_handoff_usable_argv_and_env(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    interp = _fake_interpreter(project / "fakebin", mode="ok")
    _write_runtime_record(
        ar._runtime_record_path(), python=str(interp), pythonpath=str(project / "src"), version="9.9.9",
    )

    captured = []

    class _FakeProc:
        pid = 4321

    def _fake_popen(argv, **kw):
        captured.append((argv, kw.get("env")))
        return _FakeProc()

    monkeypatch.setattr(ar, "_popen", _fake_popen)
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("HANDOFF|4321|")
    argv, env = captured[0]
    assert argv[:3] == [str(interp), "-c", ar._SPAWN_BOOTSTRAP]
    assert argv[3:6] == ["run", "--autonomous", "demo"]
    assert "QUOIN_SUPERVISOR_LOCK_TOKEN" in env
    assert env["QUOIN_HANDOFF_PYTHONPATH"] == str(project / "src")
    assert env["PYTHONPATH"].startswith(str(project / "src"))


def test_handoff_spawn_cwd_is_not_project_root(ar, project, monkeypatch, capsys):
    """The detached supervisor spawn (`python -m quoin run ...`) must not
    run with the project root as its cwd — `-m` puts the working
    directory first on sys.path, so a hostile `quoin.py` or `quoin/`
    package sitting at the project root would otherwise be imported and
    run from this hand-off. `--project-root` is still passed explicitly
    in argv, so the relaunched run still learns the real path."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    interp = _fake_interpreter(project / "fakebin", mode="ok")
    _write_runtime_record(ar._runtime_record_path(), python=str(interp), version="9.9.9")

    captured = []

    class _FakeProc:
        pid = 4321

    def _fake_popen(argv, **kw):
        captured.append((argv, kw.get("cwd")))
        return _FakeProc()

    monkeypatch.setattr(ar, "_popen", _fake_popen)
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    assert capsys.readouterr().out.strip().startswith("HANDOFF|")
    argv, cwd = captured[0]
    assert cwd != str(project)
    assert "--project-root" in argv
    assert argv[argv.index("--project-root") + 1] == str(project.resolve())


def test_handoff_spawn_cwd_is_not_writable_by_other_users(
    ar, project, monkeypatch, capsys,
):
    """The directory the detached supervisor actually spawns into must be
    the verified directory _neutral_cwd() picks: owned by root or by the
    current user, never writable by group or other, never repository
    content."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    interp = _fake_interpreter(project / "fakebin", mode="ok")
    _write_runtime_record(ar._runtime_record_path(), python=str(interp), version="9.9.9")

    captured = []

    class _FakeProc:
        pid = 4321

    def _fake_popen(argv, **kw):
        captured.append(kw.get("cwd"))
        return _FakeProc()

    monkeypatch.setattr(ar, "_popen", _fake_popen)
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    assert capsys.readouterr().out.strip().startswith("HANDOFF|")
    cwd = captured[0]
    assert cwd == os.path.abspath(os.sep)
    assert not cwd.startswith(str(project))
    st = os.lstat(cwd)
    assert not stat.S_ISLNK(st.st_mode)
    assert st.st_uid in (0, os.getuid())
    assert not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_handoff_denied_when_no_safe_cwd_available(ar, project, monkeypatch, capsys):
    """A hand-off must be refused outright — never silently spawned from a
    shared or unverified directory — when _neutral_cwd() can't find a safe
    one to use for the spawn itself. `resolve_cli`'s own probe-time check
    already covers the common case (a bad deploy root fails the probe
    first and comes back `STALE_CLI|no-safe-cwd`); this pins the
    independent, second checkpoint right before the spawn, using a memoized
    `resolve_cli` result so that check is the only one exercised."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    interp = _fake_interpreter(project / "fakebin", mode="ok")
    _write_runtime_record(ar._runtime_record_path(), python=str(interp), version="9.9.9")

    real_neutral_cwd = ar._neutral_cwd()
    assert real_neutral_cwd is not None
    calls = {"n": 0}

    def _flaky_neutral_cwd():
        calls["n"] += 1
        return real_neutral_cwd if calls["n"] == 1 else None

    monkeypatch.setattr(ar, "_neutral_cwd", _flaky_neutral_cwd)

    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "DENIED|cwd"
    assert not (memory / "run-supervisor-demo.pid").exists()


def test_handoff_cwd_decision_failure_leaves_no_lock_or_counter_change(ar, project, monkeypatch, capsys):
    """The cwd decision now runs before the lock is reserved, so a failure
    there (any exception, not just a None result) must never strand a
    reservation under this process's pid or advance the attempt counter."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    interp = _fake_interpreter(project / "fakebin", mode="ok")
    _write_runtime_record(ar._runtime_record_path(), python=str(interp), version="9.9.9")
    counter_path = memory / "auto-resume-demo.json"
    assert not counter_path.exists()

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(ar, "_neutral_cwd", _boom)

    rc = ar.main([
        "handoff", "--project-root", str(project), "--task", "demo", "--reason", "budget",
    ])

    assert rc == 0  # main()'s fail-open contract swallows the exception
    assert not (memory / "run-supervisor-demo.pid").exists()
    assert not counter_path.exists()


def test_handoff_legacy_launch_is_never_denied_for_the_cwd(ar, project, monkeypatch, capsys):
    """A no-record install launches the console script, which never puts
    its cwd on sys.path, so it needs no safe cwd: even when none can be
    found it must hand off exactly as it did before records existed."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")
    monkeypatch.setattr(ar, "_neutral_cwd", lambda: None)

    captured = []

    class _FakeProc:
        pid = 778

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: captured.append(kw.get("cwd")) or _FakeProc())
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    assert capsys.readouterr().out.strip().startswith("HANDOFF|")
    assert captured[0] == str(project.resolve())


def test_handoff_budget_reason_no_halt_writes_notes_only(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _stale_record(ar, memory)
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    assert not (memory / "autonomous-halt-demo.md").exists()
    assert "cli=stale" in (memory / "run-notes-demo.md").read_text()


# ── start (SessionStart) ─────────────────────────────────────────────────────


def test_start_owner_gone_stale_record_advisory_no_halt(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-owner-gone")
    (memory / "session-ended-sid-owner-gone.txt").touch()
    _stale_record(ar, memory)
    rc = ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="sid-new"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "auto-resume hand-off skipped" in ctx
    assert "resume manually" in ctx
    assert "via=supervisor" not in ctx
    assert not (memory / "autonomous-halt-demo.md").exists()


# ── stop ──────────────────────────────────────────────────────────────────


def test_stop_at_handoff_at_stale_record_block_unchanged_reason(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase_index=9)
    _arm(memory, "sid-1")
    _stale_record(ar, memory)
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["chain_blocks"] = ar._handoff_at() - 1
    counter["last_phase"] = ["implement", 3]
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "stop_hook_active": True})

    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "resume the run" in out["reason"] or "/run --resume" in out["reason"]
    # _stale_record's interpreter path is under memory_dir, whose own path
    # contains this test's name (pytest tmp_path naming) — asserting a
    # literal "stale" substring would pass on that coincidence alone rather
    # than on the actual interpreter-missing message text.
    assert "no longer exists" in out["systemMessage"]
    assert not (memory / "autonomous-halt-demo.md").exists()
    written = ar._load_counter(memory, "demo")
    assert written["attempts"] == 1


# ── agreement: Stop hands off iff `handoff` would ───────────────────────────


@pytest.mark.parametrize("stale", [True, False])
def test_stop_and_handoff_resolver_verdict_agrees(ar, project, monkeypatch, stale):
    """Both callers share one resolver (`resolve_cli`) — this pins that a
    given record state resolves to the same usable/stale verdict for both,
    with `_which` pinned to None so the record-less cases share the same
    `~/.local/bin` fallback too."""
    memory = project / ".workflow_artifacts" / "memory"
    monkeypatch.setattr(ar, "_which", lambda name: None)
    if stale:
        _stale_record(ar, memory)
    else:
        interp = _fake_interpreter(project / "fakebin", mode="ok")
        _write_runtime_record(ar._runtime_record_path(), python=str(interp), version="9.9.9")

    stop_verdict = ar.resolve_cli(project, "stop")
    ar._reset_cli_memo()
    handoff_verdict = ar.resolve_cli(project, "handoff")
    assert stop_verdict["status"] == handoff_verdict["status"]
    assert stop_verdict["kind"] == handoff_verdict["kind"]
    assert stop_verdict["source"] == handoff_verdict["source"]
    if not stale:
        assert stop_verdict["status"] == "usable"


# ── no-progress guard ────────────────────────────────────────────────────────


def test_do_handoff_no_progress_first_miss_does_not_deny(ar, project, monkeypatch):
    """`_evaluate_gate` only ever hands `_do_handoff` a candidate when
    either progress was made or this is the FIRST consecutive miss — a
    second miss halts at the gate itself, before `_do_handoff` runs at all.
    `_do_handoff` re-derives `progressed` from the same counter and record,
    so on a first-miss candidate its own no-progress check must agree and
    let the call through to CLI resolution instead of denying it a second
    time. Record absent + `_which` None (record-less path) isolates
    this from any interpreter-resolution branch."""
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase="implement", phase_index=3)
    monkeypatch.setattr(ar, "_which", lambda name: None)
    record = ar._load_json(memory / "run-state-demo.json")
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["last_phase"] = ["implement", 3]  # matches record -> not progressed
    counter["last_done_count"] = 0
    counter["consecutive_no_progress"] = 0  # first miss, not yet at the gate's halt threshold

    result = ar._do_handoff(
        memory, project, "demo", "stop-cap", counter, record, halt_on_cap=False, probe_caller="stop",
    )
    assert not result.startswith("DENIED|no-progress")
    assert not (memory / "autonomous-halt-demo.md").exists()


# ── resolver-error on Stop and handoff ────────────────────────────────────────


def test_stop_resolver_error_prints_block_no_halt(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase_index=9)
    _arm(memory, "sid-1")
    _write_runtime_record(ar._runtime_record_path(), python=sys.executable)
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["chain_blocks"] = ar._handoff_at() - 1
    counter["last_phase"] = ["implement", 3]
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "stop_hook_active": True})

    rc = ar.main(["stop", "--project-root", str(project)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "could not check the installed quoin" in out["systemMessage"]
    assert not (memory / "autonomous-halt-demo.md").exists()
    written = ar._load_counter(memory, "demo")
    assert written["attempts"] == 1


def test_handoff_resolver_error_halts_with_prefix(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    _write_runtime_record(ar._runtime_record_path(), python=sys.executable)
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    rc = ar.main([
        "handoff", "--project-root", str(project), "--task", "demo",
        "--reason", "budget", "--on-fail-halt", "context exhaustion",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("STALE_CLI|resolver-error|")
    halt = (memory / "autonomous-halt-demo.md").read_text()
    assert "reason: context exhaustion: " in halt


def test_start_resolver_error_advisory(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-owner-gone")
    (memory / "session-ended-sid-owner-gone.txt").touch()
    _write_runtime_record(ar._runtime_record_path(), python=sys.executable)
    monkeypatch.setattr(ar, "_probe", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    rc = ar.main(["start", "--project-root", str(project), "--source", "startup", "--session-id", "sid-new"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "could not check the installed quoin" in ctx


# ── local-bin alignment ──────────────────────────────────────────────────────


def test_stop_local_bin_alignment(ar, project, monkeypatch, capsys, tmp_path):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1", phase_index=9)
    _arm(memory, "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: None)
    home_bin = Path(os.environ["HOME"]) / ".local" / "bin"
    home_bin.mkdir(parents=True, exist_ok=True)
    quoin_bin = home_bin / "quoin"
    quoin_bin.write_text("#!/bin/sh\n")
    quoin_bin.chmod(0o755)

    captured = []

    class _FakeProc:
        pid = 55

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: captured.append(argv) or _FakeProc())

    counter = ar._default_counter("demo", "2026-09-29T00:00:00+00:00")
    counter["chain_blocks"] = ar._handoff_at() - 1
    counter["last_phase"] = ["implement", 3]
    ar._write_counter(memory, "demo", counter)
    _stop_stdin(monkeypatch, {"session_id": "sid-1", "stop_hook_active": True})
    rc = ar._cmd_stop(_Args(project_root=str(project)))
    assert rc == 0
    assert captured and captured[0][0] == str(quoin_bin)


# ── legacy byte-identity ─────────────────────────────────────────────────────


def test_legacy_argv_and_env_byte_identical(ar, project, monkeypatch, capsys):
    memory = project / ".workflow_artifacts" / "memory"
    _write_marker(memory, "demo")
    _write_record(memory, "demo", "sid-1")
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")

    captured = []

    class _FakeProc:
        pid = 777

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: captured.append((argv, kw.get("env"))) or _FakeProc())
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    argv, env = captured[0]
    remaining = argv[-1]
    assert argv == [
        "/usr/bin/quoin", "run", "--autonomous", "demo",
        "--project-root", str(project),
        "--halt-on-abort", "--max-relaunch", remaining,
    ]
    assert set(env.keys()) - set(os.environ.keys()) == {"QUOIN_SUPERVISOR_LOCK_TOKEN"}
