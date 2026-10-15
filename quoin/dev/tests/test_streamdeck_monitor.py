import json
import os
import stat
import subprocess
import sys
import time

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload, stage_runtime  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

ZENV = {"ZELLIJ_SESSION_NAME": "sess", "ZELLIJ_PANE_ID": "3"}
C_LSTART = "Tue Oct 13 12:16:19 2026"


def event(**kw):
    base = {"session_id": "sid-1", "hook_event_name": "UserPromptSubmit", "cwd": "/work"}
    base.update(kw)
    return json.dumps(base).encode()


def fake_ps(tmp_path, table="", lstart=C_LSTART, sleep=0):
    d = tmp_path / "psfake"
    d.mkdir(exist_ok=True)
    (d / "table").write_text(table)
    (d / "lstart").write_text(lstart + "\n")
    script = d / "ps"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%%s|LC_ALL=%%s|PATH=%%s|HOME=%%s|LANG=%%s\\n" "$*" "$LC_ALL" "$PATH" "$HOME" "$LANG" >> "%s/log"\n'
        "sleep %s\n"
        'case "$1" in -axo) cat "%s/table";; -p) cat "%s/lstart";; esac\n' % (d, sleep, d, d)
    )
    script.chmod(0o755)
    return script, d / "log"


# ------------------------------------------------------------ via the deployed command line

@pytest.fixture
def staged(tmp_path):
    return stage_runtime(tmp_path)


def run_monitor(staged, kind, stdin, extra=None, args=True):
    cmd = staged.command("monitor.py", *([kind] if args else []))
    return staged.run(cmd, stdin, dict(ZENV, **(extra or {})))


@pytest.mark.parametrize("name,stdin", [
    ("valid", event()),
    ("malformed", b"{not json"),
    ("empty", b""),
    ("nonobject", b"[1, 2]"),
    ("oversized", b" " * ((1 << 20) + 1)),
    ("oversized_valid", event() + b" " * ((1 << 20) + 1)),
])
def test_exit_zero_and_silent(staged, name, stdin):
    rc, out, err = run_monitor(staged, "claude", stdin)
    assert (rc, out, err) == (0, b"", b"")
    agents = staged.state_dir / "agents"
    assert (name == "valid") == (agents.exists() and any(agents.glob("*.json")))


def test_unknown_kind_and_missing_argv(staged):
    assert run_monitor(staged, "other", event()) == (0, b"", b"")
    assert run_monitor(staged, "claude", event(), args=False) == (0, b"", b"")
    assert not (staged.state_dir / "agents").exists()


def test_unwritable_state_dir_still_exits_zero(staged):
    staged.state_root.mkdir()
    staged.state_root.chmod(0o500)
    try:
        assert run_monitor(staged, "claude", event()) == (0, b"", b"")
    finally:
        staged.state_root.chmod(0o700)


# ------------------------------------------------------------ in-process behaviour

def test_deadline_is_bounded_by_the_subprocess_timeout(payload, tmp_path, monkeypatch):
    monitor = payload("monitor")
    script, _ = fake_ps(tmp_path, sleep=3)
    monkeypatch.setattr(monitor, "PS", str(script))
    for k, v in ZENV.items():
        monkeypatch.setenv(k, v)
    import io
    start = time.monotonic()
    monitor.main(["monitor.py", "claude"], io.BytesIO(event()))
    assert time.monotonic() - start < 1.7


def test_single_table_read_and_lstart_only_when_matched(payload, tmp_path, monkeypatch):
    monitor = payload("monitor")
    parent = os.getppid()
    table = "%d 1 /usr/local/bin/claude\n%d %d /usr/bin/python3\n" % (parent, os.getpid(), parent)
    script, log = fake_ps(tmp_path, table=table)
    monkeypatch.setattr(monitor, "PS", str(script))
    monitor.record("claude", json.loads(event()), environ=ZENV)
    lines = log.read_text().splitlines()
    assert [l.split("|")[0] for l in lines] == ["-axo pid=,ppid=,comm=", "-p %d -o lstart=" % parent]
    for line in lines:
        assert line.split("|")[1:] == ["LC_ALL=C", "PATH=/usr/bin:/bin", "HOME=", "LANG="]
    log.unlink()
    monitor.record("codex", json.loads(event(session_id="other")), environ=ZENV)
    assert len(log.read_text().splitlines()) == 1


def test_non_c_lstart_is_stored_empty(payload, tmp_path, monkeypatch):
    monitor = payload("monitor")
    parent = os.getppid()
    script, _ = fake_ps(tmp_path, table="%d 1 /x/claude\n" % parent, lstart="Tue 13 Oct 12:16:19 2026")
    monkeypatch.setattr(monitor, "PS", str(script))
    monitor.record("claude", json.loads(event()), environ=ZENV)
    rec = records(payload)[0]
    assert rec["pid"] == parent and rec["process_started"] == ""


def test_mixed_locale_lstart_is_c_shaped(payload, monkeypatch):
    monitor = payload("monitor")
    deckenv = payload("deckenv")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    probe = subprocess.run(["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True,
                           env=dict(os.environ), errors="replace")
    if deckenv.is_c_lstart(probe.stdout) or not probe.stdout.strip():
        pytest.skip("ru_RU locale not installed; unpinned ps is already C-shaped")
    assert deckenv.is_c_lstart(monitor.process_identity(os.getpid()))


def records(payload):
    deckenv = payload("deckenv")
    env = deckenv.current()
    return [r for _, r, _ in deckenv.iter_records(env)]


def test_held_lock_is_given_up_within_the_deadline(payload):
    import fcntl
    monitor = payload("monitor")
    deckenv = payload("deckenv")
    env = deckenv.current()
    deckenv.ensure_dir(env.state_dir)
    fd = os.open(env.monitor_lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        monitor.record("claude", json.loads(event()), environ=ZENV, identity=(1, ""))
        assert time.monotonic() - start < 1.7
    finally:
        os.close(fd)
    assert records(payload) == []


def test_record_keys_are_exactly_the_whitelist(payload):
    monitor = payload("monitor")
    monitor.record("claude", json.loads(event(prompt="p", tool_input={"a": 1}, message="m", title="t")),
                   environ=dict(ZENV, ZELLIJ_SOCKET_DIR="/sock"), identity=(os.getpid(), C_LSTART))
    rec = records(payload)[0]
    assert set(rec) == set(monitor.RECORD_KEYS)
    assert rec["socket"] == "/sock" and rec["socket_inferred"] is False
    assert rec["project"] == "/work" and rec["pid"] == os.getpid()


def test_socket_inferred_without_env(payload):
    monitor = payload("monitor")
    deckenv = payload("deckenv")
    monitor.record("claude", json.loads(event()), environ=ZENV, identity=(1, ""))
    rec = records(payload)[0]
    assert rec["socket_inferred"] is True and rec["socket"] == deckenv.current().socket_dir


def test_via_whitelist(payload):
    monitor = payload("monitor")
    for i, (value, want) in enumerate([("legacy", "legacy"), ("managed", "managed"), ("evil", "managed"), (None, "managed")]):
        extra = dict(ZENV)
        if value:
            extra["QUOIN_DECK_VIA"] = value
        monitor.record("claude", json.loads(event(session_id="s%d" % i)), environ=extra, identity=(1, ""))
    assert sorted(r["via"] for r in records(payload)) == sorted(["legacy", "managed", "managed", "managed"])


def test_event_name_whitelist_and_token_sanitising(payload):
    monitor = payload("monitor")
    assert monitor.transition({}, {"hook_event_name": "Totally<New>"})["event"] == "other"
    assert monitor.transition({}, {"hook_event_name": "Stop"})["event"] == "Stop"
    assert monitor.transition({}, {"hook_event_name": ["x"]})["event"] == "other"
    state = monitor.transition({}, {"hook_event_name": "PermissionRequest", "tool_use_id": "a b/c;" + "x" * 100})
    (token,) = state["pending"]
    assert len(token) == 64 and set(token) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
    done = monitor.transition(state, {"hook_event_name": "PostToolUse", "tool_use_id": "a b/c;" + "x" * 100})
    assert done["pending"] == {}


def test_modes_of_state_files(payload):
    monitor = payload("monitor")
    deckenv = payload("deckenv")
    old = os.umask(0o022)
    try:
        monitor.record("claude", json.loads(event()), environ=ZENV, identity=(1, ""))
    finally:
        os.umask(old)
    env = deckenv.current()
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(env.state_dir) == 0o700 and mode(env.agents_dir) == 0o700
    assert mode(next(__import__("pathlib").Path(env.agents_dir).glob("*.json"))) == 0o600
    assert mode(env.monitor_lock) == 0o600


def test_errors_log_holds_type_only(staged):
    secret = "SENTINEL-" + "z" * 20
    bad = staged.runtime / "versions" / "1.0.0" / "monitor.py"
    src = bad.read_text().replace("    record(argv[1], event, deadline=deadline)",
                                  "    raise ValueError(%r)" % secret)
    bad.write_text(src)
    rc, out, err = run_monitor(staged, "claude", event())
    assert (rc, out, err) == (0, b"", b"")
    log = (staged.state_dir / "errors.log").read_text()
    assert "ValueError" in log and secret not in log


def test_session_starting_with_a_dash_is_skipped(payload):
    monitor = payload("monitor")
    monitor.record("claude", json.loads(event()), environ=dict(ZENV, ZELLIJ_SESSION_NAME="-x"), identity=(1, ""))
    assert records(payload) == []


def test_symlinked_lock_is_not_followed_and_monitor_exits_zero(staged, tmp_path):
    state = staged.state_dir
    state.mkdir(parents=True)
    victim = tmp_path / "victim.lock"
    victim.write_text("keep")
    os.symlink(str(victim), str(state / "monitor.lock"))
    assert run_monitor(staged, "claude", event()) == (0, b"", b"")
    assert victim.read_text() == "keep"


def test_idle_reminder_after_a_finished_turn_does_not_ask_for_attention(payload):
    m = payload("monitor")
    done = m.transition({"status": "working", "pending": {}}, {"hook_event_name": "Stop"})
    assert done["status"] == "done"
    idle = m.transition(done, {"hook_event_name": "Notification", "notification_type": "idle_prompt"})
    assert idle["status"] == "done" and idle["pending"] == {}
    for kind in ("permission_prompt", "elicitation_dialog"):
        asked = m.transition(done, {"hook_event_name": "Notification", "notification_type": kind})
        assert asked["status"] == "attention"


def _step(m, state, name, **kw):
    return m.transition(state, dict({"hook_event_name": name}, **kw))


def test_finished_turn_stays_working_while_a_background_subagent_runs(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="sub-1")
    s = _step(m, s, "Stop")
    assert s["status"] == "working" and len(s["subagents"]) == 1
    s = _step(m, s, "SubagentStop", agent_id="sub-1")
    assert s["status"] == "done" and s["subagents"] == {}


def test_every_subagent_must_stop_before_the_key_turns_done(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    for name in ("a", "b"):
        s = _step(m, s, "SubagentStart", agent_id=name)
    s = _step(m, s, "Stop")
    s = _step(m, s, "SubagentStop", agent_id="a")
    assert s["status"] == "working"
    s = _step(m, s, "SubagentStop", agent_id="b")
    assert s["status"] == "done"


def test_subagent_stop_during_a_running_turn_changes_nothing(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="a")
    s = _step(m, s, "SubagentStop", agent_id="a")
    assert s["status"] == "working"


def test_a_question_outranks_running_subagents(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="a")
    s = _step(m, s, "PermissionRequest", tool_name="Bash", tool_use_id="t1")
    s = _step(m, s, "Stop")
    assert s["status"] == "working"  # Stop clears the pending question, subagent keeps it busy
    s = _step(m, s, "Notification", notification_type="permission_prompt")
    s = _step(m, s, "SubagentStop", agent_id="a")
    assert s["status"] == "attention"


def test_a_subagent_that_never_stops_expires(payload, monkeypatch):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="lost")
    s = _step(m, s, "Stop")
    assert s["status"] == "working"
    real = m.time.time
    monkeypatch.setattr(m.time, "time", lambda: real() + m.SUBAGENT_TTL_S + 1)
    s = _step(m, s, "Stop")
    assert s["status"] == "done" and s["subagents"] == {}


def test_session_start_forgets_subagents_and_the_cap_is_enforced(payload):
    m = payload("monitor")
    s = {}
    for i in range(m.SUBAGENT_CAP + 10):
        s = _step(m, s, "SubagentStart", agent_id="s%d" % i)
    assert len(s["subagents"]) == m.SUBAGENT_CAP
    s = _step(m, s, "SessionStart")
    assert s["subagents"] == {} and s["status"] == "idle"


def test_only_subagent_start_and_stop_reach_the_record(payload, deck_home, monkeypatch):
    m = payload("monitor")
    env = {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "1"}
    ident = (1, "birth")
    m.record("claude", {"session_id": "sid", "hook_event_name": "UserPromptSubmit", "cwd": "/w"}, environ=env, identity=ident)
    m.record("claude", {"session_id": "sid", "hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_id": "x"},
             environ=env, identity=ident)
    m.record("claude", {"session_id": "sid", "hook_event_name": "SubagentStart", "agent_id": "x"}, environ=env, identity=ident)
    de = payload("deckenv")
    records = [r for _, r, _ in de.iter_records(de.current()) if isinstance(r, dict)]
    assert len(records) == 1 and records[0]["event"] == "SubagentStart" and len(records[0]["subagents"]) == 1
    assert "x" not in records[0]["subagents"]


def test_compaction_restart_keeps_running_subagents_and_the_turn_state(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="a")
    s = _step(m, s, "SessionStart", source="compact")
    assert s["status"] == "working" and len(s["subagents"]) == 1
    s = _step(m, s, "Stop")
    assert s["status"] == "working"
    fresh = _step(m, s, "SessionStart", source="startup")
    assert fresh["status"] == "idle" and fresh["subagents"] == {}


def test_main_agent_activity_after_stop_ends_the_done_turn_state(payload):
    m = payload("monitor")
    s = _step(m, {}, "UserPromptSubmit")
    s = _step(m, s, "SubagentStart", agent_id="bg")
    s = _step(m, s, "Stop")
    s = _step(m, s, "PreToolUse", tool_name="Bash", tool_use_id="t")
    assert s["turn_done"] is False and s["status"] == "working"
    s = _step(m, s, "SubagentStart", agent_id="fg")
    s = _step(m, s, "SubagentStop", agent_id="fg")
    s = _step(m, s, "SubagentStop", agent_id="bg")
    assert s["status"] == "working"


def test_displayed_status_applies_the_subagent_expiry(payload):
    m = payload("monitor")
    now = 1_000_000.0
    live = {"status": "working", "turn_done": True, "subagents": {"a": now - 60}}
    stale = {"status": "working", "turn_done": True, "subagents": {"a": now - m.SUBAGENT_TTL_S - 1}}
    running_turn = {"status": "working", "turn_done": False, "subagents": {}}
    assert m.displayed_status(live, now) == "working"
    assert m.displayed_status(stale, now) == "done"
    assert m.displayed_status(running_turn, now) == "working"
    assert m.displayed_status({"status": "attention", "turn_done": True, "subagents": {}}, now) == "attention"
