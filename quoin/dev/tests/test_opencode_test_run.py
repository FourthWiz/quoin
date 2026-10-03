"""The configured test run: workspace, environment, result location and CLI."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

import _opencode_gate_helpers as h
from quoin import cli
from quoin.opencode_adapter import paths, runstore, testrun

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PASSING = ["sh", "-c", "test -f untracked.txt && grep -q changed src/app.py && env/bin/tool"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    h.isolate_git(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    for name in ("QUOIN_OPENCODE_STATE_DIR", "OPENCODE_CONFIG", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    root = h.make_repo(tmp_path / "proj", {"src/app.py": "value = 1\n", ".gitignore": "env/\n"})
    tool = root / "env" / "bin" / "tool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    (root / "src" / "app.py").write_text("value = changed\n")
    (root / "untracked.txt").write_text("new\n")
    state_root = tmp_path / "state"
    return type("World", (), {"root": root, "state": state_root, "tmp": tmp_path})()


def configure(world, command=PASSING, include=("env",), state=None, **kwargs):
    return testrun.configure(
        world.root, "t1", command=command, include=include, state_root=state or world.state, **kwargs,
    )


def run(world, stage=None):
    return testrun.run_tests(world.root, "t1", stage, state_root=world.state)


def tree_state(root):
    return (
        h.git(root, "rev-parse", "HEAD"),
        h.git(root, "status", "--porcelain"),
        h.git(root, "for-each-ref", "refs/heads", "refs/tags"),
        h.git(root, "worktree", "list", "--porcelain"),
    )


def test_configured_command_passes_against_overlaid_copy(world):
    configure(world)
    before = tree_state(world.root)
    revisions = runstore.repo_revisions(world.root, source=True)
    result = run(world)
    assert result.outcome == "PASSED" and result.exit_code == 0 and result.reason is None
    assert result.output_sha256 and result.started_at and result.finished_at
    assert result.real_tree == "unchanged" and result.workspace_removed is True
    assert tree_state(world.root) == before
    assert runstore.repo_revisions(world.root, source=True) == revisions
    assert h.git(world.root, "worktree", "list").count("\n") == 1
    work = world.state / "tests" / paths.project_key(world.root) / "work"
    assert list(work.iterdir()) == []


def test_result_lives_outside_the_project_and_is_private(world):
    configure(world)
    run(world, 2)
    path = testrun.result_path(world.state, world.root, "t1", 2)
    assert path.name == "2-latest.json" and path.is_file()
    assert world.root not in path.parents
    assert testrun.result_path(world.state, world.root, "t1", None).name == "root-latest.json"
    assert (path.parent.stat().st_mode & 0o077) == 0
    data = json.loads(path.read_text())
    assert data["outcome"] == "PASSED" and data["task"] == "t1" and data["stage"] == 2


def test_second_worktree_metadata_survives_a_successful_run(world):
    other = world.tmp / "second"
    h.git(world.root, "worktree", "add", "--detach", str(other), "HEAD")
    moved = world.tmp / "second-away"
    other.rename(moved)
    try:
        configure(world)
        assert run(world).outcome == "PASSED"
        assert "second" in h.git(world.root, "worktree", "list", "--porcelain")
    finally:
        moved.rename(other)


def test_writes_inside_the_workspace_do_not_reach_the_real_tree(world):
    configure(world, command=["sh", "-c", "echo x > src/app.py; echo y > created.txt"])
    run(world)
    assert (world.root / "src" / "app.py").read_text() == "value = changed\n"
    assert not (world.root / "created.txt").exists()


def test_env_files_are_never_copied(world):
    (world.root / ".env.local").write_text("SECRET=1\n")
    configure(world, command=["sh", "-c", "test ! -e .env.local"])
    assert run(world).outcome == "PASSED"


def test_moving_a_ref_fails_the_run(world):
    configure(world, command=["sh", "-c", "git update-ref refs/heads/moved HEAD"], include=())
    # The worktree's git directory is the real repository's, so the ref lands there.
    result = run(world)
    assert result.outcome == "FAILED" and result.reason == "tests-real-tree-changed"
    assert result.real_tree == "changed" and result.exit_code == 0
    h.git(world.root, "update-ref", "-d", "refs/heads/moved")


def test_failing_command_is_failed(world):
    configure(world, command=["sh", "-c", "echo boom; exit 3"], include=())
    result = run(world)
    assert result.outcome == "FAILED" and result.exit_code == 3 and result.reason == "tests-failed"
    assert "boom" in result.output_tail


def test_timeout_kills_the_process_group(world):
    pidfile = world.tmp / "child.pid"
    configure(world, command=["sh", "-c", "sleep 60 & echo $! > \"$0\"; wait", str(pidfile)], include=(), timeout_s=1)
    result = run(world)
    assert result.timed_out is True and result.outcome == "FAILED" and result.reason == "tests-timeout"
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_unconfigured_is_refused_and_writes_nothing(world):
    result = run(world)
    assert result.outcome == "REFUSED" and result.reason == "tests-not-configured"
    assert not testrun.result_path(world.state, world.root, "t1", None).exists()


def test_busy_file_refuses_without_touching_the_result(world):
    configure(world)
    assert run(world).outcome == "PASSED"
    path = testrun.result_path(world.state, world.root, "t1", None)
    before = path.read_text()
    (path.parent / ".busy").write_text("%d\n" % os.getpid())
    result = run(world)
    assert result.outcome == "REFUSED" and result.reason == "test-run-busy"
    assert path.read_text() == before


def test_stale_busy_file_from_a_dead_process_is_replaced(world):
    configure(world)
    assert run(world).outcome == "PASSED"
    path = testrun.result_path(world.state, world.root, "t1", None)
    (path.parent / ".busy").write_text("999999999\n")
    assert run(world).outcome == "PASSED"
    assert not (path.parent / ".busy").exists()


def test_configure_validates_values(world):
    for bad in ([], "pytest", ["a\0b"], [""], [1]):
        with pytest.raises(testrun.TestRunError) as info:
            configure(world, command=bad)
        assert info.value.code == "command-invalid"
    for bad in ("../x", "/abs", "src"):
        with pytest.raises(testrun.TestRunError):
            configure(world, include=(bad,))
    with pytest.raises(testrun.TestRunError) as info:
        configure(world, include=("missing",))
    assert info.value.code == "include-invalid"
    with pytest.raises(testrun.TestRunError) as info:
        configure(world, timeout_s=0)
    assert info.value.code == "timeout-invalid"
    settings = configure(world, timeout_s=30)
    assert settings == {"test_command": PASSING, "test_include": ["env"], "test_timeout_s": 30}
    state = runstore.load_workflow_state(runstore.store_dir(world.root), "t1")
    assert state["settings"]["test_command"] == PASSING


def test_include_must_be_ignored_by_git(world):
    (world.root / "build").mkdir()
    (world.root / "build" / "x").write_text("x")
    with pytest.raises(testrun.TestRunError) as info:
        configure(world, include=("build",))
    assert info.value.code == "include-not-ignored"


def test_sibling_repos_in_a_plain_directory(world):
    plain = world.tmp / "plain"
    a = h.make_repo(plain / "a", {"f.txt": "a\n"})
    b = h.make_repo(plain / "b", {"f.txt": "b\n"})
    (a / "f.txt").write_text("changed-a\n")
    (b / "f.txt").write_text("changed-b\n")
    before = (tree_state(a), tree_state(b))
    testrun.configure(plain, "t1", command=["sh", "-c", "pwd; cat a/f.txt b/f.txt"], state_root=world.state)
    result = testrun.run_tests(plain, "t1", state_root=world.state)
    assert result.outcome == "PASSED" and result.workspace_removed is True
    lines = result.output_tail.split()
    assert "changed-a" in lines and "changed-b" in lines
    assert os.path.realpath(str(plain)) != lines[0] and lines[0].startswith(str(world.state.resolve()))
    assert (tree_state(a), tree_state(b)) == before


def test_command_env_scrubs_launcher_and_credentials(world, monkeypatch, tmp_path):
    out = tmp_path / "env.txt"
    configure(world, command=["sh", "-c", 'env > "$0"', str(out)], include=())
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", str(world.state))
    monkeypatch.setenv("OPENCODE_CONFIG", "/x/config.json")
    monkeypatch.setenv("XDG_DATA_HOME", "/x/data")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("PYTHONPATH", "/x/py")
    assert run(world).outcome == "PASSED"
    names = {line.split("=", 1)[0] for line in out.read_text().splitlines() if "=" in line}
    for gone in ("QUOIN_OPENCODE_STATE_DIR", "OPENCODE_CONFIG", "XDG_DATA_HOME", "ANTHROPIC_API_KEY", "PYTHONPATH"):
        assert gone not in names
    assert {"PATH", "HOME"} <= names


def test_command_env_outside_the_launcher_drops_only_launcher_names(monkeypatch):
    env = {"PATH": "/bin", "PYTHONPATH": "/p", "OPENCODE_CONFIG": "c", "XDG_DATA_HOME": "d"}
    assert testrun.command_env(env) == {"PATH": "/bin", "PYTHONPATH": "/p"}
    launched = dict(env, QUOIN_OPENCODE_STATE_DIR="/s", ANTHROPIC_API_KEY="k", HTTPS_PROXY="p", LC_ALL="C")
    assert testrun.command_env(launched) == {"PATH": "/bin", "LC_ALL": "C"}


def test_environment_scrub_outside_launcher_keeps_ordinary_names(world, monkeypatch, tmp_path):
    out = tmp_path / "env.txt"
    configure(world, command=["sh", "-c", 'env > "$0"', str(out)], include=())
    monkeypatch.setenv("PYTHONPATH", "/x/py")
    monkeypatch.setenv("OPENCODE_CONFIG", "/x/config.json")
    run(world)
    names = {line.split("=", 1)[0] for line in out.read_text().splitlines() if "=" in line}
    assert "PYTHONPATH" in names and "OPENCODE_CONFIG" not in names


def seed_running(root, task, attempt_states):
    directory = runstore.store_dir(root, create=True)
    run_id = runstore.new_run_id()
    record = runstore.new_run_record(run_id, task, {}, {})
    for number, state in enumerate(attempt_states, 1):
        attempt = runstore.new_attempt(
            number, pid=1, pgid=1, child_start="c", driver_pid=2, driver_start="d", resume_mode="new",
        )
        attempt["state"] = state
        record["attempts"].append(attempt)
    runstore.set_state(record, "running", None)
    runstore.write_record(directory, record)
    runstore.write_pointer(directory, runstore.new_pointer(task, run_id))
    return run_id


def test_attempt_and_run_id_taken_from_a_running_record_with_two_attempts(world):
    configure(world, command=["true"], include=())
    run_id = seed_running(world.root, "t1", ["interrupted", "running"])
    result = run(world)
    assert result.run_id == run_id and result.attempt == 2
    data = json.loads(testrun.result_path(world.state, world.root, "t1", None).read_text())
    assert data["run_id"] == run_id and data["attempt"] == 2


def test_no_running_record_leaves_identity_null(world):
    configure(world, command=["true"], include=())
    result = run(world)
    assert result.run_id is None and result.attempt is None


def cli_run(capsys, *argv):
    code = cli.main(["opencode", "test-run", *argv])
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


@pytest.fixture()
def project(world, monkeypatch):
    meta = world.root / ".quoin" / "opencode-install.json"
    meta.parent.mkdir(exist_ok=True)
    meta.write_text("{}\n")
    monkeypatch.chdir(world.root)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return world


def default_state():
    return paths.state_dir(os.environ, Path.home())


def test_cli_exit_codes_and_one_json_line(project, capsys):
    configure(project, state=default_state())
    code, data = cli_run(capsys, "--task", "t1")
    assert code == 0 and data["outcome"] == "PASSED" and data["exit_code"] == 0
    configure(project, command=["false"], include=(), state=default_state())
    code, data = cli_run(capsys, "--task", "t1")
    assert code == 1 and data["outcome"] == "FAILED" and data["exit_code"] == 1
    code, data = cli_run(capsys, "--task", "other")
    assert code == 2 and data["refusal"]["code"] == "tests-not-configured"


def test_cli_state_dir_variable_wins_over_xdg(project, monkeypatch, capsys, tmp_path):
    chosen = tmp_path / "chosen"
    configure(project, command=["true"], include=(), state=chosen)
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", str(chosen))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    code, _ = cli_run(capsys, "--task", "t1")
    assert code == 0
    assert testrun.result_path(chosen, project.root, "t1", None).is_file()
    assert not (tmp_path / "xdg").exists()


def test_cli_without_variable_uses_the_adapter_state_dir(project, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    configure(project, command=["true"], include=(), state=default_state())
    code, _ = cli_run(capsys, "--task", "t1")
    assert code == 0
    expected = paths.state_dir(os.environ, Path.home())
    assert testrun.result_path(expected, project.root, "t1", None).is_file()


@pytest.mark.parametrize("value", ["relative/dir", ""])
def test_cli_refuses_a_relative_or_empty_state_dir(project, monkeypatch, capsys, value):
    configure(project, command=["true"], include=())
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", value)
    code, data = cli_run(capsys, "--task", "t1")
    assert code == 2 and data["refusal"]["code"] == "state-dir-invalid"


@pytest.mark.parametrize("argv", [
    ["--task", "t1", "--project-root", "."],
    ["--task", "t1", "--command", "ls"],
    ["--task", "t1", "--include", "env"],
    ["--task", "t1", "--sta", "1"],
])
def test_cli_rejects_other_options_and_abbreviations(project, capsys, argv):
    with pytest.raises(SystemExit) as info:
        cli.main(["opencode", "test-run", *argv])
    assert info.value.code == 2
    capsys.readouterr()


def test_cli_accepts_the_full_stage_option(project, capsys):
    configure(project, command=["true"], include=(), state=default_state())
    code, data = cli_run(capsys, "--task", "t1", "--stage", "1")
    assert code == 0 and data["outcome"] == "PASSED"


def test_settings_edited_inside_the_project_are_refused(world):
    configure(world, command=["true"], include=())
    directory = runstore.store_dir(world.root)
    state = runstore.load_workflow_state(directory, "t1")
    state["settings"]["test_command"] = ["sh", "-c", "echo forged > pwned.txt"]
    runstore.write_workflow_state(directory, state)
    result = run(world)
    assert result.outcome == "REFUSED" and result.reason == "tests-settings-changed"
    assert not (world.root / "pwned.txt").exists()
    assert not testrun.result_path(world.state, world.root, "t1", None).exists()


def test_settings_without_a_stored_digest_are_refused(world):
    configure(world, command=["true"], include=())
    (testrun.result_dir(world.state, world.root, "t1") / testrun.PIN_NAME).unlink()
    assert run(world).reason == "tests-settings-changed"


def test_include_is_checked_again_at_run_time(world):
    configure(world, command=["true"], include=("env",))
    (world.root / ".gitignore").write_text("")
    result = run(world)
    assert result.outcome == "REFUSED" and result.reason == "include-not-ignored"


def test_a_child_that_escapes_the_group_cannot_outlive_the_timeout(world):
    pidfile = world.tmp / "escaped.pid"
    script = (
        "import os, subprocess, sys; "
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True); "
        "open(sys.argv[1], 'w').write(str(p.pid)); "
        "os.execvp('sleep', ['sleep', '60'])"
    )
    import sys

    configure(world, command=[sys.executable, "-c", script, str(pidfile)], include=(), timeout_s=1)
    began = time.monotonic()
    result = run(world)
    try:
        assert time.monotonic() - began < 20
        assert result.timed_out is True and result.reason == "tests-timeout"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except OSError:
                pass


def test_work_directories_of_dead_runs_are_swept(world):
    configure(world, command=["true"], include=())
    work = world.state / "tests" / paths.project_key(world.root) / "work"
    work.mkdir(parents=True, mode=0o700)
    stale = work / "999999999-deadbeef"
    stale.mkdir()
    assert run(world).outcome == "PASSED"
    assert not stale.exists()
