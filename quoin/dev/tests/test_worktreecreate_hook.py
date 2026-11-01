"""
Integration-style tests for worktreecreate.sh hook.

These tests feed fake hook input JSON to the shell script and verify
the expected stdout/behavior without requiring a live Claude Code harness.

Note: these tests shell out to bash. They require: bash, git, jq, python3.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent.parent / "hooks"
HOOK_SCRIPT = HOOKS_DIR / "worktreecreate.sh"
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "core" / "scripts"
GIT_ROOT_SCRIPT = SCRIPTS_DIR / "git_root_for_dispatch.py"

# Skip all tests if bash or jq is not available
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq are required for hook tests",
)


def run_hook(
    hook_input: dict,
    sidecar: dict | None,
    project_root: Path,
    tmp_path: Path,
    extra_env: dict | None = None,
) -> tuple[int, str, str]:
    """Run the hook script with hook_input on stdin; optionally write sidecar.

    extra_env, if given, is merged into the child environment (e.g. to set
    QUOIN_WORKTREE_SELFGEN, TMPDIR, or a PATH shim for a fake `timeout`).

    Returns (returncode, stdout, stderr).
    """
    # Write sidecar if provided
    sidecar_path = project_root / ".workflow_artifacts" / ".dispatch-hint.json"
    if sidecar is not None:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(sidecar))

    input_json = json.dumps(hook_input)
    env = os.environ.copy()
    # Point HOME to a temp dir so the hook looks for the script in the right place
    fake_home = tmp_path / "fake_home"
    (fake_home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(GIT_ROOT_SCRIPT, fake_home / ".claude" / "scripts" / "git_root_for_dispatch.py")
    env["HOME"] = str(fake_home)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=input_json,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def make_git_repo(path: Path) -> Path:
    """Create a git repo at path with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@t.com"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "config", "user.name", "T"], capture_output=True, check=True, cwd=str(path))
    (path / ".gitkeep").write_text("x")
    subprocess.run(["git", "add", "."], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "commit", "-m", "init"], capture_output=True, check=True, cwd=str(path))
    return path


def make_v3_plan(task_dir: Path, file_refs: list[str]) -> Path:
    """Write a minimal v3-format plan."""
    refs = "\n".join(f"- `{f}` — ref" for f in file_refs)
    content = f"---\ntask: test\n---\n\n## For human\n\nTest.\n\n## Tasks\n\n{refs}\n"
    plan = task_dir / "current-plan.md"
    plan.write_text(content)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: hook creates worktree for single-repo fixture
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_returns_worktreepath_for_single_repo(tmp_path):
    """Single nested repo + sidecar → hook creates worktree and prints path."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])

    # Where the harness would create the worktree
    worktree_path = str(tmp_path / "worktree" / "repo-a-branch")
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": worktree_path,
        "branch_name": "test-branch",
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(hook_input, sidecar, project_root, tmp_path)
    assert rc == 0, f"Hook exited {rc}. stderr: {stderr}"
    assert stdout == worktree_path, f"Expected worktree path on stdout, got: {stdout!r}"
    assert Path(worktree_path).exists(), "Worktree directory was not created"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: multi-repo → hook returns no stdout (skip)
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_returns_skip_for_multi_repo(tmp_path):
    """Multi-repo plan → helper exits 2 → hook emits no stdout (skip path)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    make_git_repo(project_root / "repo-a")
    make_git_repo(project_root / "repo-b")
    (project_root / "repo-a" / "a.py").write_text("# a")
    (project_root / "repo-b" / "b.py").write_text("# b")
    plan = make_v3_plan(project_root, ["repo-a/a.py", "repo-b/b.py"])

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(hook_input, sidecar, project_root, tmp_path)
    assert rc == 0, f"Hook should fail-OPEN (exit 0), got {rc}"
    assert stdout == "", f"Expected no stdout for multi-repo skip, got: {stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: no nested git → hook returns no stdout (skip)
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_returns_skip_for_no_nested_git(tmp_path):
    """No nested .git in project root → hook emits no stdout (skip)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "plain-dir").mkdir()

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": None,
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(hook_input, sidecar, project_root, tmp_path)
    assert rc == 0, f"Hook should fail-OPEN (exit 0), got {rc}"
    assert stdout == "", f"Expected no stdout, got: {stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: malformed sidecar → hook fails open (exit 0, no stdout)
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_fails_open_on_malformed_sidecar(tmp_path):
    """Malformed sidecar JSON → hook exits 0 with no stdout."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    sidecar_path = project_root / ".workflow_artifacts" / ".dispatch-hint.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text("{not valid json")

    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "hook_event_name": "WorktreeCreate",
    }
    env = os.environ.copy()
    fake_home = tmp_path / "fake_home"
    (fake_home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(GIT_ROOT_SCRIPT, fake_home / ".claude" / "scripts" / "git_root_for_dispatch.py")
    env["HOME"] = str(fake_home)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"Hook should fail-OPEN on malformed sidecar, got {result.returncode}"
    assert result.stdout.strip() == "", f"Expected no stdout, got: {result.stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: stale sidecar (mtime > 60s) → hook exits 0, no stdout
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_rejects_stale_sidecar(tmp_path):
    """Sidecar with mtime > 60s → hook rejects it (exit 0, no stdout)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    make_git_repo(project_root / "repo-a")

    sidecar_path = project_root / ".workflow_artifacts" / ".dispatch-hint.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": None,
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }))
    # Set mtime to 120 seconds ago
    stale_mtime = time.time() - 120
    os.utime(str(sidecar_path), (stale_mtime, stale_mtime))

    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "hook_event_name": "WorktreeCreate",
    }
    env = os.environ.copy()
    fake_home = tmp_path / "fake_home"
    (fake_home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(GIT_ROOT_SCRIPT, fake_home / ".claude" / "scripts" / "git_root_for_dispatch.py")
    env["HOME"] = str(fake_home)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"Expected exit 0 for stale sidecar, got {result.returncode}"
    assert result.stdout.strip() == "", f"Expected no stdout for stale sidecar, got: {result.stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: hook consumes sidecar (single-shot)
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_consumes_sidecar(tmp_path):
    """After hook invocation, sidecar file is removed."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])
    worktree_path = str(tmp_path / "worktree-consume-test")
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": worktree_path,
        "branch_name": "consume-test-branch",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(hook_input, sidecar, project_root, tmp_path)
    assert rc == 0

    sidecar_path = project_root / ".workflow_artifacts" / ".dispatch-hint.json"
    assert not sidecar_path.exists(), f"Sidecar should have been consumed, but still exists at {sidecar_path}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: hook writes audit log
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_writes_audit_log(tmp_path):
    """Hook writes an audit log line to .workflow_artifacts/memory/worktree-hook-audit.log."""
    project_root = tmp_path / "project"
    project_root.mkdir()

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": None,
        "session_id": "test-sid",
        "written_at": "2026-05-21T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(hook_input, sidecar, project_root, tmp_path)
    assert rc == 0

    audit_log = project_root / ".workflow_artifacts" / "memory" / "worktree-hook-audit.log"
    assert audit_log.exists(), f"Audit log not found at {audit_log}"
    content = audit_log.read_text()
    assert content.strip(), "Audit log should not be empty"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: no sidecar → hook exits 0, no stdout (cwd with .git case)
# ─────────────────────────────────────────────────────────────────────────────
def test_hook_no_op_when_sidecar_absent(tmp_path):
    """No sidecar file → hook exits 0 immediately with no stdout."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    # This case could be a single-repo-at-cwd user where no sidecar is written
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt"),
        "branch_name": "test-branch",
        "hook_event_name": "WorktreeCreate",
    }
    env = os.environ.copy()
    fake_home = tmp_path / "fake_home"
    (fake_home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(GIT_ROOT_SCRIPT, fake_home / ".claude" / "scripts" / "git_root_for_dispatch.py")
    env["HOME"] = str(fake_home)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}"
    assert result.stdout.strip() == "", f"Expected no stdout, got: {result.stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# T-11 (IVG-116) — self-generation, opt-out and timeout bounding
# ─────────────────────────────────────────────────────────────────────────────


def test_hook_selfgen_when_harness_omits_path_and_branch(tmp_path):
    """(a) Sidecar present, single nested repo, harness omits worktree_path/branch_name,
    QUOIN_WORKTREE_SELFGEN default (on) → hook self-generates, runs git worktree add,
    prints a path, and records selfgen=1 in the audit log."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-07-09T00:00:00Z",
    }
    # NOTE: no worktree_path / branch_name — the observed harness-omission case.
    hook_input = {
        "cwd": str(project_root),
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }
    # Anchor self-generated worktrees under tmp_path (via TMPDIR) so pytest cleans them up.
    wt_base = tmp_path / "wtbase"
    wt_base.mkdir()

    rc, stdout, stderr = run_hook(
        hook_input, sidecar, project_root, tmp_path, extra_env={"TMPDIR": str(wt_base)}
    )
    assert rc == 0, f"Hook exited {rc}. stderr: {stderr}"
    assert stdout, "Expected a self-generated worktree path on stdout"
    assert Path(stdout).exists(), f"Self-generated worktree dir not created: {stdout!r}"
    # Path anchored outside the project (under TMPDIR), not inside the Drive-synced tree.
    assert str(project_root) not in stdout, "Self-gen worktree should be anchored outside the project root"

    audit = (project_root / ".workflow_artifacts" / "memory" / "worktree-hook-audit.log").read_text()
    assert "selfgen=1" in audit, f"Audit log missing selfgen=1 marker:\n{audit}"


def test_hook_selfgen_opt_out_restores_skip(tmp_path):
    """(b) QUOIN_WORKTREE_SELFGEN=0 → old skip path: no stdout, audit records selfgen=0."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-07-09T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(
        hook_input, sidecar, project_root, tmp_path, extra_env={"QUOIN_WORKTREE_SELFGEN": "0"}
    )
    assert rc == 0, f"Hook should fail-OPEN (exit 0), got {rc}. stderr: {stderr}"
    assert stdout == "", f"Expected no stdout with selfgen disabled, got: {stdout!r}"

    audit = (project_root / ".workflow_artifacts" / "memory" / "worktree-hook-audit.log").read_text()
    assert "missing-worktree-path-or-branch selfgen=0" in audit, f"Audit missing skip marker:\n{audit}"


def test_hook_bounds_git_worktree_add_with_timeout(tmp_path):
    """(c) `git worktree add` is wrapped in `timeout`: when the timeout binary reports
    expiry (exit 124), the hook fails-OPEN (exit 0, no stdout). A fake `timeout` shim
    proves the wrapper is active — unwrapped git would have succeeded and printed a path."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])

    # Fake `timeout` binary that simulates expiry (exit 124) without running the command.
    shim_dir = tmp_path / "shimbin"
    shim_dir.mkdir()
    fake_timeout = shim_dir / "timeout"
    fake_timeout.write_text("#!/usr/bin/env bash\nexit 124\n")
    fake_timeout.chmod(fake_timeout.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    sidecar = {
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-sid",
        "written_at": "2026-07-09T00:00:00Z",
    }
    hook_input = {
        "cwd": str(project_root),
        "worktree_path": str(tmp_path / "wt-timeout"),
        "branch_name": "timeout-branch",
        "session_id": "test-sid",
        "hook_event_name": "WorktreeCreate",
    }

    rc, stdout, stderr = run_hook(
        hook_input,
        sidecar,
        project_root,
        tmp_path,
        extra_env={"PATH": f"{shim_dir}:{os.environ.get('PATH', '')}", "QUOIN_SUBPROCESS_TIMEOUT": "1"},
    )
    assert rc == 0, f"Hook should fail-OPEN on git-worktree-add timeout, got {rc}. stderr: {stderr}"
    assert stdout == "", f"Expected no stdout when git worktree add times out, got: {stdout!r}"
    assert not Path(str(tmp_path / "wt-timeout")).exists(), "No worktree should exist after a timed-out add"

