"""Progress, repair and commit-probe rules in the auto-resume gate."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"
_NOW = datetime.now(timezone.utc)
SHA_A, SHA_B = "a" * 40, "b" * 40


def _iso(offset=0):
    return (_NOW - timedelta(seconds=offset)).isoformat()


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("auto_resume_progress_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_runtime_record_path", lambda: tmp_path / "absent.json")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("QUOIN_SUPERVISOR_REPAIR_RELAUNCHES", raising=False)
    monkeypatch.delenv("QUOIN_SUPERVISOR_HEAD_PROBE", raising=False)
    calls = []

    def _guard(argv, **kw):
        calls.append(argv)
        raise AssertionError(f"unexpected _popen: {argv!r}")

    monkeypatch.setattr(module, "_popen", _guard)
    yield module
    assert calls == []


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mem(project):
    return project / ".workflow_artifacts" / "memory"


def _setup(project, counter_edit=None, marker_file=None):
    m = _mem(project)
    (m / "autonomous-run-demo.marker").write_text(
        f"task: demo\ntimestamp: {_iso()}\nautonomous: true\n", encoding="utf-8"
    )
    (m / "run-state-demo.json").write_text(json.dumps({
        "schema": 1, "task": "demo", "session_id": "sid-1", "active": True,
        "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
        "at_stage_boundary": False, "route": "", "profile": "", "artifacts": [],
        "next_action": "", "resume_command": "/run --resume demo",
        "notes_path": str(m / "run-notes-demo.md"), "updated_at": _iso(),
    }), encoding="utf-8")
    (m / "run-continue-arm-sid-1.txt").touch()
    return m


def _counter(ar, m, **fields):
    c = ar._default_counter("demo", _iso())
    c["consecutive_no_progress"] = 1
    c["last_phase"] = ["implement", 3]
    # the marker file itself is already counted by the previous evaluation
    c["last_done_count"] = ar._count_done(m, "demo")
    c.update(fields)
    ar._write_counter(m, "demo", c)
    return c


def _marker_file(m):
    d = m / "autonomous-progress-demo"
    d.mkdir(exist_ok=True)
    (d / "implement.tasks.done").write_text("x")


def _stop(ar, project, monkeypatch, payload=None):
    data = json.dumps(payload or {"session_id": "sid-1"}).encode()

    class _Buf:
        def read(self, n):
            return data[:n]

    class _Stdin:
        buffer = _Buf()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    return ar._cmd_stop(_Args(project_root=str(project)))


def _no_probe(ar, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("probe must not run")

    monkeypatch.setattr(ar, "_probe_task_heads", boom)


def _load(m):
    return json.loads((m / "auto-resume-demo.json").read_text())


def test_stop_repair_then_halt(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _marker_file(m)
    _counter(ar, m)
    assert _stop(ar, project, monkeypatch) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    c = _load(m)
    assert c["repairs_used"] == 1 and c["consecutive_no_progress"] == 1
    # allowance exhausted -> distinct halt
    _counter(ar, m, repairs_used=2)
    assert _stop(ar, project, monkeypatch) == 0
    assert capsys.readouterr().out == ""
    assert "reason: phase completion not repaired: implement" in (
        m / "autonomous-halt-demo.md"
    ).read_text()


def test_stop_allowance_zero_halts_like_before(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _counter(ar, m)
    monkeypatch.setenv("QUOIN_SUPERVISOR_REPAIR_RELAUNCHES", "0")
    _stop(ar, project, monkeypatch)
    assert "reason: no forward progress" in (m / "autonomous-halt-demo.md").read_text()
    # with a marker present the timing is unchanged; only the reason differs
    (m / "autonomous-halt-demo.md").unlink()
    _marker_file(m)
    _counter(ar, m)
    _stop(ar, project, monkeypatch)
    assert "phase completion not repaired: implement" in (
        m / "autonomous-halt-demo.md"
    ).read_text()


def test_stop_never_probes_and_keeps_stored_heads(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _counter(ar, m, last_heads=[["quoin", SHA_A]], last_phase=["implement", 2])
    _no_probe(ar, monkeypatch)
    _stop(ar, project, monkeypatch)
    assert _load(m)["last_heads"] == [["quoin", SHA_A]]


def test_stop_cap_handoff_never_probes(ar, project, monkeypatch, capsys):
    m = _setup(project)
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")

    class _P:
        pid = 4242

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: _P())
    _counter(ar, m, last_heads=[["quoin", SHA_A]], last_phase=["implement", 2])
    _no_probe(ar, monkeypatch)
    monkeypatch.setenv("QUOIN_AUTO_RESUME_HANDOFF_AT", "1")
    _stop(ar, project, monkeypatch)
    assert _load(m)["last_heads"] == [["quoin", SHA_A]]


def test_start_never_probes_and_repair_applies(ar, project, monkeypatch, capsys):
    m = _setup(project)
    (m / "run-continue-arm-sid-1.txt").unlink()
    _marker_file(m)
    _counter(ar, m, last_heads=[["quoin", SHA_A]])
    _no_probe(ar, monkeypatch)
    monkeypatch.setattr(ar, "owner_state", lambda sid, md: "gone")
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")

    class _P:
        pid = 4242

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: _P())
    ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="sid-2"))
    c = _load(m)
    assert c["repairs_used"] == 1 and c["consecutive_no_progress"] == 1
    assert c["last_heads"] == [["quoin", SHA_A]]


def _handoff(ar, project):
    return ar._cmd_handoff(_Args(
        project_root=str(project), task="demo", reason="budget", on_fail_halt=None,
    ))


def _fake_spawn(ar, monkeypatch):
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")

    class _P:
        pid = 4242

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: _P())


def test_handoff_probes_once_and_counts_commit_as_progress(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _fake_spawn(ar, monkeypatch)
    _counter(ar, m, last_heads=[["quoin", SHA_A]])
    probes = []

    def probe(task, root, **kw):
        probes.append(task)
        return (("quoin", SHA_B),)

    monkeypatch.setattr(ar, "_probe_task_heads", probe)
    _handoff(ar, project)
    assert capsys.readouterr().out.startswith("HANDOFF|")
    c = _load(m)
    assert probes == ["demo"]
    assert c["consecutive_no_progress"] == 0
    assert c["last_heads"] == [["quoin", SHA_B]]


def test_handoff_early_returns_do_not_probe(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _fake_spawn(ar, monkeypatch)
    _no_probe(ar, monkeypatch)
    # opt-out
    monkeypatch.setenv("QUOIN_AUTO_RESUME", "0")
    _handoff(ar, project)
    assert capsys.readouterr().out.strip() == "DENIED|opt-out"
    monkeypatch.delenv("QUOIN_AUTO_RESUME")
    # cap
    monkeypatch.setenv("QUOIN_AUTO_RESUME_MAX", "10")
    _counter(ar, m, attempts=9)
    _handoff(ar, project)
    assert capsys.readouterr().out.strip() == "DENIED|cap"
    # live lock
    (m / "autonomous-halt-demo.md").unlink()
    _counter(ar, m)
    monkeypatch.setattr(ar, "_supervisor_lock_live", lambda md, t: True)
    _handoff(ar, project)
    assert capsys.readouterr().out.startswith("LOCKED|")


def test_empty_snapshots_are_not_progress(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _fake_spawn(ar, monkeypatch)
    _counter(ar, m, last_heads=[])
    monkeypatch.setattr(ar, "_probe_task_heads", lambda *a, **k: (("quoin", SHA_B),))
    assert ar._progress_outcome(m, "demo", ar._load_counter(m, "demo"), {"phase": "implement", "phase_index": 3}, (("quoin", SHA_B),))["progressed"] is False
    c = ar._load_counter(m, "demo")
    c["last_heads"] = [["quoin", SHA_A]]
    out = ar._progress_outcome(m, "demo", c, {"phase": "implement", "phase_index": 3}, ())
    assert out["progressed"] is False
    ar._apply_progress(c, out, True)
    assert c["last_heads"] == [["quoin", SHA_A]]


def test_heads_from_json_tolerates_garbage(ar):
    assert ar._heads_from_json(None) == ()
    assert ar._heads_from_json("x") == ()
    assert ar._heads_from_json([["a", "b"], ["c"], 3, [1, 2]]) == (("a", "b"),)


def test_probe_git_errors_do_not_escape_handoff(ar, project, monkeypatch, capsys):
    m = _setup(project)
    _fake_spawn(ar, monkeypatch)
    (project / ".git").mkdir()
    _counter(ar, m)

    def raiser(argv, **kw):
        raise ValueError("bad")

    monkeypatch.setattr(ar, "_git_run", raiser)
    assert ar._probe_task_heads("demo", project) == ()
    _handoff(ar, project)
    assert capsys.readouterr().out.startswith(("HANDOFF|", "DENIED|"))


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git missing")


def _git(d, *args):
    subprocess.run(
        ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _repo(d, branch):
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(d)], check=True)
    _git(d, "commit", "-q", "--allow-empty", "-m", "i")
    return d


@needs_git
def test_sibling_repo_change_is_not_progress(ar, tmp_path):
    root = tmp_path / "wrap"
    root.mkdir()
    alpha = _repo(root / "alpha", "feat/demo")
    beta = _repo(root / "beta", "main")
    before = ar._probe_task_heads("demo", root)
    assert [k for k, _ in before] == ["alpha"]
    _git(beta, "commit", "-q", "--allow-empty", "-m", "x")
    assert ar._probe_task_heads("demo", root) == before
    _git(alpha, "commit", "-q", "--allow-empty", "-m", "x")
    assert ar.heads_changed(before, ar._probe_task_heads("demo", root))


def test_counter_persistence_and_arm_reset(ar, project):
    m = _setup(project)
    (m / "run-continue-arm-sid-1.txt").unlink()
    old = ar._default_counter("demo", _iso())
    del old["last_heads"], old["repairs_used"]
    ar._write_counter(m, "demo", old)
    loaded = ar._load_counter(m, "demo")
    out = ar._progress_outcome(m, "demo", loaded, {"phase": "implement", "phase_index": 3}, ())
    assert out["repairs_used"] == 0
    c = _counter(ar, m, repairs_used=1, last_heads=[["q", SHA_A]])
    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))
    c = _load(m)
    assert c["repairs_used"] == 1 and c["last_heads"] == [["q", SHA_A]] and c["schema"] == 1
    (m / "run-continue-consent-sid-1.txt").touch()
    ar._cmd_arm(_Args(project_root=str(project), task="demo", session_id="sid-1", entry="resume"))
    c = _load(m)
    assert c["repairs_used"] == 0 and c["last_heads"] == []


def test_progress_resets_repairs(ar, project):
    m = _setup(project)
    _counter(ar, m, repairs_used=2, last_phase=["implement", 2])
    c = ar._load_counter(m, "demo")
    out = ar._progress_outcome(m, "demo", c, {"phase": "implement", "phase_index": 3}, ())
    assert out["progressed"] and out["repairs_used"] == 0 and out["streak"] == 0


def test_do_handoff_returns_denied_no_progress_on_repair_exhaustion(ar, project, monkeypatch):
    m = _setup(project)
    _marker_file(m)
    c = _counter(ar, m, repairs_used=2)
    res = ar._do_handoff(
        m, project, "demo", "budget", c,
        {"phase": "implement", "phase_index": 3, "session_id": "sid-1"}, heads=(),
    )
    assert res == "DENIED|no-progress"
    assert "phase completion not repaired: implement" in (m / "autonomous-halt-demo.md").read_text()
