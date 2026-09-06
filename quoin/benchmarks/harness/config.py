"""
config.py — Harness configuration types.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BudgetSpec:
    """Per-task execution budget constraints."""

    wall_clock_seconds: float = 600.0
    """Maximum wall-clock time per task in seconds. Task marked `timeout` on overrun."""

    usd_per_cell_pair: Optional[float] = None
    """Optional USD kill-switch applied per cell pair (not per task).
    When set, the orchestrator stops a cell after this cumulative spend is exceeded.
    Applied per cell-pair (simple-claude + quoin-claude together) to avoid
    systematically killing only quoin cells."""

    max_retries: int = 0
    """Zero retries: a failing task is recorded as fail, not retried (invariant 7)."""


@dataclass
class HarnessConfig:
    """Top-level harness configuration."""

    suite_path: Path = Path("quoin/benchmarks/suite-v1.json")
    """Path to the frozen task suite JSON."""

    run_dir: Path = Path(".workflow_artifacts/quoin-benchmarks/runs")
    """Root directory for all run outputs."""

    pricing_path: Path = Path("quoin/benchmarks/harness/pricing.json")
    """Path to the per-model pricing card."""

    quoin_install_script: Path = Path("quoin/install.sh")
    """Path to the quoin install.sh script (used by quoin-claude cell)."""

    expected_quoin_commit: Optional[str] = None
    """Commit SHA the arm intends to have installed. The quoin-claude cell
    asserts the worktree it actually installed from against this value and
    refuses to spawn on a mismatch (D-14)."""

    quoin_install_mode: str = "script"
    """One of "script" (default — shells `quoin_install_script`, today's
    behaviour), "skip" (the gate mode: install nothing, the driver already
    installed the arm), or "module" (`python -m quoin install`, pinned to
    `arm_root`). See D-16."""

    arm_root: Optional[Path] = None
    """Arm worktree root, consumed by `quoin_install_mode == "module"`."""

    cells: list[str] = field(
        default_factory=lambda: [
            "simple-claude",
            "quoin-claude",
            "simple-codex",
            "quoin-codex",
        ]
    )
    """Cell IDs to run. Must match REQUIRED_MODE_IDS in validate_benchmarks.py."""

    max_parallel: int = 4
    """Maximum number of concurrent task executions."""

    budget: BudgetSpec = field(default_factory=BudgetSpec)

    # Environment variable names for benchmark auto-approve mode (T-19)
    ENV_GATE_AUTO_APPROVE: str = "QUOIN_GATE_AUTO_APPROVE"
    ENV_BENCHMARK_RUN: str = "QUOIN_BENCHMARK_RUN"

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        """Create config with overrides from environment variables."""
        cfg = cls()
        if run_dir := os.environ.get("QUOIN_BENCH_RUN_DIR"):
            cfg.run_dir = Path(run_dir)
        return cfg
