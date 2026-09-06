"""
simple_claude.py — Cell adapter for the 'simple-claude' benchmark cell.

Invokes Claude Code CLI in non-interactive mode inside an isolated git worktree
of the fixture repo. No Quoin workflow artifacts; pure Claude Code baseline.

Model snapshot pinning:
    The model is pinned to a dated snapshot ID at implementation time (D-11).
    At run time, update PINNED_MODEL below to the resolved dated snapshot from:
        claude --version  # or from the 'model' field in a response JSON

    Dated snapshot form example: claude-opus-4-7-20261001
    Do NOT use alias IDs (claude-opus-4-7) — these may point to different
    snapshots mid-suite, violating invariant 1.

Rate-limit handling:
    The adapter catches 429 / overloaded responses from the Claude API and
    retries with exponential backoff. Waited time does NOT count against the
    per-task wall-clock budget (the budget timer pauses during backoff sleeps).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import BudgetSpec
from ..cost import estimate_cost, load_pricing

# ---------------------------------------------------------------------------
# Pin the dated model snapshot at implementation time (D-11).
# Update this at benchmark time by running: claude --version
# or inspecting the 'model' field of a claude --print --output-format json
# response. This MUST be a dated snapshot ID, not an alias.
# ---------------------------------------------------------------------------
PINNED_MODEL: str = "claude-opus-4-7"  # use alias; pin to dated snapshot before publishing results

# Overridable via environment for testing
_MODEL_ENV_VAR = "QUOIN_BENCH_CLAUDE_MODEL"


def _get_model() -> str:
    return os.environ.get(_MODEL_ENV_VAR, PINNED_MODEL)


# Environment variable that flags a gate invocation (D-08). Read from the
# environment, deliberately NOT from a threaded kwarg — a kwarg would carry
# the same silent-omission failure mode this flag exists to guard against.
ENV_BENCHMARK_GATE = "QUOIN_BENCHMARK_GATE"


def _gate_mode() -> bool:
    return os.environ.get(ENV_BENCHMARK_GATE) == "1"


def _build_claude_argv(prompt: str, model: str, max_budget_usd: Optional[float]) -> list[str]:
    """Assemble the `claude` argv shared by both Claude cells.

    A separate, importable function so the "the assembled argv actually
    contains --max-budget-usd" refusal (D-08's CRIT-2 fix) can be tested
    against a stubbed builder that silently drops the flag — the
    anti-silent-omission proof. Both cells already spawn with `--print`,
    which `--max-budget-usd` requires.
    """
    argv = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--model", model,
    ]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    argv.append(prompt)
    return argv


def _build_prompt(task_spec: dict) -> str:
    """Build the prompt to send to Claude Code for a given task."""
    source = task_spec.get("source", "")
    source_id = task_spec.get("source_id", "")
    description = task_spec.get("description", "")

    if source == "evalplus_humaneval_plus":
        return (
            f"Solve the following HumanEval+ programming task. "
            f"Write your solution as a Python function in a file called solution.py.\n\n"
            f"Task ID: {source_id}\n"
            f"Task description: {description}\n\n"
            f"Your solution should pass all tests in the evalplus test suite for this task."
        )
    elif source == "swebench_lite":
        return (
            f"Fix the following GitHub issue from the SWE-bench Lite benchmark.\n\n"
            f"Instance ID: {source_id}\n"
            f"Description: {description}\n\n"
            f"Implement the fix in the repository. When done, your changes will be "
            f"evaluated by the SWE-bench harness."
        )
    else:
        return f"Solve task: {description} (source_id={source_id})"


def invoke(
    task_spec: dict,
    workdir: Path,
    budget: BudgetSpec,
    run_id: str,
    max_budget_usd: Optional[float] = None,
) -> dict:
    """
    Invoke Claude Code CLI in simple (no-quoin) mode.

    Parameters
    ----------
    task_spec:
        Task dict from suite-v1.json.
    workdir:
        Isolated git worktree path for this task.
    budget:
        Wall-clock and USD budget constraints.
    run_id:
        The benchmark run ID (used for logging).
    max_budget_usd:
        Optional CLI-enforced spend cap (D-08), passed through to `claude
        --max-budget-usd`. In gate mode (`QUOIN_BENCHMARK_GATE=1`), a `None`
        value or an assembled argv missing the flag refuses to spawn.

    Returns
    -------
    dict with transcript_events, prompt, diff_patch, cost data, etc.
    """
    model = _get_model()
    prompt = _build_prompt(task_spec)
    task_id = task_spec["id"]

    result: dict = {
        "prompt": prompt,
        "transcript_events": [],
        "diff_patch": "",
        "cost_available": False,
        "cost_runtime_usd": None,
        "cost_estimated_usd": None,
        "cost_delta_usd": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cache_read": None,
        "tokens_cache_write": None,
        "turn_count": 0,
        "gate_intervention_count": 0,
        "verdict": None,
        "extra": {},
    }

    cmd = _build_claude_argv(prompt, model, max_budget_usd)

    # Fail closed at the point of spend (D-08's CRIT-2 fix). A cap that is
    # merely threaded is not a bound: this refusal is evaluated on the
    # ASSEMBLED argv, not the raw parameter, so a silently-dropped flag is
    # caught too. Outside gate mode the record is advisory only.
    gate_mode = _gate_mode()
    budget_cap_armed = max_budget_usd is not None and "--max-budget-usd" in cmd
    result["extra"]["budget_cap_armed"] = budget_cap_armed
    result["extra"]["max_budget_usd_applied"] = max_budget_usd
    if gate_mode and not budget_cap_armed:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "budget-cap-unarmed"
        return result

    budget_seconds = budget.wall_clock_seconds
    wall_start = time.monotonic()
    backoff_total = 0.0  # time spent in rate-limit backoff (excluded from budget)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        events = []
        total_cost_usd: Optional[float] = None
        tokens_in = tokens_out = tokens_cache_read = tokens_cache_write = 0
        turn_count = 0
        retry_delay = 1.0

        while True:
            # Check wall-clock budget (excluding backoff time)
            elapsed = time.monotonic() - wall_start - backoff_total
            if elapsed > budget_seconds:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                result["verdict"] = "timeout"
                break

            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Handle rate-limit / overloaded events
            event_type = event.get("type", "")
            if event_type in ("error", "api_error"):
                error_msg = str(event.get("error", ""))
                if "429" in error_msg or "overloaded" in error_msg.lower():
                    # Exponential backoff; pause does not count against budget
                    backoff_start = time.monotonic()
                    time.sleep(retry_delay)
                    backoff_total += time.monotonic() - backoff_start
                    retry_delay = min(retry_delay * 2, 60.0)
                    continue

            events.append(event)

            # Extract cost and token data from stream-json events
            if event_type == "result":
                cost_val = event.get("cost_usd")
                if cost_val is not None:
                    total_cost_usd = float(cost_val)
                usage = event.get("usage", {})
                tokens_in = usage.get("input_tokens", tokens_in)
                tokens_out = usage.get("output_tokens", tokens_out)
                tokens_cache_read = usage.get("cache_read_input_tokens", tokens_cache_read)
                tokens_cache_write = usage.get("cache_creation_input_tokens", tokens_cache_write)

            if event_type == "assistant":
                turn_count += 1

        proc.wait(timeout=10)

        # Get git diff from workdir
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            result["diff_patch"] = diff_proc.stdout
        except Exception:
            result["diff_patch"] = ""

        result["transcript_events"] = events
        result["turn_count"] = turn_count
        result["tokens_in"] = tokens_in if tokens_in else None
        result["tokens_out"] = tokens_out if tokens_out else None
        result["tokens_cache_read"] = tokens_cache_read if tokens_cache_read else None
        result["tokens_cache_write"] = tokens_cache_write if tokens_cache_write else None

        # Cost handling
        if total_cost_usd is not None:
            result["cost_available"] = True
            result["cost_runtime_usd"] = total_cost_usd
            # Also compute estimated value for sanity check
            try:
                pricing = load_pricing()
                estimated = estimate_cost(
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_write=tokens_cache_write,
                    cache_read=tokens_cache_read,
                    pricing=pricing,
                )
                if estimated is not None:
                    result["cost_estimated_usd"] = float(estimated)
                    result["cost_delta_usd"] = total_cost_usd - float(estimated)
            except Exception:
                pass  # Estimation failure is non-fatal
        else:
            result["cost_available"] = False

    except FileNotFoundError:
        # Claude CLI not found — mark as error
        result["verdict"] = "error"
        result["transcript_events"] = [
            {"type": "error", "error": "claude CLI not found on PATH"}
        ]
    except Exception as exc:
        result["verdict"] = "error"
        result["transcript_events"] = [{"type": "error", "error": str(exc)}]

    return result
