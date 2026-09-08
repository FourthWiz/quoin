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
import subprocess
import sys
import uuid
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
) -> Path:
    """Write run-manifest.yaml at the start of a run.

    `wall_clock_seconds` and `usd_cap` are the CONFIGURED values (T-03) —
    they used to be hardcoded literals here, which made invariants 5 and 6
    pass on a string rather than on anything the harness enforced (F-02).
    """
    manifest = {
        "run_id": run_id,
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
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
    wall_clock_seconds: float = 600,
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
    summary = aggregate_run(run_dir, run_id, started_at=started_at, finished_at=finished_at)
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


def main() -> None:
    from quoin.benchmarks.scripts import spend_ledger

    parser = argparse.ArgumentParser(
        description="Quoin benchmark orchestrator CLI"
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("quoin/benchmarks/suite-v1.json"),
        help="Path to suite-v1.json (default: quoin/benchmarks/suite-v1.json)",
    )
    parser.add_argument(
        "--cells",
        type=str,
        default="simple-claude,quoin-claude,simple-codex,quoin-codex",
        help="Comma-separated cell IDs to run",
    )
    parser.add_argument(
        "--run-id",
        required=True,
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
        default=600,
        help="Per-task wall-clock budget in seconds (default: 600)",
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
    args = parser.parse_args()

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
    suite_path = args.suite

    if not suite_path.exists():
        print(f"ERROR: Suite file not found: {suite_path}", file=sys.stderr)
        sys.exit(1)

    suite = json.loads(suite_path.read_text(encoding="utf-8"))["tasks"]

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
