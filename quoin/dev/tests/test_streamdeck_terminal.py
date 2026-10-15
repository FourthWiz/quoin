import json
import os
import shutil
import subprocess

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

SCRIPT = h.PAYLOAD_DIR / "terminal.zsh"
ZSH = "/bin/zsh" if os.path.exists("/bin/zsh") else shutil.which("zsh")
needs_zsh = pytest.mark.skipif(ZSH is None, reason="zsh missing")


def run(tmp_path, *args, env=None, cwd=None):
    base = {"HOME": str(tmp_path / "zhome"), "PATH": "/usr/bin:/bin"}
    base.update(env or {})
    os.makedirs(base["HOME"], exist_ok=True)
    return subprocess.run([ZSH, str(SCRIPT), *args], env=base, cwd=str(cwd or tmp_path), capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=20)


def fake_agentdesk(tmp_path, marker=True):
    path = tmp_path / "ad" / "agentdesk.zsh"
    path.parent.mkdir(exist_ok=True)
    path.write_text(("AGENTDESK_API_VERSION=1\n" if marker else "# no marker\n") +
                    'agentdesk() { print -r -- "$@" > "$ARGV_LOG" }\n')
    return path


def make_project(tmp_path, name="proj"):
    project = tmp_path / name
    project.mkdir()
    return project


@needs_zsh
def test_new_uses_the_public_interface(tmp_path):
    project = make_project(tmp_path)
    log = tmp_path / "argv.log"
    result = run(tmp_path, "new", "s1", str(project),
                 env={"QUOIN_DECK_AGENTDESK": str(fake_agentdesk(tmp_path)), "ARGV_LOG": str(log)})
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == "--name s1 --panes codex,codex,shell --no-prompt --if-exists attach"


@needs_zsh
@pytest.mark.parametrize("variant", ["no-marker", "unset", "missing-file"])
def test_new_without_a_usable_agentdesk_fails_cleanly(tmp_path, variant):
    project = make_project(tmp_path)
    log = tmp_path / "argv.log"
    env = {"ARGV_LOG": str(log)}
    if variant == "no-marker":
        env["QUOIN_DECK_AGENTDESK"] = str(fake_agentdesk(tmp_path, marker=False))
    elif variant == "missing-file":
        env["QUOIN_DECK_AGENTDESK"] = str(tmp_path / "nope.zsh")
    result = run(tmp_path, "new", "s1", str(project), env=env)
    assert result.returncode == 1 and "Agent Desk is not installed" in result.stderr
    assert not log.exists()


@needs_zsh
def test_attach_runs_zellij_attach(tmp_path):
    project = make_project(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "z.log"
    fake = bindir / "zellij"
    fake.write_text('#!/bin/sh\necho "$@" > "%s"\n' % log)
    fake.chmod(0o755)
    result = run(tmp_path, "attach", "s1", str(project), env={"PATH": "%s:/usr/bin:/bin" % bindir})
    assert result.returncode == 0 and log.read_text().strip() == "attach s1"


@needs_zsh
def test_claude_found_through_recorded_tool_dir(payload, deck_home, tmp_path):
    tools = tmp_path / "recorded-tools"
    tools.mkdir()
    claude = tools / "claude"
    claude.write_text("#!/bin/sh\npwd\n")
    claude.chmod(0o755)
    manifest = json.loads((deck_home.runtime / "install-manifest.json").read_text())
    manifest["binaries"]["claude"] = str(claude)
    (deck_home.runtime / "install-manifest.json").write_text(json.dumps(manifest))
    de = payload("deckenv")
    path_env = de.load({"HOME": str(deck_home.home), "QUOIN_DECK_RUNTIME": str(deck_home.runtime),
                        "PATH": "/usr/bin:/bin"}).path_env
    assert str(tools) in path_env.split(":")
    project = make_project(tmp_path, "my proj $(touch pwned) ; x")
    bare = run(tmp_path, "claude", "s1", str(project))
    assert bare.returncode == 127
    result = run(tmp_path, "claude", "s1", str(project), env={"PATH": path_env}, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(project))
    assert not (tmp_path / "pwned").exists() and not (project / "pwned").exists()


@needs_zsh
def test_missing_tool_prints_a_message_and_does_not_wait(tmp_path):
    project = make_project(tmp_path)
    result = run(tmp_path, "codex", "s1", str(project))
    assert result.returncode == 127 and "codex not found in PATH" in result.stderr


@needs_zsh
def test_unknown_mode(tmp_path):
    project = make_project(tmp_path)
    assert run(tmp_path, "bogus", "s", str(project)).returncode == 2


@needs_zsh
def test_syntax_and_static_shape():
    assert subprocess.run([ZSH, "-n", str(SCRIPT)], capture_output=True).returncode == 0
    text = SCRIPT.read_text()
    assert "export PATH=" not in text and "/opt/homebrew" not in text
    assert "_agentdesk_save_layout" not in text and "_agentdesk_layout_key" not in text
