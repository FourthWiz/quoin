"""Unit tests for IVG-153 Stage 2 T-07: quoin.supervisor's headless
launch_fn implementation.

All tests mock `subprocess.run` — no real `claude` binary is invoked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quoin import supervisor as sup


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# build_relaunch_argv
# ---------------------------------------------------------------------------


def test_build_relaunch_argv_carries_resume_and_autonomous() -> None:
    argv = sup.build_relaunch_argv("demo-task")
    assert argv[0] == "claude"
    assert "-p" in argv
    prompt = argv[argv.index("-p") + 1]
    assert prompt == "/run --resume --autonomous demo-task"
    assert "--resume" in prompt
    assert "--autonomous" in prompt


def test_build_relaunch_argv_default_uses_allowed_tools_permission_mode() -> None:
    argv = sup.build_relaunch_argv("demo-task")
    assert "--allowedTools" in argv
    assert "--dangerously-skip-permissions" not in argv
    idx = argv.index("--allowedTools")
    tools = set(argv[idx + 1:])
    for tool in ("Read", "Write", "Edit", "Bash", "Agent", "Skill"):
        assert tool in tools


def test_build_relaunch_argv_bypass_mode_uses_dangerous_flag() -> None:
    argv = sup.build_relaunch_argv("demo-task", permission_mode="bypassPermissions")
    assert "--dangerously-skip-permissions" in argv
    assert "--allowedTools" not in argv


# ---------------------------------------------------------------------------
# resolve_repo_root
# ---------------------------------------------------------------------------


def test_resolve_repo_root_calls_git_rev_parse(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = sup.resolve_repo_root(tmp_path)
    assert result == tmp_path
    assert calls[0][0] == ["git", "rev-parse", "--show-toplevel"]
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_resolve_repo_root_falls_back_when_git_fails(tmp_path: Path, monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return _FakeCompleted(returncode=128, stdout="", stderr="not a git repo")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = sup.resolve_repo_root(tmp_path)
    assert result == tmp_path


def test_resolve_repo_root_falls_back_when_git_missing(tmp_path: Path, monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = sup.resolve_repo_root(tmp_path)
    assert result == tmp_path


# ---------------------------------------------------------------------------
# make_launch_fn
# ---------------------------------------------------------------------------


def test_make_launch_fn_uses_resolved_git_root_as_cwd_and_devnull_stdin(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(repo_root) + "\n")
        return _FakeCompleted(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_fn = sup.make_launch_fn(tmp_path)
    result = launch_fn("demo-task")

    assert result.returncode == 0
    claude_argv, claude_kwargs = calls[-1]
    assert claude_argv[0] == "claude"
    assert claude_kwargs["cwd"] == str(repo_root)
    assert claude_kwargs["stdin"] == subprocess.DEVNULL


def test_make_launch_fn_argv_contains_claude_prompt_and_permission_flag(
    tmp_path: Path, monkeypatch
) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        seen["argv"] = argv
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    sup.make_launch_fn(tmp_path)("demo-task")
    argv = seen["argv"]
    assert "claude" in argv
    assert "-p" in argv
    assert "/run --resume --autonomous demo-task" in argv
    assert "--autonomous" in argv[argv.index("-p") + 1]
    assert "--allowedTools" in argv


def test_make_launch_fn_surfaces_nonzero_exit_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        return _FakeCompleted(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_fn = sup.make_launch_fn(tmp_path)
    result = launch_fn("demo-task")  # must not raise
    assert result.returncode == 1
    assert "boom" in result.stderr


def test_make_launch_fn_timeout_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_fn = sup.make_launch_fn(tmp_path)
    result = launch_fn("demo-task")  # must not raise
    assert result.timed_out is True
    assert result.returncode != 0


def test_make_launch_fn_missing_binary_surfaces_oserror_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_fn = sup.make_launch_fn(tmp_path)
    result = launch_fn("demo-task")  # must not raise
    assert result.returncode != 0
    assert "claude" in result.stderr


def test_make_launch_fn_respects_bypass_permission_mode(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        seen["argv"] = argv
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    sup.make_launch_fn(tmp_path, permission_mode="bypassPermissions")("demo-task")
    assert "--dangerously-skip-permissions" in seen["argv"]


# ---------------------------------------------------------------------------
# Import-leanness guard (T-06's constraint must survive the T-07 addition)
# ---------------------------------------------------------------------------


def test_supervisor_still_import_lean_after_launcher_addition() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sup))
    top_level_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])

    assert "subprocess" not in top_level_imports


# ---------------------------------------------------------------------------
# End-to-end: supervise() wired to the real (mocked-subprocess) launcher
# ---------------------------------------------------------------------------


def test_supervise_with_real_launch_fn_reaches_success(tmp_path: Path, monkeypatch) -> None:
    """Wires make_launch_fn into supervise() end-to-end (mocked subprocess.run
    only) to prove the T-06 loop and T-07 launcher compose correctly."""

    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=0, stdout=str(tmp_path) + "\n")
        # Simulate the relaunch doing real work: write a completion sentinel,
        # then on the second launch also write the done sentinel.
        d = sup.progress_dir("demo-task", tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        existing = len(list(d.glob("*.done")))
        (d / f"discover.batch-{existing + 1}.done").write_text("done\n")
        if existing >= 1:
            sup.done_path("demo-task", tmp_path).parent.mkdir(parents=True, exist_ok=True)
            sup.done_path("demo-task", tmp_path).write_text("done\n")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    class _FakeClock:
        def sleep(self, seconds: float) -> None:
            pass

    launch_fn = sup.make_launch_fn(tmp_path)
    result = sup.supervise(
        "demo-task", tmp_path, launch_fn=launch_fn, max_relaunch=5, clock=_FakeClock()
    )
    assert result.status == "SUCCESS"
    # The progress probe must not spawn git on a root without a .git entry.
    assert not [a for a in recorded if a[:2] == ["git", "-C"]]
    assert len([a for a in recorded if a and a[0] == "claude"]) == 2
