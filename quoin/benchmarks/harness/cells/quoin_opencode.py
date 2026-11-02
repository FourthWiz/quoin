"""Cell adapter for the 'quoin-opencode' benchmark cell.

Installs the Quoin OpenCode scaffold into the work tree, then runs the same
pinned OpenCode binary, model and endpoint as the simple cell.
Shared behaviour lives in `harness/opencode_cells.py`.
"""

from __future__ import annotations

from pathlib import Path

from ..config import BudgetSpec
from ..opencode_cells import invoke_opencode


def invoke(task_spec: dict, workdir: Path, budget: BudgetSpec, run_id: str) -> dict:
    return invoke_opencode("quoin-opencode", task_spec, workdir, budget.wall_clock_seconds, run_id)
