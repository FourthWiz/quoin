"""--setup-agentdesk: last-step run, warning-only failures, hint wording,
non-interactive invariance and a real-terminal smoke test."""
from __future__ import annotations

import errno
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import quoin.cli as cli
import quoin.install_prompts as ip
import quoin.installer as inst

import conftest
from test_install_questions_cli import QUOIN_SRC, REPO, Session

try:
    import pty
except ImportError:  # pragma: no cover
    pty = None


@pytest.fixture
def sess(tmp_path, monkeypatch, capsys):
    return Session(tmp_path, monkeypatch, capsys)


def _tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            if p.name == "quoin-runtime.json":
                data = re.sub(rb'"[^"]*(time|at)[^"]*": *"[^"]*"', b"", data)
            out[p.relative_to(root).as_posix()] = data
    return out


class TestSetupFlag:
    def test_flag_runs_runner_and_prints_finished(self, sess, capsys):
        assert sess.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        out = capsys.readouterr().out
        assert len(sess.runner_calls) == 1
        assert "agentdesk setup finished" in out
        assert "re-run the install with --setup-agentdesk" not in out

    def test_runner_sees_install_record(self, tmp_path, monkeypatch, capsys):
        s = Session(tmp_path, monkeypatch, capsys)
        record_seen = []
        # Session.run installs its own runner stub, so swap ours in once the
        # agentdesk files are deployed (still before the install record).
        real_deploy = inst.deploy_agentdesk

        def deploy(source_dir, dest):
            real_deploy(source_dir, dest)
            monkeypatch.setattr(
                ip, "run_agentdesk_setup",
                lambda script: record_seen.append(
                    any((s.home / ".claude").glob("*runtime*.json"))
                ) or 0,
            )

        monkeypatch.setattr(inst, "deploy_agentdesk", deploy)
        assert s.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        assert record_seen == [True]

    def test_failing_script_is_a_warning(self, tmp_path, monkeypatch, capsys):
        s = Session(tmp_path, monkeypatch, capsys)
        real_deploy = inst.deploy_agentdesk

        def deploy(source_dir, dest):
            real_deploy(source_dir, dest)
            (dest / "setup-agentdesk.sh").write_text('echo "stub ran"\nexit 7\n')
            monkeypatch.setattr(ip, "run_agentdesk_setup", conftest.REAL_RUN_AGENTDESK_SETUP)

        monkeypatch.setattr(inst, "deploy_agentdesk", deploy)
        assert s.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        # run() re-stubbed the runner before the install; deploy restored the real one.
        cap = capsys.readouterr()
        assert "--setup-agentdesk" in cap.err
        assert "status 7" in cap.err

    def test_interrupted_runner_is_a_warning(self, sess, capsys):
        real_deploy = inst.deploy_agentdesk

        def deploy(source_dir, dest):
            real_deploy(source_dir, dest)
            sess.mp.setattr(ip, "run_agentdesk_setup", lambda s: 130)

        sess.mp.setattr(inst, "deploy_agentdesk", deploy)
        assert sess.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        assert "status 130" in capsys.readouterr().err

    def test_missing_script_is_a_warning(self, sess, capsys):
        real_deploy = inst.deploy_agentdesk

        def deploy(source_dir, dest):
            real_deploy(source_dir, dest)
            (dest / "setup-agentdesk.sh").unlink()

        sess.mp.setattr(inst, "deploy_agentdesk", deploy)
        assert sess.run(scope="user", interactive=False, setup_agentdesk=True) == 0
        assert "script not found" in capsys.readouterr().err
        assert sess.runner_calls == []


class TestHint:
    def test_plain_non_interactive_prints_new_hint(self, sess, capsys):
        assert sess.run(scope="user", interactive=False) == 0
        out = capsys.readouterr().out
        assert sess.runner_calls == []
        assert "re-run the install with --setup-agentdesk" in out
        assert not any("bash " in ln and "setup-agentdesk.sh" in ln for ln in out.splitlines())

    def test_declined_interactive_gets_hint(self, sess, capsys):
        assert sess.run(scope="user", answers=["n", "n", "n"]) == 0
        assert "--setup-agentdesk" in capsys.readouterr().out


class TestInvariance:
    def test_question_phase_is_a_noop_without_a_terminal(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a").mkdir()
        a = Session(tmp_path / "a", monkeypatch, capsys)
        assert a.run(scope="user", interactive=False) == 0
        capsys.readouterr()
        tree_a = (_tree(a.home / ".claude"), _tree(a.home / ".config" / "agentdesk"))

        (tmp_path / "b").mkdir()
        b = Session(tmp_path / "b", monkeypatch, capsys)
        monkeypatch.setattr(
            cli, "_ask_install_questions",
            lambda args, installer, source_dir, dest_root, *, is_project_mode, ct_mode, wt_mode:
                cli._InstallAnswers(ct_mode, wt_mode, False, False, False, ()),
        )
        assert b.run(scope="user", interactive=False) == 0
        tree_b = (_tree(b.home / ".claude"), _tree(b.home / ".config" / "agentdesk"))
        assert tree_a[1] == tree_b[1]
        # Install-dir paths are embedded in a few deployed files; compare names.
        assert set(tree_a[0]) == set(tree_b[0])

    def test_baseline_has_no_mods(self, sess):
        assert sess.run(scope="user", interactive=False) == 0
        skills = sess.home / ".claude" / "skills"
        assert not (skills / "context-tracker").exists()
        assert not (skills / "workflow-tasks").exists()
        assert (sess.home / ".claude" / "CLAUDE.md").exists()


def _read_until(fd, needle: bytes, buf: bytearray, deadline: float) -> bool:
    while time.time() < deadline:
        if needle in buf:
            return True
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return needle in buf
                raise
            if not chunk:
                return needle in buf
            buf.extend(chunk)
    return needle in buf


def _drain(fd, buf: bytearray, proc, deadline: float):
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
        elif proc.poll() is not None:
            return


@pytest.mark.skipif(pty is None or sys.platform == "win32", reason="needs a POSIX pty")
class TestRealTerminal:
    def _env(self, tmp_path):
        env = dict(os.environ)
        env.pop("QUOIN_INSTALL_NO_PROMPT", None)
        env["PYTHONPATH"] = str(REPO / "src")
        env["HOME"] = str(tmp_path / "home")
        return env

    def _cmd(self, proj):
        return [
            sys.executable, "-m", "quoin", "install",
            "--scope", f"project:{proj}", "--source-dir", str(QUOIN_SRC),
        ]

    def test_questions_appear_on_a_terminal_and_n_skips_mods(self, tmp_path):
        proj = tmp_path / "proj"
        (tmp_path / "home").mkdir()
        proj.mkdir()
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            self._cmd(proj), stdin=slave, stdout=slave, stderr=slave,
            env=self._env(tmp_path), cwd=str(REPO), close_fds=True,
        )
        os.close(slave)
        buf = bytearray()
        deadline = time.time() + 120
        try:
            assert _read_until(master, b"context-tracker mod", buf, deadline), buf.decode(errors="replace")
            os.write(master, b"n\n")
            assert _read_until(master, b"workflow-tasks mod", buf, deadline), buf.decode(errors="replace")
            os.write(master, b"n\n")
            _drain(master, buf, proc, deadline)
            try:
                rc = proc.wait(timeout=max(1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("install did not finish within 120 s")
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
        assert not (proj / ".claude/skills/context-tracker").exists()
        assert not (proj / ".claude/skills/workflow-tasks").exists()
        if shutil.which("claude") is not None:
            assert rc == 0, buf.decode(errors="replace")

    def test_piped_stdout_asks_nothing(self, tmp_path):
        proj = tmp_path / "proj"
        (tmp_path / "home").mkdir()
        proj.mkdir()
        master, slave = pty.openpty()
        try:
            proc = subprocess.run(
                self._cmd(proj), stdin=slave, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=self._env(tmp_path), cwd=str(REPO),
                timeout=120, text=True,
            )
        finally:
            os.close(slave)
            os.close(master)
        assert "mod (" not in proc.stdout
        assert "Install the" not in proc.stdout
