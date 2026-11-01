"""
quoin_codex.py — Cell adapter for the 'quoin-codex' benchmark cell.

Interpretation of "quoin + codex" (D-04):
    This cell uses the "thin ambient guidance" interpretation:
    (i)  Repo-local AGENTS.md content from quoin (quoin/AGENTS.md-based content
         copied into the worktree so Codex sees quoin's repo-local guidance).
    (ii) Bootstrapped .workflow_artifacts/ layout per the Codex adapter docs
         (quoin/adapters/codex/workflow.md).
    (iii) Portable workflow contracts under quoin/core/workflow/ rendered as
          plain-English instructions in the Codex prompt.

    This cell does NOT emulate slash commands, §0 dispatch, or Claude-specific
    skill invocations — these are Claude adapter features not portable to Codex
    (per quoin/adapters/codex/installable-feature.md).

Pre-registered expectations (D-04, must be documented in methodology.md):
    - Expected delta: ≤5 percentage points on pass@1 from this thin intervention.
    - This is below the MDE at N=20 SWE-bench Lite tasks (see methodology.md MDE table).
    - This cell exists to verify the harness works end-to-end on Codex, NOT to
      produce a publishable quoin-effect-on-Codex claim. That is deferred to v2.
    - The quoin-codex cell is exploratory-only in v1.

Per-task isolation (same as quoin-claude, invariants 13, 14):
    Fresh empty .workflow_artifacts/ per task; no carried-over data.

Cost capture (D-12):
    Writes cost: not_available per quoin/adapters/codex/cost.md.
    No token-derived estimates. Cost reconciliation deferred to OpenAI dashboard.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import BudgetSpec
from .simple_codex import _get_codex_model, _build_codex_prompt, PINNED_CODEX_MODEL

# Path to AGENTS.md source within the quoin repo
_AGENTS_MD_RELATIVE = "AGENTS.md"


def _bootstrap_quoin_codex_environment(
    workdir: Path,
    quoin_repo_root: Optional[Path] = None,
) -> None:
    """
    Bootstrap the quoin-codex environment in the worktree.

    Steps:
    1. Remove any existing .workflow_artifacts/ (ensure fresh start per-task).
    2. Create empty .workflow_artifacts/ structure.
    3. Copy quoin's AGENTS.md into the worktree root so Codex sees quoin guidance.
    4. Write a plain-English portable workflow contracts summary as context.
    """
    # Step 1 & 2: Fresh .workflow_artifacts/
    artifacts_dir = workdir / ".workflow_artifacts"
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "memory").mkdir(parents=True)

    # Step 3: Copy AGENTS.md from quoin repo into worktree root
    if quoin_repo_root:
        agents_src = quoin_repo_root / _AGENTS_MD_RELATIVE
        if agents_src.exists():
            shutil.copy2(agents_src, workdir / "AGENTS.md")

    # Step 4: Write portable workflow contracts as QUOIN_WORKFLOW.md in worktree
    workflow_guidance = _build_portable_workflow_guidance()
    (workdir / "QUOIN_WORKFLOW.md").write_text(workflow_guidance, encoding="utf-8")


def _build_portable_workflow_guidance() -> str:
    """
    Build a plain-English summary of quoin's portable workflow contracts.

    This is the 'thin ambient guidance' that quoin provides to Codex.
    It covers the portable artifact discipline without slash-command references.
    """
    return """\
# Quoin Portable Workflow Guidance

This file describes the quoin workflow discipline for coding agents. Follow
these practices to improve planning, traceability, and session continuity.

## Key Practices

1. **Plan before coding.** Before writing code, create a plan in
   `.workflow_artifacts/<task-name>/current-plan.md` describing what you will
   implement, why, and how you will verify it. Include acceptance criteria.

2. **Document architecture.** For non-trivial tasks, write
   `.workflow_artifacts/<task-name>/architecture.md` describing the system
   design, affected components, and integration points.

3. **Write tests alongside code.** Tests are part of the deliverable, not
   an afterthought. Identify what to test in the plan and implement tests
   during the coding phase.

4. **Self-review before finishing.** After coding, review your own diff:
   - No debug code left in
   - No hardcoded values that should be configurable
   - No missing error handling
   - No accidental file inclusions

5. **Commit changes cleanly.** Use conventional commit messages:
   `<type>(<scope>): <short description>` where type is one of:
   feat, fix, refactor, test, docs, chore, perf, ci.

## Artifact Structure

```
.workflow_artifacts/<task-name>/
  current-plan.md    — implementation plan with acceptance criteria
  architecture.md    — design and integration analysis (for complex tasks)
```

## What Quoin Does Not Require of Codex

This guidance covers the portable artifact discipline. Claude Code slash
commands (/plan, /implement, /review, /gate, etc.) are Claude-specific and
are not available in Codex. Use your native Codex planning capabilities;
write the artifacts above as plain Markdown files.
"""


def invoke(
    task_spec: dict,
    workdir: Path,
    budget: BudgetSpec,
    run_id: str,
    quoin_repo_root: Optional[Path] = None,
) -> dict:
    """
    Invoke Codex CLI with thin quoin ambient guidance.

    This cell:
    - Bootstraps a fresh .workflow_artifacts/ per-task
    - Copies quoin's AGENTS.md into the worktree
    - Writes QUOIN_WORKFLOW.md with portable guidance
    - Invokes Codex CLI with the standard simple_codex invocation (same flags)

    Cost: always not_available per D-12 and quoin/adapters/codex/cost.md.

    Parameters
    ----------
    task_spec:
        Task dict from suite-v1.json.
    workdir:
        Isolated git worktree path for this task.
    budget:
        Wall-clock and USD budget constraints.
    run_id:
        The benchmark run ID.
    quoin_repo_root:
        Optional path to the quoin repo root. Used to locate AGENTS.md.
    """
    model = _get_codex_model()
    base_prompt = _build_codex_prompt(task_spec)

    # Prepend a brief quoin guidance reference to the prompt
    prompt = (
        "Please review QUOIN_WORKFLOW.md and AGENTS.md (if present in this "
        "repository) for workflow guidance before starting. Then:\n\n"
        + base_prompt
    )

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

    # Bootstrap quoin-codex environment in worktree
    _bootstrap_quoin_codex_environment(workdir, quoin_repo_root)

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

            try:
                event = json.loads(line)
                events.append(event)
                if event.get("type") in ("assistant", "message"):
                    turn_count += 1
            except json.JSONDecodeError:
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
        # Cost: not_available for Codex (D-12)
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
        try:
            prompt_file.unlink(missing_ok=True)
        except Exception:
            pass

    return result
