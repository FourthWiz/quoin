"""Tests for discovery_dirty.py, the scripted discovery-out-of-date decision."""

import subprocess
import sys
from pathlib import Path

import pytest

from quoin.core.scripts import discovery_dirty as dd

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "discovery_dirty.py"


@pytest.mark.parametrize("entries,before,after,expected", [
    # Internal edits, new files, tests and docs: clean.
    ([("M", ["src/app/core.py"]), ("A", ["src/app/new.py"])], ["src"], ["src"], []),
    ([("D", ["tests/test_old.py"]), ("M", ["docs/guide.md"]), ("A", ["README.md"])],
     ["src", "tests", "docs"], ["src", "tests", "docs"], []),
    ([("M", ["poetry.lock"]), ("M", ["package-lock.json"])], [], [], []),
    # Manifests, including one that ends in .txt.
    ([("M", ["requirements-dev.txt"])], [], [], ["manifest: requirements-dev.txt"]),
    ([("M", ["services/api/package.json"])], ["services"], ["services"],
     ["manifest: services/api/package.json"]),
    # Structure.
    ([("A", ["worker/main.py"])], ["src"], ["src", "worker"], ["structure: added worker/"]),
    ([("D", ["legacy/x.py"])], ["legacy", "src"], ["src"], ["structure: removed legacy/"]),
    ([("A", [".github/ci.yml"]), ("A", ["my pkg/x.py"])], ["src"], ["src", ".github", "my pkg"],
     ["structure: added my pkg/"]),
    ([("A", ["src/app/plugins/__init__.py"])], ["src"], ["src"],
     ["structure: package src/app/plugins"]),
    # Moving a module inside a package is an internal refactor.
    ([("R", ["src/app/a.py", "src/app/b.py"]), ("D", ["src/app/c.py"])], ["src"], ["src"], []),
    # Interface definition files.
    ([("M", ["api/user.proto"]), ("A", ["db/migrations/0002_add.sql"])], ["api", "db"], ["api", "db"],
     ["interface: api/user.proto", "interface: db/migrations/0002_add.sql"]),
    ([("A", ["spec/openapi.yaml"])], ["spec"], ["spec"], ["interface: spec/openapi.yaml"]),
])
def test_classify(entries, before, after, expected):
    assert dd.classify(entries, before, after) == expected


def test_parse_name_status_z_keeps_paths_unquoted():
    text = "R087\0src/old.py\0src/new.py\0A\0src/caf\u00e9/__init__.py\0"
    assert dd.parse_name_status(text) == [
        ("R", ["src/old.py", "src/new.py"]), ("A", ["src/caf\u00e9/__init__.py"])]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, files, message):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


def _run(*args):
    return subprocess.run([sys.executable, str(WRAPPER), *args], capture_output=True, text=True)


def test_cli_writes_marker_only_for_structural_commit(tmp_path):
    repo, root = tmp_path / "repo", tmp_path / "project"
    repo.mkdir()
    root.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, {"src/app.py": "x = 1\n"}, "init")
    marker = root / dd.MARKER

    # A root commit is compared with the empty tree.
    out = _run("--repo", str(repo), "--task", "t0", "--project-root", str(root), "--dry-run")
    assert out.stdout.strip() == "discovery-dirty: yes (structure: added src/)"

    _commit(repo, {"src/app.py": "x = 2\n", "tests/test_app.py": "\n"}, "edit")
    out = _run("--repo", str(repo), "--task", "t1", "--project-root", str(root))
    assert out.returncode == 0 and out.stdout.strip() == "discovery-dirty: no"
    assert not marker.exists()

    _commit(repo, {"pyproject.toml": "[project]\n"}, "deps")
    out = _run("--repo", str(repo), "--task", "t2", "--project-root", str(root))
    assert out.stdout.strip() == "discovery-dirty: yes (manifest: pyproject.toml)"
    line = marker.read_text().strip()
    assert line.endswith(" t2: manifest: pyproject.toml")

    # --base covers every commit since the task branched, but not commits
    # that land on the base branch afterwards; --dry-run leaves the marker alone.
    _git(repo, "branch", "base", "HEAD~1")
    _git(repo, "checkout", "-q", "base")
    _commit(repo, {"go.mod": "module x\n"}, "upstream")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, {"src/caf\u00e9/__init__.py": "\n"}, "more")
    out = _run("--repo", str(repo), "--task", "t3", "--project-root", str(root),
               "--base", "base", "--dry-run")
    assert out.stdout.strip() == (
        "discovery-dirty: yes (manifest: pyproject.toml; structure: package src/caf\u00e9)")
    assert marker.read_text().strip() == line


def test_cli_outside_git_warns_and_exits_zero(tmp_path):
    out = _run("--repo", str(tmp_path), "--task", "t", "--project-root", str(tmp_path))
    assert out.returncode == 0
    assert "WARN" in out.stderr
    assert not (tmp_path / dd.MARKER).exists()
