"""Shared gate for the two guards that only apply while the OpenCode isolation work is open.

The guards pin properties of one feature branch (what it may change outside the OpenCode
adapter). Once that branch is merged, or on any later branch, they must stay quiet so they
never block unrelated work. ``task_guard_active`` answers whether the current checkout is
the branch the guards were written for.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

ENV_FORCE = "QUOIN_OPENCODE_TASK_GUARDS"
SKIP_REASON = "task-scoped guard: not on the task branch"


def _current_branch(root) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def task_guard_active(root, task_branch, environ=None) -> Optional[str]:
    """Return a skip reason when the guards do not apply here, or ``None`` when they do."""
    env = os.environ if environ is None else environ
    if shutil.which("git") is None:
        return SKIP_REASON
    if env.get(ENV_FORCE) == "1":
        return None
    branch = _current_branch(Path(root))
    if branch is not None and branch == task_branch:
        return None
    return SKIP_REASON
