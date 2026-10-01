"""Tests for child_watch.py: one watch window over a handed-off autonomous run.

Collaborators (clock, sleep, pid liveness, ps command line, HEAD probe) are
injected, so nothing here sleeps for real or spawns a process, apart from the
CLI subprocess checks at the end.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_RUN = subprocess.run

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "child_watch.py"
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "child_watch.py"

_spec = importlib.util.spec_from_file_location("child_watch_under_test", CORE_PATH)
cw = importlib.util.module_from_spec(_spec)
sys.modules["child_watch_under_test"] = cw
_spec.loader.exec_module(cw)

TASK = "demo-task"
SUP = 1111
CHILD = 2222
SID = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
SUP_CMD = "python3 -m quoin run --autonomous demo-task"
CHILD_CMD = "claude --session-id " + SID


class FakeDeps:
    def __init__(self):
        self.now = 1_000_000.0
        self.alive = {SUP, CHILD}
        self.cmds = {SUP: SUP_CMD, CHILD: CHILD_CMD}
        self.heads = (("repo", "a" * 40),)
        self.sleeps = []
        self.on_sleep = None
        self.raise_in_probe = False

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs
        if self.on_sleep:
            self.on_sleep(secs)

    def pid_alive(self, pid):
        return pid in self.alive

    def cmdline(self, pid):
        return self.cmds.get(pid)

    def probe(self, task, root):
        if self.raise_in_probe:
            raise RuntimeError("boom")
        return self.heads


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        cw.INTERVAL_KNOB,
        cw.STALL_KNOB,
        cw.MAX_HOURS_KNOB,
        cw.GRACE_KNOB,
    ):
        monkeypatch.delenv(name, raising=False)

    def no_spawn(*a, **k):
        raise AssertionError("subprocess.run must not be called in unit tests")

    monkeypatch.setattr(cw.subprocess, "run", no_spawn)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "proj"
    mem = root / ".workflow_artifacts" / "memory"
    mem.mkdir(parents=True)
    return root, mem


def _lock(mem, **fields):
    data = {"pid": SUP, "child_pid": CHILD, "child_session_id": SID}
    data.update(fields)
    data = {k: v for k, v in data.items() if v is not None}
    (mem / f"run-supervisor-{TASK}.pid").write_text(json.dumps(data), encoding="utf-8")


def _go(root, deps, *extra, window="60", poll="30", pid=SUP, once=False):
    argv = ["--project-root", str(root), "--task", TASK, "--window-secs", window, "--poll-secs", poll]
    if pid is not None:
        argv += ["--supervisor-pid", str(pid)]
    if once:
        argv.append("--once")
    argv += list(extra)
    return cw.run(argv, deps)


def _fields(line):
    parts = line.split("|")
    assert parts[0] == "WATCH" and parts[-1] == "observe-only"
    out = {"state": parts[1]}
    for p in parts[2:-1]:
        k, _, v = p.partition("=")
        out[k] = v
    assert len(parts) == 7, line
    return out


def _state(mem):
    return json.loads((mem / f"child-watch-{TASK}.json").read_text())


def _done_files(mem, n):
    d = mem / f"autonomous-progress-{TASK}"
    d.mkdir(exist_ok=True)
    for i in range(n):
        (d / f"phase{i}.done").write_text("x")


def _notes(mem):
    p = mem / f"run-notes-{TASK}.md"
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------- states


def test_alive_window(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    line, code = _go(root, d)
    f = _fields(line)
    assert (f["state"], code) == ("ALIVE", 0)
    assert "--supervisor-pid %d" % SUP in f["next"]


def test_done_mid_window_returns_early(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.on_sleep = lambda s: (mem / f"autonomous-done-{TASK}.md").write_text("done")
    line, code = _go(root, d, window="600")
    f = _fields(line)
    assert (f["state"], code) == ("DONE", 10)
    assert f["next"] == "report-and-stop"
    assert d.now < 1_000_000.0 + 600


def test_halted_reason_parsed(env):
    root, mem = env
    _lock(mem)
    (mem / f"autonomous-halt-{TASK}.md").write_text("task: x\nphase: y\nreason: no-progress\n")
    line, code = _go(root, FakeDeps())
    f = _fields(line)
    assert (f["state"], code) == ("HALTED", 10)
    assert "reason=no-progress" in f["detail"]


def test_halted_without_reason(env):
    root, mem = env
    _lock(mem)
    (mem / f"autonomous-halt-{TASK}.md").write_text("task: x\n")
    f = _fields(_go(root, FakeDeps())[0])
    assert "reason=unknown" in f["detail"]


def test_halt_reason_is_sanitized(env):
    root, mem = env
    _lock(mem)
    (mem / f"autonomous-halt-{TASK}.md").write_text('reason: a|b "c"   d\nother: |\n')
    f = _fields(_go(root, FakeDeps())[0])
    assert "|" not in f["detail"] and "  " not in f["detail"]


def test_needs_decision_new_file(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.on_sleep = lambda s: (mem / f"needs-decision-{TASK}.md").write_text("q")
    f = _fields(_go(root, d)[0])
    assert f["state"] == "NEEDS_DECISION"


def test_preexisting_needs_decision_not_reported_until_rewritten(env):
    root, mem = env
    _lock(mem)
    nd = mem / f"needs-decision-{TASK}.md"
    nd.write_text("old")
    os.utime(nd, (1000, 1000))
    d = FakeDeps()
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"
    os.utime(nd, (2000, 2000))
    assert _fields(_go(root, d)[0])["state"] == "NEEDS_DECISION"


def test_dead_without_lock(env):
    root, mem = env
    d = FakeDeps()
    line, code = _go(root, d)
    f = _fields(line)
    assert (f["state"], code) == ("DEAD", 10)
    assert cw.DEAD_GRACE_SECS in d.sleeps


def test_dead_grace_reread_prefers_late_halt(env):
    root, mem = env
    d = FakeDeps()
    d.on_sleep = lambda s: (mem / f"autonomous-halt-{TASK}.md").write_text("reason: late\n")
    f = _fields(_go(root, d)[0])
    assert f["state"] == "HALTED"


def test_expired(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    st = _state(mem)
    st["first_armed_at"] = d.now - 13 * 3600
    (mem / f"child-watch-{TASK}.json").write_text(json.dumps(st))
    line, code = _go(root, d)
    assert (_fields(line)["state"], code) == ("EXPIRED", 10)


def test_progress_by_count_and_heads(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    _done_files(mem, 1)
    f = _fields(_go(root, d)[0])
    assert f["state"] == "PROGRESS"
    d.heads = (("repo", "b" * 40),)
    assert _fields(_go(root, d)[0])["state"] == "PROGRESS"
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"


def test_stall_reported_once_then_rearmed_after_progress(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    states = [_fields(_go(root, d)[0])["state"] for _ in range(4)]
    assert states == ["ALIVE", "ALIVE", "STALL", "ALIVE"]
    _done_files(mem, 1)
    assert _fields(_go(root, d)[0])["state"] == "PROGRESS"
    states = [_fields(_go(root, d)[0])["state"] for _ in range(3)]
    assert states == ["ALIVE", "ALIVE", "STALL"]


def test_empty_fresh_probe_never_progress_and_keeps_heads(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    d.heads = ()
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"
    assert _state(mem)["baseline_heads"] == [["repo", "a" * 40]]


def test_empty_heads_baseline_persists_stall_and_expiry(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.heads = ()
    first = None
    states = []
    for _ in range(3):
        states.append(_fields(_go(root, d)[0])["state"])
        st = _state(mem)
        first = first or st["first_armed_at"]
        assert st["first_armed_at"] == first
        assert st["baseline_heads"] == []
    assert states == ["ALIVE", "ALIVE", "STALL"]
    d.now = first + 13 * 3600
    assert _fields(_go(root, d)[0])["state"] == "EXPIRED"


# -------------------------------------------------------------- liveness


def test_alive_pid_not_named_in_lock_is_dead(env):
    root, mem = env
    _lock(mem, pid=None, child_pid=None)
    d = FakeDeps()
    d.alive = {SUP, CHILD, 9999}
    assert _fields(_go(root, d)[0])["state"] == "DEAD"


def test_supervisor_cmdline_must_name_quoin(env):
    root, mem = env
    _lock(mem, child_pid=None)
    d = FakeDeps()
    d.cmds[SUP] = "python3 other.py demo-task"
    assert _fields(_go(root, d)[0])["state"] == "DEAD"


def test_supervisor_cmdline_must_name_task(env):
    root, mem = env
    _lock(mem, child_pid=None)
    d = FakeDeps()
    d.cmds[SUP] = "python3 -m quoin run other-task"
    assert _fields(_go(root, d)[0])["state"] == "DEAD"


def test_missing_ps_is_trusted(env):
    root, mem = env
    _lock(mem, child_pid=None)
    d = FakeDeps()
    d.cmds = {}
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"


def test_child_keyed_on_session_id(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.alive = {CHILD}
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"
    d.cmds[CHILD] = "claude --session-id " + SID2
    assert _fields(_go(root, d)[0])["state"] == "DEAD"


def test_supervisor_dead_child_alive_not_dead(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.alive = {CHILD}
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"


def test_lock_without_child_id_needs_claude_in_cmdline(env):
    root, mem = env
    _lock(mem, pid=None, child_session_id=None)
    d = FakeDeps()
    d.alive = {CHILD}
    d.cmds[CHILD] = "node something"
    assert _fields(_go(root, d)[0])["state"] == "DEAD"
    d.cmds[CHILD] = "claude -p"
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"


def test_replaced_supervisor_keeps_state_key(env):
    root, mem = env
    other = 4444
    _lock(mem, pid=other, child_pid=None)
    d = FakeDeps()
    d.alive = {other}
    d.cmds[other] = SUP_CMD
    lines = [_fields(_go(root, d, pid=SUP)[0]) for _ in range(2)]
    for f in lines:
        assert f["state"] == "ALIVE"
        assert "supervisor replaced pid=%d" % other in f["detail"]
        assert "--supervisor-pid %d" % SUP in f["next"]
    st = _state(mem)
    assert st["supervisor_pid"] == SUP


def test_relaunched_child_lock_id_wins(env):
    root, mem = env
    _lock(mem, pid=None, child_session_id=SID2)
    d = FakeDeps()
    d.alive = {CHILD}
    d.cmds[CHILD] = "claude --session-id " + SID2
    line, _ = _go(root, d, "--child-session", SID)
    f = _fields(line)
    assert f["state"] == "ALIVE"
    assert "child_session=%s " % SID2 in f["takeover"]
    d.cmds[CHILD] = "claude --session-id " + SID
    assert _fields(_go(root, d, "--child-session", SID)[0])["state"] == "DEAD"


def test_child_session_flag_is_pointer_fallback(env):
    root, mem = env
    _lock(mem, child_session_id=None)
    f = _fields(_go(root, FakeDeps(), "--child-session", SID)[0])
    assert "child_session=%s " % SID in f["takeover"]


# ----------------------------------------------------------------- state


def test_fresh_state_on_pid_change(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    first = _state(mem)["first_armed_at"]
    d.now += 100
    _go(root, d, pid=SUP + 1)
    assert _state(mem)["first_armed_at"] != first


def test_fresh_state_on_schema_mismatch_and_garbage(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    p = mem / f"child-watch-{TASK}.json"
    st = _state(mem)
    st["schema"] = 99
    p.write_text(json.dumps(st))
    d.now += 100
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"
    assert _state(mem)["schema"] == 1
    p.write_text("{not json")
    assert _fields(_go(root, d)[0])["state"] == "ALIVE"


@pytest.mark.parametrize(
    "field,value",
    [("no_change_windows", "3"), ("baseline_heads", "abc"), ("no_change_windows", True),
     ("stall_reported", 1), ("first_armed_at", "x"), ("baseline_done", False), ("nd_mtime", "x")],
)
def test_type_invalid_state_rebuilds(env, field, value):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    first = _state(mem)["first_armed_at"]
    st = _state(mem)
    st[field] = value
    (mem / f"child-watch-{TASK}.json").write_text(json.dumps(st))
    d.now += 100
    line, code = _go(root, d)
    assert code == 0 and _fields(line)["state"] in ("ALIVE", "PROGRESS")
    assert _state(mem)["first_armed_at"] != first


def test_malformed_head_entries_keep_state(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    first = _state(mem)["first_armed_at"]
    st = _state(mem)
    st["baseline_heads"] = [["repo", "a" * 40], 5, ["x"]]
    (mem / f"child-watch-{TASK}.json").write_text(json.dumps(st))
    d.now += 100
    _go(root, d)
    assert _state(mem)["first_armed_at"] == first


def test_no_change_windows_persist(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d)
    _go(root, d)
    assert _state(mem)["no_change_windows"] == 2


def test_once_never_writes(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    _go(root, d, once=True)
    assert not (mem / f"child-watch-{TASK}.json").exists()
    assert _notes(mem) == ""
    _go(root, d)
    before = (mem / f"child-watch-{TASK}.json").read_bytes()
    notes_before = _notes(mem)
    (mem / f"autonomous-halt-{TASK}.md").write_text("reason: r\n")
    _go(root, d, once=True)
    assert (mem / f"child-watch-{TASK}.json").read_bytes() == before
    assert _notes(mem) == notes_before


def test_once_after_run_ended(env):
    root, mem = env
    d = FakeDeps()
    (mem / f"autonomous-done-{TASK}.md").write_text("d")
    line, code = _go(root, d, once=True, pid=None)
    assert (_fields(line)["state"], code) == ("DONE", 10)
    (mem / f"autonomous-done-{TASK}.md").unlink()
    (mem / f"autonomous-halt-{TASK}.md").write_text("reason: r\n")
    assert _fields(_go(root, d, once=True, pid=None)[0])["state"] == "HALTED"
    (mem / f"autonomous-halt-{TASK}.md").unlink()
    line, code = _go(root, d, once=True, pid=None)
    assert (_fields(line)["state"], code) == ("DEAD", 10)


def test_once_honours_needs_decision_baseline(env):
    root, mem = env
    _lock(mem)
    nd = mem / f"needs-decision-{TASK}.md"
    nd.write_text("old")
    os.utime(nd, (1000, 1000))
    d = FakeDeps()
    _go(root, d)
    assert _fields(_go(root, d, once=True)[0])["state"] == "ALIVE"
    assert _fields(_go(root, FakeDeps(), once=True, pid=None)[0])["state"] == "ALIVE"


def test_once_live_lock_without_pid_arg(env):
    root, mem = env
    _lock(mem)
    line, code = _go(root, FakeDeps(), once=True, pid=None)
    assert (_fields(line)["state"], code) == ("ALIVE", 0)


def test_once_standing_stall_and_progress(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    for _ in range(3):
        _go(root, d)
    assert _state(mem)["stall_reported"] is True
    assert _fields(_go(root, d, once=True)[0])["state"] == "STALL"
    _done_files(mem, 1)
    assert _fields(_go(root, d, once=True)[0])["state"] == "PROGRESS"


# ---------------------------------------------------------------- output


def test_next_command_shape(env):
    root, mem = env
    _lock(mem)
    f = _fields(_go(root, FakeDeps())[0])
    toks = shlex.split(f["next"])
    assert toks[0] == "python3"
    assert toks[1] == str(WRAPPER_PATH)
    assert "--task" in toks and "--supervisor-pid" in toks
    assert "--child-session" not in f["next"]
    assert "--window-secs" not in f["next"] and "--poll-secs" not in f["next"]


def test_terminal_next_is_report_and_stop(env):
    root, mem = env
    f = _fields(_go(root, FakeDeps())[0])
    assert f["next"] == "report-and-stop"


def test_notes_on_terminal_and_first_stall_only(env):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    for _ in range(4):
        _go(root, d)
    assert _notes(mem).count(cw.NOTE_PREFIX) == 1
    (mem / f"autonomous-done-{TASK}.md").write_text("d")
    _go(root, d)
    assert _notes(mem).count(cw.NOTE_PREFIX) == 2


# ---------------------------------------------------------------- errors


def test_error_cases(env, tmp_path):
    root, mem = env
    d = FakeDeps()
    cases = [
        ["--project-root", str(root), "--task", "bad task!", "--supervisor-pid", "1"],
        ["--project-root", str(tmp_path / "nope"), "--task", TASK, "--supervisor-pid", "1"],
        ["--project-root", str(root) + "|x", "--task", TASK, "--supervisor-pid", "1"],
        ["--project-root", str(root), "--task", TASK, "--supervisor-pid", "abc"],
        ["--project-root", str(root), "--task", TASK, "--supervisor-pid", "-4"],
        ["--project-root", str(root), "--task", TASK],  # no pid, no lock
        ["--project-root", str(root)],  # argparse error
    ]
    for argv in cases:
        line, code = cw.run(argv, d)
        assert code == 2, argv
        f = _fields(line)
        assert f["state"] == "ERROR" and f["next"] == "report-and-stop"


def test_injected_exception_is_error_line(env, capsys):
    root, mem = env
    _lock(mem)
    d = FakeDeps()
    d.raise_in_probe = True
    line, code = _go(root, d)
    assert code == 2 and _fields(line)["state"] == "ERROR"
    assert "Traceback" not in line


# ------------------------------------------------------------------ knobs


def test_knob_clamps(monkeypatch):
    monkeypatch.setenv(cw.INTERVAL_KNOB, "5")
    assert cw._interval_secs() == 60
    monkeypatch.setenv(cw.INTERVAL_KNOB, "99999")
    assert cw._interval_secs() == 1500
    monkeypatch.setenv(cw.INTERVAL_KNOB, "junk")
    assert cw._interval_secs() == 600
    monkeypatch.setenv(cw.STALL_KNOB, "0")
    assert cw._stall_windows() == 1
    monkeypatch.setenv(cw.MAX_HOURS_KNOB, "500")
    assert cw._max_hours() == 72


# --------------------------------------------------- CLI, wrapper, py3.8

_SAFE_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "QUOIN_CHILD_WATCH_DEAD_GRACE_SECS": "0"}


def _cli(args, python=None, extra_env=None):
    env = dict(_SAFE_ENV)
    env.update(extra_env or {})
    return _REAL_RUN(
        [python or sys.executable, "-B", str(WRAPPER_PATH)] + args,
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_cli_wrapper_once_dead(tmp_path):
    (tmp_path / ".workflow_artifacts" / "memory").mkdir(parents=True)
    r = _cli(["--project-root", str(tmp_path), "--task", TASK, "--once"])
    assert r.returncode == 10, r.stderr
    assert r.stdout.count("\n") == 1 and r.stdout.startswith("WATCH|DEAD|")


def test_cli_invalid_task(tmp_path):
    r = _cli(["--project-root", str(tmp_path), "--task", "bad task", "--once"])
    assert r.returncode == 2
    assert r.stdout.count("\n") == 1 and r.stdout.startswith("WATCH|ERROR|")


def test_cli_argparse_error_is_one_line(tmp_path):
    r = _cli(["--bogus"])
    assert r.returncode == 2
    assert r.stdout.count("\n") == 1 and r.stdout.startswith("WATCH|ERROR|")


def test_ast_parses_as_py38():
    ast.parse(CORE_PATH.read_text(encoding="utf-8"), feature_version=(3, 8))


def test_future_annotations_present():
    tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _find_real_python38():
    candidates = []
    for name in ("python3.8",):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.append("/usr/bin/python3")
    found = shutil.which("python3")
    if found:
        candidates.append(found)
    for c in candidates:
        try:
            r = _REAL_RUN([c, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip() == "3 8":
            return c
    return None


def test_real_38_once_run(tmp_path):
    py = _find_real_python38()
    if py is None:
        pytest.skip("no real Python 3.8 interpreter found")
    (tmp_path / ".workflow_artifacts" / "memory").mkdir(parents=True)
    r = _cli(["--project-root", str(tmp_path), "--task", TASK, "--once"], python=py)
    assert r.returncode == 10, r.stderr
    assert r.stdout.startswith("WATCH|DEAD|")


def test_installer_registers_script():
    text = (REPO_ROOT / "src" / "quoin" / "installer.py").read_text(encoding="utf-8")
    assert text.count('"child_watch.py"') == 2
