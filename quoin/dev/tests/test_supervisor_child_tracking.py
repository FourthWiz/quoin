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
