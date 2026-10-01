"""`quoin opencode start`: validation, the injected-launcher flow, and the
default launcher that replaces the quoin process with the TUI."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from quoin import cli
from quoin.opencode_adapter import driver, install, runstore

from _opencode_run_helpers import InstalledProject

REPLAY = ("replay", {"fixture": "plain-complete.jsonl"})
REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = InstalledProject(tmp_path, monkeypatch, REPLAY)
    factory = project.driver_factory()

    def make(root):
        drv = factory(root)
        drv._env["ANTHROPIC_API_KEY"] = "ambient-provider-key-0000"  # noqa: SLF001
        return drv

    monkeypatch.setattr(cli, "_make_opencode_driver", make)
    calls = []

    def launcher(argv, cwd, env):
        calls.append((argv, cwd, env))
        return 17

    monkeypatch.setattr(cli, "_opencode_tui_launcher", launcher)
    project.calls = calls
    yield project
    project.cleanup()


def start(project, *extra):
    return cli.main(["opencode", "start", "--profile", "work", "--project-root", str(project.root)] + list(extra))


def lock_files(project):
    memory = project.root / ".workflow_artifacts" / "memory"
    return sorted(memory.glob("run-supervisor-*")) if memory.exists() else []


def test_launches_the_tui_in_the_project_and_returns_its_exit_code(world):
    assert start(world) == 17
    argv, cwd, env = world.calls[0]
    assert argv == [str(world.shim), str(world.root)]
    assert cwd == str(world.root)
    assert "OPENCODE_CONFIG" in env and "XDG_DATA_HOME" in env
    assert "ambient-provider-key-0000" not in env.values() and "ANTHROPIC_API_KEY" not in env


def test_environment_names_match_the_headless_run(world):
    drv = cli._make_opencode_driver(world.root)
    interactive = drv.prepare_interactive("work")
    headless = drv.prepare(driver.RunRequest(
        project_root=world.root, task="demo", stage=None, phase="plan", profile="work"))
    assert interactive.env_names == headless.env_names


def test_nothing_is_written_to_the_run_store_and_no_lock_is_taken(world):
    assert start(world) == 17
    assert runstore.inspect_store(world.root) is None
    assert lock_files(world) == []


def test_dry_run_prints_and_does_not_launch(world, capsys):
    assert start(world, "--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"argv", "cwd", "config_path", "env_names", "runtime_version"}
    assert out["argv"] == [str(world.shim), str(world.root)]
    assert "OPENCODE_CONFIG" in out["env_names"] and "ambient-provider-key-0000" not in json.dumps(out)
    assert world.calls == []


def refusal(capsys):
    err = capsys.readouterr().err
    assert err.startswith("quoin: opencode start refused (") and "Traceback" not in err
    return err


def test_missing_binary_is_refused(world, monkeypatch, capsys):
    factory = cli._make_opencode_driver

    def make(root):
        drv = factory(root)
        drv._which = lambda name: None  # noqa: SLF001
        return drv

    monkeypatch.setattr(cli, "_make_opencode_driver", make)
    assert start(world) == 3
    assert "(missing-binary/opencode-binary-absent)" in refusal(capsys)
    assert world.calls == []


def test_wrong_version_is_refused(world, capsys):
    world.version = "9.9.9"
    assert start(world) == 3
    assert "(unsupported-version/opencode-version)" in refusal(capsys)


def test_unqualified_gateway_is_refused(world, capsys):
    for path in list((world.world.tmp / "xdg" / "quoin" / "opencode" / "qualifications").glob("*.json")):
        path.unlink()
    assert start(world) == 3
    assert "(unqualified-gateway/not-launchable)" in refusal(capsys)


def test_not_installed_is_refused(world, monkeypatch, capsys):
    monkeypatch.setattr(install, "load_metadata", lambda root: None)
    assert start(world) == 3
    assert "(workflow-validation/not-installed)" in refusal(capsys)


@pytest.mark.parametrize("kind", ["agent", "command", "config", "skill"])
def test_owned_file_drift_is_refused(world, capsys, kind):
    rel = next(r for r, rec in sorted(install.load_metadata(world.root).owned.items()) if rec["kind"] == kind)
    path = world.root / rel
    path.write_bytes(path.read_bytes() + b"\n# local edit\n")
    assert start(world) == 3
    assert "(workflow-validation/owned-file-drift)" in refusal(capsys)
    assert world.calls == []


def test_global_plugin_directory_is_refused_in_prepare_format(world, capsys):
    plugin = world.world.tmp / "xdg" / "opencode" / "plugin"
    plugin.mkdir(parents=True)
    (plugin / "x.ts").write_text("export default {}")
    assert start(world) == 3
    assert "(policy-denial/plugin-directory-present)" in refusal(capsys)


def test_unknown_profile_is_refused(world, capsys):
    assert cli.main(["opencode", "start", "--profile", "nope", "--project-root", str(world.root)]) == 3
    refusal(capsys)


def test_the_default_launcher_is_the_exec_launcher():
    assert cli._exec_tui.__name__ == "_exec_tui"


def test_default_launcher_is_wired_when_not_patched():
    code = "import quoin.cli as c; print(c._opencode_tui_launcher is c._exec_tui)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(SRC_DIR), PYTHONDONTWRITEBYTECODE="1"))
    assert out.stdout.strip() == "True"


# --- the default launcher, in a subprocess ---------------------------------------


def run_launcher(argv, cwd, tmp_path):
    code = textwrap.dedent("""
        import json, sys
        import quoin.cli as cli
        argv, cwd = json.loads(sys.argv[1]), sys.argv[2]
        sys.exit(cli._exec_tui(argv, cwd, {"PATH": "/usr/bin:/bin"}))
    """)
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-c", code, json.dumps(argv), str(cwd)],
        capture_output=True, text=True, env=env, start_new_session=True, timeout=60)


def test_exec_replaces_quoin_with_the_tui_and_survives_sigint(tmp_path):
    fake = tmp_path / "fake-opencode"
    fake.write_text("#!/bin/sh\ntrap '' INT\necho $$\npwd\nkill -INT 0\nexit 7\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    project = tmp_path / "project"
    project.mkdir()
    # a shell that exits 7 keeps the status through exec only if it really was exec'd
    code = textwrap.dedent("""
        import json, os, sys
        import quoin.cli as cli
        print(os.getpid(), flush=True)
        sys.exit(cli._exec_tui([sys.argv[1], sys.argv[2]], sys.argv[2], {"PATH": "/usr/bin:/bin"}))
    """)
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", code, str(fake), str(project)], capture_output=True,
                          text=True, env=env, start_new_session=True, timeout=60)
    lines = proc.stdout.split()
    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert lines[0] == lines[1], "the TUI must run as the quoin process itself"
    assert os.path.realpath(lines[2]) == os.path.realpath(str(project))
    assert "Traceback" not in proc.stderr and "KeyboardInterrupt" not in proc.stderr


def test_exec_of_a_missing_binary_exits_3_with_one_line(tmp_path):
    proc = run_launcher([str(tmp_path / "nope"), str(tmp_path)], tmp_path, tmp_path)
    assert proc.returncode == 3
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0].startswith("quoin: opencode start failed:")


def test_sigpipe_and_sigxfsz_are_reset_before_exec(tmp_path, monkeypatch):
    seen = {}

    def fake_execve(path, argv, env):
        seen["pipe"] = signal.getsignal(signal.SIGPIPE)
        seen["xfsz"] = signal.getsignal(signal.SIGXFSZ)
        raise OSError(2, "gone")

    before = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGPIPE", "SIGXFSZ")}
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "execve", fake_execve)
    try:
        assert cli._exec_tui(["/x", str(tmp_path)], str(tmp_path), {}) == 3
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), handler)
    assert seen == {"pipe": signal.SIG_DFL, "xfsz": signal.SIG_DFL}


class _Child:
    def __init__(self, code, during):
        self.code, self.during = code, during

    def wait(self):
        self.during.append(signal.getsignal(signal.SIGINT))
        return self.code


def test_non_posix_branch_ignores_sigint_while_waiting_and_returns_the_code(monkeypatch, tmp_path):
    during = []
    monkeypatch.setattr(cli, "_is_posix", lambda: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, cwd, env: _Child(9, during))
    before = signal.getsignal(signal.SIGINT)
    assert cli._exec_tui(["x", "y"], str(tmp_path), {}) == 9
    assert during == [signal.SIG_IGN] and signal.getsignal(signal.SIGINT) == before


def test_non_posix_branch_reports_a_start_failure(monkeypatch, tmp_path, capsys):
    def boom(argv, cwd, env):
        raise OSError(2, "gone")

    monkeypatch.setattr(cli, "_is_posix", lambda: False)
    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    before = signal.getsignal(signal.SIGINT)
    assert cli._exec_tui(["x", "y"], str(tmp_path), {}) == 3
    assert signal.getsignal(signal.SIGINT) == before
    assert capsys.readouterr().err.startswith("quoin: opencode start failed:")
