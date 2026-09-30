"""Runtime driver for headless OpenCode runs.

The capability constants at the top mirror the status of one row each in the
OpenCode compatibility document and gate the code path that would otherwise
rely on an unverified claim; a test compares every flag with its row so they
cannot drift apart. Below them sit the request, prepared-run and outcome
types, the run-state transition table, and the pure functions that decide an
attempt's outcome from the facts the observer collected.
"""
from __future__ import annotations

import collections
import dataclasses
import os
import queue
import re
import shutil
import signal as _signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from . import compiler, doctor, frontmatter, install, jsonio, launch_env, manifest, names
from . import paths as adapter_paths
from . import proctree, qualification, retry, roles, runstore
from . import events as ev
from . import secrets as credential_refs
from .errors import ConfigErrors
from .events import EventType
from .launch_env import LaunchEnv
from .proctree import Identity

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


# ------------------------------------------------------------- the driver

DEFAULT_TIMEOUT_S = 1800.0
_STAGE_RE = re.compile(r"^[0-9]{1,3}$")
_AGENT_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CONFIG_ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY")
_POLICY_CLASSES = frozenset(
    (
        "personal-profile-for-work",
        "provider-excluded",
        "personal-provider-kind-for-work",
        "allowlist-broadening",
        "cross-profile-fallback-enabled",
    )
)
_FORBIDDEN_FLAGS = ("--auto", "--yolo", "--dangerously-skip-permissions", "--agent")
_STAGED = "staged"


def _iso(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


class _PrepareState:
    """What `prepare` has established so far, so a refusal can be recorded."""

    def __init__(self) -> None:
        self.run_id: Optional[str] = None
        self.directory: Optional[Path] = None
        self.record: Optional[Dict[str, Any]] = None
        self.resume = False


@dataclass
class AttemptSeed:
    """Everything a spawned attempt inherits from earlier attempts of the run.

    A fresh run uses the defaults. A resumed run passes the rebuilt
    deduper, the next sequence number, facts seeded from the whole run and
    the session id to continue.
    """

    attempt: Optional[int] = None
    resume_mode: str = "fresh"
    start_sequence: int = 1
    deduper: Optional[ev.Deduper] = None
    facts: Optional[AttemptFacts] = None
    native_session_id: Optional[str] = None
    ran_anything: bool = False
    record: Optional[Dict[str, Any]] = None


class RuntimeHandle:
    """One running attempt. Created by `start`/`resume`, consumed by
    `observe`, ended by `cancel`. Internal fields are documented where the
    driver uses them; callers use `run_id`, `attempt`, `outcome`, `pid`,
    `request_cancel` and `cancellation`."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)
        # Guards every read-modify-write of the run record, the checkpoint and
        # the pointer. Never taken from a signal handler.
        self.record_lock = threading.Lock()
        # Serialises termination so it runs once.
        self.cancel_lock = threading.Lock()
        self.tracked_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.closing = threading.Event()
        self.cancel_result: Optional[CancellationResult] = None
        self.terminated_reason: Optional[str] = None
        self.observer_active = False
        self.finalizing = False
        self.finalized = False
        self.outcome: Optional[AttemptOutcome] = None
        self.tracked: Dict[int, Identity] = {}
        self.stdout_eof = False
        self.last_sequence = fields.get("start_sequence", 1) - 1
        self.event_count = 0
        self.leftovers_count = 0
        self.stderr_discarded = 0
        self.last_scan = 0.0
        self.eof_at: Optional[float] = None
        self.started_yielded = False

    def request_cancel(self) -> None:
        """Ask for cancellation without blocking or taking any lock; safe from
        a signal handler. The observer (or `cancel`) does the terminating."""
        self.cancel_event.set()

    @property
    def terminated(self) -> bool:
        return self.cancel_result is not None

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def cancellation(self) -> Optional[CancellationResult]:
        return self.cancel_result

    def __repr__(self) -> str:
        return "RuntimeHandle(run_id=%s, attempt=%s, pid=%s)" % (self.run_id, self.attempt, self.proc.pid)


# Reason -> attempt fact set by the first termination.
_REASON_FLAGS = {
    "cancel": "cancelled",
    "timeout": "timeout",
    "eof-without-exit": "eof_without_exit",
    "agent-fallback": "agent_fallback",
}


class OpenCodeDriver:
    """Prepares, launches, observes, cancels and resumes headless OpenCode runs.

    Every collaborator (binary lookup, version probe, credential resolver,
    process table, clocks) is injectable. Timing knobs are keyword arguments.

    A started run is serviced only inside `observe`: the deadline, stderr
    approvals and the stdout queue are handled there. A caller that starts a
    run must iterate `observe(handle)` or call `cancel(handle)`. The bounded
    output queue makes an unobserved child block on its own write instead of
    growing driver memory.
    """

    def __init__(
        self,
        project_root: Any,
        *,
        env: Optional[Mapping[str, str]] = None,
        home: Optional[Any] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
        version_runner: Callable[[str], Optional[str]] = doctor._default_version_runner,  # noqa: SLF001
        resolver_factory: Callable[..., Any] = credential_refs.default_resolver,
        proc: Any = proctree,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        platform: str = sys.platform,
        poll_s: float = 0.1,
        eof_grace_s: float = 5.0,
        exit_drain_s: float = 2.0,
        grace_s: float = 5.0,
        kill_grace_s: float = 2.0,
        leftover_grace_s: float = 2.0,
        descendant_scan_s: float = 5.0,
        queue_size: int = 1024,
        stderr_tail_lines: int = 64,
        checkpoint_every: int = 50,
        max_line_bytes: int = 1_048_576,
        stderr_read_delay_s: float = 0.0,
    ) -> None:
        self.project_root = Path(os.path.abspath(str(project_root)))
        self._env: Dict[str, str] = dict(os.environ if env is None else env)
        self._home = Path(home) if home is not None else Path.home()
        self._which = which
        self._version_runner = version_runner
        self._resolver_factory = resolver_factory
        self._proc = proc
        self._clock = clock
        self._monotonic = monotonic
        self._platform = platform
        self._poll_s = poll_s
        self._eof_grace_s = eof_grace_s
        self._exit_drain_s = exit_drain_s
        self._grace_s = grace_s
        self._kill_grace_s = kill_grace_s
        self._leftover_grace_s = leftover_grace_s
        self._descendant_scan_s = descendant_scan_s
        self._queue_size = queue_size
        self._stderr_tail_lines = stderr_tail_lines
        self._checkpoint_every = checkpoint_every
        self._max_line_bytes = max_line_bytes
        self._stderr_read_delay_s = stderr_read_delay_s

    # ------------------------------------------------------------- probe

    def _config_env(self) -> Dict[str, str]:
        return {key: self._env[key] for key in _CONFIG_ENV_KEYS if key in self._env}

    def _read_version(self, binary: str) -> Optional[str]:
        try:
            text = self._version_runner(binary)
        except Exception:  # noqa: BLE001 - a broken probe means "unknown"
            return None
        match = doctor._VERSION_RE.search(text) if text else None  # noqa: SLF001
        return match.group(0) if match else None

    def probe(self) -> RuntimeCapabilities:
        """What this machine and binary can do, without starting a run."""
        path = self._which("opencode")
        version = self._read_version(path) if path else None
        try:
            pinned: Optional[str] = qualification.pinned_version()
        except adapter_paths.AdapterDataMissing:
            pinned = None
        posix = bool(getattr(self._proc, "SUPPORTED", False))
        reaping = "unsupported"
        if posix:
            reaping = "proc" if self._platform.startswith("linux") else "ps"
        return RuntimeCapabilities(
            binary=Path(path) if path else None,
            version=version,
            version_supported=bool(version and pinned and version == pinned),
            step_settling="verified" if STEP_SETTLING_VERIFIED else "unverified",
            process_groups="supported" if posix else "unsupported",
            descendant_reaping=reaping,
        )

    # ----------------------------------------------------------- prepare

    def prepare(self, request: RunRequest, *, resume_run_id: Optional[str] = None) -> PreparedRun:
        """Validate everything and build a launchable run, or refuse.

        A refusal raises `PrepareRefused` before any process is spawned. On a
        new run it is written to the run record, which stays `prepared`. In
        resume mode the run's state is never changed: the refusal is written
        to the record and the run stays interrupted or failed.
        """
        state = _PrepareState()
        try:
            return self._prepare(request, resume_run_id, state)
        except PrepareRefused as exc:
            exc.run_id = state.run_id
            self._record_refusal(state, exc)
            raise
        except launch_env.LaunchRefused as exc:
            refusal = PrepareRefused(exc.category, exc.code, exc.message, state.run_id)
            self._record_refusal(state, refusal)
            raise refusal from None

    def _record_refusal(self, state: _PrepareState, exc: PrepareRefused) -> None:
        record, directory = state.record, state.directory
        if record is None or directory is None:
            return
        shown = launch_env.Redactor()
        record["refusal"] = {
            "category": exc.category,
            "code": exc.code,
            "message": shown(exc.message),
        }
        now = _iso(self._clock)
        if state.resume:
            attempts = record.get("attempts") or []
            if attempts and attempts[-1].get("state") == _STAGED:
                attempts[-1]["reason"] = "refused: " + exc.code
            record["updated_at"] = now
        else:
            transition(record, "prepared", "refused: " + exc.code, now)
        try:
            runstore.write_record(directory, record)
        except (OSError, runstore.RunStoreError):
            pass

    def _refusal(self, category: str, code: str, message: str) -> PrepareRefused:
        return PrepareRefused(category, code, message)

    def _prepare(
        self, request: RunRequest, resume_run_id: Optional[str], state: _PrepareState
    ) -> PreparedRun:
        refuse = self._refusal
        # 1. names, root and the run store
        try:
            task = runstore.check_task_name(request.task)
        except runstore.RunStoreError:
            raise refuse("workflow-validation", "invalid-task-name", "the task name is not a valid folder name") from None
        if request.stage is not None and not (isinstance(request.stage, str) and _STAGE_RE.match(request.stage)):
            raise refuse("workflow-validation", "invalid-stage", "the stage must be a number of at most three digits")
        root = self.project_root
        if os.path.realpath(str(request.project_root)) != os.path.realpath(str(root)):
            raise refuse("workflow-validation", "project-root-mismatch", "the request names a different project root")
        if not root.is_dir():
            raise refuse("workflow-validation", "invalid-project-root", "the project root is not a directory")
        try:
            directory = runstore.store_dir(root, create=True)
        except (runstore.RunStoreError, OSError):
            raise refuse(
                "workflow-validation", "sidecar-dir-unwritable",
                "the run store directory under the project cannot be used",
            ) from None
        state.directory = directory
        phase_id = request.phase.replace("-", "_")
        request_dict = {
            "task": task,
            "stage": request.stage,
            "phase": request.phase,
            "profile": request.profile,
            "effort": request.effort,
            "timeout_s": request.timeout_s,
            "budget": request.budget,
            "context_refs": list(request.context_refs),
        }
        state.resume = resume_run_id is not None
        try:
            if resume_run_id is None:
                run_id, _ = runstore.reserve_run_id(directory, self._clock)
                state.run_id = run_id
                state.record = runstore.new_run_record(run_id, task, request_dict, {}, self._clock)
            else:
                runstore.check_run_id(resume_run_id)
                existing = runstore.load_record(directory, resume_run_id)
                if existing is None:
                    raise refuse("workflow-validation", "resume-run-missing", "the run to resume has no record")
                state.run_id = resume_run_id
                stored = existing.get("request") or {}
                same = (
                    stored.get("task") == task
                    and stored.get("stage") == request.stage
                    and str(stored.get("phase", "")).replace("-", "_") == phase_id
                    and stored.get("profile") == request.profile
                )
                if not same:
                    raise refuse(
                        "workflow-validation", "resume-request-mismatch",
                        "the request does not match the run being resumed",
                    )
                state.record = existing
        except (runstore.RunStoreError, OSError) as exc:
            if isinstance(exc, runstore.RunStoreError) and getattr(exc, "code", "") == "corrupt-record":
                raise refuse("workflow-validation", "run-record-invalid", "the run record cannot be read") from None
            raise refuse(
                "workflow-validation", "sidecar-dir-unwritable",
                "the run store directory under the project cannot be used",
            ) from None

        # 2. platform
        if not getattr(self._proc, "SUPPORTED", False) or os.name != "posix":
            raise refuse(
                "workflow-validation", "process-groups-unsupported",
                "process groups are needed to stop a run and are not available here",
            )

        # 3. phase
        if phase_id == "run":
            raise refuse(
                "workflow-validation", "whole-task-unavailable",
                "a whole-task run is not available yet; name a single phase with --phase",
            )
        adapter_dir = adapter_paths.adapter_data_dir()
        if adapter_dir is None:
            raise refuse("workflow-validation", "adapter-data-missing", "the packaged adapter data was not found; reinstall quoin")
        try:
            feature = manifest.load_manifest(adapter_dir.parent.parent)
        except manifest.ManifestLoadError:
            raise refuse("workflow-validation", "adapter-data-missing", "the packaged adapter manifest cannot be read; reinstall quoin") from None
        entry = next(
            (e for e in feature.get("catalog_entries", ()) if isinstance(e, dict) and e.get("id") == phase_id),
            None,
        )
        if (
            entry is None
            or entry.get("status") != "supported"
            or "command" not in (entry.get("assets") or ())
            or not (entry.get("opencode") or {}).get("command")
        ):
            raise refuse(
                "workflow-validation", "phase-unsupported",
                "the phase %s has no supported OpenCode command" % phase_id,
            )
        command_name = names.normalize(phase_id)

        # 4. binary and version
        binary = self._which("opencode")
        if not binary:
            raise refuse("missing-binary", "opencode-binary-absent", "the opencode executable was not found on PATH")
        version = self._read_version(binary)
        try:
            pinned = qualification.pinned_version()
        except adapter_paths.AdapterDataMissing:
            raise refuse("workflow-validation", "adapter-data-missing", "the pinned runtime version was not found; reinstall quoin") from None
        if version != pinned:
            raise refuse(
                "unsupported-version", "opencode-version",
                "the opencode executable reports %s; this adapter is pinned to %s" % (version or "no version", pinned),
            )

        # 5. evaluate configuration
        cfg_env = self._config_env()
        base_redact = launch_env.Redactor()
        try:
            evaluation = compiler.evaluate(
                project_root=root, profile=request.profile, env=cfg_env, home=self._home,
                now=datetime.fromtimestamp(self._clock(), timezone.utc),
            )
        except ConfigErrors as exc:
            first = exc.errors[0] if exc.errors else None
            code = first.rejection_class if first is not None else "config-invalid"
            category = "policy-denial" if code in _POLICY_CLASSES else "invalid-configuration"
            text = "; ".join(e.message for e in exc.errors[:3]) or "the configuration is invalid"
            raise refuse(category, code, base_redact(text)) from None
        except roles.AllowUnqualifiedRefused as exc:
            raise refuse("policy-denial", "allow-unqualified-refused", base_redact(str(exc))) from None
        except adapter_paths.AdapterDataMissing:
            raise refuse("workflow-validation", "adapter-data-missing", "the packaged adapter data was not found; reinstall quoin") from None

        # 6. launchable
        if not compiler.launchable(evaluation):
            blockers = compiler.compile_blockers(evaluation)
            # A role blocked only because its model lacks a valid gateway
            # qualification is the gateway's problem, not the configuration's.
            unqualified = all(
                f.code == "role-blocked" and len(f.subject) > 1 and f.subject[1].startswith("qualification-")
                for f in blockers
            )
            if blockers and not unqualified:
                raise refuse(
                    "invalid-configuration", "compile-blocked",
                    "the configuration cannot be compiled: " + ", ".join(sorted({f.code for f in blockers})),
                )
            raise refuse(
                "unqualified-gateway", "not-launchable",
                "a role model lacks a valid gateway qualification; run the qualification probe",
            )

        # 7. compiled pair
        fresh, compiled_dir = self._compile_pair(evaluation, cfg_env)
        native_sha = fresh.sidecar["native_sha256"]
        config_path = compiled_dir / compiler.NATIVE_FILE

        # 8. install record and owned files
        try:
            metadata = install.load_metadata(root)
        except install.InstallError as exc:
            raise refuse("workflow-validation", "install-record-invalid", base_redact("the install record is unusable: %s" % exc)) from None
        if metadata is None:
            raise refuse("workflow-validation", "not-installed", "the Quoin OpenCode files are not installed in this project")
        command_rel = ".opencode/commands/%s.md" % command_name
        if command_rel not in metadata.owned:
            raise refuse("workflow-validation", "not-installed", "the command file for %s is not part of the installed set" % phase_id)
        for rel in sorted(metadata.owned):
            if not launch_env.verify_owned_file(root, rel, metadata.owned[rel]["sha256"]):
                raise refuse(
                    "workflow-validation", "owned-file-drift",
                    "the installed file %s changed or cannot be read; reinstall with quoin opencode install" % rel,
                )
        agent = self._command_agent(root, command_rel, metadata)
        owned_agents = {r: rec["sha256"] for r, rec in metadata.owned.items() if rec["kind"] == "agent"}
        owned_commands = {r: rec["sha256"] for r, rec in metadata.owned.items() if rec["kind"] == "command"}

        # 8a. non-git roots
        if adapter_paths.git_worktree_root(root, home=None) is None and not NON_GIT_DISCOVERY_VERIFIED:
            raise refuse(
                "workflow-validation", "non-git-root-unverified",
                "the project root is not inside a git worktree and OpenCode discovery there is not verified; "
                "run from a project root that is a git worktree (for example the repository that holds "
                ".opencode) or run git init at the project root",
            )

        # 9. role and model
        role = agent[len(names.PREFIX):] if agent.startswith(names.PREFIX) else agent
        resolution = next((r for r in evaluation.resolutions.roles if r.role == role), None)
        if resolution is None:
            raise refuse("workflow-validation", "role-unresolved", "the role %s has no resolved model" % role)
        effective_model = compiler.native_model_ref(resolution)

        # 10. configuration layers and environment
        scan_env = {"OPENCODE_CONFIG": str(config_path)}
        if "XDG_CONFIG_HOME" in cfg_env:
            scan_env["XDG_CONFIG_HOME"] = cfg_env["XDG_CONFIG_HOME"]
        launch_env.check_config_layers(
            cwd=root, env=scan_env, home=self._home, compiled_doc=fresh.document,
            owned_agents=owned_agents, owned_commands=owned_commands,
        )
        redactor = launch_env.Redactor()
        data = launch_env.data_dir(cfg_env, self._home, evaluation.profile)
        child_env = launch_env.build_env(
            ambient=self._env, compile_sidecar=fresh.sidecar,
            providers=evaluation.effective.providers,
            resolver=self._resolver_factory(self._env, platform=self._platform),
            data_dir=data, config_path=config_path, redactor=redactor,
        )

        # 11. limits, hashes, revisions, argv
        limits = {}
        for name in ("max_transient_retries", "max_run_seconds"):
            value = evaluation.effective.values.get("limits." + name)
            if value is not None and isinstance(value.value, int) and not isinstance(value.value, bool):
                limits[name] = value.value
        policy = retry.RetryPolicy.from_limits(limits)
        try:
            input_hashes = runstore.hash_inputs(root, task, request.context_refs)
        except runstore.RunStoreError:
            raise refuse("workflow-validation", "context-ref-invalid", "a context reference is not a path inside the project") from None
        revisions = tuple(runstore.repo_revisions(root))
        arg = task if request.stage is None else "stage %s of %s" % (request.stage, task)
        argv = (str(binary), "run", "--format", "json", "--command", command_name, "--", arg)

        prepared = PreparedRun(
            request=request,
            run_id=state.run_id or "",
            binary=Path(binary),
            runtime_version=version or "",
            role=role,
            effective_model=effective_model,
            config_digest=fresh.digest,
            native_sha256=native_sha,
            config_path=config_path,
            cwd=root,
            argv=argv,
            env_names=tuple(child_env.names()),
            policy={
                "profile": evaluation.profile,
                "classification": evaluation.effective.classification,
                "limits": limits,
                "unqualified_models": list(fresh.sidecar.get("unqualified_models") or []),
            },
            retry=policy,
            artifact_paths=runstore.run_paths(directory, state.run_id or ""),
            input_hashes=input_hashes,
            repo_revisions=revisions,
            launch_env=child_env,
        )

        # 12. persist
        record = state.record
        assert record is not None
        summary = {
            "binary": str(binary),
            "runtime_version": version,
            "role": role,
            "agent": agent,
            "command": command_name,
            "effective_model": effective_model,
            "config_digest": fresh.digest,
            "native_sha256": native_sha,
            "config_path": str(config_path),
            "cwd": str(root),
            "argv": list(argv),
            "env_names": list(child_env.names()),
            "profile": evaluation.profile,
            "effort": request.effort,
        }
        record["prepared"] = summary
        record["refusal"] = None
        now = _iso(self._clock)
        if resume_run_id is None:
            record["input_hashes"] = input_hashes
            record["repo_revisions"] = [dict(r) for r in revisions]
            runstore.write_record(directory, record)
            runstore.write_pointer(directory, runstore.new_pointer(task, state.run_id, self._clock))
        else:
            attempts = record.setdefault("attempts", [])
            number = max((a.get("attempt", 0) for a in attempts if a.get("state") != _STAGED), default=0) + 1
            staged = runstore.new_attempt(
                number, pid=None, pgid=None, child_start=None, driver_pid=os.getpid(),
                driver_start=None, resume_mode="fresh", input_hashes_before=input_hashes,
                repo_revisions_before=revisions, clock=self._clock,
            )
            staged["state"] = _STAGED
            if attempts and attempts[-1].get("state") == _STAGED:
                attempts[-1] = staged
            else:
                attempts.append(staged)
            record["updated_at"] = now
            runstore.write_record(directory, record)
        return prepared

    def _compile_pair(self, evaluation: Any, cfg_env: Mapping[str, str]) -> Tuple[Any, Path]:
        """Make sure the compiled pair on disk matches a fresh build; write it
        when it is missing or stale. Returns the fresh build and directory."""
        refuse = self._refusal
        try:
            directory = compiler.resolve_output_dir(evaluation, output=None, env=cfg_env, home=self._home)
        except compiler.OutputRefused as exc:
            raise refuse("invalid-configuration", "compiled-output-refused", str(exc)) from None
        except OSError:
            raise refuse("invalid-configuration", "compiled-output-unwritable", "the compiled output directory cannot be used") from None
        try:
            outcome = compiler.check(evaluation, directory)
            if not outcome.ok:
                if not set(outcome.reasons) <= {"missing", "stale"}:
                    raise refuse(
                        "invalid-configuration", "compiled-" + outcome.reasons[0],
                        "the compiled configuration cannot be reused: " + ", ".join(outcome.reasons),
                    )
                compiler.write(compiler.build(evaluation), directory)
                outcome = compiler.check(evaluation, directory)
                if not outcome.ok:
                    raise refuse(
                        "invalid-configuration", "compiled-stale",
                        "the compiled configuration is still stale after writing it",
                    )
            fresh = compiler.build(evaluation)
        except (compiler.CompileBlocked, compiler.CompileGateError):
            raise refuse("invalid-configuration", "compile-blocked", "the configuration cannot be compiled") from None
        except (jsonio.UnsafeDirectoryError, OSError):
            raise refuse("invalid-configuration", "compiled-output-unwritable", "the compiled output directory cannot be used") from None
        native = jsonio.read_regular_bytes(directory / compiler.NATIVE_FILE, max_bytes=compiler.MAX_OUTPUT_BYTES)
        import hashlib

        if native is None or hashlib.sha256(native[0]).hexdigest() != fresh.sidecar["native_sha256"]:
            raise refuse("invalid-configuration", "compiled-file-changed", "the compiled configuration file does not match a fresh build")
        return fresh, directory

    def _command_agent(self, root: Path, command_rel: str, metadata: Any) -> str:
        """The primary agent the phase command selects; refuses otherwise."""
        refuse = self._refusal

        def parsed(rel: str) -> Dict[str, Any]:
            got = jsonio.read_regular_bytes(root / rel, max_bytes=launch_env.MAX_OWNED_BYTES)
            if got is None:
                raise refuse("workflow-validation", "command-agent-not-primary", "%s cannot be read" % rel)
            try:
                fields, _ = frontmatter.parse(got[0].decode("utf-8"))
            except (UnicodeDecodeError, frontmatter.FrontmatterError):
                raise refuse("workflow-validation", "command-agent-not-primary", "%s has unreadable frontmatter" % rel) from None
            return fields

        agent = parsed(command_rel).get("agent")
        if not isinstance(agent, str) or not _AGENT_NAME_RE.match(agent):
            raise refuse("workflow-validation", "command-agent-not-primary", "the phase command does not name an agent")
        agent_rel = ".opencode/agents/%s.md" % agent
        if agent_rel not in metadata.owned:
            raise refuse("workflow-validation", "command-agent-not-primary", "the agent %s is not part of the installed set" % agent)
        if parsed(agent_rel).get("mode") not in ("primary", "all"):
            raise refuse("workflow-validation", "command-agent-not-primary", "the agent %s cannot run as a primary agent" % agent)
        return agent
