"""setup-agentdesk.sh --refresh and the refresh step of ``quoin install``.

A machine that already ran the agentdesk setup gets the new helper and layout
on every user-scope install, without the Homebrew, keybinding or ~/.zshrc
steps of the full setup.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import quoin.install_prompts as ip

from _agentdesk_kdl_helpers import SETUP_SH, make_bin, make_env, run_fn
from test_install_questions_cli import Session

needs_shells = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("zsh") is None or shutil.which("python3") is None,
    reason="bash, zsh and python3 are required",
)


def _run(tmp_path, *args):
    env = make_env(tmp_path, make_bin(tmp_path, with_zellij=False))
    return subprocess.run(
        ["bash", str(SETUP_SH), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _layout(tmp_path) -> Path:
    return Path(tmp_path) / "home" / ".config" / "zellij" / "layouts" / "agent-desk.kdl"


@needs_shells
class TestRefreshFlag:
    def test_replaces_the_legacy_layout_and_touches_nothing_else(self, tmp_path):
        legacy = run_fn(tmp_path, "_agentdesk_legacy_layout")
        assert legacy.returncode == 0, legacy.stderr
        layout = _layout(tmp_path)
        layout.parent.mkdir(parents=True)
        layout.write_text(legacy.stdout, encoding="utf-8")

        done = _run(tmp_path, "--refresh")

        assert done.returncode == 0, done.stdout + done.stderr
        assert layout.read_text(encoding="utf-8").startswith("// agentdesk: generated standard")
        assert list(layout.parent.glob("agent-desk.kdl.agentdesk-layout.*.bak"))
        home = Path(tmp_path) / "home"
        assert (home / ".config" / "agentdesk" / "agentdesk.zsh").is_file()
        assert not (home / ".zshrc").exists()
        assert not (home / ".config" / "zellij" / "config.kdl").exists()
        assert "Step 1" not in done.stdout
        assert "Step 3b" not in done.stdout
        assert "refresh complete" in done.stdout

    def test_customised_layout_is_kept_with_new_one_beside_it(self, tmp_path):
        layout = _layout(tmp_path)
        layout.parent.mkdir(parents=True)
        layout.write_text("layout {\n    pane\n}\n", encoding="utf-8")

        done = _run(tmp_path, "--refresh")

        assert done.returncode == 0, done.stdout + done.stderr
        assert layout.read_text(encoding="utf-8") == "layout {\n    pane\n}\n"
        assert Path(str(layout) + ".new").is_file()
        assert "Kept your customised layout" in done.stdout

    def test_cannot_be_combined(self, tmp_path):
        done = _run(tmp_path, "--refresh", "--assume-alt")
        assert done.returncode == 2
        assert "--refresh cannot be combined" in done.stderr

    def test_listed_in_help(self, tmp_path):
        assert "--refresh" in _run(tmp_path, "--help").stdout


@pytest.fixture
def sess(tmp_path, monkeypatch, capsys):
    return Session(tmp_path, monkeypatch, capsys)


class TestInstallRefresh:
    def _set_up(self, sess):
        (sess.home / ".zshrc").write_text(ip.AGENTDESK_SOURCE_LINE + "\n")

    def test_already_set_up_install_refreshes(self, sess):
        self._set_up(sess)
        assert sess.run(scope="user", interactive=False) == 0
        assert sess.refresh_calls == [sess.home / ".config/agentdesk/setup-agentdesk.sh"]
        assert sess.runner_calls == []

    def test_interactive_install_refreshes_without_asking(self, sess):
        self._set_up(sess)
        assert sess.run(scope="user", answers=["n", "n"]) == 0
        assert sess.prompts_asked == 2
        assert len(sess.refresh_calls) == 1

    def test_not_set_up_does_not_refresh(self, sess):
        assert sess.run(scope="user", interactive=False) == 0
        assert sess.refresh_calls == []

    def test_full_setup_flag_does_not_also_refresh(self, sess):
        self._set_up(sess)
        assert sess.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        assert len(sess.runner_calls) == 1
        assert sess.refresh_calls == []

    def test_project_scope_does_not_refresh(self, sess):
        self._set_up(sess)
        assert sess.run(interactive=False) == 0
        assert sess.refresh_calls == []

    def test_failed_refresh_is_a_warning(self, sess, capsys):
        self._set_up(sess)
        sess.refresh_rc = 3
        assert sess.run(scope="user", interactive=False) == 0
        err = capsys.readouterr().err
        assert "agentdesk refresh exited with status 3" in err
        assert "--refresh" in err
