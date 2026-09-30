"""Child session tracking for the autonomous supervisor: argv/text builders,
the Popen launch path and the tracked-launch wrapper. No real claude process
is started; subprocess.Popen is replaced by a scripted fake."""
from __future__ import annotations

import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from quoin import supervisor as sup

U = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"


# ---------------------------------------------------------------------------
# argv / text
# ---------------------------------------------------------------------------


def test_argv_with_session_id_sits_before_output_format():
    argv = sup.build_relaunch_argv("demo", session_id=U)
    i = argv.index("--session-id")
    assert argv[i + 1] == U
    assert argv[-2:] == ["--output-format", "text"]
    assert i == len(argv) - 4


def test_argv_without_session_id_is_unchanged():
    argv = sup.build_relaunch_argv("demo")
    assert argv == [
        "claude", "-p", "/run --resume --autonomous demo",
        "--allowedTools", *sup.DEFAULT_ALLOWED_TOOLS,
        "--output-format", "text",
    ]
    argv = sup.build_relaunch_argv("demo", permission_mode="bypassPermissions")
    assert argv == [
        "claude", "-p", "/run --resume --autonomous demo",
        "--dangerously-skip-permissions", "--output-format", "text",
    ]


def test_takeover_command_round_trips_awkward_cwd():
    cwd = "/tmp/it's a dir"
    cmd = sup.takeover_command(cwd, U)
    assert shlex.split(cmd) == ["cd", cwd, "&&", "claude", "--resume", U]


def test_pointer_and_hint_forms():
    p = sup.takeover_pointer("demo", "/a/b")
    assert p.startswith("quoin run --takeover demo")
    assert "--project-root /a/b" in p
    assert sup.takeover_hint("demo", "/a/b", U) == f"child_session={U} {p}"
    assert sup.takeover_hint("demo", "/a/b", None) == p
    assert sup.takeover_hint("demo", "/a/b", "../x") == p


@pytest.mark.parametrize("root", ["/a/b", "/My Drive/x"])
def test_notice_pointer_full_form(root):
    assert "--project-root" in sup.takeover_notice_pointer("demo", root)


@pytest.mark.parametrize("root", ["/it's/here", "/a\\b", "/a  b"])
def test_notice_pointer_bare_form(root):
    assert sup.takeover_notice_pointer("demo", root) == "quoin run --takeover demo"
    assert "--project-root" in sup.takeover_pointer("demo", root)


def test_is_child_session_id():
    assert sup.is_child_session_id(U)
    assert sup.is_child_session_id(str(uuid.uuid4()))
    assert not sup.is_child_session_id(str(uuid.uuid1()))
    assert not sup.is_child_session_id(U.upper())
    assert not sup.is_child_session_id("{" + U + "}")
    assert not sup.is_child_session_id("urn:uuid:" + U)
    assert not sup.is_child_session_id("../x")
    assert not sup.is_child_session_id("")
    assert not sup.is_child_session_id(None)


# ---------------------------------------------------------------------------
# Popen path
# ---------------------------------------------------------------------------


class FakePopen:
    instances = []
    script = {}

    def __init__(self, argv, **kwargs):
        if FakePopen.script.get("oserror"):
            raise OSError("no claude")
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = FakePopen.script.get("returncode", 0)
        self.log = []
        self.stdout = None
        self.stderr = None
        FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        self.log.append(("communicate", timeout))
        if len([c for c in self.log if c[0] == "communicate"]) > 1:
            raise AssertionError("drain called")
        exc = FakePopen.script.get("communicate_exc")
        if exc is not None:
            raise exc
        return FakePopen.script.get("out", ("out", "err"))

    def kill(self):
        self.log.append(("kill",))

    def wait(self, timeout=None):
        self.log.append(("wait", timeout))


@pytest.fixture
def popen(monkeypatch):
    FakePopen.instances = []
    FakePopen.script = {}

    def no_git(*a, **k):
        raise AssertionError("git resolution must not run when cwd is given")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(subprocess, "run", no_git)
    return FakePopen


def _launch(**kw):
    fn = sup.make_launch_fn("/proj", timeout=7)
    return fn("demo", session_id=U, cwd="/repo", on_spawn=kw.pop("on_spawn", lambda pid: None), **kw)


def test_popen_kwargs_and_argv(popen):
    _launch()
    p = popen.instances[0]
    assert p.kwargs["cwd"] == "/repo"
    assert p.kwargs["stdin"] == subprocess.DEVNULL
    assert p.kwargs["stdout"] == subprocess.PIPE
    assert p.kwargs["stderr"] == subprocess.PIPE
    assert p.kwargs["text"] is True
    assert "/run --resume --autonomous demo" in p.argv
    assert "--allowedTools" in p.argv
    assert p.argv[p.argv.index("--session-id") + 1] == U


def test_popen_bypass_mode(popen):
    fn = sup.make_launch_fn("/proj", permission_mode="bypassPermissions")
    fn("demo", session_id=U, cwd="/repo", on_spawn=lambda pid: None)
    assert "--dangerously-skip-permissions" in popen.instances[0].argv


def test_popen_nonzero_exit_surfaced(popen):
    popen.script = {"returncode": 3, "out": ("o", "e")}
    r = _launch()
    assert (r.returncode, r.stdout, r.stderr, r.timed_out) == (3, "o", "e", False)


def test_popen_timeout_kills_without_drain(popen):
    popen.script = {"communicate_exc": subprocess.TimeoutExpired("c", 7, output="partial", stderr="e")}
    r = _launch()
    assert r.timed_out and r.returncode == -1
    assert r.stdout == "partial"
    assert r.stderr == "e\n[launch timed out]"
    names = [c[0] for c in popen.instances[0].log]
    assert names == ["communicate", "kill", "wait"]
    assert popen.instances[0].log[0] == ("communicate", 7)


def test_popen_timeout_bytes_output_is_dropped(popen):
    popen.script = {"communicate_exc": subprocess.TimeoutExpired("c", 7, output=b"x", stderr=None)}
    r = _launch()
    assert r.stdout == "" and r.stderr == "\n[launch timed out]"


def test_popen_oserror_no_spawn_hook(popen):
    popen.script = {"oserror": True}
    calls = []
    r = _launch(on_spawn=calls.append)
    assert r.returncode == -1 and r.stderr == "no claude"
    assert calls == []


def test_on_spawn_called_once_before_communicate(popen):
    order = []
    orig = FakePopen.communicate

    def spy(self, timeout=None):
        order.append("communicate")
        return orig(self, timeout)

    popen.communicate = spy
    try:
        _launch(on_spawn=lambda pid: order.append(("spawn", pid)))
    finally:
        popen.communicate = orig
    assert order == [("spawn", 4242), "communicate"]


def test_on_spawn_exception_is_swallowed(popen):
    def boom(pid):
        raise RuntimeError("x")

    r = _launch(on_spawn=boom)
    assert r.returncode == 0 and r.stdout == "out"


def test_on_spawn_systemexit_kills_child_and_propagates(popen):
    def sig(pid):
        raise SystemExit(143)

    with pytest.raises(SystemExit):
        _launch(on_spawn=sig)
    names = [c[0] for c in popen.instances[0].log]
    assert names == ["kill", "wait"]


def test_communicate_keyboardinterrupt_kills_and_reraises(popen):
    popen.script = {"communicate_exc": KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        _launch()
    assert [c[0] for c in popen.instances[0].log] == ["communicate", "kill", "wait"]


def test_capability_tag():
    assert sup.make_launch_fn("/p").supports_child_tracking is True


# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------


class TaggedBase:
    supports_child_tracking = True

    def __init__(self, on_call=None):
        self.calls = []
        self.on_call = on_call

    def __call__(self, task, **kw):
        self.calls.append((task, kw))
        if self.on_call:
            self.on_call(task, kw)
        return sup.LaunchResult(0)


def _tracker(base, **kw):
    seq = iter(f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 50))
    kw.setdefault("id_fn", lambda: next(seq))
    kw.setdefault("repo_root_fn", lambda root: Path("/repo"))
    kw.setdefault("halt_fn", lambda t, r: None)
    kw.setdefault("now_fn", lambda: "TS")
    return sup.make_tracked_launch_fn("demo", "/proj", base, **kw)


def test_untagged_bases_get_task_only():
    seen = []
    fn = _tracker(lambda t: seen.append(t))
    fn("demo")

    def one_arg(t):
        seen.append(("one", t))

    recs = []
    fn2 = _tracker(one_arg, record_fn=lambda e, f: recs.append(e))
    fn2("demo")
    assert seen == ["demo", ("one", "demo")]
    assert recs == [] and fn.last is None and fn2.last is None


def test_tagged_base_gets_recorded_id_before_call():
    recs = []
    roots = []

    def root(r):
        roots.append(r)
        return Path("/repo")

    def check(task, kw):
        assert recs == [(sup.ChildLaunch(kw["session_id"], "/repo", 1, "TS"), True)]
        assert kw["cwd"] == "/repo"

    base = TaggedBase(check)
    fn = _tracker(base, record_fn=lambda e, f: recs.append((e, f)), repo_root_fn=root)
    fn("demo")
    assert len(roots) == 1 and len(base.calls) == 1
    assert fn.last.session_id == base.calls[0][1]["session_id"]


def test_first_session_id_used_once_and_invalid_ignored():
    base = TaggedBase()
    fn = _tracker(base, first_session_id=U)
    fn("demo")
    fn("demo")
    assert base.calls[0][1]["session_id"] == U
    assert base.calls[1][1]["session_id"] != U
    base2 = TaggedBase()
    fn2 = _tracker(base2, first_session_id="not-a-uuid")
    fn2("demo")
    assert base2.calls[0][1]["session_id"].endswith("000000000001")


def test_real_supervise_three_relaunches_distinct_ids(tmp_path):
    recs = []
    progress = tmp_path / ".workflow_artifacts/memory/autonomous-progress-demo"
    progress.mkdir(parents=True)
    n = {"i": 0}

    def write_done(task, kw):
        n["i"] += 1
        (progress / f"p{n['i']}.done").write_text("")

    class Clock:
        @staticmethod
        def sleep(s):
            pass

    base = TaggedBase(write_done)
    fn = sup.make_tracked_launch_fn(
        "demo", tmp_path, base, repo_root_fn=lambda r: Path("/repo"),
        record_fn=lambda e, f: recs.append(e) if f else None,
    )
    res = sup.supervise("demo", tmp_path, launch_fn=fn, max_relaunch=3, clock=Clock())
    assert res.status == "ABORTED"
    ids = [c[1]["session_id"] for c in base.calls]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert all(sup.is_child_session_id(i) for i in ids)
    assert recs[-1] == fn.last and recs[-1].session_id == ids[-1]


def test_halt_before_launch_skips():
    base = TaggedBase()
    recs = []
    fn = _tracker(base, halt_fn=lambda t, r: "stop", record_fn=lambda e, f: recs.append(e))
    r = fn("demo")
    assert fn.skipped == 1 and base.calls == [] and recs == [] and fn.last is None
    assert r.returncode == -1 and "skipped" in r.stderr


def test_halt_between_record_and_spawn_reverts():
    base = TaggedBase()
    recs = []
    answers = iter([None, "stop"])
    fn = _tracker(base, halt_fn=lambda t, r: next(answers), record_fn=lambda e, f: recs.append((e, f)))
    fn("demo")
    assert base.calls == [] and fn.skipped == 1 and fn.last is None
    assert [f for _, f in recs] == [True, False]
    assert recs[1][0] is None


def test_halt_revert_restores_previous_launch():
    base = TaggedBase()
    recs = []
    answers = iter([None, None, None, "stop"])
    fn = _tracker(base, halt_fn=lambda t, r: next(answers), record_fn=lambda e, f: recs.append((e, f)))
    fn("demo")
    first = fn.last
    fn("demo")
    assert fn.last is first
    assert recs[-1] == (first, False)


def test_record_fn_failure_modes():
    base = TaggedBase()

    def boom(e, f):
        raise RuntimeError("x")

    fn = _tracker(base, record_fn=boom)
    fn("demo")
    assert len(base.calls) == 1

    def sysexit(e, f):
        raise SystemExit(1)

    fn2 = _tracker(TaggedBase(), record_fn=sysexit)
    with pytest.raises(SystemExit):
        fn2("demo")


def test_on_pid_fn_receives_launch_sid():
    pids = []

    class Spawning(TaggedBase):
        def __call__(self, task, **kw):
            kw["on_spawn"](99)
            return super().__call__(task, **kw)

    base = Spawning()
    fn = _tracker(base, on_pid_fn=lambda sid, pid: pids.append((sid, pid)))
    fn("demo")
    assert pids == [(fn.last.session_id, 99)]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

import json
import os

from quoin import cli

RS_KEYS = ("child_session_id", "child_cwd", "child_started_at")


def _mem(project):
    return project / ".workflow_artifacts" / "memory"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (_mem(root)).mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sup._RealClock, "sleep", staticmethod(lambda s: None))
    monkeypatch.delenv("QUOIN_FIRST_CHILD_SESSION_ID", raising=False)
    monkeypatch.delenv("QUOIN_SUPERVISOR_LOCK_TOKEN", raising=False)


def _active_record(project, task="demo"):
    mem = _mem(project)
    rec = {
        "schema": 1, "task": task, "session_id": "s", "active": True, "phase": "implement",
        "phase_index": 3, "subphase": "", "step": "", "at_stage_boundary": False, "route": "",
        "profile": "", "artifacts": [], "next_action": "", "resume_command": f"/run --resume {task}",
        "notes_path": str(mem / f"run-notes-{task}.md"), "updated_at": "2026-09-30T00:00:00+00:00",
    }
    (mem / f"run-state-{task}.json").write_text(json.dumps(rec))


def _lock(project):
    return _mem(project) / "run-supervisor-demo.pid"


class TaggedFake:
    """Tagged launcher that writes the done sentinel on call ``done_on``."""

    supports_child_tracking = True

    def __init__(self, project, done_on=1, pid=4242, inspect=None):
        self.project, self.done_on, self.pid, self.inspect = project, done_on, pid, inspect
        self.n = 0
        self.kwargs = []

    def __call__(self, task, **kw):
        self.n += 1
        self.kwargs.append(kw)
        if self.inspect:
            self.inspect(self.n, kw)
        kw["on_spawn"](self.pid)
        prog = _mem(self.project) / "autonomous-progress-demo"
        prog.mkdir(exist_ok=True)
        (prog / f"p{self.n}.done").write_text("x")
        if self.n == self.done_on:
            (_mem(self.project) / "autonomous-done-demo.md").write_text("done")
        return sup.LaunchResult(0)


def _run(project, monkeypatch, fake, *extra):
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: fake)
    return cli.main(["run", "--autonomous", "demo", "--project-root", str(project), *extra])


def test_one_arg_fake_launcher_records_nothing(project, monkeypatch):
    _active_record(project)
    calls = []
    monkeypatch.setattr(sup, "make_launch_fn",
                        lambda project_root, permission_mode=None: (lambda t: calls.append(t) or (
                            _mem(project) / "autonomous-done-demo.md").write_text("d")))
    assert cli.main(["run", "--autonomous", "demo", "--project-root", str(project)]) == 0
    assert calls == ["demo"]
    assert "child_" not in (_mem(project) / "run-state-demo.json").read_text()


def test_end_to_end_records_child_before_launch(project, monkeypatch):
    _active_record(project)
    monkeypatch.setenv("QUOIN_FIRST_CHILD_SESSION_ID", U)
    monkeypatch.setenv("QUOIN_SUPERVISOR_LOCK_TOKEN", "tok")
    _lock(project).write_text(json.dumps(
        {"pid": os.getpid(), "started_at": "x", "granted": 5, "writer": "handoff", "token": "tok"}))
    seen = {}

    def inspect(n, kw):
        rec = json.loads((_mem(project) / "run-state-demo.json").read_text())
        lock = json.loads(_lock(project).read_text())
        seen["rec"], seen["lock"], seen["kw"] = rec, lock, kw
        seen["notes"] = (_mem(project) / "run-notes-demo.md").read_text()

    fake = TaggedFake(project, inspect=inspect)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: fake)
    lock_after = {}
    orig = cli._release_supervisor_lock

    def spy(lock_path, pid):
        lock_after.update(json.loads(lock_path.read_text()))
        orig(lock_path, pid)

    monkeypatch.setattr(cli, "_release_supervisor_lock", spy)
    assert cli.main(["run", "--autonomous", "demo", "--project-root", str(project)]) == 0
    assert seen["rec"]["child_session_id"] == U
    assert seen["lock"]["child_session_id"] == U
    assert seen["lock"]["child_cwd"] == seen["kw"]["cwd"]
    assert f"[quoin-autonomous-child] task=demo launch=1 session={U}" in seen["notes"]
    assert "quoin run --takeover demo --project-root" in seen["notes"]
    assert lock_after["child_pid"] == 4242
    assert "QUOIN_FIRST_CHILD_SESSION_ID" not in os.environ


def test_second_launch_clears_then_sets_pid(project, monkeypatch):
    _active_record(project)
    pids_at_record = []

    def inspect(n, kw):
        pids_at_record.append(json.loads(_lock(project).read_text()).get("child_pid"))

    fake = TaggedFake(project, done_on=2, inspect=inspect)
    assert _run(project, monkeypatch, fake) == 0
    assert fake.n == 2
    assert pids_at_record == [None, None]
    ids = [k["session_id"] for k in fake.kwargs]
    assert ids[0] != ids[1]


def test_notes_pointer_for_space_and_apostrophe_roots(tmp_path, monkeypatch):
    for parts, full in ((("My Drive", "p"), True), (("it's",), False)):
        root = tmp_path.joinpath(*parts)
        _mem(root).mkdir(parents=True)
        _active_record(root)
        assert _run(root, monkeypatch, TaggedFake(root)) == 0
        line = [l for l in (_mem(root) / "run-notes-demo.md").read_text().splitlines()
                if "quoin-autonomous-child" in l][0]
        assert ("--project-root" in line) is full
        if not full:
            assert line.rstrip().endswith("takeover: quoin run --takeover demo")


def test_halt_revert_appends_no_note(project, monkeypatch):
    _active_record(project)
    calls = {"n": 0}
    real_read_halt = sup.read_halt

    def halt_fn(task, root):
        calls["n"] += 1
        return "stop" if calls["n"] >= 2 else None

    monkeypatch.setattr(sup, "read_halt", real_read_halt)
    fake = TaggedFake(project)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: fake)
    orig = sup.make_tracked_launch_fn
    monkeypatch.setattr(sup, "make_tracked_launch_fn",
                        lambda *a, **k: orig(*a, halt_fn=halt_fn, **k))
    cli.main(["run", "--autonomous", "demo", "--project-root", str(project)])
    notes = (_mem(project) / "run-notes-demo.md").read_text() if (_mem(project) / "run-notes-demo.md").exists() else ""
    assert fake.n == 0
    assert notes.count("[quoin-autonomous-child]") == 1
    assert "child_" not in (_mem(project) / "run-state-demo.json").read_text()


def test_core_script_unavailable_still_runs(project, monkeypatch, capsys):
    _active_record(project)
    monkeypatch.setattr(cli, "_load_core_script", lambda name: None)
    seen = {}
    fake = TaggedFake(project, inspect=lambda n, kw: seen.update(json.loads(_lock(project).read_text())))
    assert _run(project, monkeypatch, fake) == 0
    assert seen["child_session_id"] == fake.kwargs[0]["session_id"]
    assert "run-state module unavailable" in capsys.readouterr().err


def test_invalid_env_id_is_ignored(project, monkeypatch, capsys):
    _active_record(project)
    monkeypatch.setenv("QUOIN_FIRST_CHILD_SESSION_ID", "not-a-uuid")
    fake = TaggedFake(project)
    assert _run(project, monkeypatch, fake) == 0
    assert fake.kwargs[0]["session_id"] != "not-a-uuid"
    assert sup.is_child_session_id(fake.kwargs[0]["session_id"])
    assert "ignoring invalid QUOIN_FIRST_CHILD_SESSION_ID" in capsys.readouterr().err


def test_takeover_and_autonomous_conflict(project):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--takeover", "--autonomous", "demo", "--project-root", str(project)])
    assert exc.value.code == 2


def test_update_supervisor_lock_refuses_foreign_lock(project):
    paths = cli._supervisor_paths(project, "demo")
    _lock(project).write_text(json.dumps({"pid": 2**22 + 999, "token": "a"}))
    assert cli._update_supervisor_lock(paths, None, child_pid=1) is False
    assert cli._update_supervisor_lock(paths, "b", child_pid=1) is False
    assert cli._update_supervisor_lock(paths, "a", child_pid=1) is True
    assert json.loads(_lock(project).read_text())["child_pid"] == 1
