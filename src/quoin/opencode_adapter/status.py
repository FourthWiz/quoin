"""Read-only report on the latest OpenCode phase run of a task.

Nothing here writes, repairs, reconciles or signals anything. Liveness comes
from one process-table snapshot taken per call: a pid is alive when it is a
live entry of the table, and when the table cannot be read liveness is
reported as unknown (`None`) rather than guessed. The module never imports
the CLI; the caller supplies a `lock_reader` for the task lock.
"""
from __future__ import annotations

import shlex
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import proctree, runstore

REPORT_KEYS = (
    "runtime", "task", "run_id", "state", "display_state", "phase", "stage",
    "profile", "reason", "resume_blocked", "resume_hint", "refusal", "attempts",
    "child", "sidecar", "torn_tail", "last_event", "lock", "superseded_running",
    "updated_at",
)

LockReader = Callable[[str], Optional[Mapping[str, Any]]]


class StatusError(Exception):
    """The status request could not be answered; the CLI exits 2 with the text."""


def _is_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _lock_view(lock: Optional[Mapping[str, Any]], table: Optional[Mapping[int, Any]]) -> Dict[str, Any]:
    if lock is None:
        return {"present": False, "pid": None, "runtime": None, "alive": None, "stale": None}
    pid = lock.get("pid")
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    runtime = lock.get("runtime")
    runtime = runtime if isinstance(runtime, str) else None
    alive = runstore.pid_alive(pid, table) if pid is not None else (None if table is None else False)
    if table is None or runtime is None:
        stale = None
    else:
        stale = runtime == "opencode" and alive is False
    return {"present": True, "pid": pid, "runtime": runtime, "alive": alive, "stale": stale}


def _empty(task: Optional[str], lock: Dict[str, Any]) -> Dict[str, Any]:
    report: Dict[str, Any] = {key: None for key in REPORT_KEYS}
    report.update(
        runtime="opencode", task=task, state="none", display_state="no run",
        attempts=0, torn_tail=False, lock=lock, superseded_running=[],
        child={"pid": None, "pgid": None, "alive": None},
    )
    return report


def _last_attempt(record: Mapping[str, Any]) -> Mapping[str, Any]:
    attempts = record.get("attempts") or []
    last = attempts[-1] if attempts else None
    return last if isinstance(last, Mapping) else {}


def _reason(record: Mapping[str, Any]) -> Optional[str]:
    history = record.get("history") or []
    last = history[-1] if history and isinstance(history[-1], Mapping) else {}
    reason = last.get("reason")
    return reason if isinstance(reason, str) else None


def _redact_all(value: Any, redact: Callable[[Any], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_all(item, redact) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_all(item, redact) for item in value]
    return value


def collect(
    project_root: Any,
    *,
    task: Optional[str] = None,
    run_id: Optional[str] = None,
    lock_reader: Optional[LockReader] = None,
    proc: Any = proctree,
) -> Dict[str, Any]:
    """The status report for a task (its latest run) or one run id."""
    from . import launch_env  # noqa: PLC0415

    try:
        if run_id is not None:
            runstore.check_run_id(run_id)
        else:
            runstore.check_task_name(task or "")
        directory = runstore.inspect_store(project_root)
        record: Optional[Dict[str, Any]] = None
        if run_id is not None:
            if directory is not None:
                record = runstore.load_record(directory, run_id)
            task = record.get("task") if record else None
        elif directory is not None:
            pointer = runstore.load_pointer(directory, task)
            if pointer and isinstance(pointer.get("run_id"), str):
                record = runstore.load_record(directory, pointer["run_id"])
    except runstore.RunStoreError as exc:
        raise StatusError("the run store cannot be read (%s)" % exc.code) from None
    except OSError as exc:
        raise StatusError("the run store cannot be read (%s)" % type(exc).__name__) from None

    table = proc.snapshot()
    lock = _lock_view(lock_reader(task) if (lock_reader and task) else None, table)
    redact = launch_env.Redactor()
    if record is None:
        return _redact_all(_empty(task, lock), redact)

    state = record.get("state")
    driver_lost = table is not None and runstore.orphan_state(record, table) == "driver-lost"
    if state == "running":
        display = "running (driver lost)" if driver_lost else "running"
    else:
        display = state
    last = _last_attempt(record)
    child_alive = None if table is None else bool(runstore.live_identities(record, table))
    paths = runstore.run_paths(directory, record["run_id"])
    event, torn = runstore.last_event(paths.sidecar)
    others, _skipped = runstore.list_records(directory, task)
    superseded: List[Dict[str, Any]] = []
    for other in sorted(others, key=lambda r: str(r.get("created_at")), reverse=True):
        if other.get("state") != "running" or other.get("run_id") == record.get("run_id"):
            continue
        other_last = _last_attempt(other)
        superseded.append({
            "run_id": other.get("run_id"), "pid": other_last.get("pid"),
            "pgid": other_last.get("pgid"),
            "driver_lost": table is not None and runstore.orphan_state(other, table) == "driver-lost",
        })
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    report: Dict[str, Any] = {
        "runtime": "opencode",
        "task": record.get("task"),
        "run_id": record.get("run_id"),
        "state": state,
        "display_state": display,
        "phase": request.get("phase"),
        "stage": request.get("stage"),
        "profile": request.get("profile"),
        "reason": _reason(record),
        "resume_blocked": record.get("resume_blocked"),
        "resume_hint": record.get("resume_hint"),
        "refusal": record.get("refusal"),
        "attempts": len(record.get("attempts") or []),
        "child": {"pid": last.get("pid"), "pgid": last.get("pgid"), "alive": child_alive},
        "sidecar": str(paths.sidecar),
        "torn_tail": torn,
        "last_event": None if event is None else {
            "type": event.type.value, "sequence": event.sequence,
            "attempt": event.attempt, "observed_at": event.observed_at,
        },
        "lock": lock,
        "superseded_running": superseded,
        "updated_at": record.get("updated_at"),
    }
    return _redact_all(report, redact)


def _kill_hints(pid: Any, pgid: Any) -> List[str]:
    lines: List[str] = []
    if _is_pid(pgid):
        lines.append("  check: ps -o pid,command -g %d; stop: kill -TERM -%d" % (pgid, pgid))
    elif _is_pid(pid):
        lines.append("  check: ps -p %d; stop: kill -TERM %d" % (pid, pid))
    return lines


def _flag(value: Any) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def render_text(report: Mapping[str, Any]) -> str:
    """A short human report that names the remedy for each stuck condition."""
    lines: List[str] = []
    task = report.get("task")
    if report.get("state") == "none":
        lines.append("no OpenCode run recorded%s" % (" for task %s" % task if task else ""))
    else:
        lines.append("task: %s" % task)
        lines.append("run: %s" % report.get("run_id"))
        lines.append("state: %s" % report.get("display_state"))
        lines.append(
            "phase: %s  stage: %s  profile: %s  attempts: %s"
            % (report.get("phase"), report.get("stage"), report.get("profile"), report.get("attempts"))
        )
        if report.get("reason"):
            lines.append("reason: %s" % report["reason"])
        child = report.get("child") or {}
        lines.append(
            "child: pid %s, group %s, alive %s"
            % (child.get("pid"), child.get("pgid"), _flag(child.get("alive")))
        )
        event = report.get("last_event")
        if event:
            lines.append("last event: %s #%s at %s" % (event["type"], event["sequence"], event["observed_at"]))
        if report.get("torn_tail"):
            lines.append("the event sidecar ends in an incomplete line (left as is)")
        lines.append("sidecar: %s" % report.get("sidecar"))
    lock = report.get("lock") or {}
    if lock.get("present"):
        lines.append(
            "lock: runtime %s, pid %s, alive %s"
            % (lock.get("runtime") or "unknown", lock.get("pid"), _flag(lock.get("alive")))
        )
        if lock.get("stale"):
            lines.append(
                "  the lock names an OpenCode run that is gone: re-run the phase with "
                "quoin run --runtime opencode ... (it reclaims a dead lock), or remove the lock file"
            )
    elif report.get("task"):
        lines.append("lock: none")
    if report.get("display_state") == "running (driver lost)":
        child = report.get("child") or {}
        lines.append("the quoin run that drove this phase has exited; its child may still be running")
        lines.extend(_kill_hints(child.get("pid"), child.get("pgid")))
        lines.append("  then start over with --new-run")
    for other in report.get("superseded_running") or []:
        lines.append(
            "older run %s still reads running%s" % (other["run_id"], " (driver lost)" if other.get("driver_lost") else "")
        )
        lines.extend(_kill_hints(other.get("pid"), other.get("pgid")))
        lines.append("  then start over with --new-run")
    if report.get("resume_blocked"):
        lines.append(
            "resume is blocked (%s): start the phase over with --new-run" % report["resume_blocked"]
        )
    if report.get("resume_hint"):
        lines.append("next: %s" % report["resume_hint"])
    return "\n".join(lines) + "\n"
