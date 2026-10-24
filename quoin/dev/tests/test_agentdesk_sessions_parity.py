"""Parity between the Python session helpers and the surfaces they mirror.

Each test compares the real output of both sides: the zsh launcher run in a
sandbox, or the Stream Deck payload's own parser. Nothing here touches the real
home directory, the real socket directory or a real zellij.
"""
import os
import shutil
import subprocess

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload  # noqa: F401
from quoin import agentdesk_sessions as ads

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

AGENTDESK = h.REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"

LIST_FIXTURE = (
    "proj-agents [Created 2h 3m ago] \n"
    "here-agents [Created 5s ago] (current)\n"
    "old-agents_2 [Created 1day ago] (EXITED - attach to resurrect)\n"
    "\x1b[31mcol-agents\x1b[m [Created 4m ago] \n"
    "stray line without a created marker\n"
)


def test_parser_matches_stream_deck_controller(payload):
    controller = payload("controller")
    text = LIST_FIXTURE.replace("\x1b[31m", "").replace("\x1b[m", "")
    ours = [(s.name, s.live) for s in ads.parse_list_output(text)]
    assert ours, "fixture parsed to nothing"
    assert ours == controller.parse_sessions(text)


@pytest.fixture
def zsh_sandbox(tmp_path):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh missing")
    home, work, stub, rt = (tmp_path / n for n in ("zh", "zw", "stub", "rt"))
    for d in (home, work, stub, rt):
        d.mkdir()
    for name in ("mkdir", "zellij"):
        (stub / name).write_text("#!/bin/sh\nexit 0\n")
        (stub / name).chmod(0o755)
    environ = {"HOME": str(home), "PATH": "%s:/usr/bin:/bin" % stub, "XDG_RUNTIME_DIR": str(rt)}

    def call(func, arg):
        proc = subprocess.run(
            [zsh, "-f", "-c", 'source "$1"; ' + func + ' "$2"', "_", str(AGENTDESK), arg],
            env=environ, cwd=str(work), capture_output=True, text=True, timeout=30)
        return proc.stdout.splitlines()[-1] if proc.stdout.strip() else None

    return call


BASES = ["proj", "my proj", "a.b", ".hidden", "-lead", "trail-", "a---b", "a!!b", "x_y", "---"]


@pytest.mark.parametrize("base", BASES)
def test_session_name_matches_project_base(zsh_sandbox, base):
    # The launcher always hands over an absolute realpath, so feed both sides one.
    root = "/r/" + base
    got = zsh_sandbox("_agentdesk_session_name", root)
    assert got, "zsh produced no output"
    assert got == ads.project_base(root)


@pytest.mark.parametrize("root", ["/", ""])
def test_session_name_falls_back_to_agentdesk(zsh_sandbox, root):
    got = zsh_sandbox("_agentdesk_session_name", root)
    assert got, "zsh produced no output"
    assert got == ads.project_base(root) == "agentdesk"


@pytest.mark.parametrize("raw", BASES + ["a b c", "ok_name-1.x"])
def test_sanitize_matches_launcher(zsh_sandbox, raw):
    got = zsh_sandbox("_sanitize_session_name", raw)
    expected = ads.sanitize_name(raw)
    if expected:
        assert got == expected
    else:
        assert not got
