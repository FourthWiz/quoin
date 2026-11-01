"""Key hints in the launch message and in agentdesk-attach.

Runs the real agentdesk.zsh against the recording zellij stub from the public
launch tests, in a hermetic HOME and a private runtime dir.
"""
import subprocess
from pathlib import Path

import pytest

import test_agentdesk_public_launch as pl
from test_agentdesk_public_launch import rt_dir  # noqa: F401  (fixture)
from quoin import agentdesk_sessions as ads

pytestmark = pl.pytestmark

NEEDLES = ("Alt s, then d", "Alt s, then q", "macos-option-as-alt", "Ctrl+Q")
CREATE_MARK = "STUB-CREATE-CALLED"


def _mark_create(env):
    """Make the stub print a line on stdout when the create call arrives."""
    stub = Path(env["PATH"].split(":")[0]) / "zellij"
    text = stub.read_text().replace(
        'if [ "$prev" = "--new-session-with-layout" ]; then',
        'if [ "$prev" = "--new-session-with-layout" ]; then\n    echo %s' % CREATE_MARK,
        1,
    )
    stub.write_text(text)


def _launch(tmp_path, rt_dir, args, sessions=()):
    env = pl._env(tmp_path, rt_dir, sessions=sessions)
    _mark_create(env)
    return pl._run_pipe(args, env, tmp_path, rt_dir)


def test_launch_banner_shows_hints_before_the_create_call(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell", "--no-prompt"])
    pl._assert_created(res, "deck-1")
    assert CREATE_MARK in res.out
    head = res.out.split(CREATE_MARK)[0]
    for needle in NEEDLES:
        assert needle in head, needle
    assert res.out.count("Alt s, then d") == 1


def test_attach_command_shows_the_same_hints(tmp_path, rt_dir):
    env = pl._env(tmp_path, rt_dir, sessions=["deck-1"])
    argv = ["zsh", "-c", 'source "$0"; agentdesk-attach deck-1', str(pl.AGENTDESK_ZSH)]
    proc = subprocess.run(argv, env=env, cwd=str(tmp_path / "proj"), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=pl.SUBPROCESS_TIMEOUT)
    assert proc.returncode == 0, proc.stderr
    for needle in NEEDLES:
        assert needle in proc.stdout, needle
    assert proc.stdout.count("Alt s, then d") == 1
    assert (tmp_path / "cap" / "attach.txt").read_text() == "deck-1"


def test_attach_existing_through_launcher_prints_hints_once(tmp_path, rt_dir):
    res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell", "--no-prompt"],
                  sessions=["deck-1"])
    assert "Attaching to existing session" in res.out
    assert CREATE_MARK not in res.out
    assert res.out.count("Alt s, then d") == 1


def test_missing_session_attach_prints_no_hints(tmp_path, rt_dir):
    env = pl._env(tmp_path, rt_dir)
    argv = ["zsh", "-c", 'source "$0"; agentdesk-attach nosuch', str(pl.AGENTDESK_ZSH)]
    proc = subprocess.run(argv, env=env, cwd=str(tmp_path / "proj"), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=pl.SUBPROCESS_TIMEOUT)
    assert proc.returncode == 1 and "Alt s" not in proc.stdout


def test_zsh_hints_match_python_key_hints_byte_for_byte(tmp_path, rt_dir):
    argv = ["zsh", "-f", "-c", 'source "$0"; _agentdesk_key_hints', str(pl.AGENTDESK_ZSH)]
    proc = subprocess.run(argv, env=pl._env(tmp_path, rt_dir), cwd=str(tmp_path),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=pl.SUBPROCESS_TIMEOUT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout != ""
    assert proc.stdout == ads.KEY_HINTS
