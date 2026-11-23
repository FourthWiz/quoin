"""Install questions in `quoin install`: the mod question, flags, aborts, help."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import quoin.cli as cli
import quoin.install_prompts as ip
import quoin.installer as inst

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"
INSTALL_SH = QUOIN_SRC / "install.sh"


def _no_which(*_a, **_k):
    return None


class Session:
    """One scripted install: fake home, optional project, scripted answers."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        self.tmp, self.mp, self.capsys = tmp_path, monkeypatch, capsys
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.project = tmp_path / "proj"
        self.project.mkdir(exist_ok=True)
        self.prompts_asked = 0

    def run(self, *, scope="project", answers=None, interactive=True, **flags):
        mp = self.mp
        if interactive:
            mp.delenv("QUOIN_INSTALL_NO_PROMPT", raising=False)
            mp.setattr(ip, "is_interactive", lambda: True)
        it = iter(answers or [])
        def fake_input(*a):
            self.prompts_asked += 1
            try:
                v = next(it)
            except StopIteration:
                raise AssertionError("unexpected extra question")
            if isinstance(v, BaseException):
                raise v
            return v
        mp.setattr("builtins.input", fake_input)
        ns = dict(
            scope="project" if scope == "project" else "user",
            allow_hook_merge=True, runtime="claude", check=False,
            source_dir=str(QUOIN_SRC), force_merge=False, dev=False, use_pip=False,
            with_workflow_tasks=False, remove_workflow_tasks=False,
            no_addons=True,
        )
        if scope == "project":
            ns["scope"] = f"project:{self.project}"
        ns.update(flags)
        args = argparse.Namespace(**ns)
        cwd = os.getcwd()
        with contextlib.ExitStack() as st:
            st.enter_context(unittest_patch(inst, "check_prerequisites", lambda: []))
            st.enter_context(unittest_patch(inst, "stage_plugin", lambda *a, **kw: "0+test"))
            st.enter_context(unittest_patch(inst, "install_plugin", lambda *a, **kw: "install"))
            st.enter_context(unittest_patch_call("time.sleep"))
            st.enter_context(unittest_patch(Path, "home", classmethod(lambda cls: self.home)))
            st.enter_context(unittest_patch_call("shutil.which", _no_which))
            os.chdir(self.project)
            try:
                rc = cli._cmd_claude_install(args)
            finally:
                os.chdir(cwd)
        return rc

    def prompts(self):
        return self.capsys.readouterr().out


def unittest_patch(obj, name, value):
    import unittest.mock
    return unittest.mock.patch.object(obj, name, value)


def unittest_patch_call(target, new=None):
    import unittest.mock
    return unittest.mock.patch(target, new) if new is not None else unittest.mock.patch(target)


@pytest.fixture
def sess(tmp_path, monkeypatch, capsys):
    return Session(tmp_path, monkeypatch, capsys)


def _installed(project):
    return (project / ".claude" / "skills" / "workflow-tasks").exists()


class TestModQuestion:
    def test_enter_installs(self, sess):
        assert sess.run(answers=[""]) == 0
        assert _installed(sess.project)
        assert sess.prompts_asked == 1
        assert "/quoin-tasks" in sess.prompts()

    @pytest.mark.parametrize("answer,expected", [("n", False), ("y", True)])
    def test_answers(self, sess, answer, expected):
        assert sess.run(answers=[answer]) == 0
        assert _installed(sess.project) == expected

    def test_abort_writes_nothing(self, sess, capsys):
        with pytest.raises(SystemExit) as info:
            sess.run(answers=[EOFError()])
        assert info.value.code == 1
        assert "Aborted." in capsys.readouterr().err
        assert not (sess.project / ".claude").exists()
        assert not (sess.project / "CLAUDE.md").exists()

    def test_with_flag_suppresses_the_question(self, sess):
        assert sess.run(answers=[], with_workflow_tasks=True) == 0
        assert sess.prompts_asked == 0
        assert _installed(sess.project)

    def test_remove_flag_suppresses_the_question(self, sess):
        assert sess.run(answers=[], remove_workflow_tasks=True) == 0
        assert sess.prompts_asked == 0
        assert not _installed(sess.project)

    def test_installed_mod_refreshed_without_question(self, sess):
        assert sess.run(interactive=False, with_workflow_tasks=True) == 0
        edited = sess.project / ".claude/skills/workflow-tasks/hooks/tasks.ts"
        good = edited.read_text()
        edited.write_text("// edited\n")
        assert sess.run(answers=[]) == 0
        assert sess.prompts_asked == 0
        assert edited.read_text() == good

    def test_foreign_folder_not_offered(self, sess, capsys):
        folder = sess.project / ".claude/skills/workflow-tasks/.claude-plugin"
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text(json.dumps({"name": "other"}))
        before = folder.joinpath("plugin.json").read_text()
        assert sess.run(answers=[]) == 0
        assert sess.prompts_asked == 0
        out = capsys.readouterr().out
        assert "not offering the workflow-tasks mod" in out
        assert folder.joinpath("plugin.json").read_text() == before

    def test_user_copy_blocks_project_offer(self, sess, capsys):
        inst.deploy_workflow_tasks(QUOIN_SRC, sess.home / ".claude", strict=True)
        assert sess.run(answers=[]) == 0
        assert sess.prompts_asked == 0
        out = capsys.readouterr().out
        assert "not offering the workflow-tasks mod" in out
        assert "double load" in out

    def test_non_interactive_asks_nothing(self, sess):
        assert sess.run(interactive=False, answers=[]) == 0
        assert sess.prompts_asked == 0
        assert not _installed(sess.project)

    def test_preflight_warning_precedes_question(self, sess, capsys):
        # A project copy of the mod alongside a user-scope install: the warning
        # must be on screen before the question that would add the second copy.
        inst.deploy_workflow_tasks(QUOIN_SRC, sess.project / ".claude", strict=True)
        assert sess.run(scope="user", answers=["n"]) == 0
        cap = capsys.readouterr()
        assert cap.err.count("will load alongside this user copy") == 1

    def test_codex_runtime_rejected(self):
        with pytest.raises(SystemExit) as info:
            cli.main(["install", "--runtime", "codex", "--with-workflow-tasks"])
        assert info.value.code == 2


class TestHelp:
    def test_cli_help_lists_flags(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["install", "--help"])
        out = " ".join(capsys.readouterr().out.split())
        for flag in ("--with-workflow-tasks", "--remove-workflow-tasks"):
            assert flag in out
        for gone in ("--with-context-tracker", "--setup-agentdesk"):
            assert gone not in out
        assert out.count("answer yes when asked") == 1
        assert "Opt-in, off by default" not in out

    def test_install_sh_help_and_forwarding(self):
        out = subprocess.run(
            ["bash", str(INSTALL_SH), "--help"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60,
        ).stdout
        for flag in ("--with-workflow-tasks", "--remove-workflow-tasks"):
            assert flag in out
        for gone in ("--with-context-tracker", "--setup-agentdesk"):
            assert gone not in out
        assert "answer yes when asked" in out
        assert "Off by default" not in out.split("--with-workflow-tasks\n", 1)[1]

    def test_install_sh_syntax(self):
        assert subprocess.run(["bash", "-n", str(INSTALL_SH)]).returncode == 0
