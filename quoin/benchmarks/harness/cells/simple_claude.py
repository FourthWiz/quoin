"""
simple_claude.py — Cell adapter for the 'simple-claude' benchmark cell.

Invokes Claude Code CLI in non-interactive mode inside an isolated git worktree
of the fixture repo. No Quoin workflow artifacts; pure Claude Code baseline.

Model snapshot pinning:
    Starting with the 4.6 generation, every Claude model ID is itself a
    pinned snapshot — including the dateless form (verified against
    Anthropic's docs, platform.claude.com/docs/en/models/opus-4-7/overview
    and .../sonnet-4-6/overview: "Every Claude model ID is a pinned
    snapshot, including the dateless IDs used from the 4.6 generation on").
    There is no separate dated snapshot to discover for these models — the
    bare ID below IS the permanent pin, confirmed by a live probe
    (`claude --print --output-format json --model claude-opus-4-7 ...`
    returns `canonicalModel: "claude-opus-4-7"` with no date suffix
    anywhere in the response, and the installed CLI binary's own static
    model table has no dated string for this alias either). No further
    pinning action is possible or needed for this model generation.

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
# This dateless ID IS the permanent pin (D-11) for 4.6+-generation models —
# see the module docstring. Verified 2026-09-06 against a live probe
# (canonicalModel: "claude-opus-4-7", no date suffix anywhere in the
# response) and against Anthropic's docs, which state this explicitly as
# policy. There is no dated snapshot to swap in later.
# ---------------------------------------------------------------------------
PINNED_MODEL: str = "claude-opus-4-7"

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


# Verified against CLI 2.1.261 (D-14): the result event carries
# `total_cost_usd`; a bare `cost_usd` key does not appear anywhere in the
# binary. Read the real field first, with the legacy name as a fallback so
# this stays compatible with both older and newer CLI builds.
def _extract_cost_usd(event: dict) -> Optional[float]:
    cost_val = event.get("total_cost_usd")
    if cost_val is None:
        cost_val = event.get("cost_usd")
    if cost_val is None:
        return None
    return float(cost_val)


# Strings the CLI itself emits when `--max-budget-usd` halts a session
# (verified against CLI 2.1.261, D-08). Detected on combined stdout-event
# text and captured stderr, since neither the exact event `type` nor the
# exact stream carrying the message is documented — a substring match on
# the CLI's own enforcement strings is what fires without over-fitting to
# either channel.
_BUDGET_HALT_MARKERS = (
    "Reached maximum budget ($",
    "Session cost is not a number",
)


def _detect_budget_halt(text: str) -> bool:
    return any(marker in text for marker in _BUDGET_HALT_MARKERS)


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
                cost_val = _extract_cost_usd(event)
                if cost_val is not None:
                    total_cost_usd = cost_val
                usage = event.get("usage", {})
                tokens_in = usage.get("input_tokens", tokens_in)
                tokens_out = usage.get("output_tokens", tokens_out)
                tokens_cache_read = usage.get("cache_read_input_tokens", tokens_cache_read)
                tokens_cache_write = usage.get("cache_creation_input_tokens", tokens_cache_write)

            if event_type == "assistant":
                turn_count += 1

        proc.wait(timeout=10)
        stderr_output = ""
        if proc.stderr is not None:
            try:
                stderr_output = proc.stderr.read() or ""
            except Exception:
                stderr_output = ""

        # A CLI-enforced budget halt (D-08) is reported as capped, not
        # silently short — this cell's own refusal above only catches an
        # UNARMED cap; this catches the cap actually firing mid-session.
        if result["verdict"] is None:
            halt_text = stderr_output + "".join(
                str(evt.get("result", "")) + str(evt.get("error", "")) for evt in events
            )
            if _detect_budget_halt(halt_text):
                result["verdict"] = "budget_stopped"
                result["extra"]["failure_reason"] = "budget-halt-detected"

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
