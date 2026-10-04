"""Install questions in `quoin install`: mods, agentdesk, flags, aborts, help."""
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
        self.runner_calls = []

    def run(self, *, scope="project", answers=None, interactive=True, darwin=True, **flags):
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
        mp.setattr("sys.platform", "darwin" if darwin else "linux")
        mp.setattr(ip, "run_agentdesk_setup", lambda script: self.runner_calls.append(script) or 0)
        ns = dict(
            scope="project" if scope == "project" else "user",
            allow_hook_merge=True, runtime="claude", check=False,
            source_dir=str(QUOIN_SRC), force_merge=False, dev=False, use_pip=False,
            with_context_tracker=False, remove_context_tracker=False,
            with_workflow_tasks=False, remove_workflow_tasks=False,
        )
        if scope == "project":
            ns["scope"] = f"project:{self.project}"
        ns.update(flags)
        args = argparse.Namespace(**ns)
        cwd = os.getcwd()
        with contextlib.ExitStack() as st:
            st.enter_context(unittest_patch(inst, "check_prerequisites", lambda: []))
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


def _mods(project):
    skills = project / ".claude" / "skills"
    return (
        (skills / "context-tracker").exists(),
        (skills / "workflow-tasks").exists(),
    )


class TestModQuestions:
    def test_enter_enter_installs_both(self, sess):
        assert sess.run(answers=["", ""]) == 0
        out = sess.prompts()
        assert _mods(sess.project) == (True, True)
        assert sess.prompts_asked == 2
        assert out.index("/ctx") < out.index("/quoin-tasks")

    @pytest.mark.parametrize(
        "answers,expected",
        [(["n", "n"], (False, False)), (["y", "n"], (True, False)), (["n", "y"], (False, True))],
    )
    def test_answers(self, sess, answers, expected):
        assert sess.run(answers=answers) == 0
        assert _mods(sess.project) == expected

    @pytest.mark.parametrize("answers", [[EOFError()], ["y", KeyboardInterrupt()]])
    def test_abort_writes_nothing(self, sess, answers, capsys):
        with pytest.raises(SystemExit) as info:
            sess.run(answers=answers)
        assert info.value.code == 1
        assert "Aborted." in capsys.readouterr().err
        assert not (sess.project / ".claude").exists()
        assert not (sess.project / "CLAUDE.md").exists()

    def test_with_flag_suppresses_its_question(self, sess):
        assert sess.run(answers=["n"], with_workflow_tasks=True) == 0
        assert sess.prompts_asked == 1
        assert _mods(sess.project) == (False, True)
        assert "context-tracker" in sess.prompts()

    def test_remove_flag_suppresses_its_question(self, sess):
        assert sess.run(answers=["y"], remove_context_tracker=True) == 0
        assert sess.prompts_asked == 1
        assert _mods(sess.project) == (False, True)

    def test_installed_mod_refreshed_without_question(self, sess):
        assert sess.run(interactive=False, with_workflow_tasks=True) == 0
        edited = sess.project / ".claude/skills/workflow-tasks/hooks/tasks.ts"
        good = edited.read_text()
        edited.write_text("// edited\n")
        assert sess.run(answers=["n"]) == 0
        assert sess.prompts_asked == 1  # only the context-tracker question
        assert edited.read_text() == good

    def test_foreign_folder_not_offered(self, sess, capsys):
        folder = sess.project / ".claude/skills/workflow-tasks/.claude-plugin"
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text(json.dumps({"name": "other"}))
        before = folder.joinpath("plugin.json").read_text()
        assert sess.run(answers=["n"]) == 0
        assert sess.prompts_asked == 1
        out = capsys.readouterr().out
        assert "not offering the workflow-tasks mod" in out
        assert folder.joinpath("plugin.json").read_text() == before

    def test_user_copy_blocks_project_offer(self, sess, capsys):
        inst.deploy_workflow_tasks(QUOIN_SRC, sess.home / ".claude", strict=True)
        assert sess.run(answers=["n"]) == 0
        assert sess.prompts_asked == 1
        out = capsys.readouterr().out
        assert "not offering the workflow-tasks mod" in out
        assert "double load" in out

    def test_non_interactive_asks_nothing(self, sess):
        assert sess.run(interactive=False, answers=[]) == 0
        assert sess.prompts_asked == 0
        assert _mods(sess.project) == (False, False)

    def test_preflight_warning_precedes_question(self, sess, capsys):
        # A project copy of the mod alongside a user-scope install: the warning
        # must be on screen before the question that would add the second copy.
        inst.deploy_context_tracker(QUOIN_SRC, sess.project / ".claude", strict=True)
        assert sess.run(scope="user", answers=["n", "n", "n"]) == 0
        cap = capsys.readouterr()
        assert cap.err.count("will load alongside this user copy") == 1


class TestAgentdeskQuestion:
    def test_asked_last_default_no(self, sess):
        assert sess.run(scope="user", answers=["n", "n", ""]) == 0
        assert sess.prompts_asked == 3
        assert sess.runner_calls == []
        assert "Ghostty" in sess.prompts()

    def test_yes_runs_deployed_script_once(self, sess):
        assert sess.run(scope="user", answers=["n", "n", "y"]) == 0
        assert sess.runner_calls == [sess.home / ".config/agentdesk/setup-agentdesk.sh"]

    def test_skipped_when_zshrc_has_source_line(self, sess, capsys):
        (sess.home / ".zshrc").write_text(ip.AGENTDESK_SOURCE_LINE + "\n")
        assert sess.run(scope="user", answers=["n", "n"]) == 0
        assert sess.prompts_asked == 2
        assert "--setup-agentdesk" not in capsys.readouterr().out

    def test_not_asked_off_darwin_but_hint_and_flag_remain(self, sess, capsys):
        assert sess.run(scope="user", darwin=False, answers=["n", "n"]) == 0
        assert sess.prompts_asked == 2
        assert "--setup-agentdesk" in capsys.readouterr().out
        assert sess.run(scope="user", darwin=False, answers=["n", "n"], setup_agentdesk=True) == 0
        assert len(sess.runner_calls) == 1

    def test_never_asked_at_project_scope(self, sess):
        sess.run(answers=["n", "n"])
        assert sess.prompts_asked == 2

    def test_abort_at_agentdesk_question_writes_nothing(self, sess, capsys):
        with pytest.raises(SystemExit) as info:
            sess.run(scope="user", answers=["n", "n", EOFError()])
        assert info.value.code == 1
        assert "Aborted." in capsys.readouterr().err
        assert not (sess.home / ".claude").exists()
        assert not (sess.home / ".config" / "agentdesk").exists()

    def test_flags_answer_everything(self, sess):
        assert sess.run(
            scope="user", answers=[], with_context_tracker=True,
            with_workflow_tasks=True, setup_agentdesk=True,
        ) == 0
        assert sess.prompts_asked == 0
        assert len(sess.runner_calls) == 1

    def test_project_scope_flag_rejected_before_any_write(self, sess, capsys):
        with pytest.raises(SystemExit) as info:
            sess.run(setup_agentdesk=True, interactive=False)
        assert info.value.code == 2
        assert not (sess.project / ".claude").exists()

    def test_codex_runtime_rejected(self):
        with pytest.raises(SystemExit) as info:
            cli.main(["install", "--runtime", "codex", "--setup-agentdesk"])
        assert info.value.code == 2


class TestHelp:
    def test_cli_help_lists_flags(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["install", "--help"])
        out = " ".join(capsys.readouterr().out.split())
        for flag in ("--with-workflow-tasks", "--remove-workflow-tasks", "--setup-agentdesk"):
            assert flag in out
        assert out.count("answer yes when asked") == 2
        assert "Opt-in, off by default" not in out

    def test_install_sh_help_and_forwarding(self):
        out = subprocess.run(
            ["bash", str(INSTALL_SH), "--help"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60,
        ).stdout
        for flag in (
            "--with-context-tracker", "--with-workflow-tasks",
            "--remove-workflow-tasks", "--setup-agentdesk",
        ):
            assert flag in out
        assert "answer yes when asked" in out
        assert "Off by default" not in out.split("--with-context-tracker\n", 1)[1]
        src = INSTALL_SH.read_text()
        assert '--setup-agentdesk) SETUP_AGENTDESK_FLAG="--setup-agentdesk"' in src
        assert 'INSTALL_ARGS+=("$SETUP_AGENTDESK_FLAG")' in src
        assert not any(
            "bash" in ln and "setup-agentdesk.sh" in ln for ln in src.splitlines()
        )

    def test_install_sh_syntax(self):
        assert subprocess.run(["bash", "-n", str(INSTALL_SH)]).returncode == 0
