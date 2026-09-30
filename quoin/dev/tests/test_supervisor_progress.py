"""Progress, repair and commit-probe behavior of the supervisor loop."""
from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from quoin import cli
from quoin import supervisor as sup

TASK = "demo-task"
SHA_A = "a" * 40
SHA_B = "b" * 40


class _Clock:
    def sleep(self, seconds):
        pass


def _progress(root: Path) -> Path:
    d = sup.progress_dir(TASK, root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memory(root: Path) -> Path:
    m = root / ".workflow_artifacts" / "memory"
    m.mkdir(parents=True, exist_ok=True)
    return m


# --- pure predicates -------------------------------------------------------


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ((), (), False),
        ((), (("a", SHA_A),), False),
        ((("a", SHA_A),), (), False),
        ((("a", SHA_A),), (("a", SHA_A),), False),
        ((("a", SHA_A), ("b", SHA_A)), (("a", SHA_A), ("b", SHA_B)), True),
        ((("a", SHA_A),), (("b", SHA_B),), False),
    ],
)
def test_heads_changed(before, after, expected):
    assert sup.heads_changed(before, after) is expected


def test_progress_made():
    assert sup.progress_made(0, 1, (), ()) is True
    assert sup.progress_made(1, 1, (), ()) is False
    assert sup.progress_made(2, 1, (), ()) is False
    assert sup.progress_made(1, 1, (("a", SHA_A),), (("a", SHA_B),)) is True


def test_repair_pending_phases(tmp_path):
    assert sup.repair_pending_phases(tmp_path / "missing") == ()
    d = tmp_path / "p"
    d.mkdir()
    (d / "implement.tasks.done").write_text("x")
    assert sup.repair_pending_phases(d) == ("implement",)
    (d / "implement.done").write_text("x")
    assert sup.repair_pending_phases(d) == ()
    d2 = tmp_path / "p2"
    d2.mkdir()
    (d2 / "implement.batch-1.done").write_text("x")
    (d2 / "review.tasks.done").write_text("x")
    assert sup.repair_pending_phases(d2) == ()


def test_next_streak_branches():
    assert sup.next_streak(True, ("implement",), 1, 2, 2) == (0, 0, "progress")
    assert sup.next_streak(False, ("implement",), 1, 0, 2) == (1, 1, "repair")
    assert sup.next_streak(False, ("implement",), 1, 2, 2) == (2, 2, "stall")
    assert sup.next_streak(False, (), 0, 0, 2) == (1, 0, "stall")
    assert sup.next_streak(False, ("implement",), 0, 0, 0) == (1, 0, "stall")


def test_abort_reason():
    assert sup.abort_reason(1, ("implement",)) is None
    assert sup.abort_reason(2, ()) == "no forward progress"
    assert (
        sup.abort_reason(2, ("implement",))
        == "phase completion not repaired: implement"
    )


def test_knob_readers():
    assert sup.repair_allowance_from_env({}) == 2
    assert sup.repair_allowance_from_env({sup.REPAIR_RELAUNCHES_ENV: "-3"}) == 0
    assert sup.repair_allowance_from_env({sup.REPAIR_RELAUNCHES_ENV: "99"}) == 5
    assert sup.repair_allowance_from_env({sup.REPAIR_RELAUNCHES_ENV: "x"}) == 2
    assert sup.launch_timeout_from_env({}) == 5400.0
    assert sup.launch_timeout_from_env({sup.LAUNCH_TIMEOUT_ENV: "100"}) == 900.0
    assert sup.launch_timeout_from_env({sup.LAUNCH_TIMEOUT_ENV: "99999"}) == 14400.0
    assert sup.launch_timeout_from_env({sup.LAUNCH_TIMEOUT_ENV: "abc"}) == 5400.0
    assert sup.head_probe_enabled({sup.HEAD_PROBE_ENV: "0"}) is False
    assert sup.head_probe_enabled({sup.HEAD_PROBE_ENV: "1"}) is True
    assert sup.head_probe_enabled({}) is True


# --- probe with real git ---------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


def _git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _repo(d: Path, branch: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(d)], check=True)
    _git(d, "commit", "-q", "--allow-empty", "-m", "init")
    return d


@needs_git
def test_probe_root_repo_on_task_branch(tmp_path):
    _repo(tmp_path, f"feat/{TASK}")
    heads = sup.probe_task_heads(TASK, tmp_path)
    assert len(heads) == 1 and heads[0][0] == "."
    assert re.match(r"^[0-9a-f]{40,64}$", heads[0][1])


@needs_git
def test_probe_root_repo_on_main(tmp_path):
    _repo(tmp_path, "main")
    assert sup.probe_task_heads(TASK, tmp_path) == ()


@needs_git
def test_probe_wrapper_children(tmp_path):
    alpha = _repo(tmp_path / "alpha", f"feat/{TASK}")
    beta = _repo(tmp_path / "beta", "main")
    _repo(tmp_path / "gamma", f"feat/{TASK}-extra")
    det = _repo(tmp_path / "delta", f"feat/{TASK}")
    _git(det, "checkout", "-q", "--detach")
    before = sup.probe_task_heads(TASK, tmp_path)
    assert [k for k, _ in before] == ["alpha"]
    _git(beta, "commit", "-q", "--allow-empty", "-m", "x")
    assert sup.probe_task_heads(TASK, tmp_path) == before
    _git(alpha, "commit", "-q", "--allow-empty", "-m", "x")
    after = sup.probe_task_heads(TASK, tmp_path)
    assert after != before and sup.heads_changed(before, after)


@needs_git
def test_probe_ignores_ancestor_repo(tmp_path):
    _repo(tmp_path, f"feat/{TASK}")
    inner = tmp_path / "inner"
    inner.mkdir()
    assert sup.probe_task_heads(TASK, inner) == ()


def test_probe_wrapper_without_children(tmp_path):
    assert sup.probe_task_heads(TASK, tmp_path) == ()


# --- probe with injected runner --------------------------------------------


class _R:
    def __init__(self, stdout="", rc=0):
        self.stdout = stdout
        self.returncode = rc


def _fake_repo_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_probe_argv_and_kwargs(tmp_path):
    root = _fake_repo_dir(tmp_path)
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return _R(f"{SHA_A}\nfeat/{TASK}\n")

    assert sup.probe_task_heads(TASK, root, runner=runner) == ((".", SHA_A),)
    argv, kw = calls[0]
    assert argv == ["git", "-C", str(root), "rev-parse", "HEAD", "--abbrev-ref", "HEAD"]
    assert "shell" not in kw
    assert kw["timeout"] <= sup.HEAD_PROBE_BUDGET_SECS
    assert kw["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_probe_rejects_non_hex_first_line(tmp_path):
    root = _fake_repo_dir(tmp_path)
    runner = lambda argv, **kw: _R(f"feat/{TASK}\nfeat/{TASK}\n")  # noqa: E731
    assert sup.probe_task_heads(TASK, root, runner=runner) == ()


@pytest.mark.parametrize(
    "exc", [subprocess.TimeoutExpired("git", 1), FileNotFoundError(), ValueError()]
)
def test_probe_runner_errors_read_as_empty(tmp_path, exc):
    root = _fake_repo_dir(tmp_path)

    def runner(argv, **kw):
        raise exc

    assert sup.probe_task_heads(TASK, root, runner=runner) == ()


def _never(argv, **kw):
    raise AssertionError("runner must not be called")


def test_probe_no_call_cases(tmp_path):
    root = _fake_repo_dir(tmp_path)
    ticks = itertools.count(0, 10)
    assert sup.probe_task_heads(TASK, root, runner=_never, clock=lambda: next(ticks)) == ()
    assert sup.probe_task_heads("Bad Name", root, runner=_never) == ()
    assert sup.probe_task_heads(
        TASK, root, runner=_never, environ={sup.HEAD_PROBE_ENV: "0"}
    ) == ()
    bare = tmp_path / "nogit"
    bare.mkdir()
    assert sup.probe_task_heads(TASK, bare, runner=_never) == ()


# --- supervise loop --------------------------------------------------------


def _run(root, launch, **kw):
    kw.setdefault("progress_probe", lambda: ())
    return sup.supervise(
        TASK, root, launch_fn=launch, max_relaunch=kw.pop("max_relaunch", 10),
        clock=_Clock(), **kw,
    )


def test_unchanged_abort(tmp_path):
    n = []
    res = _run(tmp_path, lambda t: n.append(1))
    assert (res.status, res.reason, res.relaunches) == (
        "ABORTED", "no forward progress", 1,
    )
    assert len(n) == 2


def test_repair_then_abort(tmp_path):
    (_progress(tmp_path) / "implement.tasks.done").write_text("x")
    n = []
    res = _run(tmp_path, lambda t: n.append(1))
    assert res.status == "ABORTED"
    assert res.reason == "phase completion not repaired: implement"
    assert len(n) == 4


def test_repair_then_success(tmp_path):
    d = _progress(tmp_path)
    (d / "implement.tasks.done").write_text("x")
    n = []

    def launch(t):
        n.append(1)
        if len(n) == 2:
            (d / "implement.done").write_text("x")
        if len(n) == 3:
            _memory(tmp_path)
            sup.done_path(TASK, tmp_path).write_text("x")

    res = _run(tmp_path, launch)
    assert res.status == "SUCCESS"


def test_allowance_zero_restores_old_timing(tmp_path):
    (_progress(tmp_path) / "implement.tasks.done").write_text("x")
    n = []
    res = _run(tmp_path, lambda t: n.append(1), repair_allowance=0)
    # Timing is the old two-launch abort; the reason names the unrepaired phase.
    assert res.status == "ABORTED" and len(n) == 2
    assert res.reason == "phase completion not repaired: implement"


def test_never_above_cap(tmp_path):
    (_progress(tmp_path) / "implement.tasks.done").write_text("x")
    n = []
    res = _run(tmp_path, lambda t: n.append(1), repair_allowance=5, max_relaunch=3)
    assert res.reason == "relaunch cap" and len(n) <= 3


def test_head_progress_keeps_loop_alive(tmp_path):
    shas = itertools.count(1)
    probe = lambda: (("r", f"{next(shas):040x}"),)  # noqa: E731
    n = []
    res = _run(tmp_path, lambda t: n.append(1), progress_probe=probe, max_relaunch=4)
    assert res.reason == "relaunch cap" and len(n) == 4


def test_empty_probe_after_launch_is_not_progress(tmp_path):
    seq = iter([(("r", SHA_A),), (), (("r", SHA_A),), ()] * 3)
    res = _run(tmp_path, lambda t: None, progress_probe=lambda: next(seq))
    assert res.reason == "no forward progress"


def test_probe_exception_reads_as_empty(tmp_path):
    def probe():
        raise RuntimeError("boom")

    res = _run(tmp_path, lambda t: None, progress_probe=probe)
    assert res.reason == "no forward progress"


def test_default_probe_spawns_nothing_without_git_entry(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no process expected")

    monkeypatch.setattr(subprocess, "run", boom)
    res = sup.supervise(
        TASK, tmp_path, launch_fn=lambda t: None, max_relaunch=5, clock=_Clock()
    )
    assert res.reason == "no forward progress"


def test_repair_scenario_writes_no_done_files(tmp_path):
    d = _progress(tmp_path)
    (d / "implement.tasks.done").write_text("x")
    _run(tmp_path, lambda t: None)
    assert sorted(p.name for p in d.iterdir()) == ["implement.tasks.done"]


# --- launcher env and timeout ----------------------------------------------


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def test_launcher_env_on_run_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(kw) or _Done())
    monkeypatch.setattr(sup, "resolve_repo_root", lambda r: tmp_path)
    sup.make_launch_fn(tmp_path, env={"QUOIN_HEADLESS_CHILD": "1"})("t")
    assert calls[0]["env"] == {"QUOIN_HEADLESS_CHILD": "1"}
    assert calls[0]["timeout"] == 5400
    calls.clear()
    sup.make_launch_fn(tmp_path)("t")
    assert "env" not in calls[0]


def test_launcher_env_on_popen_path(monkeypatch, tmp_path):
    seen = []

    class _P:
        pid = 1
        returncode = 0

        def __init__(self, argv, **kw):
            seen.append(kw)

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", _P)
    monkeypatch.setattr(sup, "resolve_repo_root", lambda r: tmp_path)
    fn = sup.make_launch_fn(tmp_path, env={"QUOIN_HEADLESS_CHILD": "1"})
    fn("t", on_spawn=lambda pid: None)
    assert seen[0]["env"] == {"QUOIN_HEADLESS_CHILD": "1"}
    seen.clear()
    sup.make_launch_fn(tmp_path)("t", on_spawn=lambda pid: None)
    assert "env" not in seen[0]


# --- CLI wiring ------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _cli_capture(monkeypatch, project, extra=()):
    made, sups = [], []

    def fake_make(*a, **k):
        made.append(k)
        return lambda task: sup.LaunchResult(returncode=0)

    def fake_sup(task, root, **k):
        sups.append(k)
        return sup.SuperviseResult("SUCCESS")

    monkeypatch.setattr(sup, "make_launch_fn", fake_make)
    monkeypatch.setattr(sup, "supervise", fake_sup)
    cli.main(["run", "demo", "--project-root", str(project), *extra])
    return made[0], sups[0]


@pytest.mark.parametrize("value,expected", [("100", 900.0), ("99999", 14400.0), ("abc", 5400.0)])
def test_cli_timeout_knob(monkeypatch, project, value, expected):
    monkeypatch.setenv(sup.LAUNCH_TIMEOUT_ENV, value)
    made, _ = _cli_capture(monkeypatch, project)
    assert made["timeout"] == expected


def test_cli_repair_knob_and_env(monkeypatch, project):
    monkeypatch.setenv(sup.REPAIR_RELAUNCHES_ENV, "0")
    monkeypatch.setenv("QUOIN_SUPERVISOR_LOCK_TOKEN", "tok")
    monkeypatch.setenv("QUOIN_FIRST_CHILD_SESSION_ID", "x")
    monkeypatch.delenv(sup.HEADLESS_CHILD_ENV, raising=False)
    made, sups = _cli_capture(monkeypatch, project)
    assert sups["repair_allowance"] == 0
    assert "QUOIN_SUPERVISOR_LOCK_TOKEN" not in made["env"]
    assert "QUOIN_FIRST_CHILD_SESSION_ID" not in made["env"]
    assert made["env"][sup.HEADLESS_CHILD_ENV] == "1"
    import os

    assert sup.HEADLESS_CHILD_ENV not in os.environ


def test_cli_halt_reason_roundtrip(monkeypatch, project):
    reason = "phase completion not repaired: implement"
    monkeypatch.setattr(sup, "make_launch_fn", lambda *a, **k: (lambda t: None))
    monkeypatch.setattr(
        sup, "supervise",
        lambda *a, **k: sup.SuperviseResult("ABORTED", reason, 3),
    )
    code = cli.main(["run", "demo", "--project-root", str(project), "--halt-on-abort"])
    assert code == 2
    mem = project / ".workflow_artifacts" / "memory"
    text = (mem / "autonomous-halt-demo.md").read_text()
    assert f"reason: {reason}\n" in text
    assert sup.read_halt("demo", project.resolve()) == reason
