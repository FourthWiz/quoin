"""The launcher writes a per-session record of the project root.

Runs the real agentdesk.zsh with a recording zellij stub in a hermetic HOME and
a private runtime dir; nothing touches the real home or a real zellij.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

import test_agentdesk_public_launch as pl
from test_agentdesk_public_launch import rt_dir  # noqa: F401  (fixture)
from quoin import agentdesk_sessions as ads

pytestmark = pl.pytestmark


def _record_dir(tmp_path):
    return tmp_path / "home" / ".config" / "agentdesk" / "sessions"


def _sock(rt_dir):
    return "%s/zellij-agentdesk-%d" % (rt_dir, os.getuid())


def _launch(tmp_path, rt_dir, args, sessions=(), extra=None):
    env = pl._env(tmp_path, rt_dir, sessions=sessions, extra=extra)
    return env, pl._run_pipe(args, env, tmp_path, rt_dir)


def _no_temp_files(tmp_path):
    d = _record_dir(tmp_path)
    if d.is_dir():
        assert [p.name for p in d.iterdir() if p.name.startswith(".agentdesk-record.")] == []


def test_named_launch_writes_record_readable_by_python(tmp_path, rt_dir):
    env, res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "codex,codex,shell", "--no-prompt"])
    pl._assert_created(res, "deck-1")
    root = os.path.realpath(tmp_path / "proj")
    path = _record_dir(tmp_path) / "deck-1"
    assert path.read_bytes() == ("%s\n%s" % (root, _sock(rt_dir))).encode()
    rec = ads.read_record("deck-1", {"HOME": env["HOME"]}, _sock(rt_dir))
    assert rec is not None and rec.root == root
    _no_temp_files(tmp_path)


def test_default_name_writes_project_agents(tmp_path, rt_dir):
    _, res = _launch(tmp_path, rt_dir, ["--panes", "shell", "--no-prompt"])
    pl._assert_created(res, "proj-agents")
    assert (_record_dir(tmp_path) / "proj-agents").is_file()
    _no_temp_files(tmp_path)


def test_second_launch_with_new_writes_suffixed_record(tmp_path, rt_dir):
    args = ["--name", "deck-1", "--panes", "shell", "--no-prompt"]
    _launch(tmp_path, rt_dir, args)
    first = (_record_dir(tmp_path) / "deck-1").read_bytes()
    (tmp_path / "cap" / "argv.log").unlink()
    _, res = _launch(tmp_path, rt_dir, args + ["--if-exists", "new"], sessions=["deck-1"])
    pl._assert_created(res, "deck-1_1")
    assert (_record_dir(tmp_path) / "deck-1_1").is_file()
    assert (_record_dir(tmp_path) / "deck-1").read_bytes() == first
    _no_temp_files(tmp_path)


def test_unwritable_record_dir_does_not_stop_launch(tmp_path, rt_dir):
    cfg = tmp_path / "home" / ".config" / "agentdesk"
    cfg.mkdir(parents=True)
    (cfg / "sessions").write_text("not a directory")
    _, res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell", "--no-prompt"])
    assert res.rc == 0
    pl._assert_created(res, "deck-1")
    assert (cfg / "sessions").read_text() == "not a directory"


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_read_only_record_dir_does_not_stop_launch(tmp_path, rt_dir):
    d = _record_dir(tmp_path)
    d.mkdir(parents=True)
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        _, res = _launch(tmp_path, rt_dir, ["--name", "deck-1", "--panes", "shell", "--no-prompt"])
        assert res.rc == 0
        pl._assert_created(res, "deck-1")
        assert list(d.iterdir()) == []
    finally:
        d.chmod(stat.S_IRWXU)


def test_noclobber_shell_still_writes_record(tmp_path, rt_dir):
    env = pl._env(tmp_path, rt_dir)
    script = 'setopt noclobber; source "$0"; shift; agentdesk "$@"'
    argv = ["zsh", "-c", script, str(pl.AGENTDESK_ZSH), "_", "--name", "deck-1",
            "--panes", "shell", "--no-prompt"]
    proc = subprocess.run(argv, env=env, cwd=str(tmp_path / "proj"), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=pl.SUBPROCESS_TIMEOUT)
    assert proc.returncode == 0, proc.stderr
    assert (_record_dir(tmp_path) / "deck-1").is_file()
    _no_temp_files(tmp_path)


def test_record_helper_does_not_leak_variables(tmp_path, rt_dir):
    env = pl._env(tmp_path, rt_dir)
    env["ZELLIJ_SOCKET_DIR"] = _sock(rt_dir)
    os.makedirs(env["ZELLIJ_SOCKET_DIR"], exist_ok=True)
    script = ('source "$0"; _agentdesk_record_session s1 "$PWD"; '
              'echo "dir=${dir-unset} tmp=${tmp-unset}"')
    proc = subprocess.run(["zsh", "-c", script, str(pl.AGENTDESK_ZSH)], env=env,
                          cwd=str(tmp_path / "proj"), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=pl.SUBPROCESS_TIMEOUT)
    assert proc.stdout.strip().splitlines()[-1] == "dir=unset tmp=unset", proc.stdout + proc.stderr


def test_record_call_sits_between_socket_guard_and_create_call():
    lines = pl.AGENTDESK_ZSH.read_text().splitlines()
    guard = next(i for i, l in enumerate(lines) if "_sock_len" in l and "-gt 100" in l)
    call = next(i for i, l in enumerate(lines) if l.strip().startswith("_agentdesk_record_session ") and i > guard)
    create = next(i for i, l in enumerate(lines) if "zellij --new-session-with-layout" in l)
    assert guard < call < create


def test_record_temp_template_ends_in_x_run():
    text = pl.AGENTDESK_ZSH.read_text()
    assert re.search(r'mktemp "\$dir/\.agentdesk-record\.XXXXXX"', text)
