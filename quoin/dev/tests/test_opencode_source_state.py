"""Source state in run-store repository revisions: real git, opt-in."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from quoin.opencode_adapter import runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def _git_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for key, value in (
        ("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
        ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid"),
    ):
        monkeypatch.setenv(key, value)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode()


def make_repo(path, files=None):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, text in (files or {"src/x.py": "x = 1\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def entries(root, **kw):
    return {e["path"]: e for e in runstore.repo_revisions(root, source=True, **kw)}


def test_default_call_keeps_legacy_keys_and_calls(tmp_path):
    calls = []

    def fake(argv, timeout_s):
        calls.append(tuple(argv[3:]))
        return (0, "abc\n" if argv[3] == "rev-parse" else "")

    root = make_repo(tmp_path / "r")
    result = runstore.repo_revisions(root, runner=fake)
    assert [sorted(e) for e in result] == [["dirty", "error", "head", "path"]]
    assert calls == [("rev-parse", "HEAD"), ("status", "--porcelain")]


def test_clean_repo(tmp_path):
    root = make_repo(tmp_path / "r")
    entry = entries(root)["."]
    assert entry["source_dirty"] is False
    assert entry["source_digest"] is None and entry["source_error"] is None


@pytest.mark.parametrize("rel", [".workflow_artifacts/TASK/a.md", ".opencode/x", ".quoin/x", ".workspaces/x"])
def test_output_directories_do_not_make_source_dirty(tmp_path, rel):
    root = make_repo(tmp_path / "r")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("data\n")
    entry = entries(root)["."]
    assert entry["dirty"] is True
    assert entry["source_dirty"] is False


def test_project_root_nested_inside_repo(tmp_path):
    top = make_repo(tmp_path / "top", {"proj/src/x.py": "x\n"})
    project = top / "proj"
    target = project / ".workflow_artifacts" / "T" / "a.md"
    target.parent.mkdir(parents=True)
    target.write_text("a\n")
    entry = entries(project)[os.path.relpath(top, project)]
    assert entry["dirty"] is True
    assert entry["source_dirty"] is False


def test_tracked_edits_change_digest_and_revert_cleans(tmp_path):
    root = make_repo(tmp_path / "r")
    (root / "src/x.py").write_text("x = 2\n")
    first = entries(root)["."]
    assert first["source_dirty"] is True and first["source_digest"]
    (root / "src/x.py").write_text("x = 3\n")
    second = entries(root)["."]
    assert second["source_digest"] and second["source_digest"] != first["source_digest"]
    again = entries(root)["."]
    assert again["source_digest"] == second["source_digest"]
    (root / "src/x.py").write_text("x = 1\n")
    assert entries(root)["."]["source_dirty"] is False


def test_untracked_file_and_content_change(tmp_path):
    root = make_repo(tmp_path / "r")
    (root / "src/x.py").write_text("x = 2\n")
    base = entries(root)["."]["source_digest"]
    (root / "new.txt").write_text("one\n")
    added = entries(root)["."]["source_digest"]
    (root / "new.txt").write_text("two\n")
    changed = entries(root)["."]["source_digest"]
    assert len({base, added, changed}) == 3


def test_untracked_symlink_hashed_by_target_text(tmp_path):
    root = make_repo(tmp_path / "r")
    outside = tmp_path / "outside.txt"
    outside.write_text("one\n")
    (root / "link").symlink_to(outside)
    first = entries(root)["."]
    assert first["source_dirty"] is True and first["source_digest"]
    outside.write_text("two\n")
    assert entries(root)["."]["source_digest"] == first["source_digest"]


def test_nested_repo_edit_changes_only_its_own_entry(tmp_path):
    root = make_repo(tmp_path / "proj", {"a.txt": "a\n"})
    make_repo(root / "svc", {"s.txt": "s\n"})
    before = entries(root)
    assert before["."]["source_dirty"] is False
    (root / "svc" / "s.txt").write_text("changed\n")
    after = entries(root)
    assert after["svc"]["source_dirty"] is True and after["svc"]["source_digest"]
    assert after["."]["source_dirty"] is False
    assert after["."]["source_digest"] == before["."]["source_digest"]


def test_budgets(tmp_path):
    root = make_repo(tmp_path / "r")
    (root / "src/x.py").write_text("x = 2\n")
    small = entries(root, max_source_bytes=1)["."]
    assert small["source_error"] == "too-large" and small["source_digest"] is None
    assert small["source_dirty"] is True
    zero = runstore.repo_revisions(root, source=True, budget_s=0)[0]
    assert zero["error"] == "budget" and zero["head"] is None
    assert (zero["source_dirty"], zero["source_digest"], zero["source_error"]) == (None, None, "budget")
    (root / "u.txt").write_text("u\n")
    many = entries(root, max_untracked_files=0)["."]
    assert many["source_error"] == "too-many-files" and many["source_digest"] is None


def test_external_diff_driver_is_never_run(tmp_path):
    root = make_repo(tmp_path / "r")
    marker = tmp_path / "marker"
    script = tmp_path / "ext.sh"
    script.write_text("#!/bin/sh\ntouch %s\n" % marker)
    script.chmod(0o755)
    git(root, "config", "diff.external", str(script))
    (root / "src/x.py").write_text("x = 2\n")
    entry = entries(root)["."]
    assert entry["source_digest"]
    assert not marker.exists()


def test_pathspec_uses_literal_exclusions(tmp_path):
    root = make_repo(tmp_path / "r")
    specs = runstore.source_pathspecs(str(root), str(root), [str(root), str(root / "svc")])
    assert "." in specs
    assert ":(exclude,literal).workflow_artifacts" in specs
    assert ":(exclude,literal)svc" in specs
    assert specs == sorted(set(specs))


def test_hash_inputs_exclude_runs_before_the_file_cap(tmp_path):
    task = tmp_path / ".workflow_artifacts" / "T"
    task.mkdir(parents=True)
    (task / "a-excluded.md").write_text("skip\n")
    (task / "b-kept.md").write_text("keep\n")
    result = runstore.hash_inputs(
        tmp_path, "T", max_files=1, exclude=lambda rel: rel.endswith("a-excluded.md"),
    )
    assert list(result) == [".workflow_artifacts/T/b-kept.md"]
    assert "<truncated>" not in result


def test_bytes_runner_caps_output(tmp_path):
    code, data, cut = runstore._default_git_bytes_runner(("git", "--version"), 10.0, 1 << 20)
    assert code == 0 and data.startswith(b"git version") and cut is False
    code, data, cut = runstore._default_git_bytes_runner(("git", "--version"), 10.0, 3)
    assert cut is True and len(data) == 3
