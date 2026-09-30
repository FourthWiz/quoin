"""Tests for the T-04 supervisor single-driver lock and --halt-on-abort.

Covers `quoin run`'s lock acquisition (live-foreign-lock refusal, dead-pid
replacement, handoff-token adoption), the `.result`/halt-sentinel writers
gated on `--halt-on-abort`, and the SIGTERM/SIGINT abort path. Uses a fake
`launch_fn` (via a monkeypatched `supervisor.make_launch_fn`) and a no-op
clock, per the plan's own test recipe — no real `claude` subprocess or
real sleep is ever involved.

Does not attempt every branch in the architecture's exhaustive list — the
primary contracts below are what `/review` should treat as this task's
baseline.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from quoin import cli
from quoin import supervisor as sup

REPO_ROOT = Path(__file__).resolve().parents[3]
_AUTO_RESUME_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"


def _load_auto_resume():
    spec = importlib.util.spec_from_file_location("auto_resume_parity_check", _AUTO_RESUME_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The relaunch loop's backoff must never actually sleep in tests."""
    monkeypatch.setattr(sup._RealClock, "sleep", staticmethod(lambda seconds: None))


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _memory_dir(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "memory"


def _make_fake_launch_fn(project_root: Path, task: str, progress_calls: int = 0, write_done_on_call=None):
    """Returns a launch_fn(task) that:
    - writes one more `.done` completion sentinel per call, up to
      `progress_calls` times (then stalls), and/or
    - writes the done-sentinel (SUCCESS) on the given call number.
    """
    progress_dir = _memory_dir(project_root) / f"autonomous-progress-{task}"
    progress_dir.mkdir(parents=True, exist_ok=True)
    state = {"n": 0}

    def fn(t):
        state["n"] += 1
        if progress_calls and state["n"] <= progress_calls:
            (progress_dir / f"phase-{state['n']}.done").write_text("done\n")
        if write_done_on_call and state["n"] == write_done_on_call:
            (_memory_dir(project_root) / f"autonomous-done-{task}.md").write_text("done\n")

    fn.state = state
    return fn


def _patch_make_launch_fn(monkeypatch, fake_fn):
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: fake_fn)


def _lock_path(project_root: Path, task: str) -> Path:
    return _memory_dir(project_root) / f"run-supervisor-{task}.pid"


def _result_path(project_root: Path, task: str) -> Path:
    return _memory_dir(project_root) / f"run-supervisor-{task}.result"


def _halt_path(project_root: Path, task: str) -> Path:
    return _memory_dir(project_root) / f"autonomous-halt-{task}.md"


# ---------------------------------------------------------------------------
# Template parity (D-06/D-19)
# ---------------------------------------------------------------------------


def test_lock_result_halt_templates_match_auto_resume():
    ar = _load_auto_resume()
    assert cli._SUPERVISOR_LOCK_TEMPLATE == ar.LOCK_TEMPLATE
    assert cli._SUPERVISOR_RESULT_TEMPLATE == ar.RESULT_TEMPLATE
    assert cli._SUPERVISOR_HALT_TEMPLATE == ar.HALT_TEMPLATE


# ---------------------------------------------------------------------------
# No-flag behavior stays as before
# ---------------------------------------------------------------------------


def test_no_flag_success_no_result_no_halt(monkeypatch, project, capsys):
    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 0
    assert not _result_path(project, "demo").exists()
    assert not _halt_path(project, "demo").exists()
    assert not _lock_path(project, "demo").exists()  # released
    out = capsys.readouterr().out
    assert "quoin run: SUCCESS" in out
    assert "task: demo" in out


# ---------------------------------------------------------------------------
# --halt-on-abort: relaunch cap / no-progress / success
# ---------------------------------------------------------------------------


def test_halt_on_abort_relaunch_cap_writes_halt_and_result(monkeypatch, project):
    fn = _make_fake_launch_fn(project, "demo", progress_calls=100)  # always progresses
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(
        [
            "run", "--autonomous", "demo",
            "--project-root", str(project),
            "--halt-on-abort", "--max-relaunch", "3",
        ]
    )

    assert code == 2
    assert fn.state["n"] == 3  # launches == max_relaunch
    halt_text = _halt_path(project, "demo").read_text()
    assert "reason: relaunch cap" in halt_text
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["status"] == "ABORTED"
    assert result["relaunches"] == 3
    assert not _lock_path(project, "demo").exists()


def test_halt_on_abort_no_forward_progress(monkeypatch, project):
    fn = _make_fake_launch_fn(project, "demo", progress_calls=0)  # never progresses
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(
        [
            "run", "--autonomous", "demo",
            "--project-root", str(project),
            "--halt-on-abort", "--max-relaunch", "10",
        ]
    )

    assert code == 2
    # supervise() detects the stall one relaunch-count behind the true
    # launch count (D-06's accounting note) — the counting wrapper is what
    # gives the .result file the true number of headless sessions spawned.
    assert fn.state["n"] == 2
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["relaunches"] == 2
    halt_text = _halt_path(project, "demo").read_text()
    assert "reason: no forward progress" in halt_text


def test_halt_on_abort_success_writes_result_no_halt(monkeypatch, project):
    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(
        [
            "run", "--autonomous", "demo",
            "--project-root", str(project),
            "--halt-on-abort",
        ]
    )

    assert code == 0
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["status"] == "SUCCESS"
    assert not _halt_path(project, "demo").exists()


def test_preexisting_halt_not_overwritten(monkeypatch, project):
    halt_path = _halt_path(project, "demo")
    original = "task: demo\nphase: implement\nreason: needs a human\ntimestamp: 2026-01-01T00:00:00Z\nresume_hint: /run --resume demo\n"
    halt_path.write_text(original)

    fn = _make_fake_launch_fn(project, "demo")
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(
        [
            "run", "--autonomous", "demo",
            "--project-root", str(project),
            "--halt-on-abort",
        ]
    )

    assert code == 1  # HALTED
    assert fn.state["n"] == 0  # launch_fn never called — halt was seen first
    assert halt_path.read_text() == original


# ---------------------------------------------------------------------------
# Single-driver lock (D-06/D-19)
# ---------------------------------------------------------------------------


def test_live_foreign_lock_refuses_with_exit_3(monkeypatch, project):
    lock_path = _lock_path(project, "demo")
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "x", "granted": 5, "writer": "cli"}))

    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 3
    assert fn.state["n"] == 0  # never launched
    # the foreign lock is left alone — it names a live pid, not ours
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_lock_create_race_refuses_instead_of_overwriting(project, monkeypatch):
    """MAJ reproduction: two processes can both find no live lock at read
    time. The final create uses O_CREAT|O_EXCL so only one of them can
    actually win — the loser must retry against the real winner and refuse,
    never silently overwrite the winner's lock with its own pid."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    lock_path = _lock_path(project, "demo")
    result_path = memory_dir / "run-supervisor-demo.result"

    winner_pid = os.getpid()  # our own pid is always alive
    winner_lock = {"pid": winner_pid, "started_at": "x", "granted": 9, "writer": "cli", "task": "demo"}

    real_read_json = cli._read_json
    calls = {"n": 0}

    def fake_read_json(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # simulate a read before the winner's lock existed
        return real_read_json(path)

    monkeypatch.setattr(cli, "_read_json", fake_read_json)

    # The concurrent winner has already created its lock on disk by the
    # time our own O_CREAT|O_EXCL create runs.
    lock_path.write_text(json.dumps(winner_lock))

    acquired, held_pid = cli._acquire_supervisor_lock(memory_dir, lock_path, result_path, "demo", 9, None)

    assert acquired is False
    assert held_pid == winner_pid
    assert json.loads(lock_path.read_text()) == winner_lock


def test_dead_pid_cli_lock_is_replaced_and_run_proceeds(monkeypatch, project):
    dead_pid = 2**30  # not a live pid on any real machine
    lock_path = _lock_path(project, "demo")
    lock_path.write_text(json.dumps({"pid": dead_pid, "started_at": "x", "granted": 5, "writer": "cli"}))

    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 0
    assert fn.state["n"] == 1


def test_dead_handoff_lock_orphaned_result_survives_when_run_writes_no_result(monkeypatch, project):
    dead_pid = 2**30
    lock_path = _lock_path(project, "demo")
    lock_path.write_text(
        json.dumps({"pid": dead_pid, "started_at": "x", "granted": 7, "writer": "handoff", "token": "abc"})
    )

    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    # No --halt-on-abort: this run's own success never writes a `.result`,
    # so the ORPHANED write from lock acquisition is what's left on disk.
    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 0
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["status"] == "ORPHANED"
    assert result["relaunches"] == 7


def test_handoff_lock_with_matching_token_is_adopted(monkeypatch, project):
    token = "sekret-token"
    lock_path = _lock_path(project, "demo")
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x", "granted": 5, "writer": "handoff", "token": token})
    )
    monkeypatch.setenv("QUOIN_SUPERVISOR_LOCK_TOKEN", token)

    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 0
    assert fn.state["n"] == 1
    # the token is popped right after the lock decision — never leaked
    # onward to any relaunch child this process itself might spawn.
    assert "QUOIN_SUPERVISOR_LOCK_TOKEN" not in os.environ


def test_handoff_lock_with_wrong_token_and_live_pid_refuses(monkeypatch, project):
    lock_path = _lock_path(project, "demo")
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x", "granted": 5, "writer": "handoff", "token": "correct"})
    )
    monkeypatch.setenv("QUOIN_SUPERVISOR_LOCK_TOKEN", "wrong")

    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert code == 3
    assert fn.state["n"] == 0
    assert "QUOIN_SUPERVISOR_LOCK_TOKEN" not in os.environ


def test_lock_token_env_popped_even_on_plain_run(monkeypatch, project):
    monkeypatch.setenv("QUOIN_SUPERVISOR_LOCK_TOKEN", "unused")
    fn = _make_fake_launch_fn(project, "demo", write_done_on_call=1)
    _patch_make_launch_fn(monkeypatch, fn)

    cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert "QUOIN_SUPERVISOR_LOCK_TOKEN" not in os.environ


# ---------------------------------------------------------------------------
# MAJ round-2 reproduction: lock creation atomicity + conditional stale
# removal (review-2 issue 2), cli.py side.
# ---------------------------------------------------------------------------


def test_create_lock_exclusive_never_publishes_a_half_written_file(project):
    """Empty-lock-window reproduction, at the primitive level: once
    `_create_lock_exclusive` returns True the file is already fully
    populated (never observable half-written), and no temp file survives."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    lock_path = _lock_path(project, "demo")
    payload = json.dumps({"pid": 123, "granted": 3, "writer": "cli"}).encode("utf-8") + b"\n"

    assert cli._create_lock_exclusive(lock_path, payload) is True
    assert lock_path.read_bytes() == payload
    leftovers = [p for p in memory_dir.iterdir() if p.name != lock_path.name]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_acquire_lock_refuses_fresh_empty_lock_instead_of_wedging(project):
    """CLI regression (review-2 issue 2c): the old O_CREAT|O_EXCL-then-
    `os.write` split could leave an unparseable lock behind (a crash,
    ENOSPC). Because the path already existed, every later O_CREAT|O_EXCL
    create failed too, so `_read_json` returning None made the old code
    treat it as "no lock" and still try to recreate it in place — refusing
    forever with pid -1 since the create itself kept failing. A fresh
    unparseable lock must be refused as ambiguous (ordinary LOCKED-style
    refusal), never mistaken for "no lock exists" or wedged permanently."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    result_path = _result_path(project, "demo")
    lock_path = _lock_path(project, "demo")
    lock_path.write_text("")  # empty — unparseable, and freshly written

    acquired, held_pid = cli._acquire_supervisor_lock(memory_dir, lock_path, result_path, "demo", 9, None)

    assert acquired is False
    assert held_pid == -1
    assert lock_path.read_text() == "", "a fresh empty lock must not be clobbered"


def test_acquire_lock_reclaims_old_empty_lock_instead_of_refusing_forever(project):
    """The CLI regression's actual failure mode: an empty lock old enough
    that it cannot still be mid-write is genuinely abandoned and must be
    reclaimable, or `quoin run --autonomous` refuses forever until a human
    deletes the file by hand."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    result_path = _result_path(project, "demo")
    lock_path = _lock_path(project, "demo")
    lock_path.write_text("")
    old_time = time.time() - (cli._STALE_UNPARSEABLE_LOCK_SECS + 5)
    os.utime(lock_path, (old_time, old_time))

    acquired, held_pid = cli._acquire_supervisor_lock(memory_dir, lock_path, result_path, "demo", 9, None)

    assert acquired is True
    assert held_pid is None
    written = json.loads(lock_path.read_text())
    assert written["pid"] == os.getpid()


def test_acquire_lock_never_reclaims_a_live_lock(project):
    """Stale-vs-live reproduction: a lock naming a pid that is still alive
    must never be reclaimed, even when it fails our own `_read_json` call
    (simulating a racing read against a concurrent creator's lock)."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    result_path = _result_path(project, "demo")
    lock_path = _lock_path(project, "demo")
    live_lock = {"pid": os.getpid(), "started_at": "x", "granted": 3, "writer": "cli"}
    lock_path.write_text(json.dumps(live_lock))

    acquired, held_pid = cli._acquire_supervisor_lock(memory_dir, lock_path, result_path, "demo", 9, None)

    assert acquired is False
    assert held_pid == os.getpid()
    assert json.loads(lock_path.read_text()) == live_lock


def test_concurrent_double_spawn_only_one_racer_wins(project, monkeypatch):
    """Concurrent double-spawn reproduction: two processes racing to
    reclaim the same dead-pid lock must not both go on to create their own
    — `_claim_lock_for_removal`'s atomic rename means only one of them can
    ever get the lock's content back, so the loser must see a fresh lock
    (whichever racer wins the subsequent create) rather than clobbering it."""
    memory_dir = project / ".workflow_artifacts" / "memory"
    result_path = _result_path(project, "demo")
    lock_path = _lock_path(project, "demo")
    dead_pid = 2**30
    lock_path.write_text(json.dumps({"pid": dead_pid, "started_at": "x", "granted": 5, "writer": "cli"}))

    first = cli._claim_lock_for_removal(lock_path)
    second = cli._claim_lock_for_removal(lock_path)

    assert first is not None and first["pid"] == dead_pid
    assert second is None
    assert not lock_path.exists()


def test_lock_removed_only_when_it_names_our_pid():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "run-supervisor-demo.pid"
        lock_path.write_text(json.dumps({"pid": 999999, "started_at": "x", "granted": 1, "writer": "cli"}))
        cli._release_supervisor_lock(lock_path, os.getpid())
        assert lock_path.exists()  # not ours — left alone

        lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": "x", "granted": 1, "writer": "cli"}))
        cli._release_supervisor_lock(lock_path, os.getpid())
        assert not lock_path.exists()


# ---------------------------------------------------------------------------
# Exceptions and signals under --halt-on-abort (D-20)
# ---------------------------------------------------------------------------


def test_launch_fn_raising_under_flag_writes_error_result_and_halt(monkeypatch, project):
    def _raiser(task, project_root, *, launch_fn, max_relaunch, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sup, "supervise", _raiser)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: (lambda t: None))

    with pytest.raises(RuntimeError):
        cli.main(["run", "--autonomous", "demo", "--project-root", str(project), "--halt-on-abort"])

    assert not _lock_path(project, "demo").exists()
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["status"] == "ERROR"
    assert "reason: supervisor error" in _halt_path(project, "demo").read_text()


def test_launch_fn_raising_without_flag_writes_nothing_but_releases_lock(monkeypatch, project):
    def _raiser(task, project_root, *, launch_fn, max_relaunch, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sup, "supervise", _raiser)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: (lambda t: None))

    with pytest.raises(RuntimeError):
        cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])

    assert not _lock_path(project, "demo").exists()
    assert not _result_path(project, "demo").exists()
    assert not _halt_path(project, "demo").exists()


def test_sigterm_under_flag_writes_stopped_result_and_halt(monkeypatch, project):
    def _fake_supervise(task, project_root, *, launch_fn, max_relaunch, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler should have raised SystemExit before this line")

    monkeypatch.setattr(sup, "supervise", _fake_supervise)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: (lambda t: None))

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--autonomous", "demo", "--project-root", str(project), "--halt-on-abort"])

    assert exc.value.code == 143
    assert not _lock_path(project, "demo").exists()
    result = json.loads(_result_path(project, "demo").read_text())
    assert result["status"] == "STOPPED"
    assert "reason: supervisor stopped by signal" in _halt_path(project, "demo").read_text()


# ---------------------------------------------------------------------------
# Takeover hints on abort halts and terminal output
# ---------------------------------------------------------------------------


def test_abort_halt_keeps_original_lines_and_adds_pointer(monkeypatch, project, capsys):
    fn = _make_fake_launch_fn(project, "demo", progress_calls=100)
    _patch_make_launch_fn(monkeypatch, fn)
    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project),
                     "--halt-on-abort", "--max-relaunch", "2"])
    assert code == 2
    lines = _halt_path(project, "demo").read_text().splitlines()
    assert lines[0] == "task: demo" and lines[1] == "phase: run"
    assert lines[2] == "reason: relaunch cap"
    assert lines[4] == "resume_hint: /run --resume demo"
    assert lines[5].startswith("takeover_hint: quoin run --takeover demo --project-root")
    assert "claude --resume" not in "\n".join(lines)
    assert "  takeover: quoin run --takeover demo --project-root" in capsys.readouterr().out


def test_refused_prints_takeover_pointer(monkeypatch, project, capsys):
    _lock_path(project, "demo").write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x", "granted": 5, "writer": "cli"}))
    _patch_make_launch_fn(monkeypatch, _make_fake_launch_fn(project, "demo"))
    assert cli.main(["run", "--autonomous", "demo", "--project-root", str(project)]) == 3
    assert "  takeover: quoin run --takeover demo --project-root" in capsys.readouterr().out


def test_write_abort_halt_never_overwrites(project):
    mem = _memory_dir(project)
    halt = _halt_path(project, "demo")
    assert cli._write_abort_halt(mem, halt, "demo", "first") is True
    before = halt.read_text()
    assert cli._write_abort_halt(mem, halt, "demo", "second", takeover_hint="x") is False
    assert halt.read_text() == before
