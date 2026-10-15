import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")


def exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def record_tools(deck_home, tmp_path, agentdesk=True):
    manifest = json.loads((deck_home.runtime / "install-manifest.json").read_text())
    manifest["binaries"]["claude"] = exe(tmp_path / "tools-a" / "claude")
    manifest["binaries"]["codex"] = {"path": exe(tmp_path / "tools-b" / "codex")}
    if agentdesk:
        ad = tmp_path / "ad" / "agentdesk.zsh"
        ad.parent.mkdir(parents=True)
        ad.write_text("AGENTDESK_API_VERSION=1\n")
        manifest["paths"] = {"agentdesk": str(ad)}
    (deck_home.runtime / "install-manifest.json").write_text(json.dumps(manifest))


def test_no_module_env_global_and_no_hardcoded_paths(payload):
    c = payload("controller")
    for name in ("ENV", "STATE", "BASE", "ZELLIJ"):
        assert not hasattr(c, name)
    src = (h.PAYLOAD_DIR / "controller.py").read_text()
    assert "/opt/homebrew" not in src and "/tmp/zellij" not in src and "/bin/zsh" not in src


def test_apple_timeouts(payload):
    c = payload("controller")
    with patch.object(c, "run", return_value="") as run:
        c.apple("on run\nend run")
        assert run.call_args.kwargs["timeout"] == 15
        c.dialog("m")
        assert run.call_args.kwargs["timeout"] is None
        c.choose(["a"], "p")
        assert run.call_args.kwargs["timeout"] is None
        c.folder()
        assert run.call_args.kwargs["timeout"] is None
        assert os.path.basename(run.call_args.args[0][0]) == "osascript"
        c.apple("x", timeout=10)
        assert run.call_args.kwargs["timeout"] == 10


def test_run_uses_deck_environment_without_mutating_os_environ(payload):
    c = payload("controller")
    de = payload("deckenv")
    before = dict(os.environ)
    with patch.object(c.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        assert c.run(["x"], socket="/some/sock") == "ok"
    env = run.call_args.kwargs["env"]
    assert env["PATH"] == de.current().path_env and env["ZELLIJ_SOCKET_DIR"] == "/some/sock"
    assert dict(os.environ) == before


def test_terminal_command_env_and_surface(payload, deck_home, tmp_path):
    record_tools(deck_home, tmp_path)
    c = payload("controller")
    de = payload("deckenv")
    env = de.current()
    cfg = {"session": "s 1", "project": "/tmp/p q; $(x)", "projects": {}}
    with patch.object(c, "apple", return_value="surf-1") as apple:
        c.terminal("attach", cfg, socket="/sock/a")
    args = apple.call_args.args
    project, command, socket, path_env, agentdesk = args[1:6]
    assert project == cfg["project"] and socket == "/sock/a"
    assert command.startswith(env.terminal_entry) and "bin/quoin-deck-terminal" in command
    assert "versions/" not in command and "current/" not in command
    assert path_env == env.path_env and str(tmp_path / "tools-a") in path_env.split(":")
    assert str(tmp_path / "tools-b") in path_env.split(":") and agentdesk == env.agentdesk
    assert "QUOIN_DECK_AGENTDESK" in apple.call_args.args[0]
    assert de.get_surface(env, "/sock/a", "s 1") == "surf-1"


def test_surfaces_are_keyed_by_socket(payload):
    c = payload("controller")
    de = payload("deckenv")
    cfg = {"session": "same", "project": "/tmp", "projects": {}}
    with patch.object(c, "apple", side_effect=["surf-a", "surf-b"]):
        c.terminal("attach", cfg, socket="/a")
        c.terminal("attach", cfg, socket="/b")
    env = de.current()
    assert de.get_surface(env, "/a", "same") == "surf-a" and de.get_surface(env, "/b", "same") == "surf-b"
    assert de.find_surface_owner(env, "surf-b") == ("/b", "same")


def test_missing_agentdesk_message_and_no_osascript(payload):
    c = payload("controller")
    cfg = {"session": "s", "project": "/tmp", "projects": {}}
    with patch.object(c, "apple") as apple:
        with pytest.raises(RuntimeError, match="Agent Desk is not installed"):
            c.terminal("new", cfg)
        with pytest.raises(RuntimeError, match="Agent Desk is not installed"):
            c.execute("desk", {"project": "/tmp", "session": None, "projects": {}})
        apple.assert_not_called()


def test_plus_agent_run_argv_uses_stable_entry(payload, deck_home):
    c = payload("controller")
    de = payload("deckenv")
    env = de.current()
    de.set_surface(env, "/sock/x", "wanted", "surf")
    cfg = {"session": "wanted", "socket": "/sock/x", "project": "/tmp/pr", "projects": {"wanted": "/tmp/pr"}}
    for action in ("claude", "codex", "shell", "git"):
        with patch.object(c, "apple", return_value="surf"), patch.object(c, "sessions", return_value=[("wanted", True)]), \
                patch.object(c, "run") as run:
            c.execute(action, dict(cfg))
        argv = run.call_args.args[0]
        assert argv[-4:] == [env.terminal_entry, action, "wanted", "/tmp/pr"]
        assert "versions/" not in " ".join(argv) and "current/" not in " ".join(argv)
        assert run.call_args.kwargs["socket"] == "/sock/x"


def test_plus_agent_refuses_unknown_or_ambiguous_surface(payload):
    c = payload("controller")
    de = payload("deckenv")
    env = de.current()
    de.set_surface(env, "/a", "one", "surf")
    de.set_surface(env, "/b", "two", "surf")
    with patch.object(c, "apple", return_value="surf"), patch.object(c, "run") as run:
        with pytest.raises(RuntimeError):
            c.execute("claude", {"session": "one", "project": "/tmp"})
        run.assert_not_called()


def test_plus_agent_resolves_a_tab_the_deck_did_not_open_from_its_title(payload):
    c = payload("controller")
    de = payload("deckenv")
    env = de.current()
    cfg = {"session": "other", "project": "/tmp/pr", "projects": {"adopted": "/tmp/adopted"}}
    found = "surf-new\nadopted | Claude Code"
    with patch.object(c, "apple", return_value=found), patch.object(c, "sessions", return_value=[("adopted", True)]), \
            patch.object(c, "run") as run:
        c.execute("claude", dict(cfg))
    argv = run.call_args.args[0]
    assert argv[1:3] == ["--session", "adopted"] and argv[-1] == "/tmp/adopted"
    assert de.get_surface(env, env.socket_dir, "adopted") == "surf-new"


@pytest.mark.parametrize("title,live", [("adopted | Claude Code", False), ("not-a-session", True), ("-x | y", True)])
def test_plus_agent_title_fallback_refuses_dead_or_unrelated_tabs(payload, title, live):
    c = payload("controller")
    with patch.object(c, "apple", return_value="surf-x\n" + title), \
            patch.object(c, "sessions", return_value=[("adopted", live)]), patch.object(c, "run") as run:
        with pytest.raises(RuntimeError):
            c.execute("claude", {"session": "other", "project": "/tmp"})
    run.assert_not_called()


def test_require_live_uses_config_socket(payload):
    c = payload("controller")
    with patch.object(c, "sessions", return_value=[("a", True)]) as sessions, patch.object(c, "run"):
        assert c.require_live({"session": "a", "socket": "/s"}) == "a"
    sessions.assert_called_with("/s")


def test_nav_actions_are_kept(payload):
    c = payload("controller")
    assert {"prev-pane", "next-pane", "fullscreen", "prev-tab", "next-tab", "floating", "save"} <= set(c.NAV)


def test_main_logs_type_and_short_message_then_dialogs(payload):
    c = payload("controller")
    de = payload("deckenv")
    with patch.object(c, "execute", side_effect=ValueError("bad\nsecond line")), patch.object(c, "dialog") as dialog, \
            patch.object(c, "load", return_value={}), patch.object(sys, "argv", ["controller.py", "desk"]):
        c.main()
    dialog.assert_called_once_with("bad\nsecond line")
    line = open(de.current().errors_log).read()
    assert line.rstrip("\n").split(" ", 1)[1] == "ValueError: bad" and line.count("\n") == 1


def test_single_flight_lock(payload):
    import fcntl
    c = payload("controller")
    de = payload("deckenv")
    env = de.current()
    de.ensure_dir(env.state_dir)
    fd = os.open(env.controller_lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with patch.object(c, "execute") as execute, patch.object(sys, "argv", ["controller.py", "desk"]):
            c.main()
        execute.assert_not_called()
    finally:
        os.close(fd)


@pytest.mark.parametrize("bad", ["-x", "a\nb", "x" * 300])
def test_invalid_session_names_are_refused(payload, bad):
    c = payload("controller")
    with patch.object(c, "sessions", return_value=[("a", True)]), patch.object(c, "run") as run:
        with pytest.raises(RuntimeError, match="(?i)session"):
            c.require_live({"session": bad})
        with pytest.raises(RuntimeError, match="(?i)session"):
            c.execute("next-pane", {"session": bad, "project": "/tmp"})
        run.assert_not_called()
    with patch.object(c, "sessions", return_value=[(bad, True)]), patch.object(c, "choose", return_value=bad + " • active"), \
            patch.object(c, "folder", return_value="/tmp"):
        with pytest.raises(RuntimeError, match="(?i)session"):
            c.select({"projects": {}})


def test_projects_map_is_capped_at_200_newest(payload):
    c = payload("controller")
    config = {"projects": {"s%d" % i: "/p" for i in range(200)}, "project": "/tmp"}
    with patch.object(c, "sessions", return_value=[("fresh", True)]), patch.object(c, "choose", return_value="fresh • active"), \
            patch.object(c, "folder", return_value="/proj"), patch.object(c, "save"):
        assert c.select(config) is True
    assert len(config["projects"]) == 200 and "s0" not in config["projects"] and config["projects"]["fresh"] == "/proj"
