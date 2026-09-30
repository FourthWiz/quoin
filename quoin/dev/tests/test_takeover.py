"""quoin run --takeover: ordering, the confirm-dead gate and its fallbacks.

Every process, signal and file side effect the sequence performs goes
through a fake TakeoverOps over a fake process table. Real os.kill,
subprocess.run and Popen are trapped so a leaked real call fails the test."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from quoin import cli, takeover
from quoin import supervisor as sup

REPO_ROOT = Path(__file__).resolve().parents[3]
A = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
B = "1b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e9"
SUP, CHILD = 5000, 6000
TERM, KILL = signal.SIGTERM, signal.SIGKILL


@pytest.fixture(autouse=True)
def _trap_real_processes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real process side effect in a hermetic test")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _mem(root):
    return root / ".workflow_artifacts" / "memory"


class FakeOps:
    def __init__(self, procs=None, transcripts=(), halt_exists=False, scan=None,
                 repo=Path("/fallback/repo")):
        self.calls = []
        self.procs = procs or {}
        self.transcripts = set(transcripts)
        self.halt_exists = halt_exists
        self.scan_override = scan
        self.repo = repo
        self.clock = 0.0
        self.outs, self.errs = [], []

    def ops(self):
        return takeover.TakeoverOps(
            pid_alive=self.pid_alive, cmdline=self.cmdline, find_pids_with_arg=self.find,
            transcript_exists=self.transcript_exists, kill=self.kill, sleep=self.sleep,
            monotonic=lambda: self.clock, write_halt=self.write_halt, remove_arm=self.remove_arm,
            out=self.out, err=self.err, repo_root=lambda root: self.repo,
            read_halt_reason=lambda: "some reason",
        )

    def pid_alive(self, pid):
        return bool(self.procs.get(pid, {}).get("alive"))

    def cmdline(self, pid):
        self.calls.append(("cmdline", pid))
        return self.procs.get(pid, {}).get("cmdline")

    def find(self, sid):
        self.calls.append(("scan", sid))
        if self.scan_override == "none":
            return None
        return [p for p, d in self.procs.items()
                if d.get("alive") and f"--session-id {sid}" in (d.get("cmdline") or "")]

    def transcript_exists(self, sid):
        return sid in self.transcripts

    def kill(self, pid, sig):
        self.calls.append(("kill", pid, sig))
        d = self.procs.get(pid)
        if d and sig in d.get("dies_on", set()):
            d["alive"] = False

    def sleep(self, secs):
        self.clock += secs

    def write_halt(self, content):
        self.calls.append(("write_halt", content))
        return not self.halt_exists

    def remove_arm(self, sid):
        self.calls.append(("remove_arm", sid))

    def out(self, line):
        self.calls.append(("out", line))
        self.outs.append(line)

    def err(self, line):
        self.errs.append(line)
        self.calls.append(("err", line))

    def names(self):
        return [c[0] for c in self.calls]


def _sup_proc(dies_on=(TERM, KILL), task="demo"):
    return {"alive": True, "cmdline": f"python -c boot run --autonomous {task} --project-root /p",
            "dies_on": set(dies_on)}


def _child_proc(sid, dies_on=(TERM, KILL)):
    return {"alive": True,
            "cmdline": f"claude -p /run --resume --autonomous demo --session-id {sid} --output-format text",
            "dies_on": set(dies_on)}


def _write(root, lock=None, rec=None):
    mem = _mem(root)
    mem.mkdir(parents=True, exist_ok=True)
    if lock is not None:
        (mem / "run-supervisor-demo.pid").write_text(json.dumps(lock))
    if rec is not None:
        base = {"schema": 1, "task": "demo", "session_id": "s", "active": True, "phase": "implement"}
        base.update(rec)
        (mem / "run-state-demo.json").write_text(json.dumps(base))


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "proj"
    _mem(r).mkdir(parents=True)
    return r


def _go(root, fake, task="demo"):
    return takeover.run_takeover(task, root, fake.ops())


def _standard(root, **kw):
    _write(root,
           lock={"pid": SUP, "started_at": "2026-09-30T10:00:00Z", "child_session_id": A,
                 "child_cwd": "/work/repo", "child_pid": CHILD, "child_started_at": "2026-09-30T10:00:00Z"},
           rec={"child_session_id": A, "child_cwd": "/work/repo", "child_started_at": "2026-09-30T10:00:00Z"})
    return FakeOps(procs={SUP: _sup_proc(), CHILD: _child_proc(A)}, transcripts=[A], **kw)


def test_order_halt_then_kills_then_confirm_then_command(root):
    f = _standard(root)
    assert _go(root, f) == 0
    seq = [(c[0], c[1:]) for c in f.calls if c[0] in ("write_halt", "kill", "remove_arm", "out")]
    kinds = [k for k, _ in seq]
    assert kinds == ["write_halt", "kill", "kill", "remove_arm", "out"]
    assert seq[1][1] == (SUP, TERM) and seq[2][1] == (CHILD, TERM)
    assert "reason: taken over by user" in f.calls[0][1]
    assert f.outs == ["cd /work/repo && claude --resume " + A]
    last_scan = max(i for i, c in enumerate(f.calls) if c[0] == "scan")
    assert last_scan < f.names().index("remove_arm")


def test_child_ignoring_term_gets_kill(root):
    f = _standard(root)
    f.procs[CHILD] = _child_proc(A, dies_on=(KILL,))
    assert _go(root, f) == 0
    kills = [c for c in f.calls if c[0] == "kill" and c[1] == CHILD]
    assert [k[2] for k in kills] == [TERM, KILL]


def test_existing_halt_kept_and_reason_echoed(root):
    f = _standard(root, halt_exists=True)
    assert _go(root, f) == 0
    assert f.names().index("write_halt") < f.names().index("kill")
    assert any("halt already present: some reason" in e for e in f.errs)


def test_survivor_exits_4_and_prints_no_resume(root):
    f = _standard(root)
    f.procs[CHILD] = _child_proc(A, dies_on=())
    assert _go(root, f) == 4
    assert not any("claude --resume" in c[1] for c in f.calls if c[0] in ("out", "err"))
    assert any(str(CHILD) in e for e in f.errs)
    assert "remove_arm" not in f.names()


def test_diverged_stores_prefer_lock_id_and_scan_both(root):
    _write(root,
           lock={"pid": SUP, "started_at": "2026-09-30T10:00:00Z", "child_session_id": B,
                 "child_cwd": "/work/b", "child_pid": CHILD},
           rec={"child_session_id": A, "child_cwd": "/work/a"})
    f = FakeOps(procs={SUP: _sup_proc(), CHILD: _child_proc(B)}, transcripts=[A, B])
    assert _go(root, f) == 0
    assert ("kill", CHILD, TERM) in f.calls
    scans = [c[1] for c in f.calls if c[0] == "scan"]
    assert A in scans and B in scans
    assert f.outs == ["cd /work/b && claude --resume " + B]


def test_unverifiable_recorded_child_pid_exits_4(root):
    f = _standard(root)
    f.procs[CHILD]["cmdline"] = None
    assert _go(root, f) == 4
    assert not any("claude --resume" in x for x in f.outs)
    assert any("cannot verify recorded child pid" in e for e in f.errs)


def test_recorded_child_pid_dead_on_recheck_proceeds(root):
    f = _standard(root)
    f.procs[CHILD]["cmdline"] = None
    orig = f.cmdline

    def cmd(pid):
        r = orig(pid)
        if pid == CHILD:
            f.procs[CHILD]["alive"] = False
        return r

    f.cmdline = cmd
    assert _go(root, f) == 0


@pytest.mark.parametrize("task", ["../x", "a b", ""])
def test_invalid_task_touches_nothing(root, task):
    f = FakeOps()
    assert takeover.run_takeover(task, root, f.ops()) == 2
    assert f.calls == []


def test_scan_failure_exits_4(root):
    f = _standard(root, scan="none")
    assert _go(root, f) == 4
    assert f.outs == []


def test_non_matching_supervisor_never_signalled(root):
    f = _standard(root)
    f.procs[SUP]["cmdline"] = "vim notes.txt"
    assert _go(root, f) == 0
    assert not any(c[0] == "kill" and c[1] == SUP for c in f.calls)
    f2 = _standard(root)
    f2.procs[SUP]["cmdline"] = "python -c boot run --autonomous demo-2 --project-root /p"
    assert _go(root, f2) == 0
    assert not any(c[0] == "kill" and c[1] == SUP for c in f2.calls)


def test_supervisor_unverifiable_exits_4_unless_dead(root):
    f = _standard(root)
    f.procs[SUP]["cmdline"] = None
    assert _go(root, f) == 4
    f2 = _standard(root)
    f2.procs[SUP]["cmdline"] = None
    orig = f2.cmdline

    def cmd(pid):
        r = orig(pid)
        if pid == SUP:
            f2.procs[SUP]["alive"] = False
        return r

    f2.cmdline = cmd
    assert _go(root, f2) == 0


def test_supervisor_ignoring_term_gets_kill_after_grace(root):
    f = _standard(root)
    f.procs[SUP] = _sup_proc(dies_on=(KILL,))
    assert _go(root, f) == 0
    assert [c[2] for c in f.calls if c[0] == "kill" and c[1] == SUP] == [TERM, KILL]
    assert 5 <= f.clock < 60


def test_no_lock_child_dead_transcript_exists(root):
    _write(root, rec={"child_session_id": A, "child_cwd": "/work/repo"})
    f = FakeOps(transcripts=[A])
    assert _go(root, f) == 0
    assert f.names()[0] == "write_halt"
    assert f.outs == ["cd /work/repo && claude --resume " + A]


def test_no_child_recorded_exits_1(root):
    _write(root, rec={})
    f = FakeOps()
    assert _go(root, f) == 1
    assert "write_halt" in f.names()
    assert any("no child session recorded for demo" in e for e in f.errs)


def test_never_started_for_every_sid(root):
    _write(root, lock={"pid": 1, "child_session_id": B}, rec={"child_session_id": A})
    f = FakeOps()
    assert _go(root, f) == 1
    assert any(f"recorded child sessions {B}, {A} never started" in e for e in f.errs)
    assert f.outs == []


def test_lock_sid_without_transcript_falls_back_to_record_sid(root):
    _write(root, lock={"pid": 1, "child_session_id": B, "child_cwd": "/b"},
           rec={"child_session_id": A, "child_cwd": "/a"})
    f = FakeOps(transcripts=[A])
    assert _go(root, f) == 0
    assert f.outs == ["cd /a && claude --resume " + A]
    assert any(f"session {B} never started" in e for e in f.errs)


def test_cwd_chain(root):
    _write(root, lock={"pid": 1, "child_session_id": A, "child_cwd": '/we"ird'},
           rec={"child_session_id": A, "child_cwd": "/sanitized"})
    f = FakeOps(transcripts=[A])
    assert _go(root, f) == 0
    assert f.outs == ['cd \'/we"ird\' && claude --resume ' + A]
    _write(root, rec={"child_session_id": A})
    (_mem(root) / "run-supervisor-demo.pid").unlink()
    f2 = FakeOps(transcripts=[A], repo=Path("/fallback/repo"))
    assert _go(root, f2) == 0
    assert f2.outs == ["cd /fallback/repo && claude --resume " + A]


def test_candidate_filtering(root):
    other = 7000
    _write(root, lock={"pid": SUP, "child_session_id": A, "child_pid": other},
           rec={"child_session_id": A})
    f = FakeOps(procs={SUP: _sup_proc(), other: _child_proc(B),
                       8000: {"alive": True, "cmdline": f"unrelated {A} --session-id={A}",
                              "dies_on": {TERM}}},
                transcripts=[A])
    assert _go(root, f) == 0
    assert not any(c[0] == "kill" and c[1] in (other, 8000) for c in f.calls)


def test_previous_span_label(root):
    def run(child_started, lock_started):
        lock = {"pid": 1, "child_session_id": A, "child_started_at": child_started}
        if lock_started:
            lock["started_at"] = lock_started
        _write(root, lock=lock, rec={"child_session_id": A})
        f = FakeOps(transcripts=[A])
        assert _go(root, f) == 0
        return "\n".join(f.errs)

    assert "previous span" in run("2026-09-30T10:00:00Z", "2026-09-30T10:05:00+00:00")
    assert "previous span" not in run("2026-09-30T10:00:00Z", "2026-09-30T10:00:03Z")
    assert "previous span" not in run("2026-09-30T10:00:00Z", None)


def test_arm_file_removed_for_real(root):
    mem = _mem(root)
    _write(root, rec={"child_session_id": A})
    (mem / f"run-continue-arm-{A}.txt").write_text("")
    ops = takeover.real_ops("demo", root)
    ops.remove_arm(A)
    assert not (mem / f"run-continue-arm-{A}.txt").exists()
    ops.remove_arm(A)  # already gone: no error


def test_real_write_halt_never_overwrites(root):
    ops = takeover.real_ops("demo", root)
    assert ops.write_halt("first\n") is True
    assert ops.write_halt("second\n") is False
    assert (_mem(root) / "autonomous-halt-demo.md").read_text() == "first\n"
    assert not list(_mem(root).glob("*.tmp"))


def test_halt_text_shape(root):
    _write(root, rec={"child_session_id": A, "resume_command": "/run --resume demo"})
    f = FakeOps()
    _go(root, f)
    text = f.calls[0][1]
    lines = dict(l.split(": ", 1) for l in text.splitlines())
    assert lines["task"] == "demo" and lines["phase"] == "implement"
    assert lines["reason"] == "taken over by user"
    assert lines["takeover_hint"].startswith(f"child_session={A} quoin run --takeover demo")


def test_stand_down_after_halt(root, monkeypatch):
    monkeypatch.setattr(sup._RealClock, "sleep", staticmethod(lambda s: None))
    ops = takeover.real_ops("demo", root)
    assert ops.write_halt("task: demo\nreason: taken over by user\n") is True
    launched = []
    res = sup.supervise("demo", root, launch_fn=lambda t: launched.append(t))
    assert res.status == "HALTED" and launched == []
    spec = importlib.util.spec_from_file_location(
        "ar_takeover_test", REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py")
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)
    (_mem(root) / "autonomous-run-demo.marker").write_text(
        "task: demo\ntimestamp: 2026-09-29T00:00:00+00:00\nautonomous: true\n")
    _write(root, rec={"updated_at": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()})
    cands = ar._select_candidates(_mem(root), "start", "other-sid")
    assert ar._evaluate_gate(_mem(root), "start", cands)["action"] != "candidate"


def test_arm_for_taken_over_sid_clears_ended_marker(root, monkeypatch):
    """Attaching interactively ends with a session-ended marker for the
    resumed session; a later arm for that session removes it, so the marker
    is a harmless side effect of the takeover."""
    spec = importlib.util.spec_from_file_location(
        "ar_takeover_arm", REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py")
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)
    mem = _mem(root)
    (mem / "autonomous-run-demo.marker").write_text(
        "task: demo\ntimestamp: 2026-09-29T00:00:00+00:00\nautonomous: true\n")
    _write(root, rec={})
    marker = mem / f"session-ended-{A}.txt"
    marker.write_text("")

    class Args:
        project_root = str(root)
        task = "demo"
        session_id = A

    assert ar._cmd_arm(Args()) == 0
    assert not marker.exists()
    assert (mem / f"run-continue-arm-{A}.txt").exists()


def test_real_ps_calls_use_wide_output(monkeypatch):
    calls = []

    class R:
        returncode = 0
        stdout = "  10 claude --session-id X\n"

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or R())
    takeover._real_cmdline(10)
    takeover._real_find_pids_with_arg("X")
    assert all("-ww" in argv for argv in calls) and len(calls) == 2


def test_ps_parser():
    p = takeover.parse_ps_pids_with_arg
    assert p("10 claude -p x --session-id S --out\n11 claude --session-id=S\n", "S", 999) == [10]
    assert p("10 claude --session-id S\n", "S", 10) == []
    assert p("garbage line\n", "S", 1) is None
    assert p("", "S", 1) is None


def test_ps_failures_return_none(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", missing)
    assert takeover._real_find_pids_with_arg("S") is None

    class Bad:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Bad())
    assert takeover._real_find_pids_with_arg("S") is None
    assert takeover._real_cmdline(1) is None


def test_transcript_exists_honours_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "cfg" / "projects" / "p1"
    d.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert takeover._real_transcript_exists(A) is False
    (d / f"{A}.jsonl").write_text("")
    assert takeover._real_transcript_exists(A) is True
