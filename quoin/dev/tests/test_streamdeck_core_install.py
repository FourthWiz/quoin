"""Core `quoin install` never creates the Stream Deck component and leaves an installed one alone."""
import json
import os
from pathlib import Path

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
import quoin.cli as cli
import quoin.install_prompts as ip
import quoin.installer as inst
from quoin.streamdeck import cli as sdcli, hooks_merge, macos, notice

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
QUOIN_SRC = Path(__file__).resolve().parents[3] / "quoin"
NAMES = ("QuoinStreamDeck",)


class Core:
    """One scripted user-scope `quoin install` inside the test home."""

    def __init__(self, lh, monkeypatch, capsys):
        self.lh, self.mp, self.capsys = lh, monkeypatch, capsys
        self.setup_calls, self.prereq_calls, self.notice_calls = [], [], []
        self.notice_lines = []
        self.real_notice = notice.maybe_print

    def install(self, *flags):
        mp, lh = self.mp, self.lh
        mp.setenv("QUOIN_INSTALL_NO_PROMPT", "1")
        mp.setenv("QUOIN_DISABLE_PREAMBLE_CHECK", "1")
        mp.setattr(ip, "is_interactive", lambda: False)
        mp.setattr(ip, "run_agentdesk_setup", lambda script: self.setup_calls.append(script) or 0)
        mp.setattr(inst, "check_prerequisites", lambda: self.prereq_calls.append(1) or [])
        mp.setattr("sys.platform", "darwin")
        def spying(**kwargs):
            self.notice_calls.append(1)
            return self.real_notice(out=self.notice_lines.append, **kwargs)

        mp.setattr(notice, "maybe_print", spying)
        project = lh.root / "proj"
        project.mkdir(exist_ok=True)
        cwd = os.getcwd()
        mp.setattr("time.sleep", lambda *_a, **_k: None)
        mp.setattr(Path, "home", classmethod(lambda cls: lh.home))
        mp.setattr("shutil.which", lambda *_a, **_k: None)
        os.chdir(str(project))
        try:
            rc = cli.main(["install", "--scope", "user", "--source-dir", str(QUOIN_SRC)] + list(flags))
        finally:
            os.chdir(cwd)
        out = self.capsys.readouterr().out
        return rc, out


@pytest.fixture
def core(lifecycle_home, monkeypatch, capsys):
    return Core(lifecycle_home, monkeypatch, capsys)


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def core_commands(lh):
    found = set()
    for groups in load(lh.claude_settings).get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if "/hooks/" in command and hooks_merge.classify(command, runtime_names=NAMES) is None:
                    found.add(command)
    return found


def assert_core_ran(core, rc, out):
    lh = core.lh
    assert rc == 0, out
    assert core.prereq_calls, "the prerequisite stub was not the one called"
    assert "Prerequisites OK" in out
    skills = lh.home / ".claude" / "skills"
    assert skills.is_dir() and os.listdir(str(skills))
    assert core_commands(lh), "the core hook stanzas were not written"
    assert core.notice_calls == [1]


@pytest.mark.parametrize("flags", [(), ("--setup-agentdesk",)])
def test_core_install_creates_no_component(core, subprocess_calls, flags):
    lh = core.lh
    rc, out = core.install(*flags)
    assert_core_ran(core, rc, out)
    for path in (lh.runtime, lh.plugin_dir, lh.state_root):
        assert not path.exists(), path
    for command in ops.component_commands(lh):
        pytest.fail("a component hook command was written: %s" % command)
    assert not (lh.codex_home / "hooks.json").exists()
    for argv in subprocess_calls:
        text = " ".join(argv)
        for word in ("streamdeck", "quoin-deck", "monitor.py", "Stream Deck"):
            assert word not in text, argv
    assert bool(core.setup_calls) is bool(flags)
    if not flags:
        assert "already set up" not in out


def test_the_notice_appears_once_for_an_older_component(core):
    lh = core.lh
    lc.make_installed(lh, component_version="0.9.0", version_dir_name="0.9.0")
    before = ops.snapshot_owned(lh)
    settings_before = lh.claude_settings.read_bytes()
    rc, out = core.install()
    assert_core_ran(core, rc, out)
    assert len(core.notice_lines) == 1 and "0.9.0" in core.notice_lines[0] and "quoin streamdeck upgrade" in core.notice_lines[0]
    after = ops.snapshot_owned(lh)
    for key, value in before.items():
        if not key.startswith("settings/"):
            assert after[key] == value, key
    assert settings_before != lh.claude_settings.read_bytes()


def test_no_notice_for_a_current_or_unreadable_manifest(core):
    lh = core.lh
    lc.make_installed(lh)
    rc, out = core.install()
    assert_core_ran(core, rc, out)
    assert core.notice_lines == []
    (lh.runtime / "install-manifest.json").write_text("{broken")
    core.notice_calls.clear()
    core.notice_lines.clear()
    rc, out = core.install()
    assert_core_ran(core, rc, out)
    assert core.notice_lines == []


def test_the_notice_never_raises(lifecycle_home, monkeypatch):
    monkeypatch.setattr(notice.manifest, "load_any", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    assert notice.maybe_print(out=lambda line: None) is False


def test_component_entries_survive_core_install_upgrade_and_uninstall(core, monkeypatch):
    lh = core.lh
    env = ops.make_env(lh)
    monkeypatch.setattr(sdcli, "_ctx_factory", env.ctx_factory)
    monkeypatch.setattr(sdcli, "_sysops", lambda: env.sysops)
    monkeypatch.setattr(sdcli, "_platform", lambda: DARWIN)
    monkeypatch.setattr(sdcli, "_now", lambda: lc.NOW)
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", env.environ["ZELLIJ_SOCKET_DIR"])
    assert cli.main(["streamdeck", "install"]) == 0
    core.capsys.readouterr()
    rc, out = core.install()
    assert_core_ran(core, rc, out)
    commands = core_commands(lh)
    assert commands
    assert cli.main(["streamdeck", "status", "--json"]) == 0
    core.capsys.readouterr()
    assert cli.main(["streamdeck", "upgrade"]) == 0
    assert core_commands(lh) == commands
    assert cli.main(["streamdeck", "uninstall"]) == 0
    assert core_commands(lh) == commands
