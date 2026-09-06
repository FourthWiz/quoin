"""
result_writer.py — Write per-task result files to the run output directory.

Each task result directory has the structure:
    <run_dir>/<run_id>/<cell>/<task_id>/
        prompt.txt        — the prompt sent to the agent
        transcript.jsonl  — the full agent session transcript (one JSON obj per line)
        diff.patch        — git diff of the worktree after agent execution
        judge.json        — verdict from judge.py: {verdict, task_id, source_benchmark, ...}
        metrics.json      — timing and secondary metrics
        cost.json         — cost data: runtime value and/or estimated value
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class RunResult:
    """Result for a single task in a single cell."""

    cell: str
    task_id: str
    run_id: str

    # Written to transcript.jsonl
    transcript_events: list[dict] = field(default_factory=list)

    # Written to judge.json (populated by judge.py)
    verdict: str = "unknown"  # pass | fail | error | timeout
    evidence_path: Optional[str] = None
    judge_runtime_seconds: float = 0.0

    # Written to metrics.json
    wall_clock_seconds: float = 0.0
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    tokens_cache_read: Optional[int] = None
    tokens_cache_write: Optional[int] = None
    gate_intervention_count: int = 0
    turn_count: int = 0

    # Written to cost.json
    cost_runtime_usd: Optional[float] = None
    cost_estimated_usd: Optional[float] = None
    cost_delta_usd: Optional[float] = None
    cost_available: bool = False

    # Written to prompt.txt
    prompt: str = ""

    # Written to diff.patch
    diff_patch: str = ""

    # Extra metadata
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CellResult:
    """Aggregated result for a cell across all tasks."""

    cell: str
    run_id: str
    task_results: list[RunResult] = field(default_factory=list)
    budget_stopped: bool = False
    """True when the between-task SpendTracker cap (T-03, D-08's secondary
    bound) stopped this cell before every suite task ran."""


def task_result_dir(run_dir: Path, run_id: str, cell: str, task_id: str) -> Path:
    """Return the canonical per-task result directory path."""
    return run_dir / run_id / cell / task_id


def write_run_result(result: RunResult, run_dir: Path) -> Path:
    """
    Write all result files for a single task to disk.

    Returns the task result directory path.
    """
    out_dir = task_result_dir(run_dir, result.run_id, result.cell, result.task_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # prompt.txt
    (out_dir / "prompt.txt").write_text(result.prompt, encoding="utf-8")

    # transcript.jsonl
    transcript_lines = [
        json.dumps(event, ensure_ascii=False) for event in result.transcript_events
    ]
    (out_dir / "transcript.jsonl").write_text(
        "\n".join(transcript_lines) + ("\n" if transcript_lines else ""),
        encoding="utf-8",
    )

    # diff.patch
    (out_dir / "diff.patch").write_text(result.diff_patch, encoding="utf-8")

    # judge.json
    judge_data = {
        "task_id": result.task_id,
        "source_benchmark": _infer_source(result.task_id),
        "verdict": result.verdict,
        "evidence_path": result.evidence_path,
        "judge_runtime_seconds": result.judge_runtime_seconds,
    }
    (out_dir / "judge.json").write_text(
        json.dumps(judge_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # metrics.json
    metrics_data: dict[str, Any] = {
        "task_id": result.task_id,
        "cell": result.cell,
        "wall_clock_seconds": result.wall_clock_seconds,
        "turn_count": result.turn_count,
        "gate_intervention_count": result.gate_intervention_count,
    }
    if result.tokens_in is not None:
        metrics_data["tokens_in"] = result.tokens_in
    if result.tokens_out is not None:
        metrics_data["tokens_out"] = result.tokens_out
    if result.tokens_cache_read is not None:
        metrics_data["tokens_cache_read"] = result.tokens_cache_read
    if result.tokens_cache_write is not None:
        metrics_data["tokens_cache_write"] = result.tokens_cache_write
    metrics_data.update(result.extra)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # cost.json
    if result.cost_available and result.cost_runtime_usd is not None:
        cost_data: dict[str, Any] = {
            "cost_available": True,
            "cost_runtime_usd": result.cost_runtime_usd,
        }
        if result.cost_estimated_usd is not None:
            cost_data["cost_estimated_usd"] = result.cost_estimated_usd
            cost_data["cost_delta_usd"] = result.cost_delta_usd
    else:
        cost_data = {
            "cost_available": False,
            "cost": "not_available",
            "reason": "No runtime cost telemetry; see quoin/adapters/codex/cost.md",
        }
    (out_dir / "cost.json").write_text(
        json.dumps(cost_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return out_dir


def is_result_complete(task_dir: Path) -> bool:
    """
    Return True if a task result directory is well-formed and verdict-present.
    Used by --resume to skip already-completed tasks.
    """
    required = ["prompt.txt", "transcript.jsonl", "diff.patch", "judge.json",
                "metrics.json", "cost.json"]
    for fname in required:
        if not (task_dir / fname).exists():
            return False
    try:
        judge = json.loads((task_dir / "judge.json").read_text(encoding="utf-8"))
        verdict = judge.get("verdict", "")
        if verdict not in ("pass", "fail", "error", "timeout"):
            return False
    except (json.JSONDecodeError, OSError):
        return False
    return True


def _infer_source(task_id: str) -> str:
    if task_id.startswith("humaneval"):
        return "evalplus_humaneval_plus"
    if task_id.startswith("swebench"):
        return "swebench_lite"
    return "unknown"
