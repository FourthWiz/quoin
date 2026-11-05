"""Changed-file resolution when a task's code lives in a linked git worktree.

Layout under test (real git repos, built in tmp_path):

    <project>/.workflow_artifacts/<task>/     task context at the outer root
    <project>/quoin/                          main checkout, dirty with unrelated work
    <project>/.workspaces/feat/quoin/         `git worktree add` for the task branch

`--project-root <project>` alone finds only the main checkout. With
`--repo-root <worktree>` the changed set must come from the worktree: its
branch diff against the merge-base plus its own uncommitted edits, and none of
the main checkout's dirty files. `--task <name>` derives the same worktree from
`.workspaces/<slug>/` so callers need not pass `--repo-root` by hand.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "core" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_quoin_core_{name}_worktree_test", _SCRIPTS / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_at = _load("affected_tests")
_ci = _load("ci_mirror")


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _capture(main, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOIN_BASE_BRANCH", raising=False)
    monkeypatch.delenv("QUOIN_DISABLE_AFFECTED_TESTS", raising=False)
    monkeypatch.delenv("QUOIN_DISABLE_CI_MIRROR", raising=False)
    monkeypatch.delenv("QUOIN_DISABLE_CHILD_REPO_SCAN", raising=False)
    monkeypatch.delenv("QUOIN_REQUIRE_TASK_CONTEXT", raising=False)

    project = tmp_path / "my project"
    (project / ".workflow_artifacts" / "the-task").mkdir(parents=True)
    main = project / "quoin"
    main.mkdir()
    _git("init", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t.com", cwd=main)
    _git("config", "user.name", "T", cwd=main)
    for name in ("test_base.py", "test_main_dirty.py"):
        (main / name).write_text("def test_ok():\n    pass\n")
    _git("add", ".", cwd=main)
    _git("commit", "-m", "baseline", cwd=main)

    worktree = project / ".workspaces" / "feat" / "quoin"
    _git("worktree", "add", "-b", "feat", str(worktree), cwd=main)
    (worktree / "test_branch.py").write_text("def test_ok():\n    pass\n")
    _git("add", "test_branch.py", cwd=worktree)
    _git("commit", "-m", "branch work", cwd=worktree)
    # Uncommitted edit inside the worktree.
    (worktree / "test_base.py").write_text("def test_ok():\n    assert 1\n")

    # Another session's unrelated, uncommitted work in the main checkout.
    (main / "test_main_dirty.py").write_text("def test_ok():\n    assert 0\n")
    return project, main, worktree


def test_project_root_alone_resolves_main_checkout(layout):
    project, _main, _worktree = layout
    rc, out = _capture(_at.main, ["--project-root", str(project), "--select-only"])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_main_dirty.py"]


def test_repo_root_drives_changed_set_from_worktree(layout, tmp_path):
    project, _main, worktree = layout
    sel_file = tmp_path / "selectors.txt"
    rc, out = _capture(_at.main, [
        "--project-root", str(project), "--repo-root", str(worktree),
        "--require-task-context", "--select-only",
        "--selectors-out", str(sel_file),
    ])
    assert rc == 0, out
    data = json.loads(out)
    assert data["changed"] == ["test_base.py", "test_branch.py"]
    assert "test_main_dirty.py" not in out
    selectors = sel_file.read_text().splitlines()
    assert sorted(Path(s).name for s in selectors) == ["test_base.py", "test_branch.py"]
    assert all(Path(s).resolve().parent == worktree.resolve() for s in selectors)


def test_repo_root_that_is_not_a_work_tree_top_exits_3(layout, capsys):
    project, _main, worktree = layout
    sub = worktree / "sub"
    sub.mkdir()
    rc = _at.main(["--project-root", str(project), "--repo-root", str(sub), "--select-only"])
    assert rc == 3
    err = json.loads(capsys.readouterr().err)
    assert err["exit_reason"] == "undeterminable-no-repo"
    assert "--repo-root" in err["error"]


def test_branch_diff_includes_uncommitted_edits(layout):
    _project, _main, worktree = layout
    files, reason = _at.changed_files(worktree)
    assert reason == "base-branch-diff"
    assert files == ["test_base.py", "test_branch.py"]


def test_ci_mirror_repo_root_drives_changed_set(layout):
    project, _main, worktree = layout
    rc, out = _capture(_ci.main, [
        "--project-root", str(project), "--repo-root", str(worktree), "--derive-only",
    ])
    assert rc == 0, out
    data = json.loads(out)
    assert data["changed"] == ["test_base.py", "test_branch.py"]
    assert data["exit_reason"] == "no-deliverable"


def test_ci_mirror_project_root_alone_resolves_main_checkout(layout):
    project, _main, _worktree = layout
    rc, out = _capture(_ci.main, ["--project-root", str(project), "--derive-only"])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_main_dirty.py"]


def test_task_derives_worktree_for_both_helpers(layout):
    project, _main, _worktree = layout
    rc, out = _capture(_at.main, [
        "--project-root", str(project), "--task", "Feat", "--select-only",
    ])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_base.py", "test_branch.py"]
    rc, out = _capture(_ci.main, [
        "--project-root", str(project), "--task", "feat", "--derive-only",
    ])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_base.py", "test_branch.py"]


def test_task_without_workspace_keeps_main_checkout(layout, capsys):
    project, _main, _worktree = layout
    rc, out = _capture(_at.main, [
        "--project-root", str(project), "--task", "the-task", "--select-only",
    ])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_main_dirty.py"]
    assert "using the main checkout" in capsys.readouterr().err


def test_task_worktree_picks_repo_named_like_main_checkout(layout):
    project, main, worktree = layout
    other = project / ".workspaces" / "feat" / "other"
    _git("worktree", "add", "-b", "feat-other", str(other), cwd=main)
    assert _at.task_worktree(project, "feat") == worktree.resolve()
    assert _at.task_worktree(project, "") is None


def test_explicit_repo_root_wins_over_task(layout, capsys):
    project, main, _worktree = layout
    rc, out = _capture(_at.main, [
        "--project-root", str(project), "--repo-root", str(main),
        "--task", "feat", "--select-only",
    ])
    assert rc == 0, out
    assert json.loads(out)["changed"] == ["test_main_dirty.py"]
