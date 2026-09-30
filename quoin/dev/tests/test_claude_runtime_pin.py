"""Golden pin for the Claude supervisor launch path.

Expected values are written out literally (never recomputed from the code
under test) so a refactor that changes both the code and a derived
expectation still fails here. Only the flag-less ``quoin run <task>``
invocation is pinned at the CLI level; the parser's option set is left
free to grow.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from quoin import cli
from quoin import supervisor as sup

ISO = "2026-01-01T00:00:00Z"
TIMEOUT_MSG = "\n[launch timed out]"


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_iso_now", lambda: ISO)


def _memory(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "memory"


# --- argv ------------------------------------------------------------------


def test_default_argv_golden():
    assert sup.build_relaunch_argv("demo") == [
        "claude", "-p", "/run --resume --autonomous demo",
        "--allowedTools", "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "Agent", "Skill", "TaskCreate", "TaskUpdate",
        "--output-format", "text",
    ]


def test_bypass_argv_golden():
    assert sup.build_relaunch_argv("demo", permission_mode="bypassPermissions") == [
        "claude", "-p", "/run --resume --autonomous demo",
        "--dangerously-skip-permissions", "--output-format", "text",
    ]


def test_custom_allowed_tools_argv():
    assert sup.build_relaunch_argv("demo", allowed_tools=("Read", "Bash")) == [
        "claude", "-p", "/run --resume --autonomous demo",
        "--allowedTools", "Read", "Bash", "--output-format", "text",
    ]


def test_unknown_mode_follows_allow_list_branch():
    argv = sup.build_relaunch_argv("demo", permission_mode="weird")
    assert "--allowedTools" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_module_constants():
    assert sup.DEFAULT_PERMISSION_MODE == "allowedTools"
    assert sup.DEFAULT_ALLOWED_TOOLS == (
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "Agent", "Skill", "TaskCreate", "TaskUpdate",
    )
    assert sup.DEFAULT_LAUNCH_TIMEOUT_SECONDS == 1800.0
    assert sup.DEFAULT_MAX_RELAUNCH == 10
    fields = [(f.name, f.default) for f in dataclasses.fields(sup.LaunchResult)]
    assert fields[0][0] == "returncode"
    assert fields[1:] == [("stdout", ""), ("stderr", ""), ("timed_out", False)]


# --- make_launch_fn --------------------------------------------------------


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _patched_launch(monkeypatch, repo_root, behavior):
    calls = []
    resolved = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    def fake_resolve(project_root):
        resolved.append(project_root)
        return repo_root

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sup, "resolve_repo_root", fake_resolve)
    return calls, resolved


@pytest.mark.parametrize("mode", ["allowedTools", "bypassPermissions"])
def test_launch_kwargs_exact(monkeypatch, tmp_path, mode):
    calls, resolved = _patched_launch(monkeypatch, tmp_path / "repo", _Proc(0, "o", "e"))
    fn = sup.make_launch_fn(tmp_path / "proj", permission_mode=mode)
    res = fn("demo")
    argv, kwargs = calls[0]
    assert argv == sup.build_relaunch_argv("demo", permission_mode=mode)
    assert kwargs == {
        "cwd": str(tmp_path / "repo"),
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 1800.0,
    }
    assert resolved == [tmp_path / "proj"]
    assert (res.returncode, res.stdout, res.stderr, res.timed_out) == (0, "o", "e", False)


def test_launch_timeout_override(monkeypatch, tmp_path):
    calls, _ = _patched_launch(monkeypatch, tmp_path, _Proc())
    sup.make_launch_fn(tmp_path, timeout=12.5)("demo")
    assert calls[0][1]["timeout"] == 12.5


def test_launch_nonzero_rc(monkeypatch, tmp_path):
    _patched_launch(monkeypatch, tmp_path, _Proc(7, "", "bad"))
    res = sup.make_launch_fn(tmp_path)("demo")
    assert res.returncode == 7 and res.stderr == "bad" and not res.timed_out


def test_launch_timeout_str_output(monkeypatch, tmp_path):
    exc = subprocess.TimeoutExpired(["claude"], 1, output="partial", stderr="err")
    _patched_launch(monkeypatch, tmp_path, exc)
    res = sup.make_launch_fn(tmp_path)("demo")
    assert res.returncode == -1
    assert res.stdout == "partial"
    assert res.stderr == "err" + TIMEOUT_MSG
    assert res.timed_out is True


def test_launch_timeout_bytes_output(monkeypatch, tmp_path):
    exc = subprocess.TimeoutExpired(["claude"], 1, output=b"partial", stderr=b"err")
    _patched_launch(monkeypatch, tmp_path, exc)
    res = sup.make_launch_fn(tmp_path)("demo")
    assert res.returncode == -1
    assert res.stdout == ""
    assert res.stderr == TIMEOUT_MSG
    assert res.timed_out is True


def test_launch_oserror(monkeypatch, tmp_path):
    _patched_launch(monkeypatch, tmp_path, OSError("boom"))
    res = sup.make_launch_fn(tmp_path)("demo")
    assert res.returncode == -1
    assert res.stderr == "boom"


# --- resolve_repo_root -----------------------------------------------------


def test_resolve_repo_root_argv_and_kwargs(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return _Proc(0, "/some/top\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sup.resolve_repo_root(tmp_path) == Path("/some/top")
    argv, kwargs = seen[0]
    assert argv == ["git", "rev-parse", "--show-toplevel"]
    assert kwargs == {
        "cwd": str(tmp_path), "capture_output": True, "text": True, "timeout": 30,
    }


@pytest.mark.parametrize("behavior", [_Proc(1, "/x\n"), _Proc(0, "  \n"), OSError("x")])
def test_resolve_repo_root_fallback(monkeypatch, tmp_path, behavior):
    def fake_run(argv, **kwargs):
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sup.resolve_repo_root(tmp_path) == tmp_path


# --- quoin run -------------------------------------------------------------


def _patch_run(monkeypatch, supervise_fn):
    made = []

    def fake_make(*args, **kwargs):
        made.append((args, kwargs))

        def launch(task):
            return sup.LaunchResult(returncode=0)

        return launch

    sup_calls = []

    def fake_supervise(*args, **kwargs):
        sup_calls.append((args, kwargs))
        return supervise_fn(*args, **kwargs)

    monkeypatch.setattr(sup, "make_launch_fn", fake_make)
    monkeypatch.setattr(sup, "supervise", fake_supervise)
    return made, sup_calls


@pytest.mark.parametrize("mode", ["allowedTools", "bypassPermissions"])
def test_run_wiring(monkeypatch, project, mode):
    made, sup_calls = _patch_run(
        monkeypatch, lambda *a, **k: sup.SuperviseResult("SUCCESS")
    )
    argv = ["run", "demo", "--project-root", str(project)]
    if mode != "allowedTools":
        argv += ["--permission-mode", mode]
    assert cli.main(argv) == 0
    assert made == [((project.resolve(),), {"permission_mode": mode})]
    args, kwargs = sup_calls[0]
    assert args == ("demo", project.resolve())
    assert set(kwargs) == {"launch_fn", "max_relaunch"}
    assert kwargs["max_relaunch"] == 10


def test_run_max_relaunch_passthrough(monkeypatch, project):
    _, sup_calls = _patch_run(monkeypatch, lambda *a, **k: sup.SuperviseResult("SUCCESS"))
    cli.main(["run", "demo", "--project-root", str(project), "--max-relaunch", "4"])
    assert sup_calls[0][1]["max_relaunch"] == 4


def test_lock_payload_bytes(monkeypatch, project):
    lock = _memory(project) / "run-supervisor-demo.pid"
    seen = {}

    def fake_supervise(*a, **k):
        seen["bytes"] = lock.read_bytes()
        return sup.SuperviseResult("SUCCESS")

    _patch_run(monkeypatch, fake_supervise)
    cli.main(["run", "demo", "--project-root", str(project)])
    expected = json.dumps(
        {"granted": 10, "pid": os.getpid(), "started_at": ISO, "task": "demo", "writer": "cli"},
        sort_keys=True,
    ) + "\n"
    assert seen["bytes"] == expected.encode("utf-8")
    assert not lock.exists()


def test_halt_and_result_text(monkeypatch, project):
    made_calls = {"n": 0}

    def fake_supervise(task, root, *, launch_fn, max_relaunch):
        for _ in range(3):
            launch_fn(task)
        return sup.SuperviseResult("ABORTED", "relaunch cap", 7)

    made, _ = _patch_run(monkeypatch, fake_supervise)

    def counting_make(*args, **kwargs):
        def launch(task):
            made_calls["n"] += 1
            return sup.LaunchResult(returncode=0)
        return launch

    monkeypatch.setattr(sup, "make_launch_fn", counting_make)
    code = cli.main(["run", "demo", "--project-root", str(project), "--halt-on-abort"])
    assert code == 2
    mem = _memory(project)
    assert (mem / "autonomous-halt-demo.md").read_text() == (
        "task: demo\nphase: run\nreason: relaunch cap\n"
        f"timestamp: {ISO}\nresume_hint: /run --resume demo\n"
    )
    assert (mem / "run-supervisor-demo.result").read_text() == json.dumps(
        {"finished_at": ISO, "relaunches": 3, "status": "ABORTED"}, sort_keys=True
    ) + "\n"
    assert made_calls["n"] == 3


def test_relaunch_counter_vs_stdout_counter(monkeypatch, project, capsys):
    def fake_supervise(task, root, *, launch_fn, max_relaunch):
        for _ in range(3):
            launch_fn(task)
        return sup.SuperviseResult("ABORTED", "relaunch cap", 7)

    _patch_run(monkeypatch, fake_supervise)
    cli.main(["run", "demo", "--project-root", str(project), "--halt-on-abort"])
    assert "  relaunches: 7\n" in capsys.readouterr().out
    assert json.loads((_memory(project) / "run-supervisor-demo.result").read_text())["relaunches"] == 3


def test_supervise_error_marks_halt_and_error(monkeypatch, project):
    def fake_supervise(*a, **k):
        raise RuntimeError("kaboom")

    _patch_run(monkeypatch, fake_supervise)
    with pytest.raises(RuntimeError):
        cli.main(["run", "demo", "--project-root", str(project), "--halt-on-abort"])
    mem = _memory(project)
    assert "reason: supervisor error\n" in (mem / "autonomous-halt-demo.md").read_text()
    assert json.loads((mem / "run-supervisor-demo.result").read_text())["status"] == "ERROR"
    assert not (mem / "run-supervisor-demo.pid").exists()


# --- exit codes and stdout -------------------------------------------------


def test_success_stdout(monkeypatch, project, capsys):
    _patch_run(monkeypatch, lambda *a, **k: sup.SuperviseResult("SUCCESS"))
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 0
    assert capsys.readouterr().out == "quoin run: SUCCESS\n  task: demo\n  relaunches: 0\n"


def test_halted_exit(monkeypatch, project, capsys):
    _patch_run(monkeypatch, lambda *a, **k: sup.SuperviseResult("HALTED", "why", 1))
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 1
    assert capsys.readouterr().out.splitlines()[0] == "quoin run: HALTED (why)"


def test_aborted_exit(monkeypatch, project):
    _patch_run(monkeypatch, lambda *a, **k: sup.SuperviseResult("ABORTED", "x", 1))
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 2


def test_live_foreign_lock_refuses(monkeypatch, project, capsys):
    ppid = os.getppid()
    (_memory(project) / "run-supervisor-demo.pid").write_text(
        json.dumps({"pid": ppid, "writer": "cli", "task": "demo"}) + "\n"
    )
    _, sup_calls = _patch_run(monkeypatch, lambda *a, **k: sup.SuperviseResult("SUCCESS"))
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 3
    assert capsys.readouterr().out == (
        f"quoin run: REFUSED (supervisor lock held by pid {ppid})\n  task: demo\n"
    )
    assert sup_calls == []
