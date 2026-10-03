"""Supervision of one workflow phase on the OpenCode runtime.

``run_phase`` drives the driver's attempts for a single phase: it picks a
fresh run or resumes an interrupted one, relaunches interrupted attempts under
one budget, retries failed attempts under the retry policy's budget, stops
when two attempts in a row make no progress, and honours a cooperative cancel.
It never reads or writes the done or halt sentinels; the caller owns the task
lock and the sentinel files.

This module never imports ``quoin.cli``. ``quoin.supervisor`` is imported
lazily, and only to reach the default backoff.
"""
from __future__ import annotations

import os
import shlex
import signal as _signal
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import driver, launch_env, retry, runstore

OUTCOMES: Tuple[str, ...] = (
    "COMPLETED",
    "COMPLETED_UNVERIFIED",
    "AWAITING_APPROVAL",
    "CANCELLED",
    "FAILED",
    "ABORTED",
    "INTERRUPTED",
    "REFUSED",
    "ERROR",
)

# Block reasons the phase loop itself reports; the driver owns its own set.
LOOP_BLOCK_REASONS: Tuple[str, ...] = ("checkpoint-invalid",)

_WAIT_SLICE_S = 0.2

_EXIT_CODES = {
    "COMPLETED": 0,
    "COMPLETED_UNVERIFIED": 6,
    "AWAITING_APPROVAL": 4,
    "INTERRUPTED": 5,
    "REFUSED": 3,
    "FAILED": 2,
    "ABORTED": 2,
    "ERROR": 2,
}


class CancelToken:
    """A cancel flag that is safe to set from a signal handler.

    The flag and the signal number are plain attributes: a handler running
    between two bytecodes of the main thread can never block on a lock the
    interrupted code holds. Waiting is a sliced sleep, so a cancel is noticed
    within one slice.
    """

    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flag = False
        self.signum: Optional[int] = None
        self._handle: Any = None
        self._sleep = sleep
        self._monotonic = monotonic

    def cancel(self, signum: Optional[int] = None) -> None:
        if self.signum is None and signum is not None:
            self.signum = signum
        if self._flag:
            # A second signal must not re-enter the handle's cancel path.
            return
        self._flag = True
        handle = self._handle
        if handle is not None:
            handle.request_cancel()

    def is_set(self) -> bool:
        return self._flag

    def wait(self, seconds: float) -> bool:
        end = self._monotonic() + seconds
        while not self._flag:
            remaining = end - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(_WAIT_SLICE_S, remaining))
        return self._flag

    def attach(self, handle: Any) -> None:
        self._handle = handle
        if self._flag:
            handle.request_cancel()

    def detach(self) -> None:
        self._handle = None


@dataclass(frozen=True)
class PhaseResult:
    outcome: str
    reason: Optional[str] = None
    run_id: Optional[str] = None
    run_state: Optional[str] = None
    evidence: Optional[str] = None
    resume_blocked: Optional[str] = None
    attempts: int = 0
    refusal: Optional[Dict[str, Optional[str]]] = None
    sidecar: Optional[str] = None
    artifact_coverage: str = "unknown"
    signum: Optional[int] = None
    superseded_run: Optional[Dict[str, Any]] = None


def artifact_coverage(record: Optional[Mapping[str, Any]]) -> str:
    """Whether the artifact events saw the whole tree.

    ``partial`` when any input snapshot hit its size limit and carries the
    truncation marker, ``unknown`` when there is no record to inspect."""
    if not isinstance(record, Mapping):
        return "unknown"

    def truncated(hashes: Any) -> bool:
        return isinstance(hashes, Mapping) and "<truncated>" in hashes

    if truncated(record.get("input_hashes")):
        return "partial"
    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, Mapping):
            continue
        for key, value in attempt.items():
            if str(key).startswith("input_hashes") and truncated(value):
                return "partial"
    return "full"


def annotate_record(
    project_root: Any, run_id: str, hint: str, resume_blocked: Optional[str] = None
) -> None:
    """Store the resume hint (and the block reason, when there is one) in the
    run record. Call only while holding the task lock: the driver takes no
    record lock of its own."""
    directory = runstore.store_dir(project_root)
    record = runstore.load_record(directory, run_id)
    if record is None:
        return
    record["resume_hint"] = hint
    if resume_blocked:
        record["resume_blocked"] = resume_blocked
    runstore.write_record(directory, record)


def _last_attempt(record: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    attempts = (record or {}).get("attempts") or []
    return attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}


def _is_pid(value: Any) -> bool:
    """A real process or group id worth naming in a kill hint: an int above 1
    (0 and 1 would address the caller's group or init)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _stale_run_message(record: Mapping[str, Any]) -> str:
    last = _last_attempt(record)
    pid, pgid = last.get("pid"), last.get("pgid")
    run_id = record.get("run_id")
    task = record.get("task")
    head = (
        "run %s for %s still reads running, but the quoin run that drove it has exited"
        % (run_id, task)
    )
    if _is_pid(pgid) and pgid != os.getpgrp():
        return (
            "%s; its opencode child may still be alive (pid %s, process group %d). "
            "Check with: ps -o pid,command -g %d; stop it with: kill -TERM -%d; "
            "then start over with --new-run" % (head, pid, pgid, pgid, pgid)
        )
    if _is_pid(pid):
        return (
            "%s; its opencode child may still be alive (pid %d). "
            "Check with: ps -p %d; stop it with: kill -TERM %d; "
            "then start over with --new-run" % (head, pid, pid, pid)
        )
    return (
        "%s and no opencode child was recorded; if no opencode process for this task "
        "is alive (ps -ax | grep opencode), start over with --new-run" % head
    )


def _same_request(record: Mapping[str, Any], request: "driver.RunRequest") -> bool:
    stored = record.get("request") or {}
    return (
        stored.get("task") == request.task
        and stored.get("stage") == request.stage
        and str(stored.get("phase", "")).replace("-", "_") == str(request.phase).replace("-", "_")
        and stored.get("profile") == request.profile
    )


def run_phase(
    drv: Any,
    request: "driver.RunRequest",
    *,
    max_relaunch: int,
    cancel: CancelToken,
    new_run: bool = False,
    backoff_fn: Optional[Callable[[int], float]] = None,
    monotonic: Callable[[], float] = time.monotonic,
    default_timeout_s: float = driver.DEFAULT_TIMEOUT_S,
    on_prepared: Optional[Callable[[Any], None]] = None,
) -> PhaseResult:
    t0 = monotonic()
    attempts = interrupted_relaunches = failed_count = no_progress = 0
    run_id: Optional[str] = None
    superseded: Optional[Dict[str, Any]] = None
    evidence: Optional[str] = None
    directory = runstore.store_dir(request.project_root)

    if backoff_fn is None:
        from quoin import supervisor as _supervisor  # noqa: PLC0415

        backoff_fn = _supervisor.default_backoff

    def finish(
        outcome: str,
        reason: Optional[str] = None,
        *,
        resume_blocked: Optional[str] = None,
        refusal: Optional[Dict[str, Optional[str]]] = None,
        run_id_override: Optional[str] = None,
    ) -> PhaseResult:
        rid = run_id_override or run_id
        record = None
        sidecar = None
        if rid is not None:
            try:
                record = runstore.load_record(directory, rid)
            except Exception:  # noqa: BLE001 - the summary must still be built
                record = None
            try:
                sidecar = str(runstore.run_paths(directory, rid).sidecar)
            except Exception:  # noqa: BLE001
                sidecar = None
        return PhaseResult(
            outcome=outcome,
            reason=reason,
            run_id=rid,
            run_state=(record or {}).get("state") if record else None,
            evidence=evidence,
            resume_blocked=resume_blocked,
            attempts=attempts,
            refusal=refusal,
            sidecar=sidecar,
            artifact_coverage=artifact_coverage(record),
            signum=cancel.signum,
            superseded_run=superseded,
        )

    def refused(category: Optional[str], code: str, message: str, rid: Optional[str] = None) -> PhaseResult:
        return finish(
            "REFUSED", code,
            refusal={"category": category, "code": code, "message": message},
            run_id_override=rid,
        )

    if cancel.is_set():
        return finish("CANCELLED", "cancelled")
    existing = drv.reconcile_task(request.task)
    if existing and existing.get("state") == "running":
        # Holding the task lock means the driver that wrote "running" has
        # exited; only an orphaned child could still be alive.
        if not new_run:
            return refused(
                "workflow-validation", "run-in-progress", _stale_run_message(existing),
                existing.get("run_id"),
            )
        last = _last_attempt(existing)
        superseded = {
            "run_id": existing.get("run_id"), "pid": last.get("pid"), "pgid": last.get("pgid"),
        }
    elif (
        not new_run and existing and existing.get("state") == "interrupted"
        and _same_request(existing, request)
        and not (isinstance(existing.get("telemetry"), dict) and existing["telemetry"].get("final") is True)
    ):
        run_id = existing.get("run_id")
    elif (
        not new_run and existing and existing.get("state") == "interrupted"
        and _same_request(existing, request)
    ):
        # A closed (costed) run cannot be resumed; stopping here keeps the
        # explicit restart acknowledgement instead of silently starting over.
        blocked = existing.get("resume_blocked")
        return finish(
            "INTERRUPTED", blocked if isinstance(blocked, str) and blocked else "run-closed",
            resume_blocked=blocked if isinstance(blocked, str) and blocked else "run-closed",
            run_id_override=existing.get("run_id"),
        )

    budget: Optional[float] = None
    policy: Optional[retry.RetryPolicy] = None
    policy_t0: Optional[float] = None

    while True:
        if cancel.is_set():
            return finish("CANCELLED", "cancelled")
        fresh = run_id is None
        prepared = None
        try:
            if fresh:
                prepared = drv.prepare(request)
                run_id = prepared.run_id
                handoff = None
            else:
                handoff = _handoff_for(directory, run_id)
                prepared = drv.prepare(request, resume_run_id=run_id)
            if policy is None:
                policy = prepared.retry if prepared.retry is not None else retry.RetryPolicy.from_limits({})
            if policy_t0 is None:
                policy_t0 = policy.start()
            if budget is None:
                limits = (prepared.policy or {}).get("limits") or {}
                value = limits.get("max_run_seconds")
                budget = float(value) if value is not None else None
            left = budget - (monotonic() - t0) if budget is not None else float("inf")
            if left <= 0:
                return finish("INTERRUPTED", "run-budget")
            deadline = min(left, request.timeout_s or default_timeout_s)
            if cancel.is_set():
                return finish("CANCELLED", "cancelled")
            if fresh:
                if on_prepared is not None:
                    try:
                        on_prepared(prepared)
                    except Exception:  # noqa: BLE001 - the callback records its own errors
                        pass
                handle = drv.start(prepared, deadline_s=deadline)
            else:
                handle = drv.resume(handoff, prepared, deadline_s=deadline)
        except _CheckpointInvalid:
            return finish("INTERRUPTED", "checkpoint-invalid", resume_blocked="checkpoint-invalid")
        except driver.PrepareRefused as exc:
            redact = getattr(getattr(prepared, "launch_env", None), "redactor", None)
            if redact is None:
                redact = launch_env.Redactor()
            return refused(exc.category, exc.code, redact(exc.message), exc.run_id)
        except launch_env.LaunchRefused as exc:
            redact = getattr(getattr(prepared, "launch_env", None), "redactor", None)
            if redact is None:
                redact = launch_env.Redactor()
            return refused(exc.category, exc.code, redact(exc.message))
        except driver.ResumeBlocked as exc:
            return finish("INTERRUPTED", exc.reason, resume_blocked=exc.reason)
        except Exception as exc:  # noqa: BLE001
            if run_id is None:
                raise
            return finish("ERROR", "driver-error: " + type(exc).__name__)

        cancel.attach(handle)
        try:
            for _ in drv.observe(handle):
                pass
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            return finish("ERROR", "driver-error: " + type(exc).__name__)
        finally:
            cancel.detach()
        outcome = handle.outcome
        attempts += 1
        evidence = outcome.evidence

        # 1. terminal states
        if outcome.state == "completed":
            if outcome.evidence == "full":
                return finish("COMPLETED", outcome.reason)
            return finish("COMPLETED_UNVERIFIED", outcome.reason)
        if outcome.state == "awaiting_approval":
            return finish("AWAITING_APPROVAL", outcome.reason)
        if outcome.state == "cancelled":
            return finish("CANCELLED", outcome.reason)
        if outcome.state == "interrupted" and outcome.resume_blocked:
            return finish("INTERRUPTED", outcome.reason, resume_blocked=outcome.resume_blocked)

        # 2. progress guard
        no_progress = no_progress + 1 if outcome.new_native_events == 0 else 0
        if no_progress >= 2:
            return finish("ABORTED", "no forward progress")

        # 3. the budget of this attempt's kind
        if outcome.state == "failed":
            failed_count += 1
            decision = policy.decide(
                failed_count, policy_t0, outcome.failure or retry.Failure("config")
            )
            if isinstance(decision, retry.GiveUp):
                return _give_up(finish, decision.reason)
            delay = decision.delay
        else:
            if (
                outcome.reason == "timeout" and budget is not None
                and monotonic() - t0 >= budget
            ):
                return finish("INTERRUPTED", "run-budget")
            if interrupted_relaunches >= max_relaunch:
                return finish("INTERRUPTED", "relaunch cap")
            interrupted_relaunches += 1
            delay = backoff_fn(interrupted_relaunches)
        if cancel.wait(delay):
            return finish("CANCELLED", "cancelled")


class _CheckpointInvalid(Exception):
    pass


def _handoff_for(directory: Any, run_id: str) -> "driver.Handoff":
    try:
        checkpoint = runstore.load_checkpoint(directory, run_id)
        if checkpoint is None:
            return driver.Handoff(run_id, 0, None, (), False, False)
        return driver.Handoff.from_checkpoint(checkpoint)
    except (ValueError, runstore.RunStoreError):
        raise _CheckpointInvalid() from None


def _give_up(finish: Callable[..., PhaseResult], reason: str) -> PhaseResult:
    if reason == "not-transient":
        return finish("FAILED", "not-transient")
    if reason == "mutating-tool-in-flight":
        return finish("FAILED", "effect-uncertain")
    if reason == "attempts-exhausted":
        return finish("ABORTED", "relaunch cap")
    return finish("ABORTED", "run budget")


def exit_code(result: PhaseResult) -> int:
    if result.outcome == "CANCELLED":
        return 130 if result.signum == _signal.SIGINT else 143
    return _EXIT_CODES[result.outcome]


def resume_hint(result: PhaseResult, ident: Mapping[str, Any], project_root: Any) -> Optional[str]:
    """The command that continues or restarts the phase; none when the phase
    completed or was refused before any run started."""
    if result.outcome in ("COMPLETED", "REFUSED"):
        return None
    parts = [
        "quoin", "run", "--runtime", "opencode",
        "--profile", shlex.quote(str(ident["profile"])),
        "--phase", shlex.quote(str(ident["phase"])),
    ]
    if ident.get("stage") is not None:
        parts += ["--stage", shlex.quote(str(ident["stage"]))]
    parts += [shlex.quote(str(ident["task"])), "--project-root", shlex.quote(str(project_root))]
    resumable = result.run_state == "interrupted" and result.resume_blocked is None
    if not resumable:
        parts.append("--new-run")
    return " ".join(parts)


def summary(result: PhaseResult, ident: Mapping[str, Any], hint: Optional[str]) -> Dict[str, Any]:
    return {
        "runtime": "opencode",
        "task": ident.get("task"),
        "stage": ident.get("stage"),
        "phase": ident.get("phase"),
        "profile": ident.get("profile"),
        "outcome": result.outcome,
        "exit_code": exit_code(result),
        "run_state": result.run_state,
        "evidence": result.evidence,
        "reason": result.reason,
        "resume_blocked": result.resume_blocked,
        "run_id": result.run_id,
        "sidecar": result.sidecar,
        "attempts": result.attempts,
        "artifact_coverage": result.artifact_coverage,
        "refusal": result.refusal,
        "resume_hint": hint,
        "superseded_run": result.superseded_run,
        "workflow_validated": False,
    }
