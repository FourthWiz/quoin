"""
quoin_claude.py — Cell adapter for the 'quoin-claude' benchmark cell.

Invokes Claude Code CLI with the full Quoin workflow enabled, inside an isolated
git worktree. Gates auto-approve via QUOIN_GATE_AUTO_APPROVE=1 AND
QUOIN_BENCHMARK_RUN=1 (both must be set; see T-19 for the plumbing).

Key differences from simple_claude.py:
  1. Bootstraps a clean per-task .workflow_artifacts/ using quoin/install.sh
     followed by /init_workflow in non-interactive mode.
  2. Sets QUOIN_GATE_AUTO_APPROVE=1 AND QUOIN_BENCHMARK_RUN=<run_id> in the
     subprocess environment. Both must be set for /gate to auto-approve.
  3. Prepends "Use /run end-to-end on this task" to the agent prompt.
  4. Post-run: captures .workflow_artifacts/<task-name>/ into the run output
     folder as evidence; validates that architecture.md and current-plan.md
     exist for at least one sampled task.

Per-task isolation (invariants 13, 14, R-06):
  The .workflow_artifacts/ directory is initialized from empty PER TASK, not
  per cell. No lessons-learned.md, no sessions/, no cache is carried over
  between tasks. The installed ~/.claude/skills/ toolkit is shared (it IS the
  system under test).

Cost:
  Sum of all Claude Code session costs spawned during the run, parsed from
  JSONL session files written under ~/.claude/projects/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..config import BudgetSpec
from ..cost import estimate_cost, load_pricing
from .simple_claude import _build_claude_argv, _build_prompt, _gate_mode, _get_model

# ---------------------------------------------------------------------------
# Invariant: same dated model snapshot as simple-claude (invariant 1).
# This module reuses _get_model() from simple_claude to guarantee byte-for-byte
# model ID equality within the Claude pair.
# ---------------------------------------------------------------------------

ENV_GATE_AUTO_APPROVE = "QUOIN_GATE_AUTO_APPROVE"
ENV_BENCHMARK_RUN = "QUOIN_BENCHMARK_RUN"


_INSTALL_TIMEOUT_SECONDS = 300  # a cold install is not a 60-second operation (T-02)


def _tail(text: Optional[str], limit: int = 4000) -> str:
    """Bound a captured stdout/stderr string to its last `limit` characters."""
    if not text:
        return ""
    return text[-limit:]


def _initialize_workflow_artifacts(
    workdir: Path,
    quoin_install_script: Path,
    quoin_install_mode: str = "script",
    arm_root: Optional[Path] = None,
) -> dict:
    """
    Bootstrap a fresh .workflow_artifacts/ inside the worktree.

    Steps:
    1. Remove any existing .workflow_artifacts/ (guarantee fresh start).
    2. Install quoin per `quoin_install_mode` (see below).
    3. Create empty .workflow_artifacts/ structure (step 1 above).

    `quoin_install_mode` (D-16), one of:
      "script" (DEFAULT — today's behaviour, byte-unchanged): shell
        `bash {quoin_install_script}`.
      "skip" (what the gate uses): install nothing. The driver has already
        installed and verified the arm; a per-task reinstall buys nothing
        and, for the main arm, would fire `install.sh`'s pip tier-3 path
        and downgrade the machine's quoin (D-12, R-14).
      "module": `PYTHONPATH={arm_root}/src {sys.executable} -m quoin
        install --source-dir {arm_root}/quoin --scope user` (D-12's
        arm-pinned form, derived from the worktree ROOT). When `arm_root`
        is `None`, it is derived as `quoin_install_script.resolve().parent.parent`.

    Returns {"ok": bool, "reason": str, "returncode": int | None,
             "stdout_tail": str, "stderr_tail": str}. A failed, skipped or
    timed-out install is surfaced here rather than swallowed — the caller
    treats a non-ok result as fatal before any paid spend (D-03).
    """
    artifacts_dir = workdir / ".workflow_artifacts"
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    artifacts_dir.mkdir(parents=True)

    if quoin_install_mode == "skip":
        return {
            "ok": True, "reason": "skip-driver-installed",
            "returncode": None, "stdout_tail": "", "stderr_tail": "",
        }

    if quoin_install_mode == "module":
        root = arm_root or quoin_install_script.resolve().parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        argv = [
            sys.executable, "-m", "quoin", "install",
            "--source-dir", str(root / "quoin"), "--scope", "user",
        ]
        return _run_install_subprocess(argv, env=env)

    # "script" mode — today's behaviour, byte-unchanged except that a
    # failure, timeout or missing script is now reported, not swallowed.
    if not quoin_install_script.exists():
        return {
            "ok": False, "reason": "script-missing",
            "returncode": None, "stdout_tail": "", "stderr_tail": "",
        }
    return _run_install_subprocess(["bash", str(quoin_install_script)])


def _run_install_subprocess(argv: list[str], env: Optional[dict] = None) -> dict:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "reason": "install-timeout", "returncode": None,
            "stdout_tail": _tail(exc.stdout), "stderr_tail": _tail(exc.stderr),
        }
    except Exception as exc:
        return {
            "ok": False, "reason": "install-error", "returncode": None,
            "stdout_tail": "", "stderr_tail": str(exc),
        }
    if proc.returncode != 0:
        return {
            "ok": False, "reason": "install-failed", "returncode": proc.returncode,
            "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr),
        }
    return {
        "ok": True, "reason": "ok", "returncode": proc.returncode,
        "stdout_tail": _tail(proc.stdout), "stderr_tail": _tail(proc.stderr),
    }


def _capture_workflow_artifacts(
    workdir: Path,
    run_output_dir: Path,
    task_id: str,
) -> dict:
    """
    Copy .workflow_artifacts/<task>/ from the worktree to the run output dir.

    Returns a dict with validation results:
      {
        "captured": bool,
        "has_architecture_md": bool,
        "has_current_plan_md": bool,
        "artifacts_path": str | None,
      }
    """
    src = workdir / ".workflow_artifacts"
    if not src.exists():
        return {
            "captured": False,
            "has_architecture_md": False,
            "has_current_plan_md": False,
            "artifacts_path": None,
        }

    dest = run_output_dir / "workflow_artifacts_evidence"
    try:
        shutil.copytree(src, dest, dirs_exist_ok=True)
    except Exception:
        return {
            "captured": False,
            "has_architecture_md": False,
            "has_current_plan_md": False,
            "artifacts_path": None,
        }

    # Validate: look for architecture.md and current-plan.md in any subfolder
    has_arch = any(dest.rglob("architecture.md"))
    has_plan = any(dest.rglob("current-plan.md"))

    return {
        "captured": True,
        "has_architecture_md": has_arch,
        "has_current_plan_md": has_plan,
        "artifacts_path": str(dest),
    }


def invoke(
    task_spec: dict,
    workdir: Path,
    budget: BudgetSpec,
    run_id: str,
    quoin_install_script: Optional[Path] = None,
    quoin_repo_root: Optional[Path] = None,
    quoin_install_mode: str = "script",
    arm_root: Optional[Path] = None,
    expected_quoin_commit: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> dict:
    """
    Invoke Claude Code with the full Quoin workflow.

    Parameters
    ----------
    task_spec:
        Task dict from suite-v1.json.
    workdir:
        Isolated git worktree path for this task.
    budget:
        Wall-clock and USD budget constraints.
    run_id:
        The benchmark run ID (used for QUOIN_BENCHMARK_RUN env var).
    quoin_install_script:
        Path to quoin/install.sh. Defaults to quoin/install.sh relative to cwd.
    quoin_repo_root:
        Path to the quoin repo root. Used to locate install.sh if not given.
    quoin_install_mode:
        "script" (default), "skip" or "module" — see
        `_initialize_workflow_artifacts` (D-16).
    arm_root:
        The arm worktree root, used by "module" mode and by the commit
        assertion below. Derived from `quoin_install_script` when omitted.
    expected_quoin_commit:
        The commit this arm intends to have installed (D-14). In gate mode
        (`QUOIN_BENCHMARK_GATE=1`) a `None` value refuses to spawn; whenever
        set, a mismatch against the arm's actual worktree HEAD refuses too.
    max_budget_usd:
        Optional CLI-enforced spend cap (D-08), passed through to `claude
        --max-budget-usd`. In gate mode, a `None` value or an assembled
        argv missing the flag refuses to spawn.

    Returns
    -------
    dict with transcript_events, prompt, diff_patch, cost data, etc.
    """
    model = _get_model()
    base_prompt = _build_prompt(task_spec)
    # Prepend quoin workflow directive
    prompt = f"Use /run end-to-end on this task\n\n{base_prompt}"

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

    # Resolve quoin install script path
    if quoin_install_script is None:
        if quoin_repo_root:
            quoin_install_script = quoin_repo_root / "quoin" / "install.sh"
        else:
            quoin_install_script = Path("quoin/install.sh")

    gate_mode = _gate_mode()
    resolved_arm_root = arm_root or quoin_install_script.resolve().parent.parent

    # Read the commit this arm's OWN worktree is actually at, BEFORE
    # installing anything (D-14's required assertion) — the worktree's HEAD
    # is what `--source-dir` pins the install to (D-12), so this is the
    # truthful answer to "which commit does this arm intend to install".
    installed_quoin_commit: Optional[str] = None
    commit_error: Optional[str] = None
    try:
        commit_proc = subprocess.run(
            ["git", "-C", str(resolved_arm_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if commit_proc.returncode == 0:
            installed_quoin_commit = commit_proc.stdout.strip()
        else:
            commit_error = _tail(commit_proc.stderr) or "git rev-parse failed"
    except Exception as exc:
        commit_error = str(exc)

    # Bootstrap fresh .workflow_artifacts/ per-task, installing per the
    # arm-pinned mechanism (D-12/D-16) rather than always shelling install.sh.
    install_result = _initialize_workflow_artifacts(
        workdir, quoin_install_script,
        quoin_install_mode=quoin_install_mode, arm_root=resolved_arm_root,
    )

    result["extra"].update({
        "quoin_install_script": str(quoin_install_script),
        "installed_quoin_commit": installed_quoin_commit,
        "expected_quoin_commit": expected_quoin_commit,
        "install_ok": install_result["ok"],
        "install_reason": install_result["reason"],
        "install_returncode": install_result["returncode"],
    })

    # Hard-fail paths: each returns BEFORE subprocess.Popen so no tokens are
    # spent (D-03). A failed/skipped install, an unresolvable commit, or a
    # commit mismatch are all fatal — a silently-wrong install would make
    # the candidate arm secretly measure whatever was installed last.
    if not install_result["ok"]:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "install-failed"
        return result
    if commit_error is not None:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "commit-unresolvable"
        result["extra"]["commit_error"] = commit_error
        return result
    if expected_quoin_commit is not None and installed_quoin_commit != expected_quoin_commit:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "commit-mismatch"
        return result

    # Fail-closed guards, evaluated in gate mode only (D-08's CRIT-2 fix).
    # Outside gate mode both remain advisory records so nothing outside the
    # gate changes behaviour.
    expected_commit_armed = expected_quoin_commit is not None
    result["extra"]["expected_quoin_commit_armed"] = expected_commit_armed
    if gate_mode and not expected_commit_armed:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "expected-commit-unarmed"
        return result

    cmd = _build_claude_argv(prompt, model, max_budget_usd)
    budget_cap_armed = max_budget_usd is not None and "--max-budget-usd" in cmd
    result["extra"]["budget_cap_armed"] = budget_cap_armed
    result["extra"]["max_budget_usd_applied"] = max_budget_usd
    if gate_mode and not budget_cap_armed:
        result["verdict"] = "error"
        result["extra"]["failure_reason"] = "budget-cap-unarmed"
        return result

    # Build subprocess environment with gate auto-approve
    env = os.environ.copy()
    env[ENV_GATE_AUTO_APPROVE] = "1"
    env[ENV_BENCHMARK_RUN] = run_id

    budget_seconds = budget.wall_clock_seconds
    wall_start = time.monotonic()
    backoff_total = 0.0

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        events = []
        total_cost_usd: Optional[float] = None
        tokens_in = tokens_out = tokens_cache_read = tokens_cache_write = 0
        turn_count = 0
        gate_intervention_count = 0
        retry_delay = 1.0

        while True:
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

            event_type = event.get("type", "")

            # Handle rate-limit / overloaded events (same as simple_claude)
            if event_type in ("error", "api_error"):
                error_msg = str(event.get("error", ""))
                if "429" in error_msg or "overloaded" in error_msg.lower():
                    backoff_start = time.monotonic()
                    time.sleep(retry_delay)
                    backoff_total += time.monotonic() - backoff_start
                    retry_delay = min(retry_delay * 2, 60.0)
                    continue

            events.append(event)

            # Track gate auto-approve events
            # /gate in auto-approve mode emits an event with auto_approved: true
            if event.get("auto_approved"):
                gate_intervention_count += 1

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

        # Capture .workflow_artifacts/ evidence
        # (run output dir is determined by the caller via run_id+cell+task_id)
        artifacts_evidence = _capture_workflow_artifacts(
            workdir=workdir,
            run_output_dir=workdir.parent / "artifacts_evidence",
            task_id=task_spec["id"],
        )
        # Kept at both the top level (nothing that reads them today breaks)
        # and in `extra` (where `RunResult.extra` and the judge can see
        # them — T-01/T-02 fix; they were top-level-only before).
        result["workflow_artifacts_captured"] = artifacts_evidence.get("captured", False)
        result["workflow_artifacts_has_arch"] = artifacts_evidence.get("has_architecture_md", False)
        result["workflow_artifacts_has_plan"] = artifacts_evidence.get("has_current_plan_md", False)
        result["extra"]["workflow_artifacts_captured"] = result["workflow_artifacts_captured"]
        result["extra"]["workflow_artifacts_has_arch"] = result["workflow_artifacts_has_arch"]
        result["extra"]["workflow_artifacts_has_plan"] = result["workflow_artifacts_has_plan"]

        result["transcript_events"] = events
        result["turn_count"] = turn_count
        result["gate_intervention_count"] = gate_intervention_count
        result["tokens_in"] = tokens_in if tokens_in else None
        result["tokens_out"] = tokens_out if tokens_out else None
        result["tokens_cache_read"] = tokens_cache_read if tokens_cache_read else None
        result["tokens_cache_write"] = tokens_cache_write if tokens_cache_write else None

        if total_cost_usd is not None:
            result["cost_available"] = True
            result["cost_runtime_usd"] = total_cost_usd
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
                pass
        else:
            result["cost_available"] = False

    except FileNotFoundError:
        result["verdict"] = "error"
        result["transcript_events"] = [
            {"type": "error", "error": "claude CLI not found on PATH"}
        ]
    except Exception as exc:
        result["verdict"] = "error"
        result["transcript_events"] = [{"type": "error", "error": str(exc)}]

    return result
