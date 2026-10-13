"""Prototype tests ported to the payload tree (the hooks-merge test moves with the merge code)."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")


def test_parallel_tool_does_not_hide_attention(payload):
    monitor = payload("monitor")
    a = monitor.transition({}, dict(hook_event_name="PermissionRequest", tool_name="Bash", tool_use_id="one"))
    b = monitor.transition(a, dict(hook_event_name="PostToolUse", tool_name="Read", tool_use_id="two"))
    assert b["status"] == "attention"
    c = monitor.transition(b, dict(hook_event_name="PostToolUse", tool_name="Bash", tool_use_id="one"))
    assert c["status"] == "working"


def test_question_done_and_interrupt(payload):
    monitor = payload("monitor")
    a = monitor.transition({}, dict(hook_event_name="PreToolUse", tool_name="AskUserQuestion", tool_use_id="q"))
    assert a["status"] == "attention"
    assert monitor.transition(a, {"hook_event_name": "Stop"})["status"] == "done"
    assert monitor.transition(a, {"hook_event_name": "Interrupt"})["status"] == "attention"


def test_metadata_only_and_no_desktop_threads(payload, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_DECK_STATE_ROOT", str(tmp_path / "st"))
    monitor = payload("monitor")
    state = tmp_path / "st" / "state"
    data = dict(session_id="test", hook_event_name="UserPromptSubmit", prompt="SECRET", cwd="/work")
    monitor.record("claude", data, environ={}, identity=(1, "birth"))
    assert not (state / "agents").exists()
    monitor.record("claude", data, environ={"ZELLIJ_SESSION_NAME": "test", "ZELLIJ_PANE_ID": "1"},
                   identity=(1, "birth"))
    text = next((state / "agents").glob("*.json")).read_text()
    assert "SECRET" not in text
    assert json.loads(text)["status"] == "working"


def test_closed_pane_cannot_focus(payload):
    dashboard = payload("dashboard")
    with patch.object(dashboard.c, "load", return_value={}), \
            patch.object(dashboard, "list_panes", return_value=(True, [])), \
            patch.object(dashboard.c, "apple") as apple:
        with pytest.raises(RuntimeError):
            dashboard.focus({"session": "test", "pane": "1"})
        apple.assert_not_called()


def test_parse_sessions_preserves_spaces_and_excludes_saved_from_live(payload):
    c = payload("controller")
    with patch.object(c, "run", return_value="project alpha [Created 1m ago]\nold [Created 2d ago] (EXITED - attach to resurrect)"):
        assert c.sessions() == [("project alpha", True), ("old", False)]


def test_never_redirects_to_another_session(payload):
    c = payload("controller")
    with patch.object(c, "sessions", return_value=[("other", True)]), patch.object(c, "run") as run:
        with pytest.raises(RuntimeError):
            c.execute("next-pane", {"session": "gone"})
        run.assert_not_called()


def test_navigation_is_addressed(payload):
    c = payload("controller")
    with patch.object(c, "sessions", return_value=[("wanted", True)]), patch.object(c, "run") as run:
        c.execute("next-pane", {"session": "wanted"})
        argv = run.call_args.args[0]
        assert os.path.basename(argv[0]) == "zellij"
        assert argv[1:] == ["--session", "wanted", "action", "focus-next-pane"]


def test_project_path_is_one_argument(payload):
    c = payload("controller")
    deckenv = payload("deckenv")
    project = "/tmp/project with spaces; $(nope)"
    deckenv.set_surface(deckenv.current(), deckenv.current().socket_dir, "wanted", "surface-1")
    with patch.object(c, "apple", return_value="surface-1"), patch.object(c, "sessions", return_value=[("wanted", True)]), \
            patch.object(c, "run") as run:
        c.execute("claude", {"session": "wanted", "project": project, "surfaces": {"wanted": "surface-1"}})
        argv = run.call_args.args[0]
        assert argv[argv.index("--cwd") + 1] == project
        assert argv[-1] == project
        assert "-c" not in argv


def test_cancel_session_selection_does_not_attach_or_write(payload):
    c = payload("controller")
    cfg = {"project": "/tmp", "session": "existing"}
    with patch.object(c, "sessions", return_value=[("test", True)]), patch.object(c, "choose", return_value=None), \
            patch.object(c, "terminal") as terminal, patch.object(c, "save") as save:
        c.execute("sessions", cfg)
        terminal.assert_not_called()
        save.assert_not_called()
        assert cfg["session"] == "existing"


def test_new_project_clears_old_session(payload):
    c = payload("controller")
    cfg = {"project": "/tmp/old", "session": "old"}
    with patch.object(c, "folder", return_value="/tmp/new"), patch.object(c, "save"):
        c.execute("project", cfg)
        assert cfg["session"] is None


def test_no_selection_does_not_mutate_session(payload):
    c = payload("controller")
    with patch.object(c, "run") as run:
        with pytest.raises(RuntimeError):
            c.execute("codex", {"session": None})
        run.assert_not_called()


def test_state_roundtrip(payload, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_DECK_STATE_ROOT", str(tmp_path / "state"))
    c = payload("controller")
    cfg = {"project": "/tmp/тест", "session": "test", "projects": {}}
    c.save(cfg)
    assert c.load() == cfg
