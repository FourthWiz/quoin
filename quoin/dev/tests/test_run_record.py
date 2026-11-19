"""Tests for run_record.py, /run's scripted commit bookkeeping."""

import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "run_record.py"
MARKER = Path(".workflow_artifacts") / "memory" / "discovery-dirty"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, files, message):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"src/app.py": "x = 1\n"}, "base")
    _git(repo, "checkout", "-q", "-b", "task-branch")
    return repo


def _record(repo, project, *extra, profile=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(project)}
    if profile:
        env["QUOIN_RUN_PROFILE"] = profile
    return subprocess.run(
        [sys.executable, str(WRAPPER), "--task", "t", "--repo", str(repo), "--base", "main",
         "--project-root", str(project), *extra],
        capture_output=True, text=True, env=env, check=True,
    )


def test_writes_the_r0_brief_and_marks_a_structural_commit(repo, tmp_path):
    _commit(repo, {"requirements.txt": "requests\n"}, "add dep")
    out = _record(repo, tmp_path, "--request", "IVG-1", "--verify", "pytest: pass").stdout
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    brief = (tmp_path / ".workflow_artifacts" / "t" / "brief.md").read_text()
    assert brief == (f"# t\n## Request\nIVG-1\n## Scope\nroute: r0\n## Verification\npytest: pass\n"
                     f"## Commit\nbranch: task-branch\ncommit: {head}\n")
    assert "discovery-dirty: yes" in out
    assert "manifest: requirements.txt" in (tmp_path / MARKER).read_text()


def test_appends_to_an_existing_brief_and_lean_skips_the_marker(repo, tmp_path):
    brief = tmp_path / ".workflow_artifacts" / "t" / "stage-2" / "brief.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# t stage 2\n## Scope\nroute: r1")
    _commit(repo, {"requirements.txt": "requests\n"}, "add dep")
    _record(repo, tmp_path, "--stage", "2", "--verify", "pytest: fail", profile="bench-lean")
    text = brief.read_text()
    assert text.startswith("# t stage 2\n## Scope\nroute: r1\n## Verification\npytest: fail\n## Commit\n")
    assert not (tmp_path / MARKER).exists()


def test_no_commit_records_only_verification(repo, tmp_path):
    _record(repo, tmp_path, "--no-commit", "--verify", "not run: escalated")
    brief = (tmp_path / ".workflow_artifacts" / "t" / "brief.md").read_text()
    assert brief.endswith("## Verification\nnot run: escalated\n") and "## Commit" not in brief


def test_a_git_failure_still_records_verification_and_exits_zero(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    result = _record(not_a_repo, tmp_path, "--verify", "pytest: pass")
    brief = (tmp_path / ".workflow_artifacts" / "t" / "brief.md").read_text()
    assert result.returncode == 0 and "## Verification\npytest: pass\n" in brief
    assert "## Commit" not in brief and "WARN git failed" in result.stderr
