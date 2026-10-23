"""
run_benchmark.py — Orchestrator CLI for running quoin benchmark suites.

Usage:
    python3 quoin/benchmarks/scripts/run_benchmark.py \\
        --suite quoin/benchmarks/suite-v1.json \\
        --cells simple-claude,quoin-claude,simple-codex,quoin-codex \\
        --run-id v1 \\
        --max-parallel 4 \\
        --resume

    # Dry-run (prints planned matrix + cost estimate, no agent invocations)
    python3 quoin/benchmarks/scripts/run_benchmark.py \\
        --suite quoin/benchmarks/suite-v1.json \\
        --cells simple-claude,quoin-claude \\
        --run-id v1 \\
        --dry-run

The orchestrator:
  1. Writes run-manifest.yaml at the start with pinned config + git SHA + datetime.
  2. Runs each cell sequentially (or parallel if --max-parallel > 1).
  3. Supports --resume: skips tasks whose result dir is already well-formed.
  4. After all cells complete, runs aggregation and invariants check.

See README.md for the "Running a v1 benchmark" section.
"""

from __future__ import annotations

# Path bootstrap: mirrors conftest.py so the script works when invoked directly
# (without pytest adding src/ and . to sys.path). Must run before any quoin imports.
import sys as _sys
from pathlib import Path as _Path
_repo_root = _Path(__file__).resolve().parent.parent.parent.parent  # .../quoin/
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
try:
    import quoin as _q
    _inner = str(_repo_root / "quoin")
    if _inner not in _q.__path__:
        _q.__path__.append(_inner)
except Exception:
    pass

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


def _git_sha(repo_root: Path) -> str:
    """Return the current git HEAD SHA (short) for the repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _suite_sha(suite_path: Path) -> str:
    """Return the SHA256 (first 16 chars) of the suite JSON file."""
    try:
        content = suite_path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]
    except Exception:
        return "unknown"


def _probe_claude_model() -> str:
    """
    Return the PINNED_MODEL constant (with the env override applied).

    Despite the name, this does NOT probe anything — it reads the
    deterministic constant so invariant 1's byte-identical-model-ID-within-
    pair property can't drift mid-run. For 4.6+-generation models this
    dateless ID IS the permanent pin (see cells/simple_claude.py's module
    docstring); there is no dated form to resolve. The one REAL, live probe
    lives behind `--verify-model` (T-04c), which confirms the constant still
    matches what the CLI actually reports.
    """
    from quoin.benchmarks.harness.cells.simple_claude import _get_model
    return _get_model()


def _probe_codex_model() -> str:
    """Return the pinned Codex model name (verify at benchmark time)."""
    from quoin.benchmarks.harness.cells.simple_codex import _get_codex_model
    return _get_codex_model()


def _resolve_model_id_from_probe_response(data: dict) -> Optional[str]:
    """Extract the resolved model ID from a `claude --output-format json`
    response.

    Verified 2026-09-06 against CLI 2.1.261: the response carries NO
    top-level `model` field. The resolved ID appears as the sole key of
    `modelUsage` (and again as that entry's `canonicalModel` value). The
    top-level field is checked first for forward-compatibility with a CLI
    version that does add one.
    """
    if data.get("model"):
        return str(data["model"])
    model_usage = data.get("modelUsage") or {}
    if len(model_usage) == 1:
        (only_key, only_value), = model_usage.items()
        canonical = only_value.get("canonicalModel") if isinstance(only_value, dict) else None
        return str(canonical or only_key)
    return None


def _run_live_model_probe(max_budget_usd: float) -> dict:
    """Shell the real `claude` CLI for a one-turn, budget-capped probe.

    This is the ONE live, spend-generating call behind `--verify-model`
    (T-04c) — never confused with `--dry-run`, which stays spend-free.
    """
    from quoin.benchmarks.harness.cells.simple_claude import PINNED_MODEL

    result = subprocess.run(
        [
            "claude", "--print", "--output-format", "json",
            "--model", PINNED_MODEL,
            "--max-budget-usd", str(max_budget_usd),
            "reply with the single word ok",
        ],
        capture_output=True, text=True, timeout=120,
    )
    return json.loads(result.stdout)


def verify_model(
    ledger_path: Optional[Path] = None,
    max_budget_usd: float = 1.0,
    run_probe=_run_live_model_probe,
    authorised: Optional[float] = None,
) -> int:
    """
    `--verify-model` preflight (T-04c): make ONE live, $1-capped call,
    compare the resolved model ID against `PINNED_MODEL`, and exit
    accordingly. Runs before any paid arm (T-09's dry-run gate). Prechecks
    and records against T-17's spend ledger like any other paid call — this
    is NOT a spend-free operation, unlike `--dry-run`.

    `authorised`, when given, is the operator-supplied spend ceiling
    (`--authorised-usd`) threaded through to the precheck instead of the
    ledger-derived default — the same ceiling the three-arm gate driver
    enforces, so this one live call is checked against it too.

    Returns the process exit code: 0 on match, 1 on mismatch, 2 if the
    ledger precheck itself fails (no call made).
    """
    from quoin.benchmarks.harness.cells.simple_claude import PINNED_MODEL
    from quoin.benchmarks.scripts import spend_ledger

    if ledger_path is not None:
        # Required here too, not just in the three-arm gate driver
        # (round-4 fix: MAJOR 8) — a standalone `--verify-model` call
        # against a real ledger is a real, spend-generating call, and the
        # derive-a-ceiling-from-the-ledger-it-polices fallback is exactly
        # the hole that lets any ledger-appending caller silently raise
        # its own authorisation.
        if authorised is None:
            print(
                "GATE-STOP: --verify-model with --spend-ledger requires "
                "--authorised-usd — the ceiling must be an explicit "
                "operator input, not derived from the ledger it prechecks "
                "spend against",
                file=sys.stderr,
            )
            return 2
        resolved_authorised = spend_ledger.resolve_authorised_ceiling(ledger_path, authorised)
        ok, message = spend_ledger.precheck(
            ledger_path, planned_caps=[max_budget_usd], authorised=resolved_authorised,
        )
        if not ok:
            print(message, file=sys.stderr)
            return 2

    attempt_id = str(uuid.uuid4())
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    if ledger_path is not None:
        spend_ledger.append(ledger_path, {
            "ts": ts, "attempt_id": attempt_id, "kind": "reservation",
            "invocation": "full", "gate_id": "verify-model", "arm": "probe",
            "cap_usd": max_budget_usd, "actual_usd": None, "run_id": None,
            "new_ceiling_usd": None, "note": "--verify-model preflight",
        })

    data = run_probe(max_budget_usd)
    resolved_id = _resolve_model_id_from_probe_response(data)
    actual_usd = data.get("total_cost_usd")
    if actual_usd is None:
        actual_usd = data.get("cost_usd")

    if ledger_path is not None:
        spend_ledger.append(ledger_path, {
            "ts": ts, "attempt_id": attempt_id, "kind": "settlement",
            "invocation": "full", "gate_id": "verify-model", "arm": "probe",
            "cap_usd": max_budget_usd,
            "actual_usd": float(actual_usd) if actual_usd is not None else None,
            "run_id": None, "new_ceiling_usd": None,
            "note": "--verify-model preflight",
        })

    print(f"Pinned model:   {PINNED_MODEL}")
    print(f"Resolved model: {resolved_id}")
    if resolved_id == PINNED_MODEL:
        print("--verify-model: MATCH")
        return 0
    print("--verify-model: MISMATCH", file=sys.stderr)
    return 1


def _estimate_cost_dry_run(cells: list[str], suite: list[dict]) -> dict[str, str]:
    """
    Estimate costs for a dry-run without invoking any agent. ADVISORY only
    — the only number the paid-run authorisation may be gated on is the
    WORST CASE line printed separately below (T-09), computed from T-14's
    CLI-enforced per-task cap, which is the only figure actually enforced.

    Returns {cell: cost_estimate_string}.
    """
    estimates = {}
    try:
        from quoin.benchmarks.harness.cost import load_pricing
        pricing = load_pricing()
    except Exception:
        pricing = None

    for cell in cells:
        n_tasks = len(suite)
        if "codex" in cell:
            estimates[cell] = "cost: not_available — reconcile offline via OpenAI dashboard"
        else:
            if pricing:
                model = _probe_claude_model()
                model_pricing = pricing.get("models", {}).get(model, {})
                input_rate = model_pricing.get("input_per_1m_usd", 15.0)
                output_rate = model_pricing.get("output_per_1m_usd", 75.0)

                total_est = 0.0
                for task in suite:
                    # Use the task's own est_tokens_in/out when present
                    # (T-06's scenario suite carries these); otherwise the
                    # original fixed heuristic, byte-unchanged: 2000/500 for
                    # a simple cell, 5000/2000 for a quoin cell (planning
                    # overhead).
                    if "est_tokens_in" in task and "est_tokens_out" in task:
                        tokens_in = task["est_tokens_in"]
                        tokens_out = task["est_tokens_out"]
                    elif "quoin" in cell:
                        tokens_in, tokens_out = 5000, 2000
                    else:
                        tokens_in, tokens_out = 2000, 500
                    total_est += (
                        tokens_in / 1_000_000 * input_rate + tokens_out / 1_000_000 * output_rate
                    )
                est_per_task = total_est / n_tasks if n_tasks else 0.0
                estimates[cell] = (
                    f"ADVISORY ~${total_est:.2f} estimated ({n_tasks} tasks × ~${est_per_task:.4f}/task)"
                )
            else:
                estimates[cell] = "ADVISORY pricing.json not found; cost unknown"
    return estimates


def dry_run_gate_check(
    max_budget_usd_per_task: Optional[float],
    worst_case_usd: Optional[float] = None,
    worst_case_ceiling: float = 38.0,
    rehearsal: bool = False,
) -> tuple[bool, str]:
    """
    T-09's dry-run gate: the paid run is conditional on this passing.

    PASSES only when all hold:
      1. a per-task `--max-budget-usd` is set;
      2. `worst_case_usd` (computed by the CALLER — the multiplication
         differs between a standalone `run_benchmark.py --dry-run`
         invocation, `max_budget_usd_per_task * n_tasks * len(cells)`, and
         the three-arm driver's own combined-caps total across all three
         arms) is <= `worst_case_ceiling` (round-3 correction, MIN-6: 38 is
         what per-arm caps 6/16/16 sum to for the gate; the stage total of
         ~41.50 additionally includes the rehearsal and probes, which are
         not arm spend);
      3. the pinned model has an EXACT pricing key (T-04d);
      4. the live probe equals PINNED_MODEL — WAIVED under `rehearsal`
         mode, where QUOIN_BENCH_CLAUDE_MODEL is set deliberately instead.

    Returns (passed, reason). `reason` is empty on success; on failure it
    is the specific clause that failed, for the caller to fold into the
    named `GATE-STOP: dry-run precondition failed — {reason}; ...` message.
    """
    if max_budget_usd_per_task is None:
        return False, "no per-task --max-budget-usd is set (WORST CASE: UNBOUNDED)"

    if worst_case_usd is not None and worst_case_usd > worst_case_ceiling:
        return False, (
            f"worst-case total ${worst_case_usd:.2f} exceeds the "
            f"${worst_case_ceiling:.0f} gate threshold"
        )

    from quoin.benchmarks.harness.cells.simple_claude import PINNED_MODEL
    from quoin.benchmarks.harness.cost import load_pricing
    try:
        pricing = load_pricing()
    except Exception:
        pricing = {}
    if PINNED_MODEL not in (pricing.get("models") or {}):
        return False, f"PINNED_MODEL {PINNED_MODEL!r} is not an exact key of pricing.json['models']"

    if not rehearsal:
        model_override = os.environ.get("QUOIN_BENCH_CLAUDE_MODEL")
        if model_override:
            return False, (
                "QUOIN_BENCH_CLAUDE_MODEL is set outside --rehearsal mode; "
                "full mode must verify the real pinned model, not an override"
            )

    return True, ""


def _fixture_repo_sha(fixture_repo: Optional[Path]) -> str:
    """Return the fixture repo's HEAD SHA, or the literal `not_a_git_repo`
    marker when the path isn't a git repo (or none was given). Never left
    absent — absence is what made invariant 4 report WARN (F-02)."""
    if fixture_repo is None:
        return "not_a_git_repo"
    try:
        result = subprocess.run(
            ["git", "-C", str(fixture_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "not_a_git_repo"


def _write_manifest(
    run_dir: Path,
    run_id: str,
    suite_path: Path,
    cells: list[str],
    max_parallel: int,
    resume: bool,
    repo_root: Path,
    wall_clock_seconds: float = 600,
    usd_cap: Optional[float] = None,
    fixture_repo: Optional[Path] = None,
    *,
    extra: Optional[dict] = None,
    started_at: Optional[str] = None,
) -> Path:
    """Write run-manifest.yaml at the start of a run.

    `wall_clock_seconds` and `usd_cap` are the CONFIGURED values (T-03) —
    they used to be hardcoded literals here, which made invariants 5 and 6
    pass on a string rather than on anything the harness enforced (F-02).
    """
    manifest = {
        "run_id": run_id,
        "started_at": started_at or datetime.datetime.utcnow().isoformat() + "Z",
        "suite_path": str(suite_path),
        "suite_sha": _suite_sha(suite_path),
        "cells": cells,
        "max_parallel": max_parallel,
        "resume": resume,
        "quoin_repo_sha": _git_sha(repo_root),
        "simple_claude_model": _probe_claude_model(),
        "quoin_claude_model": _probe_claude_model(),
        "simple_codex_model": _probe_codex_model(),
        "quoin_codex_model": _probe_codex_model(),
        "wall_clock_budget_seconds": wall_clock_seconds,
        "max_retries": 0,
        "usd_kill_switch_per_cell_pair": usd_cap,
        "fixture_repo_sha": _fixture_repo_sha(fixture_repo),
        "isolation_mode": "per-task",
        "network_policy": "offline-after-clone-for-fixture",
        "temperature": "0 for HumanEval+, 0.2 for SWE-bench Lite",
        "gate_auto_approve_env": "QUOIN_GATE_AUTO_APPROVE=1 AND QUOIN_BENCHMARK_RUN=<run_id>",
    }
    for key, value in (extra or {}).items():
        manifest[key] = json.dumps(value, sort_keys=True) if isinstance(value, dict) else value

    run_path = run_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    manifest_path = run_path / "run-manifest.yaml"

    # Write as YAML-like (no dependency on pyyaml for manifest writing)
    lines = []
    for k, v in manifest.items():
        if v is None:
            # A bare str(v) renders a Python None as the literal token
            # `None`, which is not YAML null and reads back as the STRING
            # "None" rather than an absent value (round-2 fix).
            lines.append(f"{k}: null")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            v_str = str(v)
            if any(c in v_str for c in (":", "#", "&", "*", "?", "|", "-", "<", ">", "=", "!", "'")):
                v_str = f'"{v_str}"'
            lines.append(f"{k}: {v_str}")

    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path


def run_benchmark(
    suite_path: Path,
    cells: list[str],
    run_id: str,
    run_dir: Path,
    max_parallel: int = 4,
    resume: bool = False,
    repo_root: Optional[Path] = None,
    fixture_repo: Optional[Path] = None,
    quoin_install_script: Optional[Path] = None,
    quoin_install_mode: str = "script",
    arm_root: Optional[Path] = None,
    expected_quoin_commit: Optional[str] = None,
    max_budget_usd_per_task: Optional[float] = None,
    usd_cap: Optional[float] = None,
    wall_clock_seconds: Optional[float] = None,
) -> None:
    """Execute the full benchmark run.

    `usd_cap` is a BETWEEN-task cumulative bound (T-03's `SpendTracker`,
    D-08's secondary bound) — it cannot stop a one-task suite, since it is
    only checked after a task returns. `max_budget_usd_per_task` is the
    intra-task, CLI-enforced bound (T-14's `--max-budget-usd`), the primary
    one for a gate pilot.
    """
    from quoin.benchmarks.harness.config import HarnessConfig, BudgetSpec
    from quoin.benchmarks.harness.runner import run_cell, SpendTracker
    from quoin.benchmarks.harness.aggregate import aggregate_run
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    if repo_root is None:
        repo_root = Path(".")
    if wall_clock_seconds is None:
        wall_clock_seconds = 600

    # Load suite
    suite = json.loads(suite_path.read_text(encoding="utf-8"))["tasks"]

    # Write manifest
    manifest_path = _write_manifest(
        run_dir, run_id, suite_path, cells, max_parallel, resume, repo_root,
        wall_clock_seconds=wall_clock_seconds, usd_cap=usd_cap,
        fixture_repo=fixture_repo,
    )
    print(f"Run manifest written to: {manifest_path}")

    config_kwargs: dict = dict(
        suite_path=suite_path,
        run_dir=run_dir,
        cells=cells,
        max_parallel=max_parallel,
        budget=BudgetSpec(wall_clock_seconds=wall_clock_seconds, max_retries=0),
        quoin_install_mode=quoin_install_mode,
    )
    if quoin_install_script is not None:
        config_kwargs["quoin_install_script"] = quoin_install_script
    if expected_quoin_commit is not None:
        config_kwargs["expected_quoin_commit"] = expected_quoin_commit
    if arm_root is not None:
        config_kwargs["arm_root"] = arm_root
    if max_budget_usd_per_task is not None:
        config_kwargs["max_budget_usd"] = max_budget_usd_per_task
    config = HarnessConfig(**config_kwargs)

    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    print(f"\nStarting benchmark run: {run_id}")
    print(f"  Cells: {', '.join(cells)}")
    print(f"  Tasks: {len(suite)}")
    print(f"  Max parallel: {max_parallel}")
    print(f"  Resume: {resume}\n")

    def run_one_cell(cell: str):
        print(f"  Starting cell: {cell}")
        tracker = SpendTracker(cap_usd=usd_cap)
        result = run_cell(
            cell=cell,
            suite=suite,
            run_id=run_id,
            config=config,
            fixture_repo=fixture_repo,
            resume=resume,
            tracker=tracker,
        )
        n_pass = sum(1 for r in result.task_results if r.verdict == "pass")
        n_total = len(result.task_results)
        print(f"  Finished cell: {cell} — {n_pass}/{n_total} passed")
        if result.budget_stopped:
            print(f"  Cell {cell} STOPPED by the between-task spend cap (${usd_cap})")
        return result

    if max_parallel > 1 and len(cells) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {executor.submit(run_one_cell, cell): cell for cell in cells}
            for future in concurrent.futures.as_completed(futures):
                cell = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    print(f"  ERROR in cell {cell}: {exc}", file=sys.stderr)
    else:
        for cell in cells:
            run_one_cell(cell)

    finished_at = datetime.datetime.utcnow().isoformat() + "Z"

    # Aggregate results
    print("\nAggregating results...")
    summary = aggregate_run(
        run_dir, run_id, started_at=started_at, finished_at=finished_at, suite_path=suite_path,
    )
    print(f"  Summary written to: {run_dir / run_id / 'summary.md'}")

    # Run invariants check
    print("\nChecking invariants...")
    inv_report = check_invariants(run_dir, run_id)
    inv_path = run_dir / run_id / "invariants-report.md"
    inv_path.write_text(inv_report.to_markdown(), encoding="utf-8")
    print(f"  Invariants report: {inv_path}")
    if inv_report.overall_pass():
        print("  Invariants: PASS")
    else:
        print("  Invariants: FAIL — review invariants-report.md before publishing results",
              file=sys.stderr)

    print(f"\nRun complete: {run_id}")


_BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
_SUITE_FILE_RE = re.compile(r"^suite-[a-z0-9][a-z0-9-]*\.json$")


def _resolve_suite_arg(value: str, benchmarks_dir: Path = _BENCHMARKS_DIR) -> Path:
    """Map a bare suite alias such as "tiered-small" to its suite file.

    A value with no path separator and no .json suffix is an alias for
    suite-<alias>.json next to this script's benchmarks directory, whatever the
    current directory is. Anything else is treated as a plain path.
    """
    if "/" in value or os.sep in value or value.endswith(".json"):
        return Path(value)
    candidate = benchmarks_dir / f"suite-{value}.json"
    if candidate.is_file():
        return candidate
    known = sorted(
        p.name[len("suite-"):-len(".json")]
        for p in benchmarks_dir.glob("suite-*.json")
        if _SUITE_FILE_RE.match(p.name)
    )
    print(
        f"ERROR: unknown suite alias '{value}'; known aliases: {', '.join(known)}",
        file=sys.stderr,
    )
    sys.exit(1)


def _verified_tasks(tasks: list) -> list:
    return [t for t in tasks if t.get("source") == "swebench_verified"]


def _prepare_repos_main(suite_path: Path, tasks: list) -> int:
    """Build the repo mirrors and prove each Verified task prepares cleanly.

    Network access happens only in the mirror step; no model is called.
    """
    import time

    from quoin.benchmarks.harness.repo_prep import (
        RepoPrepError, check_heavy_dirs, check_pin_refs, cleanup_task_repo,
        prepare_mirrors, prepare_task_repo,
    )

    verified = _verified_tasks(tasks)
    if not verified:
        print(f"ERROR: {suite_path} has no swebench_verified tasks", file=sys.stderr)
        return 2

    problems: dict = {}
    try:
        prepare_mirrors(tasks)
    except RepoPrepError as exc:
        print(f"ERROR: mirror preparation failed: {exc}", file=sys.stderr)
        return 1
    for line in check_pin_refs(tasks):
        problems[line.split(":", 1)[0]] = line
    for line in check_heavy_dirs(tasks):
        problems.setdefault(line.split(":", 1)[0], line)

    trials: dict = {}
    for task in verified:
        if task["id"] in problems:
            continue
        started = time.monotonic()
        try:
            prepared = prepare_task_repo(task)
            note = cleanup_task_repo(prepared.path)
            trials[task["id"]] = "trial ok in %.1fs" % (time.monotonic() - started)
            if note:
                trials[task["id"]] += f" (cleanup: {note})"
        except RepoPrepError as exc:
            problems[task["id"]] = f"{task['id']}: {exc.reason}"
            trials[task["id"]] = "trial failed: %s after %.1fs" % (exc.reason, time.monotonic() - started)
        except Exception as exc:
            problems[task["id"]] = f"{task['id']}: unexpected-{type(exc).__name__}"
            trials[task["id"]] = f"trial failed: unexpected-{type(exc).__name__}"

    for task in verified:
        status = "FAIL" if task["id"] in problems else "OK"
        detail = trials.get(task["id"]) or problems.get(task["id"], "")
        print(f"{task['id']} {task['repo']} {task['base_commit'][:12]} {status} {detail}".rstrip())
    return 1 if problems else 0


def _judge_only_main(suite_path: Path, tasks: list, run_dir: Path, run_id: str) -> int:
    """Re-judge recorded Verified sessions whose verdict is not final; spawns no agent."""
    from quoin.benchmarks.harness import runner
    from quoin.benchmarks.harness.aggregate import aggregate_run
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    if not _verified_tasks(tasks):
        print(f"ERROR: {suite_path} has no swebench_verified tasks", file=sys.stderr)
        return 2
    run_path = run_dir / run_id
    if not run_path.is_dir():
        print(f"ERROR: run directory not found: {run_path}", file=sys.stderr)
        return 2
    try:
        tiered = json.loads(suite_path.read_text(encoding="utf-8")).get("tiered") or {}
    except (OSError, ValueError):
        tiered = {}
    rows = runner.judge_pending_rows(
        run_dir, run_id, tasks,
        snapshot_revision=tiered.get("dataset_revision"),
        judge=runner.judge_task,
    )
    for row in rows:
        print(f"{row['cell']} {row['task_id']} {row['verdict']} final={row['judge_final']}")
    if not rows:
        print("No pending Verified rows to judge.")

    started_at = finished_at = None
    try:
        previous = json.loads((run_path / "summary.json").read_text(encoding="utf-8"))
        started_at, finished_at = previous.get("started_at"), previous.get("finished_at")
    except (OSError, ValueError):
        pass
    aggregate_run(
        run_dir, run_id, started_at=started_at, finished_at=finished_at, suite_path=suite_path,
    )
    inv_report = check_invariants(run_dir, run_id)
    (run_path / "invariants-report.md").write_text(inv_report.to_markdown(), encoding="utf-8")
    print(f"Summary refreshed: {run_path / 'summary.md'}")
    return 0 if all(r["judge_final"] for r in rows) else 1


def _check_flag_conflicts(parser, args) -> None:
    exclusive = [
        ("--prepare-repos", args.prepare_repos),
        ("--judge-only", args.judge_only),
        ("--dry-run", args.dry_run),
        ("--verify-model", args.verify_model),
    ]
    for i, (name, active) in enumerate(exclusive[:2]):
        if not active:
            continue
        for other, other_active in exclusive:
            if other != name and other_active:
                parser.error(f"{name} cannot be combined with {other}")


def _is_paired(doc: dict) -> bool:
    tiered = doc.get("tiered")
    return isinstance(tiered, dict) and tiered.get("paired_run") is True


def _scoped_tasks(doc: dict, tiers_arg: Optional[str]):
    """Tasks of a tiered suite limited to --tiers; returns (tasks, tier names) or exits 2."""
    tiered = doc.get("tiered")
    if not isinstance(tiered, dict):
        if tiers_arg:
            print("ERROR: --tiers needs a suite with a tiered block", file=sys.stderr)
            sys.exit(2)
        return doc["tasks"], []
    known = list((tiered.get("tiers") or {}).keys())
    if not tiers_arg:
        return doc["tasks"], sorted(known)
    wanted = [t.strip() for t in tiers_arg.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in known]
    if unknown or not wanted:
        print(
            f"ERROR: unknown tier {', '.join(unknown) or tiers_arg!r}; known tiers: "
            f"{', '.join(known)}",
            file=sys.stderr,
        )
        sys.exit(2)
    return [t for t in doc["tasks"] if t.get("tier") in wanted], sorted(set(wanted))


def _refuse(clause: str, detail: str) -> int:
    print(f"REFUSED: {clause}: {detail}", file=sys.stderr)
    return 2


def _git_version_tuple() -> Optional[tuple]:
    try:
        proc = subprocess.run(["git", "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if proc.returncode == 0 and match else None


def _paired_rows_to_spawn(run_dir: Path, run_id: str, tasks: list, cells) -> Optional[int]:
    """Rows that would spawn on this invocation; None when a foreign row blocks the run."""
    from quoin.benchmarks.harness import paired_run

    count = 0
    for task in tasks:
        for cell in cells:
            t_dir = run_dir / run_id / cell / task["id"]
            sup = paired_run._superseded_dir(run_dir, run_id, cell, task["id"])
            state = paired_run.row_state(t_dir, superseded_dir=sup)
            if state == "foreign":
                print(f"REFUSED: run-dir: existing row is not from a paired run: {t_dir}",
                      file=sys.stderr)
                return None
            if state in ("absent", "respawn"):
                count += 1
    return count


def _check_resume_manifest(run_dir, run_id, suite_path, cap, tier_names) -> Optional[str]:
    """Reason the stored manifest does not match this invocation, or None."""
    from quoin.benchmarks.harness.tiered_report import read_manifest

    manifest = read_manifest(run_dir / run_id)
    if not manifest:
        return "no readable run-manifest.yaml to resume from"
    if manifest.get("suite_sha") != _suite_sha(suite_path):
        return "suite_sha differs from the stored manifest"
    try:
        stored_cap = round(float(manifest.get("max_budget_usd_per_task")), 2)
    except (TypeError, ValueError):
        return "stored max_budget_usd_per_task is unreadable"
    if stored_cap != round(float(cap), 2):
        return f"--max-budget-usd-per-task differs from the stored {manifest.get('max_budget_usd_per_task')}"
    if sorted(manifest.get("tiers") or []) != sorted(tier_names):
        return "tiers differ from the stored manifest"
    if manifest.get("arm_config_mode") != "symmetric":
        return "arm_config_mode differs from the stored manifest"
    return None


def _check_resume_commit(run_dir, run_id, expected: str) -> Optional[str]:
    """Reason a resume would mix quoin commits, or None."""
    from quoin.benchmarks.harness.tiered_report import read_manifest

    stored = read_manifest(run_dir / run_id).get("expected_quoin_commit")
    if stored != expected:
        return (f"quoin commit {expected} differs from the {stored} the earlier legs of this "
                "run used; start a new --run-id")
    return None


def _paired_dry_run(args, suite_path: Path, doc: dict) -> int:
    """Print the paired matrix and worst case; starts no process."""
    from quoin.benchmarks.harness import paired_run, repo_prep
    from quoin.benchmarks.harness.paired_spend import worst_case as _worst_case

    tasks, _tiers = _scoped_tasks(doc, args.tiers)
    tiered = doc["tiered"]
    cap = args.max_budget_usd_per_task
    target = float(tiered.get("worst_case_target_usd", 100))
    ceiling = float(tiered.get("worst_case_ceiling_usd", target))
    arms = list(paired_run.PAIR_ORDER)
    print(f"DRY RUN — Run ID: {args.run_id}")
    print(f"Suite: {suite_path} ({len(tasks)} tasks)")
    print("Paired mode: plain then quoin per task, one session at a time")
    print()
    header = f"{'Task ID':<24} {'Tier':<8} {'Arm':<7} {'Cap':<8} Wall clock"
    print(header)
    print("-" * len(header))
    for task in tasks:
        wall = paired_run.resolve_wall_clock(args.wall_clock_seconds, task)
        for cell in arms:
            arm = "plain" if cell == "simple-claude" else "quoin"
            cap_text = f"${cap:.2f}" if cap is not None else "none"
            print(f"{task['id']:<24} {task.get('tier', ''):<8} {arm:<7} {cap_text:<8} {wall:.0f}s")
    print()
    print("Cost estimates:")
    for cell, est in _estimate_cost_dry_run(arms, tasks).items():
        print(f"  {cell}: {est}")
    print()
    resolved_model = _probe_claude_model()
    print(f"Resolved model: {resolved_model}")
    try:
        from quoin.benchmarks.harness.cost import load_pricing
        pricing_models = load_pricing().get("models", {})
    except Exception:
        pricing_models = {}
    print(f"Exact pricing key: {resolved_model in pricing_models}")

    if cap is not None:
        worst = _worst_case(cap, len(tasks), len(arms))
        print(f"WORST CASE: ${worst:.2f} (cap x {len(tasks)} tasks x {len(arms)} arms)")
    else:
        worst = None
        print("WORST CASE: UNBOUNDED (no --max-budget-usd-per-task set)")
    print(f"Target: ${target:.2f}, enforced ceiling: ${ceiling:.2f}")

    print()
    print("A paid run would check (advisory, nothing is started here):")
    print(f"  cap: {'set' if cap is not None else 'MISSING (--max-budget-usd-per-task)'}")
    print(f"  spend ledger: {'set' if args.spend_ledger else 'MISSING (--spend-ledger)'}")
    print(f"  authorised: {'set' if args.authorised_usd else 'MISSING (--authorised-usd)'}")
    print(f"  suite ceiling: ${ceiling:.2f}; worst case within it: "
          f"{'yes' if worst is not None and worst <= ceiling else 'NO'}")
    auth_set = [k for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if os.environ.get(k)]
    print(f"  auth variable: {'one set' if len(auth_set) == 1 else 'MISSING or ambiguous'}")
    missing = []
    for task in tasks:
        if task.get("source") != "swebench_verified":
            continue
        found = repo_prep.pin_ref_status_offline(task)
        if found != task.get("base_commit"):
            missing.append(task["id"])
    print(f"  pin refs: {'all present' if not missing else 'missing for ' + ', '.join(missing)}")

    passed, reason = dry_run_gate_check(
        max_budget_usd_per_task=cap, worst_case_usd=worst, worst_case_ceiling=ceiling,
    )
    if not passed:
        print(
            f"GATE-STOP: dry-run precondition failed — {reason}; "
            "re-authorisation required before any paid arm",
            file=sys.stderr,
        )
        return 2
    return 0


def _paired_abort_label(outcome) -> Optional[str]:
    """Human-readable account of the check or row that ended the run, or None."""
    if outcome.stop_reason != "abort" or not outcome.abort_cell:
        return None
    detail = "; ".join(outcome.abort_detail or ["abort"])
    where = f"{outcome.abort_cell}/{outcome.abort_task_id}"
    if outcome.abort_kind == "recheck" and not outcome.abort_task_id:
        return f"{detail} (found after the last {outcome.abort_cell} session)"
    if outcome.abort_kind == "recheck":
        prior = f"; the last {outcome.abort_cell} session ran on {outcome.abort_prior_task_id}" \
            if outcome.abort_prior_task_id else ""
        return f"{detail} (found before {where}{prior})"
    return f"{detail} on {where}"


def _paired_final_manifest_checks(outcome) -> str:
    label = _paired_abort_label(outcome)
    if label is None:
        return "static-passed; runtime-passed"
    if outcome.abort_kind == "recheck":
        return f"runtime-tripped: {label}"
    from quoin.benchmarks.harness.tiered_report import IDENTITY_TRIP_REASONS

    detail = (outcome.abort_detail or ["abort"])[0]
    prefix = "runtime-tripped" if detail in IDENTITY_TRIP_REASONS else "aborted"
    return f"{prefix}: {label}"


def _paired_run_invalid_reason(outcome) -> Optional[str]:
    """A reason the whole run is invalid when the abort came from a pre-spawn check."""
    if outcome.abort_kind != "recheck":
        return None
    return f"arm contamination check failed: {_paired_abort_label(outcome)}"


@dataclass
class PairedPreflight:
    """Everything a paired run needs from the refusal checks, carried into the run."""

    tasks: list
    tier_names: list
    tiered: dict
    cap: float
    run_dir: Path
    run_id: str
    run_path: Path
    arms: list
    has_rows: bool
    n_spawns: int
    suite_ceiling: float
    auth: tuple
    version: tuple
    swebench_version: str
    arm_root: Path
    expected: str
    carried_invalid: list


def _judge_prereqs() -> tuple:
    """Probe docker and the pinned swebench; returns (ok, clause, detail, swebench_version)."""
    from quoin.benchmarks.harness import judge

    try:
        docker = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return (False, "docker", "docker info did not run; use --skip-judge to defer judging", "")
    if docker.returncode != 0:
        return (False, "docker", "docker info failed; use --skip-judge to defer judging", "")
    python = os.environ.get(judge.SWEBENCH_PYTHON_ENV) or sys.executable
    try:
        swebench_version = judge.probe_swebench_version(python) or "not installed"
    except (OSError, subprocess.SubprocessError):
        swebench_version = "not installed"
    if swebench_version != judge.SWEBENCH_PINNED_VERSION:
        return (
            False, "swebench",
            f"swebench {judge.SWEBENCH_PINNED_VERSION} is required, found {swebench_version}",
            swebench_version,
        )
    return (True, "", "", judge.SWEBENCH_PINNED_VERSION)


def _paired_preflight(args, suite_path: Path, doc: dict):
    """Run every refusal check of a paired run; returns PairedPreflight or an exit code."""
    from quoin.benchmarks.harness import paired_run, paired_spend, repo_prep, arm_sandbox
    from quoin.benchmarks.scripts import run_three_arm_gate as gate

    tasks, tier_names = _scoped_tasks(doc, args.tiers)
    tiered = doc["tiered"]
    cap = args.max_budget_usd_per_task
    run_dir: Path = args.run_dir
    run_id = args.run_id
    arms = list(paired_run.PAIR_ORDER)

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    if sorted(cells) != sorted(arms):
        return _refuse("cells", "paired suites need --cells simple-claude,quoin-claude")
    if args.fixture_repo is not None:
        return _refuse("fixture-repo", "paired suites do not take --fixture-repo")
    if cap is None:
        return _refuse("cap-set", "--max-budget-usd-per-task is required")

    run_path = run_dir / run_id
    has_rows = any(
        (run_path / cell).is_dir() and any(p.is_dir() for p in (run_path / cell).iterdir())
        for cell in arms
    )
    if has_rows and not args.resume:
        return _refuse("run-dir", f"{run_path} already holds rows; use --resume or a new --run-id")
    if args.resume and has_rows:
        why = _check_resume_manifest(run_dir, run_id, suite_path, cap, tier_names)
        if why:
            return _refuse("resume", why)

    n_spawns = _paired_rows_to_spawn(run_dir, run_id, tasks, arms)
    if n_spawns is None:
        return 2
    suite_ceiling = float(tiered.get("worst_case_ceiling_usd", 110))
    for clause, ok, detail in paired_spend.preflight_spend(
        args.spend_ledger, run_id, cap, n_spawns, args.authorised_usd, suite_ceiling,
    ):
        if not ok:
            return _refuse(clause, detail)

    try:
        auth = arm_sandbox.select_auth(os.environ)
    except ValueError as exc:
        return _refuse("auth", str(exc))

    version = _git_version_tuple()
    if version is None or version < (2, 42):
        found = ".".join(map(str, version)) if version else "unknown"
        return _refuse("git-version", f"git 2.42 or newer is required, found {found}")
    problems = repo_prep.check_pin_refs(tasks)
    if problems:
        return _refuse("pin-refs", "; ".join(problems))
    heavy = repo_prep.check_heavy_dirs(tasks)
    if heavy:
        return _refuse("heavy-dir-tracked", "; ".join(heavy))

    swebench_version = "skipped"
    if not args.skip_judge:
        ok, clause, detail, swebench_version = _judge_prereqs()
        if not ok:
            return _refuse(clause, detail)

    arm_root = Path(args.arm_root or args.repo_root).resolve()
    expected = args.expected_quoin_commit or gate.worktree_head(arm_root)
    if not expected:
        return _refuse("arm-tree", f"cannot read HEAD of {arm_root}")
    tree_problems = arm_sandbox.check_arm_tree(arm_root, expected)
    if tree_problems:
        return _refuse("arm-tree", "; ".join(tree_problems))
    carried_invalid: list = []
    if args.resume and has_rows:
        why = _check_resume_commit(run_dir, run_id, expected)
        if why:
            return _refuse("resume", why)
        from quoin.benchmarks.harness.tiered_report import read_manifest
        carried_invalid = list(read_manifest(run_path).get("run_invalid_reasons") or [])

    return PairedPreflight(
        tasks=tasks, tier_names=tier_names, tiered=tiered, cap=cap, run_dir=run_dir,
        run_id=run_id, run_path=run_path, arms=arms, has_rows=has_rows, n_spawns=n_spawns,
        suite_ceiling=suite_ceiling, auth=auth, version=version,
        swebench_version=swebench_version, arm_root=arm_root, expected=expected,
        carried_invalid=carried_invalid,
    )


def _paired_execute(args, suite_path: Path, doc: dict, pre, *, quiet_deferred_hint: bool = False) -> int:
    """Run the sessions, judge, aggregate and write the manifest for a checked paired run."""
    import signal
    import tempfile

    from quoin.benchmarks.harness import (
        arm_identity, arm_sandbox, judge, paired_run, paired_spend, runner,
    )
    from quoin.benchmarks.harness.aggregate import aggregate_run
    from quoin.benchmarks.harness.config import BudgetSpec, HarnessConfig
    from quoin.benchmarks.harness.session_state import is_judge_pending
    from quoin.benchmarks.scripts import run_three_arm_gate as gate
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    tasks, tier_names, tiered, cap = pre.tasks, pre.tier_names, pre.tiered, pre.cap
    run_dir, run_id, run_path, arms = pre.run_dir, pre.run_id, pre.run_path, pre.arms
    suite_ceiling, auth, version = pre.suite_ceiling, pre.auth, pre.version
    swebench_version, arm_root, expected = pre.swebench_version, pre.arm_root, pre.expected
    carried_invalid = pre.carried_invalid


    gate_state = paired_run.InterruptGate()
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, gate_state.handler) for sig in signals}
    sandboxes: list = []
    run_parent: Optional[Path] = None
    manifest_writer = None
    try:
        try:
            run_parent = Path(os.path.realpath(tempfile.mkdtemp(prefix="qbench_run_")))
            arm_sandbox.assert_private_dir(run_parent)
            plain = arm_sandbox.create_sandbox("plain", run_parent)
            sandboxes.append(plain)
            quoin_box = arm_sandbox.create_sandbox("quoin", run_parent)
            sandboxes.append(quoin_box)
            # Traverse only: a session of either arm cannot list the run parent
            # to find the other arm's sandbox.
            os.chmod(run_parent, 0o100)
            install = arm_sandbox.install_quoin(quoin_box, arm_root, expected)
            if not install.get("ok"):
                return _refuse("quoin-install", str(install.get("reason")))
            installer = gate._load_arm_installer(arm_root)
            canonical = tuple(installer.CANONICAL_SKILLS) if installer is not None else ()
            plain_problems = arm_identity.check_plain_sandbox(plain.home)
            if plain_problems:
                return _refuse(arm_identity.PLAIN_REASON, "; ".join(plain_problems))
            quoin_problems = arm_identity.check_quoin_sandbox(quoin_box.home, arm_root)
            if quoin_problems:
                return _refuse(arm_identity.QUOIN_REASON, "; ".join(quoin_problems))

            mode = "symmetric"
            distinctive = arm_identity.distinctive_quoin_skills(canonical)
            identities = {
                "simple-claude": arm_identity.ArmIdentity(
                    mode=arm_identity.QUOIN_FREE, config_dir=Path(os.path.realpath(plain.claude_dir)),
                    canonical=canonical, distinctive=distinctive, arm_config_mode=mode),
                "quoin-claude": arm_identity.ArmIdentity(
                    mode=arm_identity.QUOIN_REQUIRED, config_dir=Path(os.path.realpath(quoin_box.claude_dir)),
                    canonical=canonical, distinctive=distinctive, arm_config_mode=mode),
            }
            boxes = {"simple-claude": plain, "quoin-claude": quoin_box}
            envs = {
                cell: arm_sandbox.build_session_env(boxes[cell], cell, run_id, auth)
                for cell in arms
            }
            plain_print = arm_sandbox.config_fingerprint(plain.claude_dir)
            quoin_print = arm_sandbox.config_fingerprint(quoin_box.claude_dir)

            def recheck_plain():
                out = []
                if arm_sandbox.config_fingerprint(plain.claude_dir) != plain_print:
                    out.append("plain config directory changed since the start of the run")
                if arm_sandbox.installed_plugin_names(plain.claude_dir):
                    out.append("plain config directory has installed plugins")
                return out

            def recheck_quoin():
                out = []
                if arm_sandbox.config_fingerprint(quoin_box.claude_dir) != quoin_print:
                    out.append("quoin config directory changed since the install")
                return out

            configs = {
                cell: HarnessConfig(
                    suite_path=suite_path, run_dir=run_dir, cells=[cell], max_parallel=1,
                    budget=BudgetSpec(wall_clock_seconds=600, max_retries=0),
                    quoin_install_mode="skip",
                    expected_quoin_commit=expected if cell == "quoin-claude" else None,
                    arm_root=arm_root if cell == "quoin-claude" else None,
                    max_budget_usd=cap, session_env=envs[cell], arm_identity=identities[cell],
                    defer_judge=True, row_write_hold=gate_state.hold,
                )
                for cell in arms
            }
            plan = paired_run.PairedPlan(
                run_id=run_id, run_dir=run_dir, tasks=tasks, cap=cap, ledger=args.spend_ledger,
                authorised=args.authorised_usd, suite_ceiling=suite_ceiling, usd_cap=args.usd_cap,
                wall_clock_flag=args.wall_clock_seconds, configs=configs, resume=args.resume,
                identity_recheck={"simple-claude": recheck_plain, "quoin-claude": recheck_quoin},
            )
        except paired_run.PairedInterrupted as exc:
            print(f"Interrupted by {exc.signal_name} before the run started", file=sys.stderr)
            return 1

        waits = {t["id"]: paired_run.resolve_wall_clock(args.wall_clock_seconds, t) for t in tasks}
        by_tier: dict = {}
        for t in tasks:
            by_tier[t.get("tier", "")] = max(by_tier.get(t.get("tier", ""), 0), waits[t["id"]])
        worst = paired_spend.worst_case(cap, len(tasks), len(arms))
        started_at = datetime.datetime.utcnow().isoformat() + "Z"
        extra = {
            "paired_mode": True,
            "pair_order": arms,
            "effective_max_parallel": 1,
            "requested_max_parallel": args.max_parallel,
            "arm_config_mode": "symmetric",
            "contamination_checks": "static-passed",
            "sandbox_static_checks": {"simple-claude": "pass", "quoin-claude": "pass"},
            "session_env_keys": {c: arm_sandbox.session_env_keys(envs[c]) for c in arms},
            "auth_env_key": auth[0],
            "permissions_profile": arm_sandbox.SANDBOX_BASE_PERMISSIONS,
            "expected_quoin_commit": expected,
            "repo_cache_checks": "pass",
            "git_version": ".".join(map(str, version)),
            "swebench_version": swebench_version,
            "dataset_revision": tiered.get("dataset_revision"),
            "judge_phase": "skipped" if args.skip_judge else "deferred",
            "tiers": tier_names,
            "max_budget_usd_per_task": cap,
            "worst_case_usd": worst,
            "worst_case_target_usd": tiered.get("worst_case_target_usd"),
            "worst_case_ceiling_usd": suite_ceiling,
            "authorised_usd": args.authorised_usd,
            "spend_ledger": str(args.spend_ledger),
            "wall_clock_budget_seconds": max(waits.values()) if waits else 600,
            "wall_clock_budget_seconds_by_tier": by_tier,
            "resume_policy": "session-json",
            "suite_path_resolved": str(suite_path.resolve()),
        }

        invalid_reasons = list(carried_invalid)

        def write_manifest(**more):
            if invalid_reasons:
                more.setdefault("run_invalid_reasons", list(invalid_reasons))
            return _write_manifest(
                run_dir, run_id, suite_path, arms, 1, args.resume, args.repo_root,
                wall_clock_seconds=extra["wall_clock_budget_seconds"], usd_cap=args.usd_cap,
                extra={**extra, **more}, started_at=started_at,
            )

        manifest_writer = write_manifest
        write_manifest()
        print(f"Max parallel: 1 (paired mode runs one session at a time; --max-parallel "
              f"{args.max_parallel} ignored)")
        interrupted = None
        outcome = paired_run.PairedOutcome()
        try:
            outcome = paired_run.run_paired(plan)
        except paired_run.PairedRefusal as exc:
            print(f"REFUSED: run-dir: {exc}", file=sys.stderr)
            return 2
        except paired_run.PairedInterrupted as exc:
            interrupted = exc.signal_name

        if interrupted:
            late = paired_run.PairedOutcome()
            paired_run.final_recheck(plan, late, cells=arms)
            reason = _paired_run_invalid_reason(late)
            if reason and reason not in invalid_reasons:
                invalid_reasons.append(reason)
            write_manifest(interrupted=interrupted)
            print(f"Interrupted by {interrupted}; rerun with --resume to continue", file=sys.stderr)
            return 1

        if not args.skip_judge and outcome.stop_reason is None:
            for row in paired_run.judge_phase(plan, doc["tasks"], tiered.get("dataset_revision")):
                print(f"{row['cell']} {row['task_id']} {row['verdict']} final={row['judge_final']}")
        reason = _paired_run_invalid_reason(outcome)
        if reason and reason not in invalid_reasons:
            invalid_reasons.append(reason)
        write_manifest(contamination_checks=_paired_final_manifest_checks(outcome))

        finished_at = datetime.datetime.utcnow().isoformat() + "Z"
        aggregate_run(run_dir, run_id, started_at=started_at, finished_at=finished_at,
                      suite_path=suite_path.resolve())
        report = check_invariants(run_dir, run_id)
        (run_path / "invariants-report.md").write_text(report.to_markdown(), encoding="utf-8")
        print(f"Summary written to: {run_path / 'summary.md'}")

        pending = sum(
            1 for cell in arms for t in tasks
            if (run_path / cell / t["id"]).is_dir() and is_judge_pending(run_path / cell / t["id"])
        )
        if outcome.stop_reason == "abort":
            print(f"Run aborted: {'; '.join(outcome.abort_detail or [])}", file=sys.stderr)
            return 1
        if outcome.stop_reason is not None:
            print(f"Run stopped: {outcome.stop_reason}: {'; '.join(outcome.abort_detail or [])}",
                  file=sys.stderr)
            return 3
        if args.skip_judge:
            if not quiet_deferred_hint:
                print(f"Judging deferred: {pending} rows pending; run --judge-only on a host with Docker")
            return 0
        return 0 if pending == 0 else 1
    except paired_run.PairedInterrupted as exc:
        print(f"Interrupted by {exc.signal_name}", file=sys.stderr)
        if manifest_writer is not None:
            try:
                manifest_writer(interrupted=exc.signal_name)
            except Exception as err:  # noqa: BLE001 - the interrupt is the outcome to report
                print(f"could not record the interruption: {err}", file=sys.stderr)
        return 1
    finally:
        # A second signal must not abort the cleanup, so ignore them until the
        # sandboxes are gone and the previous handlers are back.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if run_parent is not None:
                try:
                    os.chmod(run_parent, 0o700)
                except OSError:
                    pass
            for box in sandboxes:
                note = arm_sandbox.remove_sandbox(box)
                if note:
                    print(note, file=sys.stderr)
            if run_parent is not None:
                shutil.rmtree(run_parent, ignore_errors=True)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _paired_main(args, suite_path: Path, doc: dict, *, quiet_deferred_hint: bool = False) -> int:
    """Run the paired benchmark; every refusal exits 2 before any repo prep or session."""
    pre = _paired_preflight(args, suite_path, doc)
    if isinstance(pre, int):
        return pre
    return _paired_execute(args, suite_path, doc, pre, quiet_deferred_hint=quiet_deferred_hint)


def build_parser() -> argparse.ArgumentParser:
    from quoin.benchmarks.scripts import spend_ledger

    parser = argparse.ArgumentParser(
        description="Quoin benchmark orchestrator CLI"
    )
    parser.add_argument(
        "--suite",
        type=str,
        default="quoin/benchmarks/suite-v1.json",
        help=(
            "Suite file path, or a bare alias such as tiered-small that resolves to"
            " quoin/benchmarks/suite-<alias>.json (default: quoin/benchmarks/suite-v1.json)"
        ),
    )
    parser.add_argument(
        "--cells",
        type=str,
        default="simple-claude,quoin-claude,simple-codex,quoin-codex",
        help="Comma-separated cell IDs to run",
    )
    parser.add_argument(
        "--run-id",
        required=False,
        help="Unique run identifier (e.g., v0-smoke, v1)",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(".workflow_artifacts/quoin-benchmarks/runs"),
        help="Root directory for run outputs",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        help="Maximum number of concurrent task executions (default: 4)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume: skip tasks whose result dir is already well-formed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned task x cell matrix and cost estimate, then exit",
    )
    parser.add_argument(
        "--fixture-repo",
        type=Path,
        default=None,
        help="Path to fixture repo for SWE-bench Lite tasks",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Path to quoin repo root (default: current directory)",
    )
    parser.add_argument(
        "--quoin-install-script",
        type=Path,
        default=None,
        help="Path to the arm's quoin/install.sh (default: HarnessConfig's own default)",
    )
    parser.add_argument(
        "--quoin-install-mode",
        choices=["script", "skip", "module"],
        default="script",
        help="How the quoin-claude cell installs quoin per task (D-16). "
             "'script' (default) shells install.sh, byte-unchanged from "
             "today. 'skip' installs nothing (the gate mode — the driver "
             "already installed the arm). 'module' runs "
             "`python -m quoin install` pinned to --arm-root.",
    )
    parser.add_argument(
        "--arm-root",
        type=Path,
        default=None,
        help="Arm worktree root, consumed by --quoin-install-mode module",
    )
    parser.add_argument(
        "--expected-quoin-commit",
        type=str,
        default=None,
        help="Commit the quoin-claude cell must confirm it installed (D-14)",
    )
    parser.add_argument(
        "--max-budget-usd-per-task",
        type=spend_ledger.positive_finite_float,
        default=None,
        help="CLI-enforced per-task spend cap, passed to `claude "
             "--max-budget-usd` (T-14, D-08's primary bound)",
    )
    parser.add_argument(
        "--usd-cap",
        type=float,
        default=None,
        help="BETWEEN-task cumulative spend cap for this invocation (T-03, "
             "D-08's secondary bound). Evaluated only after a task returns "
             "— cannot stop a one-task suite.",
    )
    parser.add_argument(
        "--wall-clock-seconds",
        type=int,
        default=None,
        help="Per-task wall-clock budget in seconds (default: the task's own value"
             " for tiered suites, else 600)",
    )
    parser.add_argument(
        "--verify-model",
        action="store_true",
        help="Make ONE live, $1-capped call to confirm PINNED_MODEL still "
             "matches what the CLI resolves (T-04c). NOT spend-free, unlike "
             "--dry-run. Exits 0 on match, 1 on mismatch, 2 on a failed "
             "ledger precheck (no call made). Runs before any paid arm.",
    )
    parser.add_argument(
        "--spend-ledger",
        type=lambda p: Path(p).resolve(),
        default=None,
        help="Path to T-17's persisted spend ledger (used by --verify-model "
             "and by the three-arm gate driver). Resolved at parse time so "
             "a mistyped or cwd-relative path cannot silently start a "
             "fresh, unledgered spend trail.",
    )
    parser.add_argument(
        "--authorised-usd",
        type=spend_ledger.positive_finite_float,
        default=None,
        help="Operator-supplied spend ceiling for --verify-model's own "
             "ledger precheck. When omitted, the ceiling is derived from "
             "the ledger's own latest reauth-note (or the $50 default).",
    )
    parser.add_argument(
        "--prepare-repos",
        action="store_true",
        help="Clone the repo mirrors for Verified tasks and check that each task"
             " prepares cleanly. Needs network access; makes no model call.",
    )
    parser.add_argument(
        "--judge-only",
        action="store_true",
        help="Re-judge recorded Verified sessions in --run-id whose verdict is not"
             " final. Spawns no agent.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Harness retries per task. Only 0 is supported in v1; final pass rate"
             " equals first-attempt pass rate.",
    )
    parser.add_argument(
        "--tiers",
        type=str,
        default=None,
        help="Comma-separated tier names to run from a tiered suite (default: all tiers)",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="Paired suites only: run the sessions and leave every row judge-pending"
             " for a later --judge-only on a host with Docker.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.run_id is None and not args.prepare_repos:
        parser.error("the following arguments are required: --run-id")
    _check_flag_conflicts(parser, args)

    if args.retries != 0:
        print(
            f"ERROR: --retries {args.retries} is not supported in v1; "
            "only --retries 0 (the default) is accepted",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.verify_model:
        if args.spend_ledger is None:
            print(
                "GATE-STOP: --verify-model requires --spend-ledger — this "
                "call is spend-generating and must be ledgered like any "
                "other paid call",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(verify_model(ledger_path=args.spend_ledger, authorised=args.authorised_usd))

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    suite_path = _resolve_suite_arg(args.suite)

    if not suite_path.exists():
        print(f"ERROR: Suite file not found: {suite_path}", file=sys.stderr)
        sys.exit(1)

    suite_doc = json.loads(suite_path.read_text(encoding="utf-8"))
    suite = suite_doc["tasks"]
    paired = _is_paired(suite_doc)
    if args.skip_judge and not paired:
        print("ERROR: --skip-judge applies only to paired suites", file=sys.stderr)
        sys.exit(2)
    if args.tiers and not isinstance(suite_doc.get("tiered"), dict):
        print("ERROR: --tiers needs a suite with a tiered block", file=sys.stderr)
        sys.exit(2)
    if args.tiers and not paired:
        print("ERROR: --tiers applies only to paired suites", file=sys.stderr)
        sys.exit(2)

    if args.prepare_repos:
        sys.exit(_prepare_repos_main(suite_path, suite))
    if args.judge_only:
        sys.exit(_judge_only_main(suite_path, suite, args.run_dir, args.run_id))

    if paired:
        sys.exit(_paired_dry_run(args, suite_path, suite_doc) if args.dry_run
                 else _paired_main(args, suite_path, suite_doc))

    if args.dry_run:
        print(f"DRY RUN — Run ID: {args.run_id}")
        print(f"Suite: {suite_path} ({len(suite)} tasks)")
        print(f"Cells: {', '.join(cells)}")
        print(f"Total task invocations: {len(suite) * len(cells)}")
        print()
        print("Cost estimates:")
        estimates = _estimate_cost_dry_run(cells, suite)
        for cell, est in estimates.items():
            print(f"  {cell}: {est}")
        print()
        print("Task x Cell matrix (first 10 tasks):")
        header = f"{'Task ID':<30} " + " ".join(f"{c:<20}" for c in cells)
        print(header)
        print("-" * len(header))
        for task in suite[:10]:
            row = f"{task['id']:<30} " + " ".join("run" .ljust(20) for _ in cells)
            print(row)
        if len(suite) > 10:
            print(f"  ... ({len(suite) - 10} more tasks)")

        print()
        resolved_model = _probe_claude_model()
        print(f"Resolved model: {resolved_model}")
        try:
            from quoin.benchmarks.harness.cost import load_pricing
            pricing_models = load_pricing().get("models", {})
        except Exception:
            pricing_models = {}
        exact_key = resolved_model in pricing_models
        print(f"Exact pricing key: {exact_key}")

        if args.max_budget_usd_per_task is not None:
            worst_case = args.max_budget_usd_per_task * len(suite) * len(cells)
            print(f"WORST CASE: ${worst_case:.2f} (max_budget_usd_per_task x n_tasks x len(cells))")
        else:
            worst_case = None
            print("WORST CASE: UNBOUNDED (no --max-budget-usd-per-task set)")

        passed, reason = dry_run_gate_check(
            max_budget_usd_per_task=args.max_budget_usd_per_task,
            worst_case_usd=worst_case,
        )
        if not passed:
            print(
                f"GATE-STOP: dry-run precondition failed — {reason}; "
                "re-authorisation required before any paid arm",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(0)

    run_benchmark(
        suite_path=suite_path,
        cells=cells,
        run_id=args.run_id,
        run_dir=args.run_dir,
        max_parallel=args.max_parallel,
        resume=args.resume,
        repo_root=args.repo_root,
        fixture_repo=args.fixture_repo,
        quoin_install_script=args.quoin_install_script,
        quoin_install_mode=args.quoin_install_mode,
        arm_root=args.arm_root,
        expected_quoin_commit=args.expected_quoin_commit,
        max_budget_usd_per_task=args.max_budget_usd_per_task,
        usd_cap=args.usd_cap,
        wall_clock_seconds=args.wall_clock_seconds,
    )


# Fix missing Optional import
from typing import Optional


if __name__ == "__main__":
    main()
