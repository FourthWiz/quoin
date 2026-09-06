"""
runner.py — Core harness execution entry points.

Public API:
    run_one_task(cell, task_id, run_id, config) -> RunResult
    run_cell(cell, suite, run_id, config) -> CellResult

Each call to run_one_task:
  1. Loads the task spec from suite-v1.json
  2. Creates an isolated git worktree via isolation.py
  3. Invokes the appropriate cell adapter (cells/<cell>.py)
  4. Runs the judge on the result
  5. Writes all six output files via result_writer.py
  6. Cleans up the worktree

run_cell iterates over the suite and calls run_one_task for each task,
respecting --resume (skips tasks whose result dir is already well-formed).
"""

from __future__ import annotations

import inspect
import json
import tempfile
import time
from pathlib import Path
from typing import Optional

from .config import BudgetSpec, HarnessConfig
from .isolation import create_task_worktree, cleanup_task_worktree
from .judge import judge_task
from .result_writer import (
    CellResult,
    RunResult,
    is_result_complete,
    task_result_dir,
    write_run_result,
)

# Terminal verdicts a cell can report on its own invocation result. When the
# cell reports one of these, the judge is never called — the cell already
# knows the outcome and the judge would be scoring evidence that was never
# produced (D-03).
_CELL_TERMINAL_VERDICTS = frozenset({"error", "timeout", "budget_stopped"})

# Spend-critical config fields that, if silently dropped by inspect.signature
# threading, would leave a guardrail unarmed with no error (T-01, D-08).
# Named explicitly rather than derived from "every field whose value is not
# None", because several threadable fields (quoin_install_script,
# quoin_install_mode) default to non-None values that every cell — including
# ones that declare neither spend-critical field — must keep working with.
_SPEND_CRITICAL_FIELDS = ("max_budget_usd", "expected_quoin_commit")


class ConfigThreadError(RuntimeError):
    """Raised when a spend-critical config field cannot reach any adapter.

    `inspect.signature`-based kwarg threading (D-11) fails silently: a field
    an adapter does not declare is simply not passed. That silence is
    acceptable for optional plumbing and unacceptable for a spend cap. This
    error makes the omission loud, before any task runs.
    """


# Config fields threaded to cell adapters via D-11 signature inspection,
# beyond the four base `run_one_task` kwargs. Extended as later tasks add
# cell parameters (T-02: install mode + arm root; T-14: budget cap; T-15:
# result dir). A name not yet present on HarnessConfig (a field a later task
# adds) is simply never threaded — `hasattr` guards it — so this list can be
# filled in ahead of the field existing without breaking anything.
_THREADABLE_CONFIG_FIELDS = (
    "quoin_install_script",
    "expected_quoin_commit",
    "quoin_install_mode",
    "arm_root",
    "max_budget_usd",
)


def _threadable_kwargs(adapter, config: HarnessConfig) -> dict:
    """Build the kwargs to pass to `adapter.invoke` beyond the four base ones.

    Only passes a field when the adapter's `invoke` declares a parameter of
    that exact name (D-11) — this is what lets cells that don't need a field
    stay byte-unchanged.
    """
    params = inspect.signature(adapter.invoke).parameters
    return {
        name: getattr(config, name)
        for name in _THREADABLE_CONFIG_FIELDS
        if name in params and hasattr(config, name)
    }


def _load_cell_adapter(cell: str):
    """Dynamically import the cell adapter module."""
    import importlib

    cell_module_name = cell.replace("-", "_")
    module = importlib.import_module(
        f"quoin.benchmarks.harness.cells.{cell_module_name}"
    )
    return module


def _check_spend_critical_fields_threadable(config: HarnessConfig) -> None:
    """Raise loudly, before any task runs, if a configured spend-critical
    field cannot reach any adapter in `config.cells`.

    `inspect.signature` threading (D-11) fails silently when the target
    adapter does not declare a parameter of that name — a field simply is
    not passed, with no exception and no log. That silence is acceptable for
    optional plumbing and unacceptable for a spend cap (D-08's CRIT-2 fix).
    The check is scoped to the named field list only: a rule of "any field
    whose value is not None" also fires on non-None DEFAULTS (e.g.
    `quoin_install_script`), which every existing single-cell invocation
    carries regardless of whether that cell needs it.
    """
    for field_name in _SPEND_CRITICAL_FIELDS:
        value = getattr(config, field_name, None)
        if value is None:
            continue
        declares = False
        for cell_id in config.cells:
            try:
                adapter = _load_cell_adapter(cell_id)
            except Exception:
                continue
            if field_name in inspect.signature(adapter.invoke).parameters:
                declares = True
                break
        if not declares:
            raise ConfigThreadError(
                f"{field_name!r} is set to {value!r} but no adapter among "
                f"config.cells ({', '.join(config.cells)}) declares an "
                f"'{field_name}' parameter on invoke() — it would be "
                "silently dropped and the guardrail it configures would be "
                "unarmed."
            )


def run_one_task(
    cell: str,
    task_spec: dict,
    run_id: str,
    config: Optional[HarnessConfig] = None,
    fixture_repo: Optional[Path] = None,
) -> RunResult:
    """
    Run a single task in a single cell.

    Parameters
    ----------
    cell:
        Cell ID: 'simple-claude', 'quoin-claude', 'simple-codex', 'quoin-codex'.
    task_spec:
        Task dict from suite-v1.json (id, source, source_id, ...).
    run_id:
        Unique identifier for this benchmark run.
    config:
        Harness configuration; uses defaults if None.
    fixture_repo:
        Path to the fixture repo to use as the worktree base. Required for
        SWE-bench Lite tasks; optional for HumanEval+ tasks.

    Returns
    -------
    RunResult with all fields populated.
    """
    if config is None:
        config = HarnessConfig()

    task_id = task_spec["id"]
    run_dir = config.run_dir

    # Check if already complete (--resume mode)
    t_dir = task_result_dir(run_dir, run_id, cell, task_id)
    if is_result_complete(t_dir):
        # Load existing result and return it as a RunResult stub
        judge_data = json.loads((t_dir / "judge.json").read_text(encoding="utf-8"))
        metrics_data = json.loads((t_dir / "metrics.json").read_text(encoding="utf-8"))
        return RunResult(
            cell=cell,
            task_id=task_id,
            run_id=run_id,
            verdict=judge_data.get("verdict", "unknown"),
            wall_clock_seconds=metrics_data.get("wall_clock_seconds", 0.0),
        )

    adapter = _load_cell_adapter(cell)
    budget = config.budget

    worktree_path: Optional[Path] = None
    _tmpdir_obj = None  # holds the TemporaryDirectory for HumanEval+ tasks
    try:
        # Create isolated worktree for the task.
        # SWE-bench tasks: git worktree of fixture_repo.
        # HumanEval+ (no fixture_repo): fresh temp directory so the agent
        # writes solution.py there instead of the repo root.
        if fixture_repo and fixture_repo.exists():
            worktree_path = create_task_worktree(
                fixture_repo, run_id, cell, task_id
            )
        else:
            _tmpdir_obj = tempfile.TemporaryDirectory(prefix=f"qbench_{task_id}_")
            worktree_path = Path(_tmpdir_obj.name)

        # Invoke the cell adapter, threading spend-critical and plumbing
        # config fields to whichever adapters declare them (D-11).
        start = time.monotonic()
        extra_kwargs = _threadable_kwargs(adapter, config)
        invocation_result = adapter.invoke(
            task_spec=task_spec,
            workdir=worktree_path or Path("."),
            budget=BudgetSpec(
                wall_clock_seconds=budget.wall_clock_seconds,
                max_retries=budget.max_retries,
            ),
            run_id=run_id,
            **extra_kwargs,
        )
        elapsed = time.monotonic() - start
        # Record what actually reached the cell — the evidence of what the
        # thread carried, not what was intended (round-3 addition, CRIT-2).
        invocation_extra = dict(invocation_result.get("extra", {}))
        invocation_extra["threaded_kwargs"] = sorted(extra_kwargs.keys())

        # A cell-reported terminal verdict (error / timeout / budget_stopped)
        # short-circuits the judge: the cell already knows the outcome, and
        # calling the judge would score evidence a failed invocation never
        # produced (D-03).
        if invocation_result.get("verdict") in _CELL_TERMINAL_VERDICTS:
            result = RunResult(
                cell=cell,
                task_id=task_id,
                run_id=run_id,
                transcript_events=invocation_result.get("transcript_events", []),
                verdict=invocation_result["verdict"],
                evidence_path=invocation_extra.get("failure_reason"),
                judge_runtime_seconds=0.0,
                wall_clock_seconds=elapsed,
                tokens_in=invocation_result.get("tokens_in"),
                tokens_out=invocation_result.get("tokens_out"),
                tokens_cache_read=invocation_result.get("tokens_cache_read"),
                tokens_cache_write=invocation_result.get("tokens_cache_write"),
                gate_intervention_count=invocation_result.get("gate_intervention_count", 0),
                turn_count=invocation_result.get("turn_count", 0),
                cost_runtime_usd=invocation_result.get("cost_runtime_usd"),
                cost_estimated_usd=invocation_result.get("cost_estimated_usd"),
                cost_delta_usd=invocation_result.get("cost_delta_usd"),
                cost_available=invocation_result.get("cost_available", False),
                prompt=invocation_result.get("prompt", ""),
                diff_patch=invocation_result.get("diff_patch", ""),
                extra=invocation_extra,
            )
            write_run_result(result, run_dir)
            return result

        # Run judge
        judge_result = judge_task(
            task_id=task_id,
            task_dir=worktree_path or Path("."),
            run_id=run_id,
            invocation_extra=invocation_extra,
        )

        # Build RunResult
        result = RunResult(
            cell=cell,
            task_id=task_id,
            run_id=run_id,
            transcript_events=invocation_result.get("transcript_events", []),
            verdict=judge_result.verdict,
            evidence_path=judge_result.evidence_path,
            judge_runtime_seconds=judge_result.judge_runtime_seconds,
            wall_clock_seconds=elapsed,
            tokens_in=invocation_result.get("tokens_in"),
            tokens_out=invocation_result.get("tokens_out"),
            tokens_cache_read=invocation_result.get("tokens_cache_read"),
            tokens_cache_write=invocation_result.get("tokens_cache_write"),
            gate_intervention_count=invocation_result.get("gate_intervention_count", 0),
            turn_count=invocation_result.get("turn_count", 0),
            cost_runtime_usd=invocation_result.get("cost_runtime_usd"),
            cost_estimated_usd=invocation_result.get("cost_estimated_usd"),
            cost_delta_usd=invocation_result.get("cost_delta_usd"),
            cost_available=invocation_result.get("cost_available", False),
            prompt=invocation_result.get("prompt", ""),
            diff_patch=invocation_result.get("diff_patch", ""),
            extra=invocation_extra,
        )

    except Exception as exc:
        result = RunResult(
            cell=cell,
            task_id=task_id,
            run_id=run_id,
            verdict="error",
            evidence_path=str(exc),
        )
    finally:
        if _tmpdir_obj is not None:
            _tmpdir_obj.cleanup()
        elif worktree_path:
            cleanup_task_worktree(worktree_path)

    # Write all six output files
    write_run_result(result, run_dir)
    return result


def run_cell(
    cell: str,
    suite: list[dict],
    run_id: str,
    config: Optional[HarnessConfig] = None,
    fixture_repo: Optional[Path] = None,
    resume: bool = False,
) -> CellResult:
    """
    Run all tasks in the suite for a given cell.

    Parameters
    ----------
    cell:
        Cell ID.
    suite:
        List of task specs from suite-v1.json.
    run_id:
        Unique identifier for this benchmark run.
    config:
        Harness configuration.
    fixture_repo:
        Path to fixture repo (required for SWE-bench Lite).
    resume:
        If True, skip tasks whose result dir is already well-formed.

    Returns
    -------
    CellResult with all task results.
    """
    if config is None:
        config = HarnessConfig()

    _check_spend_critical_fields_threadable(config)

    cell_result = CellResult(cell=cell, run_id=run_id)

    for task_spec in suite:
        task_id = task_spec["id"]

        if resume:
            t_dir = task_result_dir(config.run_dir, run_id, cell, task_id)
            if is_result_complete(t_dir):
                # Load stub result
                judge_data = json.loads(
                    (t_dir / "judge.json").read_text(encoding="utf-8")
                )
                stub = RunResult(
                    cell=cell,
                    task_id=task_id,
                    run_id=run_id,
                    verdict=judge_data.get("verdict", "unknown"),
                )
                cell_result.task_results.append(stub)
                continue

        result = run_one_task(
            cell=cell,
            task_spec=task_spec,
            run_id=run_id,
            config=config,
            fixture_repo=fixture_repo,
        )
        cell_result.task_results.append(result)

    return cell_result
