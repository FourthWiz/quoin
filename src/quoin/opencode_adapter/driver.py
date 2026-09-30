"""Runtime driver for headless OpenCode runs.

The capability constants at the top mirror the status of one row each in the
OpenCode compatibility document and gate the code path that would otherwise
rely on an unverified claim; a test compares every flag with its row so they
cannot drift apart. Below them sit the request, prepared-run and outcome
types, the run-state transition table, and the pure functions that decide an
attempt's outcome from the facts the observer collected.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from . import events as ev
from . import retry, runstore
from .events import EventType
from .launch_env import LaunchEnv

# Row key in the compatibility document -> why the driver depends on it.
# A key is listed only while its row is verified; an unverified capability is
# represented by its flag alone, and every code path that depends on it is
# gated off by that flag.
DRIVER_CITED_CAPABILITIES: Dict[str, str] = {
    "step-settling": (
        "every tool part of a step is terminal before its step-finish, so a "
        "stream that ends after a step-finish has no open tool part"
    ),
    "non-git-discovery": (
        "a project root that is not a git repository still loads .opencode "
        "configuration and commands, so the compiled launch is honoured there"
    ),
    "managed-config-layers": (
        "managed configuration outranks the compiled file, so it must be "
        "scanned before a launch"
    ),
    "data-dir-state": (
        "credentials, session storage and remote config live under the data "
        "directory, which the launcher isolates and reuses across attempts"
    ),
    "continuation-flags": (
        "resume selects a session with --session and treats an unknown "
        "session as exit 1 with no native events"
    ),
}

# Verified: session continuation after real work may be attempted, because a
# clean step-finish proves every tool of that step settled.
STEP_SETTLING_VERIFIED: bool = True

# Verified with a condition: a resumed run subscribes to live events only.
# Compaction pruning can republish earlier completed tool parts when a
# configuration layer enables it. This flag gates nothing: replayed part ids
# are dropped by the rebuilt deduper, and a replayed error without an id can
# only cause a false failure, never a false success.
CONTINUATION_NO_REPLAY_VERIFIED: bool = True

# Verified: a project root that is not a git repository is accepted.
NON_GIT_DISCOVERY_VERIFIED: bool = True


# ------------------------------------------------------------- constants

# Stage 4 maps doctor findings onto the same literals.
REFUSAL_CATEGORIES: Tuple[str, ...] = (
    "missing-binary",
    "unsupported-version",
    "invalid-configuration",
    "unqualified-gateway",
    "policy-denial",
    "missing-optional-integration",
    "workflow-validation",
)

RESUME_MESSAGE = "Continue the task from where you stopped. Do not repeat tool calls that already completed."

WRITABLE_TOOLS: Tuple[str, ...] = ("write", "edit", "patch", "multiedit", "bash")

# Evidence-downgrading delegation outcomes carried across attempts.
_DOWNGRADE_DELEGATIONS = ("background", "denied-tail", "failed")


# ----------------------------------------------------------------- types


class PrepareRefused(Exception):
    """A run was refused before any process was spawned."""

    def __init__(self, category: str, code: str, message: str, run_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message
        self.run_id = run_id

    def __str__(self) -> str:
        return self.message


class ResumeBlocked(Exception):
    """A resume was refused because continuing could repeat an effect."""

    def __init__(self, reason: str, run_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.run_id = run_id


class IllegalTransition(ValueError):
    """A run-state change the table does not allow."""


@dataclass(frozen=True)
class RunRequest:
    project_root: Path
    task: str
    stage: Optional[str]
    phase: str
    profile: str
    effort: Optional[str] = None
    timeout_s: Optional[float] = None
    budget: Optional[str] = None
    context_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeCapabilities:
    binary: Optional[Path]
    version: Optional[str]
    version_supported: bool
    json_events: str = "verified"
    native_stop_event: str = "absent"
    approval_observable: str = "parent-json+stderr-notice"
    child_session_events: str = "filtered"
    continuation: str = "verified"
    step_settling: str = "unverified"
    process_groups: str = "unsupported"
    descendant_reaping: str = "unsupported"


@dataclass(frozen=True)
class PreparedRun:
    request: RunRequest
    run_id: str
    binary: Path
    runtime_version: str
    role: str
    effective_model: str
    config_digest: str
    native_sha256: str
    config_path: Path
    cwd: Path
    argv: Tuple[str, ...]
    env_names: Tuple[str, ...]
    policy: Optional[Mapping[str, Any]]
    retry: Optional[retry.RetryPolicy]
    artifact_paths: Optional[runstore.RunPaths]
    input_hashes: Mapping[str, Any] = field(default_factory=dict)
    repo_revisions: Tuple[Mapping[str, Any], ...] = ()
    # Secrets live only here; never shown and never part of equality.
    launch_env: Optional[LaunchEnv] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CancellationResult:
    signalled_term: bool
    escalated_kill: bool
    group_empty: bool
    descendants_found: int
    descendants_remaining: Optional[int]
    exit_code: Optional[int]
    duration_s: float


@dataclass(frozen=True)
class Handoff:
    run_id: str
    last_sequence: int
    native_session_id: Optional[str]
    repo_revisions: Tuple[Mapping[str, Any], ...]
    step_open: bool
    ran_anything: bool

    @classmethod
    def from_checkpoint(cls, checkpoint: Mapping[str, Any]) -> "Handoff":
        if not isinstance(checkpoint, Mapping):
            raise ValueError("a checkpoint must be an object")
        try:
            run_id = checkpoint["run_id"]
            last_sequence = checkpoint["last_sequence"]
        except KeyError as exc:
            raise ValueError("the checkpoint is missing %s" % exc) from None
        if not isinstance(run_id, str) or not ev.RUN_ID_RE.match(run_id):
            raise ValueError("the checkpoint run id has the wrong shape")
        if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
            raise ValueError("the checkpoint sequence must be a non-negative integer")
        session = checkpoint.get("native_session_id")
        if session is not None and not isinstance(session, str):
            raise ValueError("the checkpoint session id must be text")
        revisions = checkpoint.get("repo_revisions") or []
        if not isinstance(revisions, (list, tuple)) or not all(isinstance(r, Mapping) for r in revisions):
            raise ValueError("the checkpoint revisions must be a list of objects")
        return cls(
            run_id=run_id,
            last_sequence=last_sequence,
            native_session_id=session,
            repo_revisions=tuple(dict(r) for r in revisions),
            step_open=bool(checkpoint.get("step_open", False)),
            ran_anything=bool(checkpoint.get("ran_anything", False)),
        )


@dataclass(frozen=True)
class AttemptOutcome:
    state: str
    evidence: Optional[str] = None
    reason: Optional[str] = None
    failure: Optional[retry.Failure] = None
    resume_blocked: Optional[str] = None
    new_native_events: int = 0
    exit_code: Optional[int] = None
    signal: Optional[int] = None


# ------------------------------------------------------------ state table

# Terminal-for-attempt states are sticky except the two a resume of the same
# run may leave. There is deliberately no path back to "prepared": a resume
# never re-prepares a run that already started.
_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "prepared": ("running", "prepared"),
    "running": ("completed", "failed", "awaiting_approval", "cancelled", "interrupted"),
    "interrupted": ("running",),
    "failed": ("running",),
    "completed": (),
    "awaiting_approval": (),
    "cancelled": (),
}
assert set(_TRANSITIONS) == set(ev.RUN_STATES)


def transition(record: Dict[str, Any], new_state: str, reason: Optional[str], at: str) -> Dict[str, Any]:
    """Move ``record`` to ``new_state`` if the table allows it; otherwise
    raise ``IllegalTransition`` and leave the record untouched."""
    current = record.get("state")
    if current not in _TRANSITIONS or new_state not in _TRANSITIONS:
        raise IllegalTransition("unknown state %r -> %r" % (current, new_state))
    if new_state not in _TRANSITIONS[current]:
        raise IllegalTransition("a run cannot go from %s to %s" % (current, new_state))
    record["state"] = new_state
    record.setdefault("history", []).append({"state": new_state, "at": at, "reason": reason})
    record["updated_at"] = at
    return record


# ------------------------------------------------------ outcome classifier


@dataclass
class AttemptFacts:
    """What the observer learned about one attempt.

    Evidence-downgrading facts are run-wide: ``seed`` copies them from the
    repaired sidecar when a resumed attempt starts and they only accumulate.
    Stop evidence (steps, exit, signal, approval, error, cancel, timeout) is
    per attempt.
    """

    # run-wide
    run_downgrades: Set[str] = field(default_factory=set)
    earlier_denied_halt: bool = False
    # this attempt: what downgraded evidence
    attempt_downgrades: Set[str] = field(default_factory=set)
    denied_halt: bool = False
    delegation_failed: bool = False
    # this attempt: stop evidence
    cancelled: bool = False
    timeout: bool = False
    approval: bool = False
    native_error: Optional[ev.ErrorPayload] = None
    agent_fallback: bool = False
    driver_error: bool = False
    orphan: bool = False
    eof_without_exit: bool = False
    session_lost: bool = False
    exit_code: Optional[int] = None
    exit_signal: Optional[int] = None
    new_native_events: int = 0
    step_events: List[Any] = field(default_factory=list)

    @classmethod
    def seed(cls, run_facts: runstore.RunFacts) -> "AttemptFacts":
        return cls(
            run_downgrades=set(run_facts.delegation_downgrades),
            earlier_denied_halt=bool(run_facts.denied_halt_seen),
        )

    def update(self, event: ev.RuntimeEvent) -> None:
        if event.origin == "native":
            self.new_native_events += 1
        if event.type is EventType.PROGRESS:
            payload = event.payload
            if payload.delegation in _DOWNGRADE_DELEGATIONS:
                self.run_downgrades.add(payload.delegation)
                if payload.delegation == "failed":
                    self.delegation_failed = True
                else:
                    self.attempt_downgrades.add(payload.delegation)
            if payload.kind == "halted" and payload.permission_outcome == "denied":
                self.denied_halt = True
            if payload.kind == "step_start":
                self.step_events.append(event)
        elif event.type is EventType.USAGE:
            self.step_events.append(event)
        elif event.type is EventType.APPROVAL_REQUIRED:
            self.approval = True
        elif event.type is EventType.ERROR and event.origin == "native":
            self.native_error = event.payload

    def steps(self) -> ev.StepSummary:
        return ev.summarize_steps(self.step_events)


def _stop_evidence(steps: ev.StepSummary) -> bool:
    return steps.finishes >= 1 and bool(steps.last_finish_terminal) and not steps.open_step


def failure_for(
    outcome: AttemptOutcome, facts: AttemptFacts, step_open: bool
) -> Optional[retry.Failure]:
    """The retry failure for an outcome, or None when nothing is retryable.
    Never produces a policy failure: approval is its own state."""
    if outcome.reason == "session-lost":
        return retry.Failure("config")
    if outcome.state != "failed":
        return None
    if outcome.reason in ("agent-fallback", "delegation-failed"):
        return retry.Failure("config")
    payload = facts.native_error
    if payload is None:
        return retry.Failure("config")
    kind, status = payload.failure_kind, payload.http_status
    if kind == "http" and status is None:
        kind = "config"
    return retry.Failure(
        kind,
        status=status,
        retry_after=payload.retry_after,
        mutating_tool_in_flight=bool(step_open),
    )


def classify(facts: AttemptFacts) -> AttemptOutcome:
    """Decide an attempt's outcome. One precedence order, first match wins."""
    steps = facts.steps()
    base = dict(
        new_native_events=facts.new_native_events,
        exit_code=facts.exit_code,
        signal=facts.exit_signal,
    )

    def make(state: str, reason: Optional[str], *, evidence: Optional[str] = None,
             resume_blocked: Optional[str] = None) -> AttemptOutcome:
        outcome = AttemptOutcome(
            state=state, evidence=evidence, reason=reason, resume_blocked=resume_blocked, **base
        )
        failure = failure_for(outcome, facts, steps.open_step)
        return dataclasses.replace(outcome, failure=failure) if failure is not None else outcome

    if facts.cancelled:
        return make("cancelled", "cancelled")
    if facts.approval:
        return make("awaiting_approval", "approval-required")
    if facts.native_error is not None and not facts.denied_halt:
        return make("failed", "error-event")
    if facts.delegation_failed:
        return make("failed", "delegation-failed")
    if facts.agent_fallback:
        return make("failed", "agent-fallback")
    if facts.timeout:
        return make("interrupted", "timeout")
    if facts.driver_error:
        return make("interrupted", "driver-error")
    if facts.orphan:
        return make("interrupted", "driver-lost")
    if facts.eof_without_exit:
        return make("interrupted", "eof-without-exit")
    if facts.session_lost:
        return make("interrupted", "session-lost", resume_blocked="session-lost")
    if facts.exit_signal is not None:
        return make("interrupted", "signal")
    if facts.denied_halt and facts.exit_code in (0, 1):
        return make("completed", "denied-halt", evidence="partial")
    if facts.exit_code is None:
        return make("interrupted", "no-exit-code")
    if facts.exit_code != 0:
        return make("interrupted", "nonzero-exit")
    if not _stop_evidence(steps):
        return make("interrupted", "no-stop-evidence")
    if facts.attempt_downgrades:
        reason = "delegated-" + sorted(facts.attempt_downgrades)[0]
        return make("completed", reason, evidence="partial")
    if facts.run_downgrades or facts.earlier_denied_halt:
        return make("completed", "earlier-attempt-downgrade", evidence="partial")
    return make("completed", None, evidence="full")
