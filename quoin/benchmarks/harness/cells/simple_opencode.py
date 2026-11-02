"""Cell adapter for the 'simple-opencode' benchmark cell.

Runs the pinned OpenCode binary with no Quoin scaffold; the paired baseline.
Shared behaviour lives in `harness/opencode_cells.py`.
"""

from __future__ import annotations

from pathlib import Path

from ..config import BudgetSpec
from ..opencode_cells import invoke_opencode


def invoke(task_spec: dict, workdir: Path, budget: BudgetSpec, run_id: str) -> dict:
    return invoke_opencode("simple-opencode", task_spec, workdir, budget.wall_clock_seconds, run_id)
