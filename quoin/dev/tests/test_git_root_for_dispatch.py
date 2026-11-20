"""
Tests for git_root_for_dispatch.py — nested git root resolver.

Tests 1-15 are from the round-3 plan (preserved verbatim).
Tests 16-19 cover sidecar mode (round-4 additions).

All tests are deterministic: no network calls, no env-dependent paths beyond tmp_path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent.parent / "core" / "scripts"
HELPER = SCRIPTS_DIR / "git_root_for_dispatch.py"


def run_helper(*args, env=None, **kwargs):
    """Run the helper script and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(HELPER)] + list(args)
    merged_env = os.environ.copy()
    # Remove opt-out var to ensure clean test environment
    merged_env.pop("QUOIN_DISABLE_DISPATCH_CWD", None)
    if env:
        merged_env.update(env)
    result = subprocess.run(cmd, capture_output=True, text=True, env=merged_env, **kwargs)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def make_git_repo(path: Path) -> Path:
    """Initialize a bare git repo at path."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        capture_output=True, check=True, cwd=str(path)
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        capture_output=True, check=True, cwd=str(path)
    )
    # Create an initial commit so the repo is valid
    (path / ".gitkeep").write_text("placeholder")
    subprocess.run(["git", "add", ".gitkeep"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(
        ["git", "commit", "-m", "init"],
        capture_output=True, check=True, cwd=str(path)
    )
    return path


def make_v3_plan(task_dir: Path, file_refs: list[str], plan_name: str = "current-plan.md") -> Path:
    """Write a minimal v3-format plan file with backtick references to file_refs."""
    refs = "\n".join(f"- `{f}` — test file" for f in file_refs)
    content = f"""---
task: test-task
---

## For human

Test plan.

## Tasks

{refs}
"""
    plan = task_dir / plan_name
    plan.write_text(content)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: single-repo plan-mode → exit 0, path on stdout
# ─────────────────────────────────────────────────────────────────────────────
def test_single_repo_plan_mode(tmp_path):
    """Plan references a file in a nested git repo → exit 0, repo path on stdout."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    src_file = repo / "src" / "main.py"
    src_file.parent.mkdir()
    src_file.write_text("# main")

    plan = make_v3_plan(project_root, ["repo-a/src/main.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: no nested repo → exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_no_nested_repo_exit1(tmp_path):
    """Plan references a file but no nested .git → exit 1."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    src_file = project_root / "src" / "main.py"
    src_file.parent.mkdir()
    src_file.write_text("# main")
    plan = make_v3_plan(project_root, ["src/main.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: multi-repo plan → exit 2, paths on stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_repo_plan_exit2(tmp_path):
    """Plan references files in two nested repos → exit 2."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo_a = make_git_repo(project_root / "repo-a")
    repo_b = make_git_repo(project_root / "repo-b")
    (repo_a / "a.py").write_text("# a")
    (repo_b / "b.py").write_text("# b")
    plan = make_v3_plan(project_root, ["repo-a/a.py", "repo-b/b.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 2, f"Expected exit 2, got {rc}"
    assert "repo-a" in stderr or str(repo_a.resolve()) in stderr
    assert "repo-b" in stderr or str(repo_b.resolve()) in stderr


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: missing plan file → exit 3
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_plan_exit3(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    rc, stdout, stderr = run_helper(
        "--plan", str(project_root / "does-not-exist.md"),
        "--cwd", str(project_root)
    )
    assert rc == 3, f"Expected exit 3, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: cwd is git root → exit 1 (no nested-git problem)
# ─────────────────────────────────────────────────────────────────────────────
def test_cwd_is_git_root_exit1(tmp_path):
    """When project root is itself a git repo, no pivot needed → exit 1."""
    cwd_repo = make_git_repo(tmp_path / "cwd-repo")
    plan = make_v3_plan(cwd_repo, ["src/main.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(cwd_repo))
    assert rc == 1, f"Expected exit 1, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: QUOIN_DISABLE_DISPATCH_CWD=1 → exit 1 immediately
# ─────────────────────────────────────────────────────────────────────────────
def test_opt_out_env_var_exit1(tmp_path):
    """QUOIN_DISABLE_DISPATCH_CWD=1 → exit 1 unconditionally."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "a.py").write_text("# a")
    plan = make_v3_plan(project_root, ["repo-a/a.py"])
    rc, stdout, stderr = run_helper(
        "--plan", str(plan), "--cwd", str(project_root),
        env={"QUOIN_DISABLE_DISPATCH_CWD": "1"}
    )
    assert rc == 1, f"Expected exit 1 (opt-out), got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: cwd-scan-only with one child .git → exit 0
# ─────────────────────────────────────────────────────────────────────────────
def test_cwd_scan_only_one_child(tmp_path):
    """--cwd-scan-only with one child .git → exit 0, path on stdout."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "single-repo")
    rc, stdout, stderr = run_helper("--cwd-scan-only", "--cwd", str(project_root))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: cwd-scan-only with zero child .git → exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_cwd_scan_only_zero_children(tmp_path):
    """--cwd-scan-only with no child .git → exit 1."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "plain-dir").mkdir()
    rc, stdout, stderr = run_helper("--cwd-scan-only", "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: cwd-scan-only with two+ child .git → exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_cwd_scan_only_multiple_children(tmp_path):
    """--cwd-scan-only with two child .git dirs → exit 2."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    make_git_repo(project_root / "repo-x")
    make_git_repo(project_root / "repo-y")
    rc, stdout, stderr = run_helper("--cwd-scan-only", "--cwd", str(project_root))
    assert rc == 2, f"Expected exit 2, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: plan with paths outside the nested repo (absolute paths, .workflow_artifacts)
#          → filtered out → no repo resolved → exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_plan_filters_absolute_and_workflow_paths(tmp_path):
    """Paths starting with / or .workflow_artifacts/ are filtered out."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    make_git_repo(project_root / "repo-a")
    plan = make_v3_plan(project_root, [
        "/usr/local/lib/python.py",
        ".workflow_artifacts/memory/sessions/foo.md",
        "~/.claude/scripts/path_resolve.py",
        "__QUOIN_HOME__/scripts/foo.py",
    ])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1 (all paths filtered), got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: v2-format plan → exit 1 (no v3 Tasks/Procedures section)
# ─────────────────────────────────────────────────────────────────────────────
def test_v2_plan_exit1(tmp_path):
    """v2-format plans (no ## For human heading) → exit 1."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "a.py").write_text("# a")
    # v2 plan: no frontmatter / no ## For human
    plan = project_root / "current-plan.md"
    plan.write_text("# Plan\n\n- `repo-a/a.py` — thing\n")
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1 for v2 plan, got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: path in plan doesn't exist on disk → skipped → exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_nonexistent_path_skipped(tmp_path):
    """Plan references a file that doesn't exist on disk → skipped → exit 1."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    make_git_repo(project_root / "repo-a")
    plan = make_v3_plan(project_root, ["repo-a/does-not-exist.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1 (file not exist), got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: deeply nested file in repo → walk-up finds the nested git root
# ─────────────────────────────────────────────────────────────────────────────
def test_deeply_nested_file_in_repo(tmp_path):
    """File deeply nested in the repo → walk-up finds nested git root."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    deep_dir = repo / "a" / "b" / "c"
    deep_dir.mkdir(parents=True)
    (deep_dir / "deep.py").write_text("# deep")
    plan = make_v3_plan(project_root, ["repo-a/a/b/c/deep.py"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: file token without code extension → filtered out
# ─────────────────────────────────────────────────────────────────────────────
def test_token_without_code_extension_filtered(tmp_path):
    """Backtick tokens without a recognized code extension are filtered out."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "Makefile").write_text("build:")
    # Makefile has no recognized extension
    plan = make_v3_plan(project_root, ["repo-a/Makefile"])
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 1, f"Expected exit 1 (Makefile not in _CODE_EXT), got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: Plan with only Procedures section containing repo reference
# ─────────────────────────────────────────────────────────────────────────────
def test_procedures_section_scanned(tmp_path):
    """Paths in the ## Procedures section are scanned (not only ## Tasks)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.sh").write_text("#!/bin/bash")
    plan_content = """---
task: test-task
---

## For human

Plan.

## Tasks

No references here.

## Procedures

See `repo-a/main.sh` for the implementation.
"""
    plan = project_root / "current-plan.md"
    plan.write_text(plan_content)
    rc, stdout, stderr = run_helper("--plan", str(plan), "--cwd", str(project_root))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: sidecar mode with plan_path set → behaves like --plan
# ─────────────────────────────────────────────────────────────────────────────
def test_sidecar_mode_plan_path_set(tmp_path):
    """Sidecar with plan_path → resolves to that repo; exit 0."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "repo-a")
    (repo / "main.py").write_text("# main")
    plan = make_v3_plan(project_root, ["repo-a/main.py"])

    sidecar_path = project_root / ".dispatch-hint.json"
    sidecar_path.write_text(json.dumps({
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": str(plan),
        "session_id": "test-session",
        "written_at": "2026-05-21T00:00:00Z",
    }))

    rc, stdout, stderr = run_helper("--sidecar", str(sidecar_path))
    assert rc == 0, f"Expected exit 0, got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 17: sidecar mode with plan_path null → falls through to cwd-scan-only
# ─────────────────────────────────────────────────────────────────────────────
def test_sidecar_mode_no_plan_path(tmp_path):
    """Sidecar with plan_path: null → falls through to cwd-scan-only behavior."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    repo = make_git_repo(project_root / "single-repo")

    sidecar_path = project_root / ".dispatch-hint.json"
    sidecar_path.write_text(json.dumps({
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": None,
        "session_id": "test-session",
        "written_at": "2026-05-21T00:00:00Z",
    }))

    rc, stdout, stderr = run_helper("--sidecar", str(sidecar_path))
    assert rc == 0, f"Expected exit 0 (cwd-scan-only), got {rc}. stderr: {stderr}"
    assert Path(stdout) == repo.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 18: sidecar mode with malformed JSON → exit 3
# ─────────────────────────────────────────────────────────────────────────────
def test_sidecar_mode_malformed_json(tmp_path):
    """Sidecar with malformed JSON → exit 3."""
    sidecar_path = tmp_path / ".dispatch-hint.json"
    sidecar_path.write_text("{not: valid json")
    rc, stdout, stderr = run_helper("--sidecar", str(sidecar_path))
    assert rc == 3, f"Expected exit 3 (malformed JSON), got {rc}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 19: sidecar mode with stale mtime (> 60s) → exit 3
# ─────────────────────────────────────────────────────────────────────────────
def test_sidecar_mode_stale_mtime(tmp_path):
    """Sidecar older than 60s → exit 3."""
    project_root = tmp_path / "project"
    project_root.mkdir()

    sidecar_path = project_root / ".dispatch-hint.json"
    sidecar_path.write_text(json.dumps({
        "skill_name": "implement",
        "project_root": str(project_root),
        "plan_path": None,
        "session_id": "test-session",
        "written_at": "2026-05-21T00:00:00Z",
    }))

    # Set mtime to 120 seconds ago (stale)
    stale_mtime = time.time() - 120
    os.utime(str(sidecar_path), (stale_mtime, stale_mtime))

    rc, stdout, stderr = run_helper("--sidecar", str(sidecar_path))
    assert rc == 3, f"Expected exit 3 (stale sidecar), got {rc}"
