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

TESTS_DIR = Path(__file__).resolve().parent
LIST_PATH = TESTS_DIR / "opencode_changed_tests.json"
OPENCODE_PREFIXES = ("test_opencode_", "_opencode_", "test_fake_opencode")


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
    return name.startswith(OPENCODE_PREFIXES) or "dev/tests/fakes/" in relative_path


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
