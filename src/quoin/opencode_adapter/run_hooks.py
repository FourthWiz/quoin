"""Hook points that cost a single-phase run and record its workflow entry.

``quoin run --phase`` calls these around the phase loop, under the task lock:
``before_run`` takes the ledger mark, ``open_run`` names the run a new
invocation could supersede, ``prelaunch`` builds the callback that writes an
end-of-task row before launch, and ``after_phase_run`` costs the run, closes a
superseded one, and records a ``phase-run`` workflow entry for the gate.

Every step is guarded: a failure is stored in the run record as a hook error
and never raised, and never changes the run's outcome (a rewritten ledger is
the one exception, handled by the caller from ``HookOutcome.violation``).
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import boundaries, cost, evidence, gate, runstore, testrun

_CRITIC_RE = re.compile(r"^critic-response-(\d+)\.md$")
_REVIEW_RE = re.compile(r"^review-(\d+)\.md$")
_COMPOSED_ORIGINS = ("phase-run", "coordinator")


@dataclass
class HookOutcome:
    violation: bool = False
    entry_recorded: bool = False
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# read-only helpers
# ---------------------------------------------------------------------------


def before_run(project_root: Any, task: str, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """The ledger mark taken before the phase runs. Never raises."""
    return cost.ledger_mark(project_root, task, clock)


def pointer_run(project_root: Any, task: str) -> Optional[str]:
    """The run id the task's pointer names now; None on any store error."""
    try:
        directory = runstore.inspect_store(project_root)
        pointer = runstore.load_pointer(directory, task) if directory is not None else None
    except Exception:  # noqa: BLE001
        return None
    run_id = pointer.get("run_id") if isinstance(pointer, Mapping) else None
    return run_id if isinstance(run_id, str) else None


def _load(project_root: Any, run_id: str) -> Optional[Dict[str, Any]]:
    try:
        directory = runstore.inspect_store(project_root)
        return runstore.load_record(directory, run_id) if directory is not None else None
    except Exception:  # noqa: BLE001
        return None


def open_run(project_root: Any, task: str) -> Optional[str]:
    """The pointed-at run id when that run is still open, else None."""
    run_id = pointer_run(project_root, task)
    record = _load(project_root, run_id) if run_id else None
    return run_id if record is not None and cost.is_open(record) else None


def before_boundary(
    project_root: Any, task: str, clock: Optional[Callable[[], float]] = None,
) -> Optional[boundaries.Listing]:
    """The boundary listing taken before the phase runs, or None when the task
    folder is absent or the listing cannot be taken. Never raises."""
    try:
        if not cost.task_folder_present(project_root, task):
            return None
        return boundaries.take_listing(project_root, clock=clock)
    except Exception:  # noqa: BLE001 - a missing listing only leaves the boundary unrecorded
        return None


def prelaunch(
    project_root: Any, mark: Optional[Mapping[str, Any]], source_dir: Any,
    clock: Callable[[], float] = time.time,
) -> Callable[[Any], None]:
    """The callback for the phase loop's pre-launch seam."""

    def callback(prepared: Any) -> None:
        try:
            cost.prelaunch_row(project_root, prepared, source_dir=source_dir, mark=mark, clock=clock)
        except Exception as exc:  # noqa: BLE001
            _note_error(project_root, getattr(prepared, "run_id", None), "prelaunch", exc)

    return callback


def _note_error(project_root: Any, run_id: Optional[str], where: str, exc: BaseException) -> None:
    if not run_id:
        return
    try:
        directory = runstore.inspect_store(project_root)
        record = runstore.load_record(directory, run_id) if directory is not None else None
        if record is None:
            return
        telemetry = record.get("telemetry")
        if not isinstance(telemetry, dict):
            telemetry = {}
            record["telemetry"] = telemetry
        telemetry["hook_error"] = "%s: %s" % (where, type(exc).__name__)
        runstore.write_record(directory, record)
    except Exception:  # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# output detection
# ---------------------------------------------------------------------------


def _posix_rel(project_root: Any, path: Path) -> str:
    return os.path.relpath(str(path), str(project_root)).replace(os.sep, "/")


def _stage_folder(project_root: Any, task: str, stage: Optional[int], source_dir: Any) -> Tuple[Optional[Path], Optional[str]]:
    if stage is None:
        return gate.task_root(project_root, task), None
    if source_dir is None:
        return None, "stage-unresolved"
    try:
        return gate.stage_dir(project_root, task, stage, source_dir), None
    except Exception:  # noqa: BLE001 - any resolver failure fails closed
        return None, "stage-unresolved"


def _skip_marker_in(folder_rel: str, hashes: Mapping[str, Any]) -> bool:
    prefix = folder_rel + "/"
    return any(
        isinstance(value, dict) and (key == "<truncated>" or key.startswith(prefix))
        for key, value in hashes.items()
    )


def detect_outputs(
    record: Mapping[str, Any], project_root: Any, task: str, stage: Optional[int], source_dir: Any,
    pattern: "re.Pattern[str]",
) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
    """`([(n, rel_path, sha256)], error)`: the numbered files the run created or
    changed directly under the stage folder, from the first attempt's before
    hashes and the last attempt's after hashes. Incomplete evidence records
    nothing and names why."""
    folder, error = _stage_folder(project_root, task, stage, source_dir)
    if folder is None:
        return [], error
    attempts = [a for a in record.get("attempts") or [] if isinstance(a, Mapping) and a.get("state") != "staged"]
    if not attempts:
        return [], "hashes-incomplete"
    before, after = attempts[0].get("input_hashes_before"), attempts[-1].get("input_hashes_after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return [], "hashes-incomplete"
    folder_rel = _posix_rel(project_root, folder)
    if "<truncated>" in before or "<truncated>" in after:
        return [], "hashes-incomplete"
    if _skip_marker_in(folder_rel, before) or _skip_marker_in(folder_rel, after):
        return [], "hashes-incomplete"
    found: List[Tuple[int, str, str]] = []
    for rel, digest in after.items():
        if not isinstance(digest, str) or rel.rsplit("/", 1)[0] != folder_rel:
            continue
        match = pattern.match(rel.rsplit("/", 1)[-1])
        if match and before.get(rel) != digest:
            found.append((int(match.group(1)), rel, digest))
    return sorted(found), None


# ---------------------------------------------------------------------------
# after the run
# ---------------------------------------------------------------------------


def _write_evidence_note(project_root: Any, run_id: str, note: Mapping[str, Any]) -> None:
    directory = runstore.inspect_store(project_root)
    record = runstore.load_record(directory, run_id) if directory is not None else None
    if record is None:
        return
    telemetry = record.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
        record["telemetry"] = telemetry
    telemetry["evidence"] = dict(note)
    runstore.write_record(directory, record)


def _write_boundary_note(project_root: Any, run_id: str, result: "boundaries.BoundaryResult") -> None:
    directory = runstore.inspect_store(project_root)
    record = runstore.load_record(directory, run_id) if directory is not None else None
    if record is None:
        return
    telemetry = record.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
        record["telemetry"] = telemetry
    telemetry["boundary"] = {
        "status": result.status, "reason": result.reason,
        "violations": [dict(v) for v in result.violations],
    }
    runstore.write_record(directory, record)


def _last_attempt_number(record: Mapping[str, Any]) -> Optional[int]:
    numbers = [
        a.get("attempt") for a in record.get("attempts") or []
        if isinstance(a, Mapping) and a.get("state") != "staged"
        and isinstance(a.get("attempt"), int) and not isinstance(a.get("attempt"), bool)
    ]
    return max(numbers) if numbers else None


_MAX_RESULT_BYTES = 256 * 1024


def _read_test_result(
    state_root: Any, project_root: Any, task: str, stage: Optional[int], record: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """The stage's stored test result when it passed and belongs to this run's
    last attempt; otherwise None. The match is on the run id and attempt
    number, never on timestamps from different clocks."""
    if state_root is None:
        return None
    try:
        path = testrun.result_path(state_root, project_root, task, stage)
        info = os.lstat(str(path))
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RESULT_BYTES:
            return None
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, runstore.RunStoreError):
        return None
    if not isinstance(data, dict) or data.get("outcome") != testrun.PASSED:
        return None
    attempt = _last_attempt_number(record)
    if data.get("run_id") != record.get("run_id") or attempt is None or data.get("attempt") != attempt:
        return None
    return data


def _prefix_changed(project_root: Any, run_id: str) -> bool:
    record = _load(project_root, run_id)
    telemetry = (record or {}).get("telemetry")
    ledger = telemetry.get("ledger") if isinstance(telemetry, Mapping) else None
    return isinstance(ledger, Mapping) and ledger.get("prefix") == "changed"


def _dedup(items: List[Any]) -> List[Any]:
    seen = set()
    out = []
    for item in items:
        key = repr(sorted(item.items())) if isinstance(item, dict) else repr(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _record_entry(
    project_root: Any, task: str, record: Mapping[str, Any], outcome: cost.CostOutcome,
    source_dir: Any, clock: Callable[[], float],
    boundary: Optional["boundaries.BoundaryResult"] = None, state_root: Any = None,
) -> Dict[str, Any]:
    """Compose and record the run's workflow entry; returns the telemetry note."""
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    run_phase = runstore.normalize_phase(request.get("phase"))
    entry_phase = runstore.entry_phase_for_run(run_phase)
    note: Dict[str, Any] = {"recorded": False, "entry_phase": entry_phase, "stage": None, "reason": None}
    if entry_phase is None:
        note["reason"] = "phase-not-gated"
        return note
    try:
        stage = runstore.normalize_stage(request.get("stage"))
    except ValueError:
        note["reason"] = "stage-invalid"
        return note
    note["stage"] = stage
    if entry_phase in runstore.STAGELESS_PHASES and stage is not None:
        note["reason"] = "stage-not-allowed"
        return note
    if not cost.task_folder_present(project_root, task):
        note["reason"] = "task-folder-missing"
        return note

    run_id = record["run_id"]
    produced: List[Tuple[int, str, str]] = []
    outputs_error: Optional[str] = None
    if run_phase in ("plan", "thorough_plan", "critic"):
        produced, outputs_error = detect_outputs(record, project_root, task, stage, source_dir, _CRITIC_RE)
    elif entry_phase == "review":
        produced, outputs_error = detect_outputs(record, project_root, task, stage, source_dir, _REVIEW_RE)

    directory = runstore.store_dir(project_root, create=True)
    state = runstore.load_workflow_state(directory, task)
    live = runstore.current_entry(state, stage, entry_phase) if state is not None else None

    runs: List[str] = [run_id]
    critic_responses: List[str] = [rel for _n, rel, _d in produced] if run_phase != "review" else []
    ledger_uuids: List[Any] = []
    appended: List[Any] = list(outcome.appended)
    copied_violation = False
    if run_phase == "critic" and live is not None and live.get("origin") in _COMPOSED_ORIGINS:
        runs = [r for r in live.get("runs") or [] if isinstance(r, str)] + [run_id]
        critic_responses = [r for r in live.get("critic_responses") or [] if isinstance(r, str)] + critic_responses
        ledger_uuids = list(live.get("ledger_uuids") or [])
        appended = list(live.get("ledger_lines_appended_during_run") or []) + appended
        copied_violation = live.get("boundary") == "violation"
    if outcome.row in ("written", "present"):
        ledger_uuids.append(run_id)
    violation = outcome.violation or copied_violation or any(
        _prefix_changed(project_root, listed) for listed in runs if listed != run_id)
    harvested: List[Dict[str, str]] = []
    if entry_phase == "review" and produced:
        _n, rel, digest = produced[-1]
        harvested = [{"path": rel, "sha256": digest}]

    snapshot = evidence.take_snapshot(project_root, task, entry_phase)
    boundary_value: Optional[str] = "violation" if violation else None
    extra: Dict[str, Any] = {}
    if boundary is not None:
        if boundary.status == "violation":
            boundary_value = "violation"
        elif boundary.status == "ok" and not violation:
            boundary_value = "ok"
        if boundary_value is None:
            extra["boundary_reason"] = boundary.reason
    if entry_phase == "implement":
        tests = _read_test_result(state_root, project_root, task, stage, record)
        if tests is not None:
            extra["tests"] = tests
    fields: Dict[str, Any] = {
        "ledger_uuids": _dedup(ledger_uuids),
        "ledger_lines_appended_during_run": _dedup(appended),
        "outputs_recorded": True,
        "critic_responses": critic_responses,
        "harvested": harvested,
    }
    if outputs_error is not None:
        fields["outputs_error"] = outputs_error
    if record.get("resume_blocked"):
        fields["resume_blocked"] = record["resume_blocked"]
    evidence.record_evidence(
        project_root, task, stage, entry_phase, "phase-run", snapshot, runs=runs,
        boundary=boundary_value, clock=clock, **extra, **fields,
    )
    note["recorded"] = True
    if outputs_error is not None:
        note["reason"] = outputs_error
    return note


def _boundary_verdict(
    project_root: Any, task: str, record: Mapping[str, Any], run_id: str,
    before: Optional[boundaries.Listing], after: Optional[boundaries.Listing],
    superseded_candidate: Optional[str],
) -> Optional["boundaries.BoundaryResult"]:
    """The boundary result for a closed run, or None when no check applies."""
    if before is None or after is None:
        return None
    key = boundaries.role_key(record)
    if key is None:
        return None
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    try:
        stage = runstore.normalize_stage(request.get("stage"))
    except ValueError:
        stage = None
    return boundaries.verify(
        key, before, after, task=task, run_id=run_id, prior_run_id=superseded_candidate,
        stage_rel=None if stage is None else "stage-%d" % stage,
        window_partial=superseded_candidate == run_id, other_task_policy="unverified",
    )


def after_phase_run(
    project_root: Any, task: str, result: Any, *, mark: Optional[Mapping[str, Any]], source_dir: Any,
    superseded_candidate: Optional[str], clock: Callable[[], float] = time.time,
    boundary_before: Optional[boundaries.Listing] = None, state_root: Any = None,
) -> HookOutcome:
    """Cost the run, close a superseded one, and record the run's entry.

    With `boundary_before` the role boundary is also checked: the second
    listing is the first thing taken, before any hook write, and the entry
    records `violation`, `ok` or no result with the reason. The window is
    partial (never `ok`) when the run was already open at the first listing.
    `state_root` is where the stage's test result is read from. The caller
    holds the task lock. Never raises."""
    out = HookOutcome()
    run_id = getattr(result, "run_id", None)
    own: List[str] = []
    boundary_after: Optional[boundaries.Listing] = None
    if boundary_before is not None:
        try:
            boundary_after = boundaries.take_listing(project_root, clock=clock)
        except Exception:  # noqa: BLE001
            boundary_after = None

    # supersession: the pointer moved away from a run that was still open
    try:
        candidate = superseded_candidate
        if candidate and candidate != run_id:
            successor = pointer_run(project_root, task)
            old = _load(project_root, candidate)
            if successor and successor != candidate and old is not None and cost.is_open(old):
                cost.record_run(
                    project_root, candidate, source_dir=source_dir, mark=None, ended_as="superseded",
                    superseded_by=successor, clock=clock,
                )
                own.append(candidate)
    except Exception as exc:  # noqa: BLE001
        _note_error(project_root, run_id or superseded_candidate, "supersession", exc)
    if not run_id:
        out.reason = "no-run"
        return out

    try:
        record = _load(project_root, run_id)
        if record is None:
            out.reason = "record-missing"
            return out
        blocked = getattr(result, "resume_blocked", None)
        if cost.is_open(record, blocked):
            cost.store_mark(project_root, run_id, mark)
            out.reason = "run-open"
            return out
        outcome = cost.record_run(
            project_root, run_id, source_dir=source_dir, mark=mark,
            ended_as=str(getattr(result, "outcome", "ended")).lower(), resume_blocked=blocked,
            own_uuids=own, clock=clock,
        )
        out.violation = bool(outcome.violation)
        if not outcome.closed:
            out.reason = outcome.row_reason
            return out
        from_stored = outcome.from_stored
    except Exception as exc:  # noqa: BLE001
        _note_error(project_root, run_id, "cost", exc)
        return out

    try:
        if from_stored:
            # an earlier invocation already closed and costed this run: report the
            # stored outcome and record nothing new
            stored = _load(project_root, run_id) or record
            note_now = (stored.get("telemetry") or {}).get("evidence") or {}
            out.entry_recorded = note_now.get("recorded") is True
            out.reason = note_now.get("reason")
            stored_boundary = (stored.get("telemetry") or {}).get("boundary") or {}
            if isinstance(stored_boundary, Mapping) and stored_boundary.get("status") == "violation":
                out.violation = True
            return out
        record = _load(project_root, run_id) or record
        telemetry = record.get("telemetry") if isinstance(record.get("telemetry"), Mapping) else {}
        if (telemetry.get("evidence") or {}).get("recorded") is True:
            out.entry_recorded = True
            return out
        verdict = _boundary_verdict(
            project_root, task, record, run_id, boundary_before, boundary_after, superseded_candidate)
        if verdict is not None:
            if verdict.status == "violation":
                out.violation = True
            try:
                _write_boundary_note(project_root, run_id, verdict)
            except Exception as exc:  # noqa: BLE001
                _note_error(project_root, run_id, "boundary", exc)
        if getattr(result, "outcome", None) == "REFUSED":
            note: Dict[str, Any] = {"recorded": False, "entry_phase": None, "stage": None, "reason": "refused"}
        else:
            note = _record_entry(
                project_root, task, record, outcome, source_dir, clock, boundary=verdict, state_root=state_root)
        out.entry_recorded = bool(note.get("recorded"))
        out.reason = note.get("reason")
        _write_evidence_note(project_root, run_id, note)
    except Exception as exc:  # noqa: BLE001
        _note_error(project_root, run_id, "evidence", exc)
    return out
