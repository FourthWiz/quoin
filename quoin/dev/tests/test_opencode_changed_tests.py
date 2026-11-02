"""Every edit to a pre-existing test file is listed, with a reason.

The list lives in ``opencode_changed_tests.json``. The check compares the
working tree against the merge base with the main branch, so uncommitted edits
count. Only OpenCode test files may change at all: a change to any other
pre-existing test (the Claude and Codex suites) fails whatever the list says.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from _opencode_task_scope import task_guard_active

TESTS_DIR = Path(__file__).resolve().parent
LIST_PATH = TESTS_DIR / "opencode_changed_tests.json"
OPENCODE_PREFIXES = ("test_opencode_", "_opencode_", "test_fake_opencode")
# Tests of files under quoin/adapters/opencode that do not carry the prefix. A
# closed set of exact names, never a prefix.
OPENCODE_ADAPTER_TESTS = frozenset({"test_probe_gateway.py"})


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def _git_root():
    if shutil.which("git") is None:
        return None
    result = _git(TESTS_DIR, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def changed_pre_existing(root, base, pathspec):
    """Paths (relative to ``root``) modified, deleted, renamed or type-changed since ``base``."""
    result = _git(root, "diff", "--name-status", "--diff-filter=DMRT", base, "--", pathspec)
    assert result.returncode == 0, result.stderr
    paths = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if fields and fields[0]:
            # A rename lists the old path first; that old path counts as changed.
            paths.append(fields[1])
    return sorted(set(paths))


def merge_base(root, base_ref):
    for ref in (base_ref, "main"):
        result = _git(root, "merge-base", "HEAD", ref)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def is_opencode_test(relative_path):
    name = Path(relative_path).name
    return (
        name.startswith(OPENCODE_PREFIXES)
        or name in OPENCODE_ADAPTER_TESTS
        or "dev/tests/fakes/" in relative_path
    )


def test_the_diff_command_detects_a_modified_file(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    sample = repo / "tests" / "test_sample.py"
    sample.write_text("def test_a():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert changed_pre_existing(repo, base, "tests") == []
    sample.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    assert changed_pre_existing(repo, base, "tests") == ["tests/test_sample.py"]
    (repo / "tests" / "test_new.py").write_text("", encoding="utf-8")
    assert changed_pre_existing(repo, base, "tests") == ["tests/test_sample.py"]


def test_the_adapter_test_exception_is_a_closed_name_set():
    assert is_opencode_test("quoin/dev/tests/test_probe_gateway.py")
    for rejected in (
        "quoin/dev/tests/test_quoin_run_opencode_x.py",
        "quoin/dev/tests/test_probe_gateway_tool_loop.py",
        "quoin/dev/tests/test_install.py",
    ):
        assert not is_opencode_test(rejected), rejected
    assert OPENCODE_ADAPTER_TESTS == frozenset({"test_probe_gateway.py"})


def test_changed_tests_are_listed_with_reasons():
    data = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 1
    listed = data["changed"]
    for path, reason in listed.items():
        assert isinstance(reason, str) and reason.strip(), path
        assert is_opencode_test(path), "only OpenCode tests may be listed: %s" % path
    root = _git_root()
    if root is None:
        pytest.skip("no base branch available")
    reason = task_guard_active(root, data["task_branch"])
    if reason is not None:
        pytest.skip(reason)
    base = merge_base(root, data["base_ref"])
    if base is None:
        pytest.skip("no base branch available")
    prefix = TESTS_DIR.relative_to(root).as_posix()
    changed = changed_pre_existing(root, base, prefix)
    for path in changed:
        assert is_opencode_test(path), "pre-existing non-OpenCode test changed: %s" % path
        name = Path(path).name
        assert name in listed or path in listed, "changed test is not listed: %s" % path
    changed_names = {Path(path).name for path in changed} | set(changed)
    for path in listed:
        assert path in changed_names or Path(path).name in changed_names, "stale list entry: %s" % path


def _scratch_repo(tmp_path, branch):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    return repo


def test_the_task_scope_gate(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    repo = _scratch_repo(tmp_path, "feat/the-task")
    assert task_guard_active(repo, "feat/the-task", environ={}) is None
    other = _scratch_repo(tmp_path / "second", "main")
    reason = task_guard_active(other, "feat/the-task", environ={})
    assert reason == "task-scoped guard: not on the task branch"
    assert task_guard_active(other, "feat/the-task", environ={"QUOIN_OPENCODE_TASK_GUARDS": "1"}) is None
    assert task_guard_active(tmp_path, "feat/the-task", environ={}) is not None


def test_the_task_scope_gate_skips_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert task_guard_active(tmp_path, "feat/the-task", environ={}) is not None


def test_the_recorded_task_branch_is_a_branch_name():
    data = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    assert data["task_branch"].startswith("feat/")
