"""`quoin run --runtime opencode`: validation, lock handling, signals, summary."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quoin import cli
from quoin import supervisor as sup
from quoin.opencode_adapter import driver, retry, runstore

from _opencode_run_helpers import Boom, ScriptedDriver, outcome

REPO_ROOT = Path(__file__).resolve().parents[3]
KEYS = sorted([
    "runtime", "task", "stage", "phase", "profile", "outcome", "exit_code", "run_state",
    "evidence", "reason", "resume_blocked", "run_id", "sidecar", "attempts",
    "artifact_coverage", "refusal", "resume_hint", "superseded_run", "workflow_validated",
])


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root.resolve()


@pytest.fixture()
def stub(monkeypatch, project):
    """Install a scripted driver factory; returns a holder for the instance."""
    holder = {"script": [], "kwargs": {}, "driver": None, "built": 0}

    def factory(root):
        holder["built"] += 1
        drv = ScriptedDriver(root, list(holder["script"]), **holder["kwargs"])
        holder["driver"] = drv
        for name, value in holder.get("attrs", {}).items():
            setattr(drv, name, value)
        return drv

    monkeypatch.setattr(cli, "_make_opencode_driver", factory)
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
    return holder


def memory(project):
    return project / ".workflow_artifacts" / "memory"


def lock_path(project):
    return memory(project) / "run-supervisor-demo.pid"


def argv(project, *extra, phase="plan", profile="work"):
    out = ["run", "demo", "--runtime", "opencode", "--project-root", str(project)]
    if profile:
        out += ["--profile", profile]
    if phase:
        out += ["--phase", phase]
    return out + list(extra)


def run(project, capsys, *extra, **kw):
    code = cli.main(argv(project, *extra, **kw))
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1]), out


def dead_pid():
    pid = 2 ** 22 + 54321
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


# --- flag validation ----------------------------------------------------------


@pytest.mark.parametrize("flag", [["--profile", "x"], ["--phase", "plan"], ["--stage", "2"], ["--new-run"]])
def test_opencode_flags_need_the_runtime(project, capsys, flag):
    with pytest.raises(SystemExit) as err:
        cli.main(["run", "demo", "--project-root", str(project)] + flag)
    assert err.value.code == 2
    assert "only valid with --runtime opencode" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra,phase_args,text",
    [
        ([], dict(profile=None), "--profile NAME"),
        (["--takeover"], {}, "--takeover is only valid"),
        (["--permission-mode", "bypassPermissions"], {}, "bypassPermissions is not available"),
        (["--max-relaunch", "-1"], {}, "--max-relaunch must be 0 or more"),
    ],
)
def test_opencode_flag_rejections(project, capsys, stub, extra, phase_args, text):
    with pytest.raises(SystemExit) as err:
        cli.main(argv(project, *extra, **phase_args))
    assert err.value.code == 2
    assert text in capsys.readouterr().err
    assert stub["built"] == 0 and not lock_path(project).exists()


def test_explicit_allowed_tools_is_accepted(project, capsys, stub):
    stub["script"] = [outcome()]
    code, summary, _ = run(project, capsys, "--permission-mode", "allowedTools")
    assert code == 0 and summary["outcome"] == "COMPLETED"


def test_takeover_with_autonomous_is_still_a_parser_error(project, capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["run", "demo", "--takeover", "--autonomous", "--project-root", str(project)])
    assert err.value.code == 2


def test_explicit_claude_runtime_wires_like_the_default(monkeypatch, project):
    made, calls = [], []

    def fake_make(*a, **k):
        made.append((a, k))
        return lambda t: sup.LaunchResult(0)

    def fake_supervise(*a, **k):
        calls.append((a, k))
        return sup.SuperviseResult("SUCCESS")

    monkeypatch.setattr(sup, "make_launch_fn", fake_make)
    monkeypatch.setattr(sup, "supervise", fake_supervise)
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 0
    assert cli.main(["run", "demo", "--runtime", "claude", "--project-root", str(project)]) == 0
    default, explicit = made
    assert explicit == default
    assert explicit[0] == (project,) and explicit[1]["permission_mode"] == "allowedTools"
    assert calls[1][0] == calls[0][0] == ("demo", project)
    assert calls[1][1]["max_relaunch"] == calls[0][1]["max_relaunch"] == 10


def test_claude_run_never_imports_the_opencode_adapter(project):
    code = (
        "import sys\n"
        "from quoin import cli, supervisor as sup\n"
        "sup.make_launch_fn = lambda *a, **k: (lambda t: sup.LaunchResult(0))\n"
        "sup.supervise = lambda *a, **k: sup.SuperviseResult('SUCCESS')\n"
        "rc = cli.main(['run', 'demo', '--project-root', %r])\n"
        "print(rc, [m for m in sys.modules if m.startswith('quoin.opencode_adapter')])\n"
    ) % str(project)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "0 []"


# --- refusals before any state ---------------------------------------------------


def test_whole_task_is_refused_without_state(monkeypatch, project, capsys, stub):
    def boom(*a, **k):
        raise AssertionError("no lock may be taken")

    monkeypatch.setattr(cli, "_acquire_supervisor_lock", boom)
    code, summary, _ = run(project, capsys, phase=None)
    assert code == 3 and sorted(summary) == KEYS
    assert summary["refusal"]["code"] == "whole-task-unavailable"
    assert summary["refusal"]["category"] == "workflow-validation"
    assert "--phase" in summary["refusal"]["message"]
    assert (summary["task"], summary["phase"], summary["profile"]) == ("demo", None, "work")
    assert not (memory(project) / "runtime").exists() and stub["built"] == 0


def test_invalid_task_name_is_refused_before_the_lock(monkeypatch, project, capsys, stub):
    monkeypatch.setattr(cli, "_acquire_supervisor_lock", lambda *a, **k: pytest.fail("lock"))
    code = cli.main(["run", "../x", "--runtime", "opencode", "--profile", "work",
                     "--phase", "plan", "--project-root", str(project)])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3 and sorted(summary) == KEYS
    assert summary["refusal"]["code"] == "invalid-task-name"
    assert (summary["task"], summary["phase"], summary["profile"]) == ("../x", "plan", "work")


def test_stage_stays_a_string(project, capsys, stub):
    stub["script"] = [outcome()]
    code, summary, _ = run(project, capsys, "--stage", "2")
    assert code == 0 and summary["stage"] == "2"
    assert stub["driver"].last_request.stage == "2"


# --- outcomes and exit codes -------------------------------------------------------


@pytest.mark.parametrize(
    "script,extra,outcome_name,exit_code,kw",
    [
        ([outcome()], [], "COMPLETED", 0, {}),
        ([outcome(evidence="partial")], [], "COMPLETED_UNVERIFIED", 6, {}),
        ([outcome("awaiting_approval", reason="approval-required")], [], "AWAITING_APPROVAL", 4, {}),
        ([outcome("interrupted", reason="signal")], ["--max-relaunch", "0"], "INTERRUPTED", 5, {}),
        ([outcome("failed", failure=retry.Failure("auth"))], [], "FAILED", 2, {}),
        ([outcome("interrupted", reason="signal", new_native_events=0)] * 2,
         ["--max-relaunch", "3"], "ABORTED", 2, {}),
        ([Boom(RuntimeError("x"), "observe")], [], "ERROR", 2, {}),
        ([outcome("cancelled", reason="cancelled")], [], "CANCELLED", 143, {}),
    ],
)
def test_outcome_json_and_exit_codes(project, capsys, stub, script, extra, outcome_name, exit_code, kw):
    stub["script"] = script
    code, summary, lines = run(project, capsys, *extra)
    assert len(lines) == 1
    assert code == exit_code and summary["exit_code"] == exit_code
    assert summary["outcome"] == outcome_name and sorted(summary) == KEYS
    assert summary["workflow_validated"] is False and summary["runtime"] == "opencode"
    assert summary["run_id"] and summary["sidecar"]
    if outcome_name in ("COMPLETED",):
        assert summary["resume_hint"] is None
    else:
        assert summary["resume_hint"].startswith("quoin run --runtime opencode --profile work")


def test_prepare_refusal_exits_three_with_the_driver_category(project, capsys, stub):
    stub["attrs"] = {"prepare_errors": {0: driver.PrepareRefused("unsupported-version", "version", "wrong")}}
    code, summary, _ = run(project, capsys)
    assert code == 3 and summary["refusal"] == {
        "category": "unsupported-version", "code": "version", "message": "wrong"}
    assert summary["resume_hint"] is None


# --- lock handling -------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        [outcome()],
        [outcome("failed", failure=retry.Failure("auth"))],
        [outcome("cancelled", reason="cancelled")],
        [outcome("interrupted", reason="signal")],
        [outcome("awaiting_approval")],
        [Boom(RuntimeError("x"), "observe")],
    ],
)
def test_lock_is_released_after_every_outcome(project, capsys, stub, script):
    stub["script"] = script
    run(project, capsys, "--max-relaunch", "0")
    assert not lock_path(project).exists()


def test_lock_is_released_when_reconcile_raises(project, capsys, stub):
    stub["attrs"] = {"reconcile_error": RuntimeError("boom")}
    code, summary, _ = run(project, capsys)
    assert code == 2 and summary["outcome"] == "ERROR" and summary["run_id"] is None
    assert summary["reason"] == "driver-error: RuntimeError"
    assert not lock_path(project).exists()
    assert summary["resume_hint"] is not None


def test_lock_is_released_on_keyboard_interrupt(project, stub):
    stub["script"] = [Boom(KeyboardInterrupt(), "observe")]
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        cli.main(argv(project))
    assert not lock_path(project).exists()
    assert signal.getsignal(signal.SIGINT) is before


def test_lock_carries_the_runtime_while_running(project, capsys, stub):
    seen = {}

    def on_observe(d, h):
        seen.update(json.loads(lock_path(project).read_text()))

    stub["script"] = [outcome()]
    stub["attrs"] = {"on_observe": on_observe}
    run(project, capsys, "--max-relaunch", "4")
    assert seen["runtime"] == "opencode" and seen["writer"] == "cli"
    assert seen["granted"] == 4 and seen["pid"] == os.getpid() and seen["task"] == "demo"


def test_state_is_written_while_the_lock_is_held(monkeypatch, project, capsys, stub):
    seen = {}
    real = cli._release_supervisor_lock

    def spy(path, pid):
        seen["lock"] = path.exists()
        run_id = stub["driver"].last_run_id
        seen["hint"] = stub["driver"].record(run_id).get("resume_hint")
        seen["result"] = (memory(project) / "run-supervisor-demo.result").exists()
        return real(path, pid)

    monkeypatch.setattr(cli, "_release_supervisor_lock", spy)
    stub["script"] = [outcome("interrupted", reason="signal")]
    orig = ScriptedDriver.prepare

    def tracking(self, request, **kw):
        prepared = orig(self, request, **kw)
        self.last_run_id = prepared.run_id
        return prepared

    monkeypatch.setattr(ScriptedDriver, "prepare", tracking)
    run(project, capsys, "--max-relaunch", "0", "--halt-on-abort")
    assert seen == {"lock": True, "hint": seen["hint"], "result": True}
    assert seen["hint"] and seen["hint"].startswith("quoin run --runtime opencode")


def test_signal_handlers_are_installed_before_the_lock(monkeypatch, project, capsys, stub):
    real = cli._acquire_supervisor_lock
    seen = {}
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    def spy(*a, **k):
        seen["during"] = {s: signal.getsignal(s) for s in before}
        return real(*a, **k)

    monkeypatch.setattr(cli, "_acquire_supervisor_lock", spy)
    live = os.getppid()
    lock_path(project).write_text(json.dumps({"pid": live, "writer": "cli", "task": "demo"}) + "\n")
    code, summary, _ = run(project, capsys)
    assert code == 3
    assert all(seen["during"][s] is not before[s] for s in before)
    assert {s: signal.getsignal(s) for s in before} == before


@pytest.mark.parametrize("runtime,expected", [(None, "claude"), ("opencode", "opencode")])
def test_live_foreign_lock_refuses_the_phase(project, capsys, stub, runtime, expected):
    data = {"pid": os.getppid(), "writer": "cli", "task": "demo"}
    if runtime:
        data["runtime"] = runtime
    lock_path(project).write_text(json.dumps(data) + "\n")
    code, summary, _ = run(project, capsys)
    assert code == 3 and sorted(summary) == KEYS
    assert summary["refusal"]["code"] == "lock-held" and summary["refusal"]["category"] is None
    assert "runtime %s" % expected in summary["refusal"]["message"]
    assert str(os.getppid()) in summary["refusal"]["message"]
    assert stub["built"] == 0 and lock_path(project).exists()
    assert (summary["task"], summary["phase"], summary["profile"]) == ("demo", "plan", "work")


def test_dead_lock_without_runtime_is_reclaimed(project, capsys, stub):
    lock_path(project).write_text(json.dumps({"pid": dead_pid(), "writer": "cli", "task": "demo"}) + "\n")
    stub["script"] = [outcome()]
    code, summary, _ = run(project, capsys)
    assert code == 0 and not lock_path(project).exists()


# --- signals --------------------------------------------------------------------------


def _signal_during_attempt(sig):
    def on_observe(d, handle):
        os.kill(os.getpid(), sig)
        end = time.time() + 5
        while handle.cancel_requests == 0 and time.time() < end:
            time.sleep(0.01)

    return on_observe


@pytest.mark.parametrize("sig,expected", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_signal_during_an_attempt(project, capsys, stub, sig, expected):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    stub["script"] = [outcome("cancelled", reason="cancelled")]
    stub["attrs"] = {"on_observe": _signal_during_attempt(sig)}
    code, summary, _ = run(project, capsys, "--halt-on-abort")
    assert code == expected and summary["outcome"] == "CANCELLED"
    assert stub["driver"].handles[0].cancel_requests >= 1
    assert {s: signal.getsignal(s) for s in before} == before
    assert not lock_path(project).exists()
    assert not (memory(project) / "autonomous-halt-demo.md").exists()


def test_sigterm_during_a_backoff_wait(monkeypatch, project, capsys, stub):
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 30)
    stub["script"] = [outcome("interrupted", reason="signal"), outcome()]
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
    started = time.time()
    timer.start()
    try:
        code, summary, _ = run(project, capsys)
    finally:
        timer.join()
    assert time.time() - started < 3
    assert code == 143 and summary["outcome"] == "CANCELLED"
    assert summary["attempts"] == 1 and summary["run_state"] == "interrupted"


# --- --halt-on-abort and the result file ------------------------------------------------


def _load_auto_resume():
    spec = importlib.util.spec_from_file_location(
        "auto_resume_cli_under_test", REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script,extra,name", [
    ([outcome("interrupted", reason="signal")], ["--max-relaunch", "0"], "INTERRUPTED"),
    ([outcome()], [], "COMPLETED"),
])
def test_halt_on_abort_writes_an_inert_result(project, capsys, stub, script, extra, name):
    stub["script"] = script
    run(project, capsys, "--halt-on-abort", *extra)
    result = json.loads((memory(project) / "run-supervisor-demo.result").read_text())
    assert result["status"] == name and result["relaunches"] == 0
    ar = _load_auto_resume()
    assert ar.settle_supervisor(memory(project), "demo", {"attempts": 2})["attempts"] == 2
    assert not (memory(project) / "autonomous-halt-demo.md").exists()


def test_without_the_flag_no_result_is_written(project, capsys, stub):
    stub["script"] = [outcome()]
    run(project, capsys)
    assert not (memory(project) / "run-supervisor-demo.result").exists()


def test_an_existing_result_is_kept(project, capsys, stub):
    path = memory(project) / "run-supervisor-demo.result"
    path.write_text(json.dumps({"status": "ORPHANED", "relaunches": 3, "finished_at": "x"}))
    before = path.read_bytes()
    stub["script"] = [outcome("interrupted", reason="signal")]
    run(project, capsys, "--halt-on-abort", "--max-relaunch", "0")
    assert path.read_bytes() == before


# --- run record annotations ----------------------------------------------------------------


def test_error_after_a_run_exists_keeps_the_run_and_annotates(project, capsys, stub):
    stub["script"] = [Boom(RuntimeError("x"), "observe")]
    code, summary, _ = run(project, capsys)
    assert code == 2 and summary["run_id"] and summary["sidecar"] and summary["resume_hint"]
    record = stub["driver"].record(summary["run_id"])
    assert record["resume_hint"] == summary["resume_hint"]


def test_stale_running_run_is_refused_then_superseded(project, capsys, stub):
    drv = ScriptedDriver(project, [])
    stale = drv.seed("running", attempts=[{"attempt": 1, "pid": 4242, "pgid": 4242, "driver_pid": 1,
                                           "driver_start": "x", "state": "running"}])
    stub["script"] = [outcome()]
    code, summary, _ = run(project, capsys)
    assert code == 3 and summary["refusal"]["code"] == "run-in-progress"
    assert "--new-run" in summary["refusal"]["message"]
    code, summary, _ = run(project, capsys, "--new-run")
    assert code == 0 and summary["superseded_run"]["run_id"] == stale
    assert summary["run_id"] != stale


def test_resume_hint_text(project, capsys, stub):
    stub["script"] = [outcome("interrupted", reason="signal")]
    code, summary, _ = run(project, capsys, "--max-relaunch", "0")
    import shlex
    assert summary["resume_hint"] == (
        "quoin run --runtime opencode --profile work --phase plan demo --project-root "
        + shlex.quote(str(project))
    )
    assert stub["driver"].record(summary["run_id"])["resume_hint"] == summary["resume_hint"]


def test_resume_hint_with_stage_and_a_blocked_resume(project, capsys, stub):
    stub["script"] = [outcome("interrupted", reason="session-lost", resume_blocked="session-lost")]
    code, summary, _ = run(project, capsys, "--stage", "2")
    assert "--stage 2 demo" in summary["resume_hint"]
    assert summary["resume_hint"].endswith(" --new-run")
    assert summary["resume_blocked"] == "session-lost"


def test_budget_and_new_run_reach_the_loop(project, capsys, stub):
    stub["script"] = [outcome()]
    seed = ScriptedDriver(project, [])
    old = seed.seed("interrupted")
    code, summary, _ = run(project, capsys, "--budget", "5", "--new-run")
    assert stub["driver"].last_request.budget == "5"
    assert summary["run_id"] != old


def test_a_done_sentinel_does_not_stop_the_phase(project, capsys, stub):
    (memory(project) / "autonomous-done-demo.md").write_text("done")
    stub["script"] = [outcome()]
    code, summary, _ = run(project, capsys)
    assert code == 0 and stub["driver"].names().count("start") == 1


def test_run_help_lists_the_new_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = capsys.readouterr().out
    for flag in ("--runtime", "--profile", "--phase", "--stage", "--new-run"):
        assert flag in text
