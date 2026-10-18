"""Retired hook stanza removal in deploy_hooks().

The shipped table is empty. The behaviour is proven by injecting an entry:
only stanzas whose every command resolves to the retired script inside the
install's hooks directory are removed; foreign and mixed stanzas stay.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"

from quoin import installer  # noqa: E402
from quoin.installer import deploy_hooks  # noqa: E402

ENTRY = (("PreToolUse", "*", "retired_demo.sh"),)


def _stanza(command, matcher="*"):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 5}]}


def _seed(dest, home):
    hooks_dir = dest.resolve() / "hooks"
    tilde = "~/" + str((dest.resolve() / "hooks" / "retired_demo.sh").relative_to(home.resolve()))
    settings = {"hooks": {"PreToolUse": [
        _stanza(str(hooks_dir / "retired_demo.sh")),
        _stanza("/opt/x/retired_demo.sh"),
        _stanza(tilde),
        {"matcher": "*", "hooks": [
            {"type": "command", "command": str(hooks_dir / "retired_demo.sh")},
            {"type": "command", "command": "/opt/y/other.sh"}]},
        _stanza(str(hooks_dir / "retired_demo.sh"), matcher="Bash"),
    ]}}
    (dest / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _commands(settings, event="PreToolUse"):
    return [[h["command"] for h in s["hooks"]] for s in settings["hooks"].get(event, [])]


def test_default_table_is_empty():
    assert installer._RETIRED_HOOK_STANZAS == ()


def test_only_quoin_owned_stanzas_are_removed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dest = home / ".claude"
    dest.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "_RETIRED_HOOK_STANZAS", ENTRY)
    _seed(dest, home)
    deploy_hooks(QUOIN_SRC, dest)
    settings = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    hooks_dir = str(dest.resolve() / "hooks")
    assert _commands(settings) == [
        ["/opt/x/retired_demo.sh"],
        [hooks_dir + "/retired_demo.sh", "/opt/y/other.sh"],
        [hooks_dir + "/retired_demo.sh"],  # same script, different matcher: kept
    ]
    assert settings["hooks"]["PreToolUse"][2]["matcher"] == "Bash"


def test_install_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dest = home / ".claude"
    dest.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "_RETIRED_HOOK_STANZAS", ENTRY)
    _seed(dest, home)
    deploy_hooks(QUOIN_SRC, dest)
    first = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    deploy_hooks(QUOIN_SRC, dest)
    second = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    assert first == second


def test_event_key_dropped_only_when_removal_emptied_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dest = home / ".claude"
    dest.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "_RETIRED_HOOK_STANZAS", ENTRY)
    hooks_dir = dest.resolve() / "hooks"
    settings = {"hooks": {"PreToolUse": [_stanza(str(hooks_dir / "retired_demo.sh"))], "Notification": []}}
    (dest / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    deploy_hooks(QUOIN_SRC, dest)
    result = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    assert "PreToolUse" not in result["hooks"]
    assert result["hooks"]["Notification"] == []


def test_relative_command_and_null_hooks_are_safe(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dest = home / ".claude"
    dest.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "_RETIRED_HOOK_STANZAS", ENTRY)
    hooks_dir = dest.resolve() / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    # the relative command would resolve into the hooks dir if cwd were dest.
    monkeypatch.chdir(dest)
    settings = {"hooks": {"PreToolUse": [
        _stanza("hooks/retired_demo.sh"),
        {"matcher": "*", "hooks": None},
        {"matcher": "*"},
        _stanza(str(hooks_dir / "retired_demo.sh")),
    ]}}
    (dest / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    deploy_hooks(QUOIN_SRC, dest)
    result = json.loads((dest / "settings.json").read_text(encoding="utf-8"))
    stanzas = result["hooks"]["PreToolUse"]
    assert len(stanzas) == 3
    assert stanzas[0]["hooks"][0]["command"] == "hooks/retired_demo.sh"
    assert stanzas[1]["hooks"] is None
    assert "hooks" not in stanzas[2]
