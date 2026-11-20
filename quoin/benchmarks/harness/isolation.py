"""
isolation.py — Contamination isolation between benchmark runs.

Per the isolation strategy (D-08, invariants 13 and 14):

  1. Each cell run for each task uses a fresh git worktree of the fixture repo
     created via `git worktree add` from a pinned commit.
  2. Each quoin cell uses a fresh .workflow_artifacts/ initialized from empty
     PER TASK (not per cell). No carried-over lessons-learned.md, no sessions/,
     no cache.
  3. Each cell run uses a fresh Docker container for the judge step.

The installed ~/.claude/skills/ toolkit IS shared across all quoin-claude runs
(it is the system under test). Only per-project memory is empty.

Residual contamination surface (explicitly accepted, documented in methodology.md):
  - Model training data
  - Toolkit code itself (quoin skills in ~/.claude/skills/)
  - Agent system prompt evolution between training and benchmark time
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def create_task_worktree(
    fixture_repo: Path,
    run_id: str,
    cell: str,
    task_id: str,
    pinned_commit: Optional[str] = None,
) -> Path:
    """
    Create an isolated git worktree for a single task.

    Each task gets a fresh, independent worktree of the fixture repo.
    The worktree is created in a temp directory that the caller is responsible
    for cleaning up via cleanup_task_worktree().

    Parameters
    ----------
    fixture_repo:
        Path to the fixture repository (must be a git repo).
    run_id:
        The benchmark run ID.
    cell:
        The cell name (e.g., 'simple-claude').
    task_id:
        The task identifier (e.g., 'humaneval_plus_000').
    pinned_commit:
        If specified, create the worktree at this commit SHA. If None,
        uses HEAD.

    Returns
    -------
    Path to the newly created worktree directory.
    """
    worktree_name = f"quoin-bench-{run_id}-{cell}-{task_id}"
    worktree_parent = Path(tempfile.gettempdir()) / "quoin-benchmarks"
    worktree_parent.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_parent / worktree_name

    # Remove existing worktree if present (from a previous failed run)
    if worktree_path.exists():
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(fixture_repo),
                capture_output=True,
                timeout=30,
            )
        except Exception:
            shutil.rmtree(worktree_path, ignore_errors=True)

    cmd = ["git", "worktree", "add"]
    if pinned_commit:
        cmd += [str(worktree_path), pinned_commit]
    else:
        # Use a detached HEAD at current HEAD to avoid branch conflicts
        head_sha = _get_head_sha(fixture_repo)
        cmd += [str(worktree_path)]
        if head_sha:
            cmd += [head_sha]

    result = subprocess.run(
        cmd,
        cwd=str(fixture_repo),
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        # Fall back to a simple directory copy if git worktree fails
        # (e.g., if the fixture is not a git repo)
        if fixture_repo.exists():
            shutil.copytree(fixture_repo, worktree_path, symlinks=True)
        else:
            worktree_path.mkdir(parents=True)

    return worktree_path


def cleanup_task_worktree(worktree_path: Path, fixture_repo: Optional[Path] = None) -> None:
    """
    Clean up a git worktree after task completion.

    Parameters
    ----------
    worktree_path:
        Path to the worktree directory to remove.
    fixture_repo:
        Path to the fixture repo (used to deregister the worktree via
        `git worktree remove`). If None, falls back to rmtree.
    """
    if fixture_repo and fixture_repo.exists():
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(fixture_repo),
                capture_output=True,
                timeout=30,
            )
            return
        except Exception:
            pass

    # Fall back to direct removal
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def ensure_clean_workflow_artifacts(task_dir: Path) -> None:
    """
    Ensure .workflow_artifacts/ in task_dir is empty (per-task isolation).

    Removes any existing .workflow_artifacts/ directory and creates a fresh
    empty one. This enforces invariants 13 and 14 (no cross-task contamination,
    no lessons-learned.md carryover).

    Called by quoin cell adapters before each task invocation.
    """
    artifacts_dir = task_dir / ".workflow_artifacts"
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "memory").mkdir(parents=True)


def verify_isolation(task_dir: Path) -> dict:
    """
    Verify isolation preconditions before running a task.

    Returns a dict with:
      {
        "lessons_learned_absent": bool,
        "sessions_absent": bool,
        "cache_absent": bool,
        "overall_ok": bool,
      }
    """
    artifacts_dir = task_dir / ".workflow_artifacts"
    result = {
        "lessons_learned_absent": True,
        "sessions_absent": True,
        "cache_absent": True,
        "overall_ok": True,
    }

    if not artifacts_dir.exists():
        return result  # No artifacts dir at all — isolation is fine

    lessons_path = artifacts_dir / "memory" / "lessons-learned.md"
    if lessons_path.exists() and lessons_path.stat().st_size > 0:
        result["lessons_learned_absent"] = False
        result["overall_ok"] = False

    sessions_path = artifacts_dir / "memory" / "sessions"
    if sessions_path.exists() and any(sessions_path.iterdir()):
        result["sessions_absent"] = False
        result["overall_ok"] = False

    cache_path = artifacts_dir / "cache"
    if cache_path.exists() and any(cache_path.iterdir()):
        result["cache_absent"] = False
        result["overall_ok"] = False

    return result


def _get_head_sha(repo: Path) -> Optional[str]:
    """Return the HEAD commit SHA for a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None
