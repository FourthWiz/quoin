"""Runtime ownership recorded in the supervisor lock, and readers of it."""
from __future__ import annotations

import json
import os

import pytest

from quoin import cli
from quoin import supervisor as sup

ISO = "2026-01-01T00:00:00Z"


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_iso_now", lambda: ISO)


def _memory(root):
    return root / ".workflow_artifacts" / "memory"


def _paths(root):
    return cli._supervisor_paths(root, "demo")


def _acquire(root, **kwargs):
    p = _paths(root)
    return cli._acquire_supervisor_lock(
        p["memory_dir"], p["lock"], p["result"], "demo", 3, None, **kwargs
    )


def _dead_pid():
    pid = 2 ** 22 + 12345
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


# --- lock payload ----------------------------------------------------------


def test_default_payload_has_no_runtime_key(project):
    assert _acquire(project) == (True, None)
    expected = (
        '{"granted": 3, "pid": %d, "started_at": "%s", "task": "demo", "writer": "cli"}\n'
        % (os.getpid(), ISO)
    )
    assert _paths(project)["lock"].read_bytes() == expected.encode()


def test_runtime_key_written_when_given(project):
    assert _acquire(project, runtime="opencode") == (True, None)
    expected = (
        '{"granted": 3, "pid": %d, "runtime": "opencode", "started_at": "%s", '
        '"task": "demo", "writer": "cli"}\n' % (os.getpid(), ISO)
    )
    assert _paths(project)["lock"].read_bytes() == expected.encode()


def test_retry_after_failed_create_keeps_runtime(monkeypatch, project):
    real = cli._create_lock_exclusive
    calls = []

    def flaky(path, payload):
        calls.append(payload)
        if len(calls) == 1:
            return False
        return real(path, payload)

    monkeypatch.setattr(cli, "_create_lock_exclusive", flaky)
    p = _paths(project)
    p["lock"].write_text(json.dumps({"pid": _dead_pid(), "writer": "cli"}) + "\n")
    assert _acquire(project, runtime="opencode") == (True, None)
    assert len(calls) == 2
    assert all(b'"runtime": "opencode"' in c for c in calls)
    assert json.loads(p["lock"].read_text())["runtime"] == "opencode"


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, "claude"),
        ({"runtime": ""}, "claude"),
        ({"runtime": 5}, "claude"),
        ({"runtime": "opencode"}, "opencode"),
        ({"runtime": "claude"}, "claude"),
        ({"runtime": "OPENCODE"}, "unknown"),
        ({"runtime": "x|y"}, "unknown"),
        (None, "claude"),
    ],
)
def test_lock_runtime(data, expected):
    assert cli._lock_runtime(data) == expected


def _patch_supervise(monkeypatch):
    calls = []
    monkeypatch.setattr(sup, "make_launch_fn", lambda *a, **k: (lambda t: sup.LaunchResult(0)))

    def fake(*a, **k):
        calls.append((a, k))
        return sup.SuperviseResult("SUCCESS")

    monkeypatch.setattr(sup, "supervise", fake)
    return calls


def test_live_opencode_lock_refuses_plain_claude_run(monkeypatch, project, capsys):
    ppid = os.getppid()
    _paths(project)["lock"].write_text(
        json.dumps({"pid": ppid, "writer": "cli", "task": "demo", "runtime": "opencode"}) + "\n"
    )
    calls = _patch_supervise(monkeypatch)
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 3
    assert capsys.readouterr().out == (
        f"quoin run: REFUSED (supervisor lock held by pid {ppid})\n  task: demo\n"
        f"  takeover: quoin run --takeover demo --project-root {project}\n"
    )
    assert calls == []


def test_dead_opencode_lock_is_reclaimed_by_claude_run(monkeypatch, project):
    _paths(project)["lock"].write_text(
        json.dumps({"pid": _dead_pid(), "writer": "cli", "task": "demo", "runtime": "opencode"}) + "\n"
    )
    calls = _patch_supervise(monkeypatch)
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 0
    assert len(calls) == 1
    assert not _paths(project)["lock"].exists()


# --- readers of the lock: auto_resume and takeover -------------------------

import importlib.util
import signal
from datetime import datetime, timezone
from pathlib import Path

from quoin import takeover

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("auto_resume_runtime_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_runtime_record_path", lambda: tmp_path / "absent-runtime.json")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    popen_calls = []

    def guard(argv, **kw):
        popen_calls.append(argv)
        raise AssertionError("no child may be spawned: %r" % (argv,))

    monkeypatch.setattr(module, "_popen", guard)
    yield module
    assert popen_calls == []


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _foreign_lock(memory, pid, runtime="opencode", **extra):
    data = {"pid": pid, "writer": "cli", "task": "demo", "granted": 3, "runtime": runtime}
    data.update(extra)
    path = memory / "run-supervisor-demo.pid"
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    return path


def _arm_task(memory, sid="sid-1"):
    (memory / "autonomous-run-demo.marker").write_text(
        "task: demo\ntimestamp: %s\nautonomous: true\n" % _now_iso()
    )
    (memory / "run-state-demo.json").write_text(json.dumps({
        "schema": 1, "task": "demo", "session_id": sid, "active": True,
        "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
        "at_stage_boundary": False, "route": "", "profile": "", "artifacts": [],
        "next_action": "", "resume_command": "/run --resume demo",
        "notes_path": str(memory / "run-notes-demo.md"), "updated_at": _now_iso(),
    }))
    (memory / ("run-continue-arm-%s.txt" % sid)).touch()


def _counter_attempts(memory):
    path = memory / "auto-resume-demo.json"
    if not path.exists():
        return 0
    return json.loads(path.read_text()).get("attempts", 0)


def _record():
    return {"session_id": "sid-1", "phase": "implement", "phase_index": 3,
            "resume_command": "/run --resume demo"}


def test_handoff_over_dead_foreign_lock_reports_runtime(ar, project):
    memory = _memory(project)
    lock = _foreign_lock(memory, _dead_pid())
    before = lock.read_bytes()
    counter = ar._default_counter("demo", _now_iso())
    result = ar._do_handoff(memory, project, "demo", "budget", counter, _record())
    assert result == "RUNTIME|opencode"
    assert lock.read_bytes() == before
    assert counter.get("attempts", 0) == 0
    assert not (memory / "run-supervisor-demo.result").exists()


def test_handoff_over_live_foreign_lock_is_still_locked(ar, project):
    memory = _memory(project)
    _foreign_lock(memory, os.getppid())
    counter = ar._default_counter("demo", _now_iso())
    result = ar._do_handoff(memory, project, "demo", "budget", counter, _record())
    assert result == "LOCKED|%d" % os.getppid()


def test_handoff_create_race_with_dead_foreign_lock(ar, project, monkeypatch):
    memory = _memory(project)
    lock = memory / "run-supervisor-demo.pid"
    dead = _dead_pid()
    monkeypatch.setattr(ar, "_which", lambda name: "/bin/quoin")
    monkeypatch.setattr(
        ar, "resolve_cli", lambda root, caller: {"status": "ok", "source": "legacy", "argv": ["quoin"]}
    )

    def racing_create(path, payload):
        _foreign_lock(memory, dead)
        return False

    monkeypatch.setattr(ar, "_create_lock_exclusive", racing_create)
    counter = ar._default_counter("demo", _now_iso())
    result = ar._do_handoff(memory, project, "demo", "budget", counter, _record())
    assert result == "RUNTIME|opencode"
    assert lock.exists()


def test_settle_keeps_dead_foreign_lock_and_charges_nothing(ar, project):
    memory = _memory(project)
    lock = _foreign_lock(memory, _dead_pid())
    counter = ar.settle_supervisor(memory, "demo", {"attempts": 1})
    assert counter["attempts"] == 1
    assert lock.exists()


@pytest.mark.parametrize("extra", [{}, {"runtime": "claude"}])
def test_settle_charges_and_removes_dead_claude_lock(ar, project, extra):
    memory = _memory(project)
    data = {"pid": _dead_pid(), "writer": "cli", "granted": 3}
    data.update(extra)
    lock = memory / "run-supervisor-demo.pid"
    lock.write_text(json.dumps(data))
    counter = ar.settle_supervisor(memory, "demo", {"attempts": 1})
    assert counter["attempts"] == 4
    assert not lock.exists()


def test_phase_result_with_zero_relaunches_charges_nothing(ar, project):
    memory = _memory(project)
    (memory / "run-supervisor-demo.result").write_text(
        json.dumps({"status": "INTERRUPTED", "relaunches": 0, "finished_at": ISO})
    )
    counter = ar.settle_supervisor(memory, "demo", {"attempts": 2})
    assert counter["attempts"] == 2


def test_handoff_subcommand_never_halts_for_foreign_lock(ar, project, capsys):
    memory = _memory(project)
    _arm_task(memory)
    _foreign_lock(memory, _dead_pid())
    rc = ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt="x",
    ))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "RUNTIME|opencode"
    assert not (memory / "autonomous-halt-demo.md").exists()


def test_stop_is_silent_for_foreign_lock(ar, project, monkeypatch, capsys):
    memory = _memory(project)
    _arm_task(memory)
    _foreign_lock(memory, _dead_pid())
    data = json.dumps({"session_id": "sid-1"}).encode()

    class _Buf:
        def read(self, n):
            return data[:n]

    class _Stdin:
        buffer = _Buf()

    monkeypatch.setattr("sys.stdin", _Stdin())
    assert ar._cmd_stop(_Args(project_root=str(project))) == 0
    assert capsys.readouterr().out == ""
    assert _counter_attempts(memory) == 0


def test_start_is_silent_for_foreign_lock(ar, project, capsys):
    memory = _memory(project)
    _arm_task(memory)
    _foreign_lock(memory, _dead_pid())
    rc = ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="sid-2"))
    assert rc == 0
    assert capsys.readouterr().out == ""


# takeover ------------------------------------------------------------------


class _Ops:
    def __init__(self, alive):
        self.alive = alive
        self.calls = []
        self.errs = []

    def build(self):
        return takeover.TakeoverOps(
            pid_alive=lambda pid: self.alive, cmdline=lambda pid: None,
            find_pids_with_arg=lambda sid: [], transcript_exists=lambda sid: False,
            kill=lambda pid, sig: self.calls.append(("kill", pid, sig)),
            sleep=lambda s: None, monotonic=lambda: 0.0,
            write_halt=lambda content: self.calls.append(("write_halt",)) or True,
            remove_arm=lambda: None, out=lambda m: None, err=self.errs.append,
            repo_root=lambda root: Path("/repo"), read_halt_reason=lambda: None,
        )


def _takeover_lock(project, **extra):
    data = {"pid": 4321, "writer": "cli", "task": "demo"}
    data.update(extra)
    (_memory(project) / "run-supervisor-demo.pid").write_text(json.dumps(data))


@pytest.fixture()
def _no_real_kill(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real kill")

    monkeypatch.setattr(os, "kill", boom)


def test_takeover_refuses_live_opencode_lock(project, _no_real_kill):
    _takeover_lock(project, runtime="opencode")
    ops = _Ops(alive=True)
    rc = takeover.run_takeover("demo", project, ops.build())
    assert rc == takeover.EXIT_NO_CHILD
    assert ops.calls == []
    assert "kill -TERM 4321" in " ".join(ops.errs)


def test_takeover_refuses_dead_opencode_lock(project, _no_real_kill):
    _takeover_lock(project, runtime="opencode")
    ops = _Ops(alive=False)
    rc = takeover.run_takeover("demo", project, ops.build())
    assert rc == takeover.EXIT_NO_CHILD
    assert ops.calls == []
    text = " ".join(ops.errs)
    assert "no longer running" in text and "kill -TERM" not in text


def test_takeover_guard_is_keyed_on_the_runtime_field(project, _no_real_kill):
    _takeover_lock(project)
    ops = _Ops(alive=False)
    takeover.run_takeover("demo", project, ops.build())
    assert ("write_halt",) in ops.calls


# --- allow-list of the runtime value in every reader -----------------------


def test_known_runtimes_mirror_the_supervisor(ar):
    assert tuple(ar._KNOWN_RUNTIMES) == tuple(sup.RUNTIMES)


@pytest.mark.parametrize("value", ["x|y", 42, "OPENCODE", "", None, ["opencode"], "claude"])
def test_auto_resume_ignores_unrecognised_runtime_values(ar, value):
    assert ar._foreign_runtime({"runtime": value}) is None


def test_auto_resume_only_exact_opencode_is_foreign(ar):
    assert ar._foreign_runtime({"runtime": "opencode"}) == "opencode"


def test_garbage_runtime_never_echoes_and_follows_the_claude_path(ar, project):
    memory = _memory(project)
    counter = ar._default_counter("demo", _now_iso())
    _foreign_lock(memory, os.getppid(), runtime="x|y")
    assert ar._do_handoff(memory, project, "demo", "budget", counter, _record()) == (
        "LOCKED|%d" % os.getppid()
    )
    _foreign_lock(memory, _dead_pid(), runtime="x|y")
    assert ar._lock_foreign_runtime(memory, "demo") is None


def test_takeover_garbage_runtime_takes_the_claude_path(project, _no_real_kill):
    _takeover_lock(project, runtime="x|y")
    ops = _Ops(alive=False)
    takeover.run_takeover("demo", project, ops.build())
    assert ("write_halt",) in ops.calls


def test_takeover_hint_for_pid_one_has_no_kill_text(project, _no_real_kill):
    _takeover_lock(project, runtime="opencode", pid=1)
    ops = _Ops(alive=True)
    rc = takeover.run_takeover("demo", project, ops.build())
    assert rc == takeover.EXIT_NO_CHILD and ops.calls == []
    assert "kill" not in " ".join(ops.errs)


def test_takeover_live_hint_asks_to_confirm_first(project, _no_real_kill):
    _takeover_lock(project, runtime="opencode")
    ops = _Ops(alive=True)
    takeover.run_takeover("demo", project, ops.build())
    text = " ".join(ops.errs)
    assert "ps -p 4321 -o command" in text and "kill -TERM 4321" in text
