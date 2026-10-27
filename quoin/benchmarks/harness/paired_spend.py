"""Spend checks and ledger rows for the paired benchmark run.

Two ceilings apply. The suite ceiling bounds what this run may spend (rows of
this run id only, so earlier pilot rows in the same ledger do not count). The
authorised ceiling bounds everything ever recorded in the ledger. A pair is
checked before either session is reserved, so a stop always lands on a pair
boundary; each single spawn is then checked against the authorised ceiling
alone.
"""
from __future__ import annotations

import datetime
import math
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

LEDGER_GATE_ID = "tiered-paired"
ARM_LABELS = {"simple-claude": "plain", "quoin-claude": "quoin"}


def _finite_nonneg(value) -> bool:
    if isinstance(value, (bool, str)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0.0


def worst_case(cap: float, n_tasks: int, n_arms: int = 2) -> float:
    return float(cap) * n_tasks * n_arms


def _spend_ledger():
    from quoin.benchmarks.scripts import spend_ledger

    return spend_ledger


def preflight_spend(
    ledger,
    run_id: str,
    cap,
    n_spawns: int,
    authorised,
    suite_ceiling: float,
) -> List[Tuple[str, bool, str]]:
    """One (clause, passed, detail) triple per spend rule. Never raises."""
    results: List[Tuple[str, bool, str]] = []
    cap_ok = cap is not None and _finite_nonneg(cap) and float(cap) > 0.0
    results.append(("cap-set", cap_ok, "" if cap_ok else "--max-budget-usd-per-task is required"))
    ledger_ok = ledger is not None
    results.append(("ledger-set", ledger_ok, "" if ledger_ok else "--spend-ledger is required"))
    auth_ok = authorised is not None and _finite_nonneg(authorised) and float(authorised) > 0.0
    results.append(
        ("authorised-set", auth_ok, "" if auth_ok else "--authorised-usd is required")
    )
    if not (cap_ok and ledger_ok and auth_ok):
        for name in ("suite-ceiling", "authorised-ceiling", "pricing-key", "model-override"):
            results.append((name, False, "skipped: an earlier clause failed"))
        return results
    spend = _spend_ledger()
    cap = float(cap)
    try:
        recorded = spend.recorded_total(Path(ledger), run_id)
        planned = cap * n_spawns
        fits = recorded + planned <= suite_ceiling
        detail = "" if fits else (
            f"run total {recorded:.2f} + planned {planned:.2f} exceeds the "
            f"${suite_ceiling:.2f} suite ceiling"
        )
        results.append(("suite-ceiling", fits, detail))
    except Exception as exc:
        results.append(("suite-ceiling", False, f"ledger unreadable: {exc}"))
    try:
        ok, message = spend.precheck(Path(ledger), [cap] * n_spawns, float(authorised))
        results.append(("authorised-ceiling", ok, message))
    except Exception as exc:
        results.append(("authorised-ceiling", False, f"ledger unreadable: {exc}"))
    try:
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        ok, reason = dry_run_gate_check(cap, worst_case_usd=None, worst_case_ceiling=suite_ceiling)
    except Exception as exc:
        ok, reason = False, f"gate check failed: {exc}"
    override = "QUOIN_BENCH_CLAUDE_MODEL" in reason
    results.append(("pricing-key", ok or override, "" if ok or override else reason))
    results.append(("model-override", ok or not override, "" if ok or not override else reason))
    return results


def pair_precheck(ledger, run_id, cap, n_spawns, authorised, suite_ceiling) -> Tuple[bool, str]:
    """Both ceilings for the sessions of one pair, before any reservation."""
    spend = _spend_ledger()
    planned = [float(cap)] * n_spawns
    try:
        recorded = spend.recorded_total(Path(ledger), run_id)
    except Exception as exc:
        return False, f"spend-ceiling: ledger unreadable: {exc}"
    if recorded + sum(planned) > suite_ceiling:
        return False, (
            f"spend-ceiling: run total {recorded:.2f} + this pair {sum(planned):.2f} "
            f"would exceed the ${suite_ceiling:.2f} suite ceiling"
        )
    try:
        ok, message = spend.precheck(Path(ledger), planned, float(authorised))
    except Exception as exc:
        return False, f"authorised-ceiling: ledger unreadable: {exc}"
    if not ok:
        return False, f"authorised-ceiling: {message}"
    return True, ""


def spawn_precheck(ledger, cap, authorised) -> Tuple[bool, str]:
    """Authorised ceiling only, for one session."""
    spend = _spend_ledger()
    try:
        ok, message = spend.precheck(Path(ledger), [float(cap)], float(authorised))
    except Exception as exc:
        return False, f"authorised-ceiling: ledger unreadable: {exc}"
    return (True, "") if ok else (False, f"authorised-ceiling: {message}")


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _row(kind, attempt_id, run_id, task_id, cell, cap, actual, note="", *,
         invocation="paired", gate_id=LEDGER_GATE_ID, arm=None):
    return {
        "ts": _now(),
        "attempt_id": attempt_id,
        "kind": kind,
        "invocation": invocation,
        "gate_id": gate_id,
        "arm": arm if arm is not None else ARM_LABELS.get(cell, cell),
        "cap_usd": float(cap),
        "actual_usd": actual,
        "run_id": run_id,
        "new_ceiling_usd": None,
        "note": note,
        "task_id": task_id,
    }


def reserve(ledger, run_id, task_id, cell, cap, *, invocation="paired",
            gate_id=LEDGER_GATE_ID, arm=None) -> str:
    attempt_id = f"{run_id}#{cell}#{task_id}#{uuid.uuid4()}"
    _spend_ledger().append(Path(ledger), _row(
        "reservation", attempt_id, run_id, task_id, cell, cap, None,
        invocation=invocation, gate_id=gate_id, arm=arm,
    ))
    return attempt_id


def settle(ledger, attempt_id, run_id, task_id, cell, cap, actual, *, invocation="paired",
           gate_id=LEDGER_GATE_ID, arm=None) -> None:
    """Record the outcome. An actual that is not a finite non-negative number
    is stored as null, which the ledger fold charges at the cap."""
    value = float(actual) if _finite_nonneg(actual) else None
    _spend_ledger().append(
        Path(ledger), _row(
            "settlement", attempt_id, run_id, task_id, cell, cap, value,
            invocation=invocation, gate_id=gate_id, arm=arm,
        )
    )


def settlement_actual(result) -> Optional[float]:
    """What to settle for a cell result: 0.0 when no process ran, the measured
    runtime cost when it is a real number, otherwise None (charged at the cap)."""
    if result is None:
        return None
    extra = getattr(result, "extra", None) or {}
    if extra.get("pre_spawn_error"):
        return 0.0
    cost = getattr(result, "cost_runtime_usd", None)
    return float(cost) if _finite_nonneg(cost) else None
