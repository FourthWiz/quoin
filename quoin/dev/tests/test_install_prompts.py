"""Unit tests for the install-time question helpers and test isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

import quoin.installer as installer
import quoin.install_prompts as ip


REPO = Path(__file__).resolve().parents[3]


def _script_input(monkeypatch, answers):
    it = iter(answers)
    calls = []

    def fake_input(*args):
        calls.append(args)
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", fake_input)
    return calls


class _Stream:
    def __init__(self, tty=True, boom=False):
        self._tty, self._boom = tty, boom

    def isatty(self):
        if self._boom:
            raise ValueError("closed")
        return self._tty


class TestAskYesNo:
    def test_question_on_stdout_and_bare_input(self, monkeypatch, capsys):
        calls = _script_input(monkeypatch, [""])
        ip.ask_yes_no("Do it?", default=True)
        out = capsys.readouterr()
        assert "Do it? [Y/n] " in out.out
        assert calls == [()]

    @pytest.mark.parametrize("default,expected", [(True, True), (False, False)])
    def test_enter_gives_default(self, monkeypatch, capsys, default, expected):
        _script_input(monkeypatch, [""])
        assert ip.ask_yes_no("q", default=default) is expected
        assert ("[Y/n]" if default else "[y/N]") in capsys.readouterr().out

    @pytest.mark.parametrize(
        "answer,expected", [("y", True), ("YES", True), ("n", False), ("No", False), (" y ", True)]
    )
    def test_answers(self, monkeypatch, answer, expected):
        _script_input(monkeypatch, [answer])
        assert ip.ask_yes_no("q", default=not expected) is expected

    def test_invalid_reasks(self, monkeypatch, capsys):
        calls = _script_input(monkeypatch, ["maybe", "y"])
        assert ip.ask_yes_no("q", default=False) is True
        assert len(calls) == 2
        assert "Please answer y or n." in capsys.readouterr().out

    @pytest.mark.parametrize("exc", [EOFError(), KeyboardInterrupt()])
    def test_abort(self, monkeypatch, capsys, exc):
        _script_input(monkeypatch, [exc])
        with pytest.raises(SystemExit) as info:
            ip.ask_yes_no("q", default=True)
        assert info.value.code == 1
        assert "Aborted." in capsys.readouterr().err


class TestIsInteractive:
    def _streams(self, monkeypatch, stdin, stdout):
        monkeypatch.delenv(ip.NO_PROMPT_ENV, raising=False)
        monkeypatch.setattr("sys.stdin", stdin)
        monkeypatch.setattr("sys.stdout", stdout)

    def test_both_tty(self, monkeypatch):
        self._streams(monkeypatch, _Stream(True), _Stream(True))
        assert ip.is_interactive() is True

    @pytest.mark.parametrize("a,b", [(True, False), (False, True), (False, False)])
    def test_not_both_tty(self, monkeypatch, a, b):
        self._streams(monkeypatch, _Stream(a), _Stream(b))
        assert ip.is_interactive() is False

    def test_env_off_switch(self, monkeypatch):
        self._streams(monkeypatch, _Stream(True), _Stream(True))
        monkeypatch.setenv(ip.NO_PROMPT_ENV, "1")
        assert ip.is_interactive() is False

    def test_isatty_raises(self, monkeypatch):
        self._streams(monkeypatch, _Stream(boom=True), _Stream(True))
        assert ip.is_interactive() is False


class TestTexts:
    def test_mod_question(self, tmp_path):
        q = ip.mod_question("workflow-tasks", tmp_path)
        assert "/quoin-tasks" in q and str(tmp_path) in q and "--with-workflow-tasks" in q

    def test_mod_commands_cover_registry(self):
        assert set(ip.MOD_COMMANDS) == {m.name for m in installer.OPT_IN_MODS}


class TestIsolation:
    def test_env_forces_non_interactive(self, monkeypatch):
        import os
        assert os.environ["QUOIN_INSTALL_NO_PROMPT"] == "1"
        monkeypatch.setattr("sys.stdin", _Stream(True))
        monkeypatch.setattr("sys.stdout", _Stream(True))
        assert ip.is_interactive() is False
