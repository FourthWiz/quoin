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

import dataclasses
import functools
import contextlib
import inspect
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import arm_identity as _arm_identity
from .config import BudgetSpec, HarnessConfig
from .isolation import create_task_worktree, cleanup_task_worktree
from .judge import judge_task
from .repo_prep import RepoPrepError, capture_patch, cleanup_task_repo, prepare_task_repo
from .result_writer import (
    CellResult,
    RunResult,
    is_result_complete,
    task_result_dir,
    write_judge_json,
    write_run_result,
)
from .session_state import (
    classify_session,
    is_judge_pending,
    iter_transcript_events,
    write_session_json,
)
from .verified_prompt import VERIFIED_PROMPT_TEMPLATE_VERSION

# Terminal verdicts a cell can report on its own invocation result. When the
# cell reports one of these, the judge is never called — the cell already
# knows the outcome and the judge would be scoring evidence that was never
# produced (D-03).
_CELL_TERMINAL_VERDICTS = frozenset({"error", "timeout", "budget_stopped"})

_ENV_BENCHMARK_GATE = "QUOIN_BENCHMARK_GATE"


def _assert_no_remote_in_gate_mode(worktree_path: Path) -> None:
    """Re-assert the fixture repo's remote is stripped, on the actual
    per-task worktree, in gate mode only.

    `git worktree add` shares its parent repo's `.git/config` (remotes
    included), so this should always agree with whatever the fixture repo
    itself reports — but that agreement is exactly what a re-clone or an
    operator skipping the manual `git remote remove origin` step (T-08a)
    would break, silently. The quoin arms run `/run --autonomous`, the one
    mode allowed to invoke `/end_of_task` and push a branch, so a leftover
    `origin` here is not a theoretical containment gap. Outside gate mode
    this is a no-op — a fixture repo legitimately keeping its remote for
    ordinary (non-gate) benchmark runs is not this check's business.
    """
    if os.environ.get(_ENV_BENCHMARK_GATE) != "1":
        return
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "remote"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(
            f"GATE-STOP: could not check fixture worktree {worktree_path} for a "
            f"configured remote: {exc}"
        ) from exc
    if result.returncode == 0 and result.stdout.strip():
        raise RuntimeError(
            f"GATE-STOP: fixture worktree {worktree_path} still has a remote "
            f"configured ({result.stdout.strip()!r}); the fixture repo's own "
            "`origin` must be removed before it is used in gate mode"
        )

# Spend-critical config fields that, if silently dropped by inspect.signature
# threading, would leave a guardrail unarmed with no error (T-01, D-08).
# Named explicitly rather than derived from "every field whose value is not
# None", because several threadable fields (quoin_install_script,
# quoin_install_mode) default to non-None values that every cell — including
# ones that declare neither spend-critical field — must keep working with.
_SPEND_CRITICAL_FIELDS = (
    "max_budget_usd",
    "expected_quoin_commit",
    "session_env",
    "arm_identity",
)


class SpendTracker:
    """A between-task cumulative-spend cap for a single `run_cell` call.

    This is a SECONDARY bound (D-08): it is only evaluated after a task
    returns, so it cannot stop anything within a one-task suite — the
    intra-task bound is the CLI's own `--max-budget-usd` (T-14). For a
    multi-task suite this stops a cell once its running total exceeds
    `cap_usd`, so a lengthy run does not silently run away between the
    per-task caps.
    """

    def __init__(self, cap_usd: Optional[float] = None) -> None:
        self.cap_usd = cap_usd
        self.total_usd: float = 0.0

    def add(self, amount: float) -> bool:
        """Record `amount` spent; return True if the cap is now exceeded."""
        self.total_usd += amount
        return self.cap_usd is not None and self.total_usd > self.cap_usd


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
    "session_env",
    "arm_identity",
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
    if task_spec.get("source") == "swebench_verified":
        return _run_one_verified_task(cell, task_spec, run_id, config, adapter, t_dir)
    budget = config.budget

    worktree_path: Optional[Path] = None
    _tmpdir_obj = None  # holds the TemporaryDirectory for HumanEval+ tasks
    # Pre-declared so the generic exception handler below can carry them
    # through even when the exception fires before `adapter.invoke()`
    # returns (round-4 fix: MAJOR 4) — most importantly
    # `transcript_streamed`, which the cell now sets at file-open time
    # specifically so a crash anywhere after that point (a judge crash,
    # `cleanup_task_worktree`, the `RunResult` construction itself) does
    # not make `write_run_result` re-open and truncate an already-streamed
    # transcript.jsonl to empty.
    invocation_extra: dict = {}
    invocation_transcript_events: list = []
    try:
        # Create isolated worktree for the task.
        # SWE-bench tasks: git worktree of fixture_repo.
        # HumanEval+ (no fixture_repo): fresh temp directory so the agent
        # writes solution.py there instead of the repo root.
        if fixture_repo and fixture_repo.exists():
            worktree_path = create_task_worktree(
                fixture_repo, run_id, cell, task_id
            )
            _assert_no_remote_in_gate_mode(worktree_path)
        else:
            _tmpdir_obj = tempfile.TemporaryDirectory(prefix=f"qbench_{task_id}_")
            worktree_path = Path(_tmpdir_obj.name)

        # Invoke the cell adapter, threading spend-critical and plumbing
        # config fields to whichever adapters declare them (D-11).
        start = time.monotonic()
        extra_kwargs = _threadable_kwargs(adapter, config)
        # `run_output_dir` (T-15) isn't a config value — it's this task's OWN
        # result directory, computed above as `t_dir` — but it's threaded by
        # the same declared-parameter mechanism, not a cell-name branch.
        if "run_output_dir" in inspect.signature(adapter.invoke).parameters:
            extra_kwargs["run_output_dir"] = t_dir
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
        invocation_transcript_events = invocation_result.get("transcript_events", [])

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
        # `extra` (carrying `transcript_streamed`) and any transcript
        # events collected before the exception are threaded through here,
        # not defaulted to `{}`/`[]` (round-4 fix: MAJOR 4) — a default
        # here is indistinguishable from "nothing was ever streamed" to
        # `write_run_result`'s own re-open guard, which then clobbers a
        # real, paid, already-on-disk transcript.jsonl with an empty one.
        result = RunResult(
            cell=cell,
            task_id=task_id,
            run_id=run_id,
            verdict="error",
            evidence_path=str(exc),
            transcript_events=invocation_transcript_events,
            extra=invocation_extra,
        )
    finally:
        if _tmpdir_obj is not None:
            _tmpdir_obj.cleanup()
        elif worktree_path:
            cleanup_task_worktree(worktree_path)

    # Write all six output files
    write_run_result(result, run_dir)
    return result


@functools.lru_cache(maxsize=None)
def _suite_dataset_revision(suite_path) -> Optional[str]:
    try:
        data = json.loads(Path(suite_path).read_text(encoding="utf-8"))
        value = data["tiered"]["dataset_revision"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _verified_result(cell, task_id, run_id, **fields) -> RunResult:
    return RunResult(
        cell=cell, task_id=task_id, run_id=run_id,
        source_benchmark="swebench_verified", **fields,
    )


def _run_one_verified_task(cell, task_spec, run_id, config, adapter, t_dir) -> RunResult:
    """Run one Verified task, holding `config.row_write_hold` (when set) from
    just before session.json is written until the final result files exist."""
    stack = contextlib.ExitStack()
    try:
        return _run_one_verified_body(cell, task_spec, run_id, config, adapter, t_dir, stack)
    finally:
        stack.close()


def _overshoot(cost, cap) -> Optional[float]:
    numeric = (int, float)
    if isinstance(cost, bool) or isinstance(cap, bool):
        return None
    if not isinstance(cost, numeric) or not isinstance(cap, numeric):
        return None
    if cost != cost or cap != cap or cost in (float("inf"), float("-inf")):
        return None
    return max(0.0, float(cost) - float(cap))


def _run_one_verified_body(cell, task_spec, run_id, config, adapter, t_dir, stack) -> RunResult:
    task_id = task_spec["id"]
    run_dir = config.run_dir
    budget = config.budget

    try:
        prepared = prepare_task_repo(task_spec, config.repo_cache_root, config.repo_work_root)
    except Exception as e:
        reason = e.reason if isinstance(e, RepoPrepError) else "unexpected-" + type(e).__name__
        result = _verified_result(
            cell, task_id, run_id,
            verdict="error",
            evidence_path=f"repo-prep: {reason}",
            transcript_events=[{
                "type": "harness_note",
                "note": "no agent session was started",
                "reason": reason,
            }],
            extra={"failure_reason": "repo-prep-" + reason, "pre_spawn_error": True},
            judge_extra={"judge_final": True, "judged": False},
        )
        write_run_result(result, run_dir)
        return result

    identity = config.arm_identity
    if identity is not None:
        problems = _arm_identity.check_workdir(prepared.path)
        if problems:
            reason = (
                _arm_identity.PLAIN_REASON
                if getattr(identity, "mode", None) == _arm_identity.QUOIN_FREE
                else "quoin-arm-contaminated"
            )
            cleanup_task_repo(prepared.path)
            result = _verified_result(
                cell, task_id, run_id,
                verdict="error",
                evidence_path=f"arm identity: {reason}",
                transcript_events=[{
                    "type": "harness_note",
                    "note": "no agent session was started",
                    "reason": reason,
                }],
                extra={
                    "failure_reason": reason,
                    "pre_spawn_error": True,
                    "identity_problems": list(problems),
                },
                judge_extra={"judge_final": True, "judged": False},
            )
            write_run_result(result, run_dir)
            return result

    cleaned = False
    patch = None
    session_written = False
    extra: dict = {}
    events_so_far: list = []
    inv: dict = {}
    elapsed = 0.0
    start = None
    try:
        start = time.monotonic()
        extra_kwargs = _threadable_kwargs(adapter, config)
        if "run_output_dir" in inspect.signature(adapter.invoke).parameters:
            extra_kwargs["run_output_dir"] = t_dir
        inv = adapter.invoke(
            task_spec=task_spec,
            workdir=prepared.path,
            budget=BudgetSpec(
                wall_clock_seconds=budget.wall_clock_seconds,
                max_retries=budget.max_retries,
            ),
            run_id=run_id,
            **extra_kwargs,
        )
        elapsed = time.monotonic() - start
        extra = dict(inv.get("extra", {}))
        extra["threaded_kwargs"] = sorted(extra_kwargs.keys())
        events_so_far = inv.get("transcript_events", [])

        capture_error = None
        try:
            patch = capture_patch(prepared, task_spec["base_commit"])
        except RepoPrepError as e:
            capture_error = e.reason
            extra["capture_error"] = e.inner_reason or e.reason
            if e.detail:
                extra["capture_error_detail"] = e.detail[:300]
        patch_bytes = patch.patch_bytes if patch else b""
        cleanup_note = cleanup_task_repo(prepared.path)
        cleaned = True
        if cleanup_note is not None:
            extra["clone_cleanup_failed"] = cleanup_note

        stats = {"skipped_lines": 0}
        if extra.get("transcript_streamed"):
            events = iter_transcript_events(t_dir / "transcript.jsonl", stats)
            outcome = classify_session(inv.get("verdict"), extra, events)
            outcome.skipped_lines += stats["skipped_lines"]
        else:
            outcome = classify_session(inv.get("verdict"), extra, events_so_far)

        not_judged_reason = None
        if outcome.judge_required and capture_error:
            not_judged_reason = "patch-capture-failed"
        elif outcome.judge_required and patch is not None and patch.decode_lossy:
            not_judged_reason = "patch-not-utf8"
        if not_judged_reason:
            outcome = dataclasses.replace(
                outcome, terminal_state="error", verdict_if_terminal="error",
                failure_reason=not_judged_reason, judge_required=False,
            )

        (t_dir).mkdir(parents=True, exist_ok=True)
        (t_dir / "judge.json").unlink(missing_ok=True)
        (t_dir / "diff.patch").write_bytes(patch_bytes)
        if config.row_write_hold is not None:
            stack.enter_context(config.row_write_hold())
        write_session_json(t_dir, {
            "task_id": task_id,
            "cell": cell,
            "run_id": run_id,
            "source": "swebench_verified",
            "source_id": task_spec.get("source_id"),
            "base_commit": task_spec.get("base_commit"),
            "tree_sha": prepared.tree_sha,
            "prep_refs": prepared.refs,
            "head_commit_after": patch.head_commit if patch else "",
            **outcome.to_dict(),
            "patch_sha256": patch.sha256 if patch else None,
            "patch_bytes": len(patch_bytes),
            "patch_excluded_paths": patch.excluded_paths if patch else [],
            "patch_decode_lossy": bool(patch and patch.decode_lossy),
            "repo_config_modified": bool(patch and patch.repo_config_modified),
            "repo_info_modified": bool(patch and patch.repo_info_modified),
            "capture_rejected_names": patch.rejected_names if patch else 0,
            "capture_oversize_files": list(patch.oversize_files) if patch else [],
            "capture_skipped_heavy_dirs": patch.skipped_heavy_dirs if patch else 0,
            "capture_attr_source_supported": patch.attr_source_supported if patch else None,
            "clone_cleanup_failed": cleanup_note,
            "cost_runtime_usd": inv.get("cost_runtime_usd"),
            "wall_clock_seconds": elapsed,
            "prompt_template_version": VERIFIED_PROMPT_TEMPLATE_VERSION,
        })
        session_written = True

        extra.update({
            "tree_sha": prepared.tree_sha,
            "prep_refs": prepared.refs,
            "head_commit_after": patch.head_commit if patch else "",
            "terminal_state": outcome.terminal_state,
            "session_errored": outcome.session_errored,
            "patch_excluded_paths": patch.excluded_paths if patch else [],
            "patch_sha256": patch.sha256 if patch else None,
            "genuine_assistant_turns": outcome.genuine_assistant_turns,
            "api_key_source": outcome.api_key_source,
            "result_subtype": outcome.result_subtype,
            "skipped_transcript_lines": outcome.skipped_lines,
        })
        if outcome.failure_reason is not None:
            extra["failure_reason"] = outcome.failure_reason
        extra["cap_usd"] = config.max_budget_usd
        extra["overshoot_usd"] = _overshoot(inv.get("cost_runtime_usd"), config.max_budget_usd)

        if outcome.judge_required and config.defer_judge:
            verdict, evidence, judge_seconds = "error", "judge-deferred", 0.0
            judge_extra = {
                "judge_final": False,
                "judged": False,
                "failure_reason": "judge-deferred",
            }
        elif outcome.judge_required:
            judged = judge_task(
                task_id, t_dir, run_id,
                evidence_dir=t_dir,
                invocation_extra=extra,
                task_spec=task_spec,
                judge_context={
                    "cell": cell,
                    "snapshot_revision": _suite_dataset_revision(str(config.suite_path)),
                },
            )
            verdict, evidence, judge_seconds = judged.verdict, judged.evidence_path, judged.judge_runtime_seconds
            judge_extra = dict(judged.extra)
        else:
            verdict, evidence, judge_seconds = outcome.verdict_if_terminal, outcome.failure_reason, 0.0
            judge_extra = {"judge_final": True, "judged": False}

        result = _verified_result(
            cell, task_id, run_id,
            transcript_events=events_so_far,
            verdict=verdict,
            evidence_path=evidence,
            judge_runtime_seconds=judge_seconds,
            wall_clock_seconds=elapsed,
            tokens_in=inv.get("tokens_in"),
            tokens_out=inv.get("tokens_out"),
            tokens_cache_read=inv.get("tokens_cache_read"),
            tokens_cache_write=inv.get("tokens_cache_write"),
            gate_intervention_count=inv.get("gate_intervention_count", 0),
            turn_count=inv.get("turn_count", 0),
            cost_runtime_usd=inv.get("cost_runtime_usd"),
            cost_estimated_usd=inv.get("cost_estimated_usd"),
            cost_delta_usd=inv.get("cost_delta_usd"),
            cost_available=inv.get("cost_available", False),
            prompt=inv.get("prompt", ""),
            extra=extra,
            judge_extra=judge_extra,
            diff_patch_bytes=patch_bytes,
        )
    except Exception as exc:
        if not elapsed and start is not None:
            elapsed = time.monotonic() - start
        if not extra.get("transcript_streamed") and not events_so_far:
            streamed = t_dir / "transcript.jsonl"
            try:
                if streamed.is_file() and streamed.stat().st_size > 0:
                    extra["transcript_streamed"] = True
            except OSError:
                pass
        extra["failure_reason"] = "harness-exception-" + type(exc).__name__
        result = _verified_result(
            cell, task_id, run_id,
            verdict="error",
            evidence_path=str(exc),
            transcript_events=events_so_far,
            wall_clock_seconds=elapsed,
            tokens_in=inv.get("tokens_in"),
            tokens_out=inv.get("tokens_out"),
            tokens_cache_read=inv.get("tokens_cache_read"),
            tokens_cache_write=inv.get("tokens_cache_write"),
            gate_intervention_count=inv.get("gate_intervention_count", 0),
            turn_count=inv.get("turn_count", 0),
            cost_runtime_usd=inv.get("cost_runtime_usd"),
            cost_estimated_usd=inv.get("cost_estimated_usd"),
            cost_delta_usd=inv.get("cost_delta_usd"),
            cost_available=inv.get("cost_available", False),
            prompt=inv.get("prompt", ""),
            extra=extra,
            judge_extra=(
                {"judge_final": False, "failure_reason": "judge-exception"}
                if session_written else {}
            ),
            diff_patch_bytes=patch.patch_bytes if patch else b"",
        )
    finally:
        if not cleaned:
            cleanup_task_repo(prepared.path)

    write_run_result(result, run_dir)
    stack.close()
    return result


def judge_pending_rows(
    run_dir,
    run_id: str,
    suite_tasks: list,
    *,
    snapshot_revision: Optional[str] = None,
    judge=None,
) -> list:
    """Re-judge Verified rows whose session was recorded but whose verdict is not final."""
    judge = judge if judge is not None else judge_task
    base = Path(run_dir) / run_id
    rows = []
    if not base.is_dir():
        return rows
    for cell_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for spec in suite_tasks:
            if spec.get("source") != "swebench_verified":
                continue
            t_dir = cell_dir / spec["id"]
            if not t_dir.is_dir() or not is_judge_pending(t_dir):
                continue
            try:
                judged = judge(
                    spec["id"], t_dir, run_id,
                    evidence_dir=t_dir,
                    task_spec=spec,
                    judge_context={"cell": cell_dir.name, "snapshot_revision": snapshot_revision},
                )
            except Exception as exc:
                rows.append({"cell": cell_dir.name, "task_id": spec["id"], "verdict": "error",
                             "judge_final": False, "error": type(exc).__name__})
                continue
            write_judge_json(
                t_dir, spec["id"], "swebench_verified", judged.verdict,
                judged.evidence_path, judged.judge_runtime_seconds, judged.extra,
            )
            rows.append({
                "cell": cell_dir.name, "task_id": spec["id"], "verdict": judged.verdict,
                "judge_final": bool(judged.extra.get("judge_final")),
            })
    return rows


def run_cell(
    cell: str,
    suite: list[dict],
    run_id: str,
    config: Optional[HarnessConfig] = None,
    fixture_repo: Optional[Path] = None,
    resume: bool = False,
    tracker: Optional[SpendTracker] = None,
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
    tracker:
        Optional `SpendTracker` enforcing a BETWEEN-task cumulative USD cap
        (T-03, D-08's secondary bound — the intra-task bound is the CLI's
        own `--max-budget-usd`, T-14). With `tracker is None` or a `None`
        `cap_usd`, this loop is byte-equivalent to today: every suite task
        runs regardless of cost.

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

        if tracker is not None:
            cap_exceeded = tracker.add(result.cost_runtime_usd or 0.0)
            if cap_exceeded:
                cell_result.budget_stopped = True
                break

    return cell_result
