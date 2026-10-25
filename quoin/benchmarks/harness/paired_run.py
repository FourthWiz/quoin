"""Paired orchestrator: runs the plain and quoin arms of each task one after
the other on a single thread, with a spend reservation around every session,
resume classification of existing rows, and interrupt handling that never
leaves a finished session without its metrics.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import signal
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paired_spend, runner
from .config import BudgetSpec, HarnessConfig
from .session_state import INVALID_REASONS, QUOIN_CELL, read_session_json
from .tiered_report import ABORT_REASONS, RESPAWN_REASONS, SYSTEMIC_REASONS

PAIR_ORDER = ("simple-claude", "quoin-claude")
DEFAULT_WALL_CLOCK_SECONDS = 600.0
SUPERSEDED_DIR = ".superseded"


class PairedRefusal(Exception):
    """An existing row cannot be classified, so the run refuses to continue."""


class PairedInterrupted(BaseException):
    """A termination signal arrived. A BaseException so that the broad
    `except Exception` handlers in the runner and the cells let it through
    while their `finally` blocks still stop the process group and clean up."""

    def __init__(self, signal_name: str = "") -> None:
        super().__init__(signal_name)
        self.signal_name = signal_name


class InterruptGate:
    """Defers a termination signal while a row is being written."""

    def __init__(self) -> None:
        self.holding = False
        self.pending: Optional[str] = None
        self._fired = False

    def handler(self, signum, frame=None) -> None:
        if self._fired:
            return
        self._fired = True
        try:
            name = signal.Signals(signum).name
        except (ValueError, TypeError):
            name = str(signum)
        if self.holding:
            self.pending = name
            return
        raise PairedInterrupted(name)

    @contextlib.contextmanager
    def hold(self):
        self.holding = True
        try:
            yield
        finally:
            self.holding = False
        if self.pending is not None:
            raise PairedInterrupted(self.pending)


@dataclass
class PairedPlan:
    run_id: str
    run_dir: Path
    tasks: list
    cap: float
    ledger: Path
    authorised: float
    suite_ceiling: float
    usd_cap: Optional[float] = None
    wall_clock_flag: Optional[float] = None
    configs: Dict[str, HarnessConfig] = field(default_factory=dict)
    resume: bool = False
    identity_recheck: Dict[str, Callable[[], List[str]]] = field(default_factory=dict)
    # Stop before the next spawn when a quoin row did not run the pipeline.
    stop_on_invalid_quoin: bool = False


@dataclass
class PairedOutcome:
    pairs_completed: int = 0
    stop_reason: Optional[str] = None
    abort_detail: Optional[list] = None
    rows: list = field(default_factory=list)
    # Where a run-ending abort was raised: the cell and task, and whether it
    # came from the check before a spawn ("recheck") or from a finished row
    # ("row"). `abort_prior_task_id` is the task whose session of that cell ran
    # last in this invocation, the one a pre-spawn check can be reporting on.
    abort_cell: Optional[str] = None
    abort_task_id: Optional[str] = None
    abort_kind: Optional[str] = None
    abort_prior_task_id: Optional[str] = None


def resolve_wall_clock(flag, task_spec) -> float:
    if isinstance(flag, (int, float)) and not isinstance(flag, bool) and flag > 0:
        return float(flag)
    value = task_spec.get("wall_clock_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return DEFAULT_WALL_CLOCK_SECONDS


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _superseded_dir(runs_root, run_id, cell, task_id) -> Path:
    return Path(runs_root) / SUPERSEDED_DIR / run_id / cell / task_id


def _earlier_harness_exception(superseded_dir) -> bool:
    if superseded_dir is None or not Path(superseded_dir).is_dir():
        return False
    for attempt in Path(superseded_dir).iterdir():
        metrics = _read_json(attempt / "metrics.json") or {}
        if str(metrics.get("failure_reason", "")).startswith("harness-exception-"):
            return True
    return False


def row_state(t_dir, *, superseded_dir=None) -> str:
    """One of absent, respawn, done, foreign."""
    t_dir = Path(t_dir)
    if not t_dir.is_dir():
        return "absent"
    session = read_session_json(t_dir)
    metrics = _read_json(t_dir / "metrics.json")
    if session is None:
        if metrics is None:
            return "respawn"
        if metrics.get("pre_spawn_error") is True:
            return "respawn"
        if str(metrics.get("failure_reason", "")).startswith("harness-exception-"):
            return "done" if _earlier_harness_exception(superseded_dir) else "respawn"
        return "foreign"
    reason = session.get("failure_reason")
    if reason in RESPAWN_REASONS:
        return "respawn"
    if reason in SYSTEMIC_REASONS and not session.get("genuine_assistant_turns"):
        return "respawn"
    return "done"


def supersede(t_dir, runs_root, run_id) -> Path:
    """Move a row out of the run directory so no report or judge pass sees it."""
    t_dir = Path(t_dir)
    base = _superseded_dir(runs_root, run_id, t_dir.parent.name, t_dir.name)
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base / str(n)).exists():
        n += 1
    dest = base / str(n)
    shutil.move(str(t_dir), str(dest))
    return dest


def settlement_actual_from_row(t_dir) -> Optional[float]:
    """Settlement for an attempt whose result object never came back, read
    from the files it left behind."""
    metrics = _read_json(Path(t_dir) / "metrics.json")
    if metrics is None:
        return None
    session = read_session_json(t_dir) or {}
    cost = session.get("cost_runtime_usd", metrics.get("cost_runtime_usd"))
    return paired_spend.settlement_actual(
        types.SimpleNamespace(extra=metrics, cost_runtime_usd=cost)
    )


def _classify_stop(message: str) -> str:
    return "spend-ceiling" if message.startswith("spend-ceiling") else "authorised-ceiling"


def final_recheck(plan: PairedPlan, outcome: PairedOutcome, cells=None) -> None:
    """Examine each arm once more after its last session, which no later spawn would check.

    `cells` defaults to the arms that ran a session in this invocation.
    """
    if cells is None:
        cells = [c for c in PAIR_ORDER if any(r["cell"] == c for r in outcome.rows)]
    for cell in cells:
        recheck = plan.identity_recheck.get(cell)
        problems = recheck() if recheck else []
        if problems:
            outcome.stop_reason = "abort"
            outcome.abort_detail = list(problems)
            outcome.abort_cell = cell
            outcome.abort_task_id = None
            outcome.abort_kind = "recheck"
            prior = [r["task_id"] for r in outcome.rows if r["cell"] == cell]
            outcome.abort_prior_task_id = prior[-1] if prior else None
            return


def run_paired(plan: PairedPlan, *, run_task=runner.run_one_task, log=print) -> PairedOutcome:
    outcome = _run_pairs(plan, run_task=run_task, log=log)
    if outcome.abort_kind is None:
        final_recheck(plan, outcome)
    return outcome


def _run_pairs(plan: PairedPlan, *, run_task, log) -> PairedOutcome:
    for config in plan.configs.values():
        runner._check_spend_critical_fields_threadable(config)

    outcome = PairedOutcome()
    tracker_total = 0.0
    runs_root = Path(plan.run_dir)

    def t_dir_for(cell, task_id):
        return runs_root / plan.run_id / cell / task_id

    for task in plan.tasks:
        task_id = task["id"]
        todo = []
        for cell in PAIR_ORDER:
            t_dir = t_dir_for(cell, task_id)
            sup = _superseded_dir(runs_root, plan.run_id, cell, task_id)
            state = row_state(t_dir, superseded_dir=sup)
            if state == "foreign":
                raise PairedRefusal(
                    f"existing row for {cell}/{task_id} is not from a paired run: {t_dir}"
                )
            if state == "done" and (t_dir / "metrics.json").is_file() and \
                    read_session_json(t_dir) is None:
                log(
                    f"harness exception repeated for {cell}/{task_id}; kept as error: "
                    f"{t_dir} and {sup}"
                )
            if state == "respawn":
                supersede(t_dir, runs_root, plan.run_id)
            if state in ("absent", "respawn"):
                todo.append(cell)
        if not todo:
            continue

        ok, message = paired_spend.pair_precheck(
            plan.ledger, plan.run_id, plan.cap, len(todo), plan.authorised, plan.suite_ceiling
        )
        if not ok:
            outcome.stop_reason = _classify_stop(message)
            outcome.abort_detail = [message]
            return outcome

        for cell in todo:
            recheck = plan.identity_recheck.get(cell)
            problems = recheck() if recheck else []
            if problems:
                outcome.stop_reason = "abort"
                outcome.abort_detail = list(problems)
                outcome.abort_cell = cell
                outcome.abort_task_id = task_id
                outcome.abort_kind = "recheck"
                prior = [r["task_id"] for r in outcome.rows if r["cell"] == cell]
                outcome.abort_prior_task_id = prior[-1] if prior else None
                return outcome
            ok, message = paired_spend.spawn_precheck(plan.ledger, plan.cap, plan.authorised)
            if not ok:
                outcome.stop_reason = "authorised-ceiling"
                outcome.abort_detail = [message]
                return outcome

            config = dataclasses.replace(
                plan.configs[cell],
                budget=BudgetSpec(
                    wall_clock_seconds=resolve_wall_clock(plan.wall_clock_flag, task),
                    max_retries=0,
                ),
            )
            attempt_id = paired_spend.reserve(plan.ledger, plan.run_id, task_id, cell, plan.cap)
            result = None
            try:
                result = run_task(cell, task, plan.run_id, config)
            finally:
                actual = (
                    paired_spend.settlement_actual(result)
                    if result is not None
                    else settlement_actual_from_row(t_dir_for(cell, task_id))
                )
                paired_spend.settle(
                    plan.ledger, attempt_id, plan.run_id, task_id, cell, plan.cap, actual
                )
            tracker_total += float(actual) if actual is not None else plan.cap
            extra = getattr(result, "extra", None) or {}
            reason = extra.get("failure_reason")
            outcome.rows.append({
                "cell": cell,
                "task_id": task_id,
                "verdict": result.verdict,
                "failure_reason": reason,
                "attempt_id": attempt_id,
                "actual_usd": actual,
            })
            if reason in ABORT_REASONS:
                outcome.stop_reason = "abort"
                outcome.abort_detail = [reason]
                outcome.abort_cell = cell
                outcome.abort_task_id = task_id
                outcome.abort_kind = "row"
                return outcome
            if plan.stop_on_invalid_quoin and cell == QUOIN_CELL and reason in INVALID_REASONS:
                outcome.stop_reason = "invalid-quoin-row"
                outcome.abort_detail = [reason]
                outcome.abort_cell = cell
                outcome.abort_task_id = task_id
                outcome.abort_kind = "row"
                return outcome
        outcome.pairs_completed += 1
        if plan.usd_cap is not None and tracker_total > plan.usd_cap:
            outcome.stop_reason = "usd-cap"
            return outcome
    return outcome


def judge_phase(plan: PairedPlan, suite_tasks, snapshot_revision, judge=None) -> list:
    return runner.judge_pending_rows(
        plan.run_dir,
        plan.run_id,
        suite_tasks,
        snapshot_revision=snapshot_revision,
        judge=judge or runner.judge_task,
    )
