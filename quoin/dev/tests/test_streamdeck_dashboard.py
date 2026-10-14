import json
import os
import subprocess
import sys
import time

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import (deck_home, forbid_dangerous_subprocess, load_frozen,  # noqa: F401
                                 payload)

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

C1 = "Tue Oct 13 12:16:19 2026"
C2 = "Tue Oct 13 13:00:00 2026"


def pane(i, title="x", **kw):
    base = dict(id=i, is_plugin=False, exited=False, title=title)
    base.update(kw)
    return base


class World(object):
    def __init__(self, payload, deck_home, monkeypatch):
        self.dash, self.mon, self.de = payload("dashboard", "monitor", "deckenv")
        self.home = deck_home
        self.env = self.de.current()
        self.sock = self.env.socket_dir
        self.sessions = {self.sock: (True, {"s": True})}
        self.panes = {(self.sock, "s"): (True, [pane(3)])}
        self.calls = []
        monkeypatch.setattr(self.dash, "list_sessions", lambda sock: self.sessions.get(sock, (False, {})))
        monkeypatch.setattr(self.dash, "list_panes", self._panes)
        self.mp = monkeypatch

    def _panes(self, sock, session):
        self.calls.append((sock, session))
        return self.panes.get((sock, session), (False, None))

    def write(self, name, layout="new", **fields):
        rec = dict(id=name, kind="claude", session="s", pane="3", socket=self.sock, socket_inferred=False,
                   status="working", pending={}, event="Stop", created=1.0, updated=time.time(),
                   pid=None, process_started="", via="managed", project="/p")
        rec.update(fields)
        if rec["pid"] is None:
            del rec["pid"], rec["process_started"]
        directory = self.env.agents_dir if layout == "new" else self.env.legacy_agents_dir
        self.de.ensure_dir(directory)
        path = os.path.join(directory, name + ".json")
        self.de.write_json_atomic(path, rec)
        return path

    def ps(self, body, tmp_path):
        script = tmp_path / ("ps%d" % len(list(tmp_path.glob("ps*"))))
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        self.mp.setattr(self.dash, "PS", str(script))
        return script

    def wrap_real_ps(self, tmp_path):
        log = tmp_path / "pslog"
        self.ps('printf "%%s|LC_ALL=%%s|PATH=%%s|LANG=%%s\\n" "$*" "$LC_ALL" "$PATH" "$LANG" >> "%s"\nexec /bin/ps "$@"\n' % log, tmp_path)
        return log

    def snap(self):
        return self.dash.snapshot()


@pytest.fixture
def w(payload, deck_home, monkeypatch):
    return World(payload, deck_home, monkeypatch)


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---------------------------------------------------------------- pid evidence

def test_invalid_pids_are_never_sent_and_never_cause_deletes(w, tmp_path):
    log = w.wrap_real_ps(tmp_path)
    live = w.mon.process_identity(os.getpid())
    w.write("live", pid=os.getpid(), process_started=live)
    w.write("big", pid=999999, process_started=C1, pane="4")
    w.write("str", pid="12a", process_started=C1, session="s", pane="5")
    w.panes[(w.sock, "s")] = (True, [pane(3), pane(4), pane(5)])
    out = w.snap()
    assert {r["id"] for r in out} == {"live", "big", "str"}
    argv = log.read_text().splitlines()
    assert len(argv) == 1 and argv[0].split("|")[0] == "-o pid=,lstart= -p %d" % os.getpid()


@pytest.mark.parametrize("rc,stdout,stderr,ok", [
    (1, "", "", True),
    (1, "", "ps: process id too large\n", False),
    (1, "", "Invalid process id\n", False),
    (2, "", "", False),
    (0, "garbage line without pid\n", "", False),
    (0, "99 " + C1 + "\n", "", False),
])
def test_ps_success_rule(w, tmp_path, rc, stdout, stderr, ok):
    w.ps("printf %%s '%s'\nprintf %%s '%s' >&2\nexit %d\n" % (stdout, stderr, rc), tmp_path)
    pid = dead_pid()
    result_ok, found = w.dash.pid_identities([pid])
    assert result_ok is ok and found == {}
    path = w.write("gone", pid=pid, process_started=C1)
    out = w.snap()
    assert (not os.path.exists(path)) is ok and (out == []) is ok


def test_ps_timeout_deletes_nothing(w, tmp_path):
    w.mp.setattr(w.dash, "PROBE_TIMEOUT", 0.2)
    w.ps("sleep 2\n", tmp_path)
    path = w.write("x", pid=dead_pid(), process_started=C1)
    assert [r["id"] for r in w.snap()] == ["x"] and os.path.exists(path)


def test_real_ps_dead_and_live(w):
    dead = dead_pid()
    ok, found = w.dash.pid_identities([os.getpid(), dead])
    assert ok and list(found) == [os.getpid()]
    assert w.dash.pid_identities([])[0] is True


def test_dead_pid_with_c_lstart_is_deleted(w):
    path = w.write("dead", pid=dead_pid(), process_started=C1)
    assert w.snap() == [] and not os.path.exists(path)


def test_empty_process_started_skips_pid_rule(w):
    path = w.write("e", pid=dead_pid(), process_started="")
    assert [r["id"] for r in w.snap()] == ["e"] and os.path.exists(path)


def test_c_shaped_different_lstart_is_deleted_but_other_shapes_kept(w):
    own = w.mon.process_identity(os.getpid())
    different = C2 if own != C2 else C1
    p1 = w.write("diff", pid=os.getpid(), process_started=different)
    w.write("gb", pid=os.getpid(), process_started="Tue 13 Oct 12:16:19 2026", pane="4")
    w.write("ru", pid=os.getpid(), process_started="вторник, 13 октября 2026 г. 12:16:19", pane="5")
    w.panes[(w.sock, "s")] = (True, [pane(3), pane(4), pane(5)])
    assert {r["id"] for r in w.snap()} == {"gb", "ru"} and not os.path.exists(p1)


@pytest.mark.parametrize("leg", ["unset", "de_DE"])
def test_locale_of_the_reader_does_not_matter(w, tmp_path, monkeypatch, leg):
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    probe = subprocess.run(["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True,
                           env=dict(os.environ), errors="replace")
    if w.de.is_c_lstart(probe.stdout) or not probe.stdout.strip():
        pytest.skip("locale not installed; ps output is already C-shaped")
    stored = w.mon.process_identity(os.getpid())
    assert w.de.is_c_lstart(stored)
    w.write("l", pid=os.getpid(), process_started=stored)
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "LC_TIME"):
        monkeypatch.delenv(key, raising=False)
    if leg == "de_DE":
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    log = w.wrap_real_ps(tmp_path)
    assert [r["id"] for r in w.snap()] == ["l"]
    assert all(l.split("|")[1:3] == ["LC_ALL=C", "PATH=/usr/bin:/bin"] for l in log.read_text().splitlines())


def test_batch_ps_parity_with_single_ps(w, monkeypatch):
    for value in (None, "ru_RU.UTF-8"):
        if value:
            monkeypatch.setenv("LC_ALL", value)
        ok, found = w.dash.pid_identities([os.getpid()])
        assert ok and found[os.getpid()] == w.mon.process_identity(os.getpid())
        assert w.de.is_c_lstart(found[os.getpid()])


# ---------------------------------------------------------------- GC

def test_gc_matrix(w):
    p = {}
    p["closed"] = w.write("closed", status="closed")
    p["ttl"] = w.write("ttl", updated=time.time() - 8 * 86400)
    p["exitedpane"] = w.write("exitedpane", pane="9")
    p["nopane"] = w.write("nopane", pane="77")
    p["nosession"] = w.write("nosession", session="gone")
    p["exitedsession"] = w.write("exitedsession", session="dead")
    p["ok"] = w.write("ok")
    w.sessions[w.sock] = (True, {"s": True, "dead": False})
    w.panes[(w.sock, "s")] = (True, [pane(3), pane(9, exited=True)])
    assert [r["id"] for r in w.snap()] == ["ok"]
    assert [k for k, v in p.items() if os.path.exists(v)] == ["ok"]


def test_no_active_sessions_message_is_a_successful_empty_list(w, tmp_path):
    w.mp.undo()  # use the real probes against the fake zellij
    d = w.home.fakebin
    (d / "zellij.out").write_text("")
    (d / "zellij.rc").write_text("1")
    # default fake prints the message on stderr with rc 1
    (d / "zellij.out").unlink()
    (d / "zellij.rc").unlink()
    assert w.dash.list_sessions(w.sock) == (True, {})
    (d / "zellij.out").write_text("boom")
    (d / "zellij.rc").write_text("3")
    assert w.dash.list_sessions(w.sock) == (False, {})


def test_list_failure_keeps_record_offline(w):
    path = w.write("a")
    w.sessions[w.sock] = (False, {})
    out = w.snap()
    assert out[0]["status"] == "offline" and os.path.exists(path)


def test_pane_listing_failure_keeps_record_offline(w):
    path = w.write("a")
    w.panes[(w.sock, "s")] = (False, None)
    assert w.snap()[0]["status"] == "offline" and os.path.exists(path)


def test_inferred_socket_absence_is_not_evidence(w):
    keep = w.write("inf", session="gone", socket_inferred=True)
    drop = w.write("exp", session="gone", socket_inferred=False, pane="4")
    out = w.snap()
    assert [(r["id"], r["status"]) for r in out] == [("inf", "offline")]
    assert os.path.exists(keep) and not os.path.exists(drop)


def test_compare_and_delete_keeps_a_rewritten_record(w):
    path = w.write("race", pane="77")
    real = w._panes

    def rewrite(sock, session):
        data = json.load(open(path))
        data["updated"] += 5
        w.de.write_json_atomic(path, data)
        return real(sock, session)

    w.mp.setattr(w.dash, "list_panes", rewrite)
    assert w.snap() == [] and os.path.exists(path)


def test_lock_not_acquired_skips_deletes_but_hides(w):
    import fcntl
    path = w.write("c", status="closed")
    fd = os.open(w.env.monitor_lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert w.snap() == [] and os.path.exists(path)
    finally:
        os.close(fd)


def test_unparsable_files_deleted_only_when_old(w):
    new = os.path.join(w.env.agents_dir, "junk.json")
    w.de.ensure_dir(w.env.agents_dir)
    open(new, "w").write("{")
    w.snap()
    assert os.path.exists(new)
    old = time.time() - 9 * 86400
    os.utime(new, (old, old))
    w.snap()
    assert not os.path.exists(new)


# ---------------------------------------------------------------- layouts

def test_shadowed_legacy_duplicate_older_is_deleted(w):
    now = time.time()
    new = w.write("dup", updated=now)
    legacy = w.write("dup", layout="legacy", updated=now - 10)
    assert [r["id"] for r in w.snap()] == ["dup"] and os.path.exists(new) and not os.path.exists(legacy)


def test_shadowed_legacy_duplicate_newer_is_left(w):
    now = time.time()
    w.write("dup", updated=now - 10)
    legacy = w.write("dup", layout="legacy", updated=now)
    out = w.snap()
    assert len(out) == 1 and out[0]["updated"] == pytest.approx(now - 10) and os.path.exists(legacy)


def test_dual_layout_pre_migration_fixture(w):
    legacy = w.write("old", layout="legacy")
    cfg = {"surfaces": {"s": "legacy-surface"}}
    w.de.ensure_dir(os.path.dirname(w.env.config_path))
    w.de.write_json_atomic(w.env.config_path, cfg)
    assert [r["id"] for r in w.snap()] == ["old"] and os.path.exists(legacy)
    assert not os.path.isdir(w.env.agents_dir) or not os.listdir(w.env.agents_dir)
    gone = w.write("gone", layout="legacy", status="closed", pane="4")
    w.snap()
    assert not os.path.exists(gone)
    calls = []
    w.mp.setattr(w.dash.c, "apple", lambda *a, **k: calls.append(a) or "")
    w.mp.setattr(w.dash.c, "run", lambda *a, **k: "")
    w.dash.focus({"session": "s", "pane": "3", "socket": w.sock})
    assert calls[0][1] == "legacy-surface"


def test_output_keys_are_whitelisted_and_additive(w):
    w.write("k", pid=os.getpid(), process_started=w.mon.process_identity(os.getpid()), junk="x", prompt="SECRET")
    w.panes[(w.sock, "s")] = (True, [pane(3), pane(5, title="Codex agent")])
    out = w.snap()
    for r in out:
        assert set(r) <= set(w.mon.RECORD_KEYS) | {"socket"}
        assert isinstance(r["pane"], str) and isinstance(r["updated"], (int, float))
    rec = [r for r in out if r["id"] == "k"][0]
    proto = {"pending", "status", "event", "updated", "id", "kind", "session", "pane", "socket", "project",
             "created", "pid", "process_started"}
    assert proto <= set(rec) and set(rec) - proto == {"socket_inferred", "via"}
    unknown = [r for r in out if r["status"] == "unknown"][0]
    assert unknown["id"] == "pane:%s:s:5" % w.sock and unknown["kind"] == "codex" and unknown["socket"] == w.sock


# ---------------------------------------------------------------- focus

def focus_spies(w):
    log = {"apple": [], "run": []}
    w.mp.setattr(w.dash.c, "apple", lambda src, *a, **k: log["apple"].append(a) or "")
    w.mp.setattr(w.dash.c, "run", lambda args, **k: log["run"].append((args, k)) or "")
    w.mp.setattr(w.dash.c, "terminal", lambda *a, **k: log.setdefault("terminal", []).append((a, k)))
    return log


def test_focus_of_vanished_agent_touches_nothing(w):
    log = focus_spies(w)
    w.panes[(w.sock, "s")] = (True, [])
    with pytest.raises(RuntimeError, match="Эта панель уже закрыта."):
        w.dash.focus({"session": "s", "pane": "3", "socket": w.sock})
    w.panes[(w.sock, "s")] = (False, None)
    with pytest.raises(RuntimeError, match="Сессия недоступна: s"):
        w.dash.focus({"session": "s", "pane": "3", "socket": w.sock})
    assert log["apple"] == [] and log["run"] == []


def test_focus_addresses_recorded_session_and_socket(w):
    log = focus_spies(w)
    other = "/other/sock"
    w.sessions[other] = (True, {"s": True})
    w.panes[(other, "s")] = (True, [pane(3)])
    w.panes[(w.sock, "s")] = (True, [pane(3)])
    w.de.set_surface(w.env, other, "s", "surface-other")
    w.dash.focus({"session": "s", "pane": "3", "socket": other})
    argv, kw = log["run"][0]
    assert argv[1:] == ["--session", "s", "action", "focus-pane-id", "terminal_3"] and kw["socket"] == other
    assert log["apple"][0][0] == "surface-other"


def test_title_fallback_refused_when_name_exists_on_two_sockets(w):
    log = focus_spies(w)
    other = "/other/sock"
    w.sessions[other] = (True, {"s": True})
    w.panes[(other, "s")] = (True, [pane(3)])
    w.write("rec", socket=other)
    w.dash.focus({"session": "s", "pane": "3", "socket": w.sock, "project": "/p"})
    assert log["apple"] == []
    (call,) = log["terminal"]
    assert call[0][0] == "attach" and call[1]["socket"] == w.sock


def test_title_fallback_used_when_name_is_unique(w):
    log = focus_spies(w)
    w.mp.setattr(w.dash.c, "apple", lambda src, *a, **k: log["apple"].append(a) or "surf-9")
    w.dash.focus({"session": "s", "pane": "3", "socket": w.sock})
    assert w.de.get_surface(w.env, w.sock, "s") == "surf-9" and "terminal" not in log


# ---------------------------------------------------------------- compatibility

def test_new_records_are_accepted_by_the_frozen_prototype(payload, deck_home, monkeypatch):
    dash, mon, de = payload("dashboard", "monitor", "deckenv")
    env = de.current()
    sock = env.socket_dir
    monkeypatch.setattr(dash, "list_sessions", lambda s: (True, {"s": True}))
    monkeypatch.setattr(dash, "list_panes", lambda s, n: (True, [pane(3)]))
    mon.record("claude", {"session_id": "sid", "hook_event_name": "Stop", "cwd": "/p"},
               environ={"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "3", "ZELLIJ_SOCKET_DIR": sock},
               identity=(4242, "X"))
    new_entries = dash.snapshot()
    (record_path,) = [p for p, _, _ in de.iter_records(env)]
    frozen = load_frozen(monkeypatch, deck_home, {
        "list-sessions --no-formatting": "s [Created 1m ago]",
    })
    agents = frozen.legacy / "agents"
    agents.mkdir()
    (agents / os.path.basename(record_path)).write_text(open(record_path).read())
    monkeypatch.setattr(frozen.dashboard, "panes", lambda session, socket=None: [pane(3)])
    monkeypatch.setattr(frozen.dashboard.monitor, "process_identity", lambda pid: "X")
    old_entries = frozen.dashboard.snapshot()
    assert [r["id"] for r in old_entries] == [r["id"] for r in new_entries] and len(old_entries) == 1
    proto = {"pending": dict, "status": str, "event": str, "updated": float, "id": str, "kind": str,
             "session": str, "pane": str, "socket": str, "project": str, "created": float, "pid": int,
             "process_started": str}
    for entry in new_entries:
        for key, kind in proto.items():
            assert isinstance(entry[key], kind), key
    assert sum(1 for c in frozen.calls if c[1] == "list-sessions") == 1


# ---------------------------------------------------------------- budgets, legacy records, pruning

def sleeping_fake(deck_home, name, seconds):
    path = deck_home.fakebin / name
    log = path.with_suffix(".log")
    path.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nsleep %s\n' % (log, seconds))
    path.chmod(0o755)
    return log


def test_snapshot_is_bounded_by_the_overall_budget(payload, deck_home, monkeypatch):
    dash, mon, de = payload("dashboard", "monitor", "deckenv")
    env = de.current()
    log = sleeping_fake(deck_home, "zellij", 30)
    monkeypatch.setattr(dash, "SNAPSHOT_BUDGET_S", 2)
    monkeypatch.setattr(dash, "PROBE_TIMEOUT", 1)
    de.ensure_dir(env.agents_dir)
    paths = []
    for i in range(4):
        rec = dict(id="r%d" % i, kind="claude", session="s%d" % i, pane="3", socket="/sock/%d" % i,
                   socket_inferred=False, status="working", pending={}, event="Stop", created=1.0,
                   updated=time.time(), via="managed", project="/p")
        path = os.path.join(env.agents_dir, "r%d.json" % i)
        de.write_json_atomic(path, rec)
        paths.append(path)
    start = time.monotonic()
    out = dash.snapshot()
    assert time.monotonic() - start < dash.SNAPSHOT_BUDGET_S + 2.5
    assert len(out) == 4 and {r["status"] for r in out} == {"offline"}
    assert all(os.path.exists(p) for p in paths)
    assert len(log.read_text().splitlines()) <= 2


def test_focus_is_bounded_and_never_issues_focus_pane_id(w, deck_home):
    log_osa = sleeping_fake(deck_home, "osascript", 30)
    w.mp.setattr(w.dash, "FOCUS_BUDGET_S", 2)
    w.de.set_surface(w.env, w.sock, "s", "surf")
    start = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        w.dash.focus({"session": "s", "pane": "3", "socket": w.sock})
    assert time.monotonic() - start < 2 + 2.5
    assert str(exc.value) and "\n" not in str(exc.value)
    zlog = deck_home.fakebin / "zellij.log"
    assert not zlog.exists() or "focus-pane-id" not in zlog.read_text()
    assert log_osa.exists()


def test_legacy_record_without_the_inferred_flag_is_offline_not_deleted(w):
    path = w.write("proto", layout="legacy", session="gone")
    rec = json.load(open(path))
    rec.pop("socket_inferred")
    w.de.write_json_atomic(path, rec)
    out = w.snap()
    assert [(r["id"], r["status"]) for r in out] == [("proto", "offline")]
    assert os.path.exists(path)


def test_surfaces_pruned_only_after_a_successful_listing(w):
    w.write("a")
    w.de.set_surface(w.env, w.sock, "s", "keep")
    w.de.set_surface(w.env, w.sock, "gone", "drop")
    w.de.set_surface(w.env, "/unknown/sock", "x", "other")
    w.sessions[w.sock] = (False, {})
    w.snap()
    assert set(json.load(open(w.env.surfaces_path))) == {w.sock + "\ts", w.sock + "\tgone", "/unknown/sock\tx"}
    w.sessions[w.sock] = (True, {"s": True})
    w.snap()
    assert set(json.load(open(w.env.surfaces_path))) == {w.sock + "\ts", "/unknown/sock\tx"}


def test_invalid_session_in_a_legacy_record_is_offline_and_never_reaches_zellij(w):
    w.mp.undo()
    calls = []
    import subprocess as sp
    real = sp.run

    def spy(args, *a, **k):
        calls.append(list(args))
        return real(args, *a, **k)
    w.mp.setattr(w.dash.subprocess, "run", spy)
    path = w.write("bad", layout="legacy", session="-x")
    out = w.dash.snapshot()
    assert [(r["id"], r["status"]) for r in out if r["id"] == "bad"] == [("bad", "offline")]
    assert os.path.exists(path)
    assert not any("-x" in argv for argv in calls)
    with pytest.raises(RuntimeError):
        w.dash.focus({"session": "-x", "pane": "3"})
    assert not any("-x" in argv for argv in calls)


def test_focus_failure_path_shows_no_dialog(payload, deck_home, tmp_path):
    runtime = deck_home.runtime
    dash = runtime / "dash-launch.py"
    code = ("import sys\nsys.argv=['dashboard.py','focus','{\"session\":\"s\",\"pane\":\"3\"}']\n"
            "sys.path.insert(0, %r)\nimport runpy\nrunpy.run_path(%r, run_name='__main__')\n"
            % (str(h.PAYLOAD_DIR), str(h.PAYLOAD_DIR / "dashboard.py")))
    dash.write_text(code)
    osa = deck_home.fakebin / "osascript"
    env = dict(os.environ, QUOIN_DECK_RUNTIME=str(runtime), HOME=str(deck_home.home))
    proc = subprocess.run([sys.executable, "-E", "-s", str(dash)], env=env, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 1
    log = osa.with_suffix(".log")
    assert not log.exists() or "display dialog" not in log.read_text()
