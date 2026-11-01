"""
simple_codex.py — Cell adapter for the 'simple-codex' benchmark cell.

Invokes Codex CLI in non-interactive batch mode inside an isolated git worktree.
No Quoin guidance; pure Codex baseline.

Codex CLI invocation (T-06):
    codex exec -m <pinned-model> -s workspace-write \
        --dangerously-bypass-approvals-and-sandbox \
        -C <worktree>

The --dangerously-bypass-approvals-and-sandbox flag is the correct non-interactive
batch mechanism per `codex exec --help` (no --approval-mode flag exists as of
Codex CLI v0.130.0; verified against installed CLI).

IMPORTANT: At implementation time, verify and record:
  1. codex --version (exact Codex CLI semver)
  2. The exact dated model alias accepted by codex exec -m
     (model name is tentative; verify against `codex exec --help` or release notes)

Cost capture (D-12):
    Codex cells write cost: not_available in cost.json.
    No token-derived estimates are computed.
    Per quoin/adapters/codex/cost.md, which explicitly forbids token inference
    ("Do not infer token counts from chat length, transcript size, model name,
    elapsed time, or file changes").
    The headline pass/USD metric is undefined for Codex cells in v1.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import BudgetSpec

# ---------------------------------------------------------------------------
# Pin the dated Codex model at implementation time.
# At benchmark time: run `codex exec --help` or check Codex release notes
# to verify the correct dated model alias. Record it in run-manifest.yaml.
# The model name below is TENTATIVE — verify before any real run.
# ---------------------------------------------------------------------------
PINNED_CODEX_MODEL: str = "o4-mini-2025-04-16"  # UPDATE AT BENCHMARK TIME — verify via codex exec --help

_CODEX_MODEL_ENV_VAR = "QUOIN_BENCH_CODEX_MODEL"


def _get_codex_model() -> str:
    return os.environ.get(_CODEX_MODEL_ENV_VAR, PINNED_CODEX_MODEL)


def _build_codex_prompt(task_spec: dict) -> str:
    """Build the prompt to send to Codex CLI for a given task."""
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
) -> dict:
    """
    Invoke Codex CLI in simple (no-quoin) mode.

    IMPORTANT: At implementation time, verify:
      1. `codex --version` — record exact Codex CLI semver in run-manifest.yaml
      2. The dated model alias — confirm `codex exec -m <model>` accepts it
      3. The --dangerously-bypass-approvals-and-sandbox flag is present in
         `codex exec --help` output

    Cost: always not_available per D-12 and quoin/adapters/codex/cost.md.
    """
    model = _get_codex_model()
    prompt = _build_codex_prompt(task_spec)

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
    }

    # Write prompt to a temp file for Codex stdin
    prompt_file = workdir / ".bench_prompt.txt"
    try:
        prompt_file.write_text(prompt, encoding="utf-8")
    except OSError:
        result["verdict"] = "error"
        return result

    cmd = [
        "codex", "exec",
        "-m", model,
        "-s", "workspace-write",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        prompt,
    ]

    # VERIFY AT BENCHMARK TIME: Run `codex exec --help` to confirm:
    # - The -m flag accepts the model name above
    # - The -s flag accepts 'workspace-write'
    # - --dangerously-bypass-approvals-and-sandbox is a valid flag
    # If any flag has changed, update this command and record in run-manifest.yaml.

    budget_seconds = budget.wall_clock_seconds
    wall_start = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        events = []
        turn_count = 0

        while True:
            elapsed = time.monotonic() - wall_start
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

            # Attempt to parse JSONL events if Codex supports --json flag.
            # If not available, tee stdout/stderr as plain text events.
            try:
                event = json.loads(line)
                events.append(event)
                if event.get("type") in ("assistant", "message"):
                    turn_count += 1
            except json.JSONDecodeError:
                # Not JSON — record as plain text event
                events.append({"type": "stdout", "text": line})

        proc.wait(timeout=10)

        # A plain `git diff` in the agent's clone reads agent-writable
        # .git/config (fsmonitor, filters) and would run it under the
        # operator's environment. Verified rows are scored from the
        # harness-owned capture_patch() bytes instead, so skip it there.
        if task_spec.get("source") != "swebench_verified":
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
        # Cost: always not_available for Codex (D-12, codex/cost.md)
        result["cost_available"] = False

    except FileNotFoundError:
        result["verdict"] = "error"
        result["transcript_events"] = [
            {"type": "error", "error": "codex CLI not found on PATH"}
        ]
    except Exception as exc:
        result["verdict"] = "error"
        result["transcript_events"] = [{"type": "error", "error": str(exc)}]
    finally:
        # Clean up temp prompt file
        try:
            prompt_file.unlink(missing_ok=True)
        except Exception:
            pass

    return result
