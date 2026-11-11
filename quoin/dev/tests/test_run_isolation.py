"""Concurrent /run isolation: per-checkout registry and per-task worktrees.

Covers run_isolation.py (claim / release / status), the out-of-project
location support it relies on in workspace.py, stage worktrees created from a
task worktree, and the /run skill wiring (claim, release, fail-open).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core" / "scripts"
WRAPPER = ROOT / "scripts" / "run_isolation.py"
RUN_SKILL = ROOT / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
LIGHT = ROOT / "adapters" / "claude" / "skills" / "run" / "light-pipeline.md"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_test_ri_{name}", CORE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ri = _load("run_isolation")
ws = ri.workspace  # the instance run_isolation itself uses
ps = _load("parallel_stages")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Project folder holding one repo; registry and worktrees under tmp."""
    project = tmp_path / "project"
    repo = project / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    monkeypatch.setenv("QUOIN_RUN_REGISTRY", str(tmp_path / "runs"))
    monkeypatch.setenv("QUOIN_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(os.getpid()))  # a live owner
    monkeypatch.setenv("QUOIN_WORKSPACE_DISABLE_GH", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return project, repo.resolve(), tmp_path


def _claim(env, task, sid, **kw):
    project, repo, _ = env
    return ri.claim(task, repo, project.resolve(), branch=kw.pop("branch", f"feat/{task}"),
                    session_id=sid, **kw)


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_single_run_works_in_the_checkout(env):
    project, repo, _ = env
    code, out = _claim(env, "one", "s1")
    assert code == 0 and out["mode"] == "main" and out["reason"] == "free"
    assert out["worktree"] == str(repo)
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not (project / ".workflow_artifacts" / "memory" / "workspaces").exists()
    assert ri.release("one", repo)["released"] is True
    assert ri.status(repo)["runs"] == []


def test_second_live_run_gets_its_own_worktree(env):
    project, repo, tmp = env
    _claim(env, "first", "s1")
    code, out = _claim(env, "second", "s2")
    assert code == 0 and out["mode"] == "worktree" and out["reason"] == "conflict"
    assert out["blocking"] == "first"
    wt = Path(out["worktree"])
    assert wt == tmp / "worktrees" / "app" / "second" / "app"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "feat/second"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # untouched
    record = json.loads((project / ".workflow_artifacts" / "memory" / "workspaces"
                         / "second.json").read_text())
    assert record["workspace_path"] == str(wt.parent)
    # Independent trees: a change in the worktree never shows in the checkout.
    (wt / "b.txt").write_text("b\n")
    assert _git(repo, "status", "--porcelain") == ""
    assert not (project / ".gitignore").exists()  # nothing landed in the project


def test_simultaneous_start_gives_the_checkout_to_one_run(env):
    project, repo, _ = env
    procs = [
        subprocess.Popen(
            [sys.executable, str(WRAPPER), "claim", "--task", f"t{i}", "--branch", f"b{i}",
             "--repo", str(repo), "--project-root", str(project), "--session-id", f"s{i}"],
            stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        for i in range(4)
    ]
    results = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
    modes = sorted(r["mode"] for r in results)
    assert modes == ["main", "worktree", "worktree", "worktree"]
    assert len({r["worktree"] for r in results}) == 4


def test_dead_owner_does_not_block(env, monkeypatch):
    _, repo, _ = env
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(_dead_pid()))
    _claim(env, "crashed", "s1")
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(os.getpid()))
    code, out = _claim(env, "next", "s2")
    assert code == 0 and out["mode"] == "main"
    runs = {r["slug"]: r["live"] for r in ri.status(repo)["runs"]}
    assert runs == {"crashed": False, "next": True}


def test_same_session_leftover_does_not_block(env):
    _claim(env, "aborted", "s1")
    code, out = _claim(env, "fresh", "s1")
    assert out["mode"] == "main"


def test_isolate_forces_a_worktree(env):
    code, out = _claim(env, "solo", "s1", isolate=True)
    assert code == 0 and out["mode"] == "worktree" and out["reason"] == "isolate"
    assert out["blocking"] is None


def test_resume_returns_to_the_same_worktree(env):
    project, repo, _ = env
    _claim(env, "holder", "s1")
    _, first = _claim(env, "task", "s2")
    # A relaunched session (new id, same task) resumes in the same tree...
    _, again = _claim(env, "task", "s3", branch=None)
    assert again["mode"] == "worktree" and again["reason"] == "resume"
    assert again["worktree"] == first["worktree"] and again["branch"] == "feat/task"
    # ...even after the registry entry was released, via the workspace record.
    ri.release("task", repo)
    ri.release("holder", repo)
    _, third = _claim(env, "task", "s4", branch=None)
    assert third["reason"] == "resume" and third["worktree"] == first["worktree"]


def test_teardown_finds_the_out_of_project_workspace(env):
    project, repo, _ = env
    _claim(env, "holder", "s1")
    _, out = _claim(env, "done", "s2")
    wt = Path(out["worktree"])
    _git(repo, "merge", "-q", "feat/done")  # provably merged → safe to remove
    result = ws.teardown_workspace("done", project)
    assert result.folder_removed and not wt.exists()


def test_record_outside_the_worktree_root_is_ignored(env, tmp_path):
    project, _, _ = env
    victim = tmp_path / "elsewhere" / "evil"
    victim.mkdir(parents=True)
    ws.write_ownership_record(project, "evil", {"workspace_path": str(victim)})
    assert ws.resolve_workspace_root(project.resolve(), "evil",
                                     record={"workspace_path": str(victim)}) \
        == project.resolve() / ".workspaces" / "evil"
    ws.teardown_workspace("evil", project)
    assert victim.exists()


def test_parallel_stage_worktrees_branch_from_the_task_worktree(env):
    _claim(env, "holder", "s1")
    _, out = _claim(env, "staged", "s2")
    task_wt, branch = out["worktree"], out["branch"]
    info = ps.make_worktree(task_wt, branch, "S-01", None)
    assert Path(info["path"]).is_relative_to(Path(task_wt))
    (Path(info["path"]) / "s1.txt").write_text("s1\n")
    _git(info["path"], "add", "s1.txt")
    _git(info["path"], "commit", "-qm", "S-01")
    assert _git(task_wt, "status", "--porcelain") == ""
    code, res = ps.merge_stage(task_wt, branch, info["branch"])
    assert code == 0 and res["status"] == "merged"
    assert ps.cleanup_stage(task_wt, branch, info["branch"], False)[1]["status"] == "cleaned"
    assert (Path(task_wt) / "s1.txt").exists()


def test_run_skill_claims_releases_and_fails_open():
    text = RUN_SKILL.read_text()
    assert 'run_isolation.py" claim --task "<task>"' in text
    assert 'run_isolation.py" release --task "<task>"' in text
    assert "[quoin-run: run isolation unavailable; working in this checkout]" in text
    assert "**`isolate:`**" in text and "`isolate:` and the like" in text
    assert "`<repo>` below is the stage repo's task working tree" in LIGHT.read_text()


def test_reused_pid_does_not_block(env):
    _, repo, _ = env
    _claim(env, "old", "s1")
    entry_path = ri.registry_dir(repo) / "old.json"
    entry = json.loads(entry_path.read_text())
    entry["owner_pid_start"] = "Thu Jan  1 00:00:00 1970"  # pid now names another process
    entry_path.write_text(json.dumps(entry))
    assert _claim(env, "new", "s2")[1]["mode"] == "main"


def test_foreign_host_entry_falls_back_to_staleness(env):
    _, repo, _ = env
    _claim(env, "remote", "s1")
    entry_path = ri.registry_dir(repo) / "remote.json"
    entry = json.loads(entry_path.read_text())
    entry.update(hostname="other-host", last_seen="2000-01-01T00:00:00Z")
    entry_path.write_text(json.dumps(entry))
    old = 946684800  # 2000-01-01: the file mtime must look stale too
    os.utime(entry_path, (old, old))
    assert _claim(env, "local", "s2")[1]["mode"] == "main"


def test_release_from_inside_the_worktree_finds_the_entry(env):
    _, repo, _ = env
    _claim(env, "holder", "s1")
    _, out = _claim(env, "iso", "s2")
    assert ri.repo_toplevel(Path(out["worktree"])) == repo
    assert ri.main(["release", "--task", "iso", "--repo", out["worktree"]]) == 0
    assert [r["slug"] for r in ri.status(repo)["runs"]] == ["holder"]


def test_resumed_checkout_task_is_not_moved_while_held(env, monkeypatch):
    _claim(env, "mine", "s1")
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(_dead_pid()))
    _claim(env, "mine", "s1")  # owner of "mine" dies
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(os.getpid()))
    _claim(env, "other", "s2")  # another run takes the checkout
    code, out = _claim(env, "mine", "s3")
    assert code == 3 and out["reason"] == "resume-conflict" and out["blocking"] == "other"


@pytest.mark.parametrize("shape", ["repo-folder", "root"])
def test_crafted_record_cannot_widen_teardown(env, shape):
    project, repo, tmp = env
    _claim(env, "holder", "s1")
    _, out = _claim(env, "victim", "s2")
    victim_wt = Path(out["worktree"])
    (victim_wt / "wip.txt").write_text("uncommitted\n")
    parent = ws.worktree_root() / "app"
    slug, path = ("app", parent) if shape == "repo-folder" else ("worktrees", ws.worktree_root())
    ws.write_ownership_record(project, slug, {"workspace_path": str(path)})
    ws.teardown_workspace(slug, project)
    assert (victim_wt / "wip.txt").exists()
