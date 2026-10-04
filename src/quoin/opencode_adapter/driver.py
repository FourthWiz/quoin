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
import stat
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
    "tui-project-argument": (
        "the default command takes the project directory as its positional "
        "argument and changes into it before starting the interface"
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

# Reasons a run refuses to continue on its own; the operator starts it over.
RESUME_BLOCK_REASONS: Tuple[str, ...] = (
    "effect-uncertain",
    "session-lost",
    "sidecar-behind-checkpoint",
    "session-invalid",
)

# Appended to the command argument of a run that must not ask questions.
NON_INTERACTIVE_MARKER = " (non-interactive run)"

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
    workspace: Optional[Path] = None
    non_interactive: bool = False


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
    agent: str = ""
    # Secrets live only here; never shown and never part of equality.
    launch_env: Optional[LaunchEnv] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class InteractiveLaunch:
    """A validated interactive (TUI) launch. Secrets live only in
    `launch_env`; nothing about a run is recorded."""

    binary: Path
    runtime_version: str
    argv: Tuple[str, ...]
    cwd: Path
    config_path: Path
    config_digest: str
    env_names: Tuple[str, ...]
    profile: str
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
        # Any text id loads; whether it is safe for argv is decided by resume().
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

    @classmethod
    def from_continuation(cls, record: Mapping[str, Any], directory: Path) -> Optional["Handoff"]:
        """The checkpoint token a continuation record points at, or None.

        The token is returned only for an interrupted OpenCode run that the
        record names, that is still resumable and whose checkpoint agrees with
        the record; anything else means the phase starts a fresh native
        session. Damaged run data never blocks a fresh start."""
        try:
            if record.get("origin_runtime") != "opencode":
                return None
            native = record.get("native")
            if not isinstance(native, Mapping):
                return None
            run_id = native.get("run_id")
            if not isinstance(run_id, str) or not ev.RUN_ID_RE.match(run_id):
                return None
            run = runstore.load_record(directory, run_id)
            if run is None or run.get("task") != record.get("task") or run.get("state") != "interrupted":
                return None
            pointer = runstore.load_pointer(directory, run["task"])
            if not isinstance(pointer, Mapping) or pointer.get("run_id") != run_id:
                return None
            if run.get("resume_blocked") is not None:
                return None
            if any(a.get("driver_lost") for a in run.get("attempts") or []):
                return None
            request = run.get("request") or {}
            phase = record.get("phase") or {}
            if runstore.entry_phase_for_run(request.get("phase")) != phase.get("current"):
                return None
            if runstore.normalize_stage(request.get("stage")) != phase.get("stage"):
                return None
            checkpoint = runstore.load_checkpoint(directory, run_id)
            if checkpoint is None or checkpoint.get("run_id") != run_id:
                return None
            recorded, saved = native.get("session_id"), checkpoint.get("native_session_id")
            if recorded is not None and saved is not None and recorded != saved:
                return None
            return cls.from_checkpoint(checkpoint)
        except (runstore.RunStoreError, ValueError, KeyError, TypeError, AttributeError):
            return None


# The id goes into argv, so it must be unable to read as a flag.
NATIVE_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


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


def _model_priced(document: Any, model_ref: str) -> bool:
    from . import cost  # noqa: PLC0415 - cost imports gate, which must not load with the driver

    return cost.model_priced(document, model_ref)


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
    state_changing: Iterable[str] = ()


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
        self.cancel_requested = False
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
        """Ask for cancellation by setting a plain attribute. It touches no
        lock and no threading primitive, so it is safe from a signal handler
        that interrupted code holding either. The observer (or `cancel`) does
        the terminating."""
        self.cancel_requested = True

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
        # Guards record writes made before any handle exists (reconciliation
        # and blocked resumes). Never taken from a signal handler.
        self._record_lock = threading.Lock()

    # ------------------------------------------------------------- probe

    def _config_env(self) -> Dict[str, str]:
        return {key: self._env[key] for key in _CONFIG_ENV_KEYS if key in self._env}

    @property
    def state_root(self) -> Path:
        """The adapter state directory this driver resolves."""
        return adapter_paths.state_dir(self._config_env(), self._home)

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
        workspace = request.workspace
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
            "workspace": str(request.workspace) if request.workspace is not None else None,
            "non_interactive": bool(request.non_interactive),
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
                    and stored.get("workspace") == request_dict["workspace"]
                    and bool(stored.get("non_interactive")) == request.non_interactive
                )
                if not same:
                    raise refuse(
                        "workflow-validation", "resume-request-mismatch",
                        "the request does not match the run being resumed",
                    )
                state.record = existing
                if workspace is not None and not os.path.isdir(str(workspace)):
                    raise refuse(
                        "workflow-validation", "workspace-missing",
                        "the workspace of the run being resumed no longer exists",
                    )
        except (runstore.RunStoreError, OSError) as exc:
            if isinstance(exc, runstore.RunStoreError) and getattr(exc, "code", "") == "corrupt-record":
                raise refuse("workflow-validation", "run-record-invalid", "the run record cannot be read") from None
            raise refuse(
                "workflow-validation", "sidecar-dir-unwritable",
                "the run store directory under the project cannot be used",
            ) from None

        if workspace is not None:
            workspace = self._check_workspace(workspace, root)
        run_cwd = workspace if workspace is not None else root

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
        binary, version = self._check_binary()

        # 5. evaluate configuration, 6. launchable
        cfg_env = self._config_env()
        base_redact = launch_env.Redactor()
        evaluation = self._evaluate_launchable(request.profile, cfg_env, base_redact)

        # 7. compiled pair
        fresh, compiled_dir = self._compile_pair(evaluation, cfg_env)
        native_sha = fresh.sidecar["native_sha256"]
        config_path = compiled_dir / compiler.NATIVE_FILE

        # 8. install record and owned files
        metadata = self._load_install(root, base_redact)
        command_rel = ".opencode/commands/%s.md" % command_name
        if command_rel not in metadata.owned:
            raise refuse("workflow-validation", "not-installed", "the command file for %s is not part of the installed set" % phase_id)
        self._verify_owned(root, metadata)
        if workspace is not None:
            if not os.path.isfile(os.path.join(str(workspace), command_rel)):
                raise refuse(
                    "workflow-validation", "workspace-not-installed",
                    "the workspace does not hold the installed command file for %s" % phase_id,
                )
            self._verify_owned(workspace, metadata)
        agent = self._command_agent(root, command_rel, metadata)

        # 8a. non-git roots
        if workspace is not None:
            isolated = adapter_paths.git_worktree_root(workspace, home=None)
            if isolated is None or os.path.realpath(str(isolated)) != os.path.realpath(str(workspace)):
                raise refuse(
                    "workflow-validation", "workspace-not-isolated",
                    "the workspace must be the root of its own git worktree so OpenCode discovery stops there",
                )
        elif adapter_paths.git_worktree_root(root, home=None) is None and not NON_GIT_DISCOVERY_VERIFIED:
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
        redactor = launch_env.Redactor()
        child_env = self._scan_and_build_env(
            root, cfg_env, config_path, fresh, evaluation, metadata, redactor, cwd=run_cwd
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
        if request.context_refs:
            arg += " (context: %s)" % ", ".join(request.context_refs)
        if request.non_interactive:
            arg += NON_INTERACTIVE_MARKER
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
            cwd=run_cwd,
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
            agent=agent,
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
            "cwd": str(run_cwd),
            "workspace": request_dict["workspace"],
            "non_interactive": bool(request.non_interactive),
            "argv": list(argv),
            "env_names": list(child_env.names()),
            "profile": evaluation.profile,
            "effort": request.effort,
            "provider": resolution.provider_id,
            "native_provider": effective_model.split("/", 1)[0],
            "configured_effort": resolution.effort if resolution.effort_options is not None else None,
            "effort_origin": resolution.effort_origin,
            "variant": (
                compiler.VARIANT_PREFIX + resolution.effort
                if resolution.effort_options is not None and resolution.effort else None
            ),
            "effort_diagnostic": resolution.effort_diagnostic,
            "model_priced": _model_priced(fresh.document, effective_model),
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

    # ------------------------------------------- steps shared with start

    def _check_binary(self) -> Tuple[str, Optional[str]]:
        """The resolved executable and its version, refusing when it is
        missing or is not the pinned release."""
        refuse = self._refusal
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
        return binary, version

    def _evaluate_launchable(self, profile: str, cfg_env: Mapping[str, str], base_redact: Any) -> Any:
        """Evaluate the configuration for a profile and refuse unless it can
        be launched."""
        refuse = self._refusal
        try:
            evaluation = compiler.evaluate(
                project_root=self.project_root, profile=profile, env=cfg_env, home=self._home,
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
        return evaluation

    def _load_install(self, root: Path, redact: Any) -> Any:
        try:
            metadata = install.load_metadata(root)
        except install.InstallError as exc:
            raise self._refusal(
                "workflow-validation", "install-record-invalid", redact("the install record is unusable: %s" % exc)
            ) from None
        if metadata is None:
            raise self._refusal(
                "workflow-validation", "not-installed", "the Quoin OpenCode files are not installed in this project"
            )
        return metadata

    def _verify_owned(self, root: Path, metadata: Any) -> None:
        for rel in sorted(metadata.owned):
            if not launch_env.verify_owned_file(root, rel, metadata.owned[rel]["sha256"]):
                raise self._refusal(
                    "workflow-validation", "owned-file-drift",
                    "the installed file %s changed or cannot be read; reinstall with quoin opencode install" % rel,
                )

    def _check_workspace(self, workspace: Path, root: Path) -> Path:
        """A workspace is an absolute path to a real directory (not a symlink)
        that lies outside the project root."""
        refuse = self._refusal
        text = str(workspace)
        bad = refuse("workflow-validation", "workspace-invalid", "the workspace is not usable for a run")
        if not os.path.isabs(text):
            raise bad
        try:
            if not stat.S_ISDIR(os.lstat(text).st_mode):
                raise bad
        except OSError:
            raise bad from None
        real = os.path.realpath(text)
        real_root = os.path.realpath(str(root))
        if real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep):
            raise bad
        return Path(text)

    def _scan_and_build_env(
        self, root: Path, cfg_env: Mapping[str, str], config_path: Path, fresh: Any,
        evaluation: Any, metadata: Any, redactor: Any, cwd: Optional[Path] = None,
    ) -> Any:
        """Refuse on a configuration layer that would change the compiled
        pair, then build the child environment. May raise `LaunchRefused`."""
        owned_agents = {r: rec["sha256"] for r, rec in metadata.owned.items() if rec["kind"] == "agent"}
        owned_commands = {r: rec["sha256"] for r, rec in metadata.owned.items() if rec["kind"] == "command"}
        scan_env = {"OPENCODE_CONFIG": str(config_path)}
        if "XDG_CONFIG_HOME" in cfg_env:
            scan_env["XDG_CONFIG_HOME"] = cfg_env["XDG_CONFIG_HOME"]
        launch_env.check_config_layers(
            cwd=cwd if cwd is not None else root, env=scan_env, home=self._home,
            compiled_doc=fresh.document,
            owned_agents=owned_agents, owned_commands=owned_commands,
        )
        data = launch_env.data_dir(cfg_env, self._home, evaluation.profile)
        return launch_env.build_env(
            ambient=self._env, compile_sidecar=fresh.sidecar,
            providers=evaluation.effective.providers,
            resolver=self._resolver_factory(self._env, platform=self._platform),
            data_dir=data, config_path=config_path, redactor=redactor,
            state_dir=self.state_root,
        )

    def prepare_interactive(self, profile: str) -> "InteractiveLaunch":
        """Validate a profile for an interactive TUI session, or refuse.

        Runs the same checks as `prepare` in the same order, minus the ones
        that only matter for a headless run (run store, phase command, role
        and process groups). Creates no run store, record, pointer, sidecar
        or lock; it may write the compiled configuration pair, as `prepare`
        does."""
        try:
            return self._prepare_interactive(profile)
        except launch_env.LaunchRefused as exc:
            raise PrepareRefused(exc.category, exc.code, exc.message) from None

    def _prepare_interactive(self, profile: str) -> "InteractiveLaunch":
        refuse = self._refusal
        root = self.project_root
        if not root.is_dir():
            raise refuse("workflow-validation", "invalid-project-root", "the project root is not a directory")
        if adapter_paths.adapter_data_dir() is None:
            raise refuse(
                "workflow-validation", "adapter-data-missing",
                "the packaged adapter data was not found; reinstall quoin",
            )
        binary, version = self._check_binary()
        cfg_env = self._config_env()
        base_redact = launch_env.Redactor()
        evaluation = self._evaluate_launchable(profile, cfg_env, base_redact)
        fresh, compiled_dir = self._compile_pair(evaluation, cfg_env)
        config_path = compiled_dir / compiler.NATIVE_FILE
        metadata = self._load_install(root, base_redact)
        self._verify_owned(root, metadata)
        redactor = launch_env.Redactor()
        child_env = self._scan_and_build_env(root, cfg_env, config_path, fresh, evaluation, metadata, redactor)
        return InteractiveLaunch(
            binary=Path(binary),
            runtime_version=version or "",
            argv=(str(binary), str(root)),
            cwd=root,
            config_path=config_path,
            config_digest=fresh.digest,
            env_names=tuple(child_env.names()),
            launch_env=child_env,
            profile=evaluation.profile,
        )

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
        try:
            return install.command_agent(root, command_rel, metadata)
        except install.CommandAgentError as exc:
            raise self._refusal("workflow-validation", "command-agent-not-primary", exc.message) from None

    # ------------------------------------------------------------- start

    def start(self, prepared: PreparedRun, *, deadline_s: Optional[float] = None) -> RuntimeHandle:
        """Move a prepared run to `running` and spawn its first attempt.

        The transition is a plain record write: cross-process exclusion is the
        caller's phase lock. The returned handle is serviced only by
        `observe`; see the class docstring.
        """
        if prepared.launch_env is None or prepared.artifact_paths is None:
            raise ValueError("the prepared run has no launch environment")
        directory = prepared.artifact_paths.sidecar.parent
        record = runstore.load_record(directory, prepared.run_id)
        if record is None:
            raise ValueError("the prepared run has no record")
        if record.get("state") != "prepared":
            raise IllegalTransition("a run in state %s cannot be started" % record.get("state"))
        launch_env.verify_compiled(prepared.config_path, prepared.native_sha256)
        transition(record, "running", None, _iso(self._clock))
        runstore.write_record(directory, record)
        return self._spawn_attempt(
            prepared, list(prepared.argv), deadline_s=deadline_s, seed=AttemptSeed(record=record)
        )

    def _spawn_attempt(
        self,
        prepared: PreparedRun,
        argv: Sequence[str],
        *,
        deadline_s: Optional[float] = None,
        seed: Optional[AttemptSeed] = None,
    ) -> RuntimeHandle:
        """Spawn one attempt; the caller has already moved the run to
        `running`. No state change happens here except recording the attempt.
        A failure after the transition ends the attempt and the run
        `interrupted` and re-raises."""
        seed = seed or AttemptSeed()
        assert prepared.artifact_paths is not None and prepared.launch_env is not None
        directory = prepared.artifact_paths.sidecar.parent
        record = seed.record if seed.record is not None else runstore.load_record(directory, prepared.run_id)
        if record is None:
            raise ValueError("the run has no record")
        attempts = record.setdefault("attempts", [])
        staged = attempts[-1] if attempts and attempts[-1].get("state") == _STAGED else None
        number = seed.attempt or (staged["attempt"] if staged else len(attempts) + 1)
        if staged is not None:
            entry = staged
        else:
            entry = runstore.new_attempt(
                number, pid=None, pgid=None, child_start=None, driver_pid=os.getpid(),
                driver_start=None, resume_mode=seed.resume_mode,
                input_hashes_before=prepared.input_hashes, repo_revisions_before=prepared.repo_revisions,
                clock=self._clock,
            )
        hashes_before = dict(entry.get("input_hashes_before") or prepared.input_hashes)
        redactor = prepared.launch_env.redactor
        writer = None
        proc = None
        try:
            launch_env.verify_compiled(prepared.config_path, prepared.native_sha256)
            writer = runstore.SidecarWriter(prepared.artifact_paths.sidecar)
            pipeline = ev.EventPipeline(
                prepared.run_id, number, redact=redactor, observed_clock=self._clock,
                start_sequence=seed.start_sequence, deduper=seed.deduper,
                max_line_bytes=self._max_line_bytes,
            )
            driver_start = self._proc.start_time(os.getpid())
            proc = subprocess.Popen(
                list(argv), cwd=str(prepared.cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                env=prepared.launch_env.materialize(), close_fds=True,
            )
        except BaseException as exc:
            if writer is not None:
                writer.close()
            self._spawn_failed(record, directory, entry, attempts, exc)
            raise

        deadline_value = deadline_s if deadline_s is not None else (prepared.request.timeout_s or DEFAULT_TIMEOUT_S)
        facts = seed.facts if seed.facts is not None else AttemptFacts()
        handle = RuntimeHandle(
            driver=self, prepared=prepared, run_id=prepared.run_id, attempt=number, proc=proc,
            pgid=None, child_start=None, directory=directory, record=record, entry=entry,
            writer=writer, pipeline=pipeline, facts=facts, redactor=redactor,
            stdout_q=queue.Queue(maxsize=self._queue_size), signal_q=queue.Queue(),
            stderr_ring=collections.deque(maxlen=self._stderr_tail_lines),
            deadline=self._monotonic() + float(deadline_value),
            hashes_before=hashes_before, counters_base=dict(record.get("counters_total") or {}),
            resume_mode=seed.resume_mode, native_session_id=seed.native_session_id,
            ran_anything=seed.ran_anything, state_changing=set(seed.state_changing), start_sequence=seed.start_sequence,
            task=prepared.request.task, started_event=None, stdout_thread=None, stderr_thread=None,
        )
        try:
            handle.pgid = os.getpgid(proc.pid)
            handle.child_start = self._proc.start_time(proc.pid)
            handle.last_scan = self._monotonic()
            if handle.child_start is not None:
                handle.tracked[proc.pid] = Identity(proc.pid, handle.child_start)
            with handle.record_lock:
                entry.update(
                    pid=proc.pid, pgid=handle.pgid, child_start=handle.child_start,
                    driver_pid=os.getpid(), driver_start=driver_start, resume_mode=seed.resume_mode,
                    state="running", started_at=_iso(self._clock), reason=None,
                    input_hashes_before=hashes_before,
                )
                if entry is not staged:
                    attempts.append(entry)
                record["updated_at"] = _iso(self._clock)
                runstore.write_record(directory, record)
                runstore.write_pointer(directory, runstore.new_pointer(prepared.request.task, prepared.run_id, self._clock))
        except BaseException as exc:
            self._kill_now(handle)
            writer.close()
            self._spawn_failed(record, directory, entry, attempts, exc)
            raise

        handle.stdout_thread = threading.Thread(target=self._read_stdout, args=(handle,), daemon=True)
        handle.stderr_thread = threading.Thread(target=self._read_stderr, args=(handle,), daemon=True)
        handle.stdout_thread.start()
        handle.stderr_thread.start()
        try:
            started = pipeline.driver_event(
                EventType.STARTED,
                ev.StartedPayload(
                    pid=proc.pid, pgid=handle.pgid, argv=tuple(redactor(a) for a in argv),
                    env_names=tuple(prepared.env_names), runtime_version=prepared.runtime_version,
                    config_digest=prepared.config_digest, role=prepared.role,
                    effective_model=prepared.effective_model, attempt=number,
                    resume_mode=seed.resume_mode,
                ),
            )
            if started is not None:
                self._write_event(handle, started)
            handle.started_event = started
            self._checkpoint(handle)
        except BaseException as exc:
            self._abort(handle, exc)
            self._close(handle)
            raise
        return handle

    def _spawn_failed(
        self, record: Dict[str, Any], directory: Path, entry: Dict[str, Any],
        attempts: List[Dict[str, Any]], exc: BaseException,
    ) -> None:
        """End the attempt and the run `interrupted(driver-error)` after a
        failure between the transition and a fully started attempt."""
        try:
            entry.update(
                state="interrupted", reason="driver-error", ended_at=_iso(self._clock),
                driver_pid=os.getpid(),
            )
            if not any(a is entry for a in attempts):
                attempts.append(entry)
            if record.get("state") == "running":
                transition(record, "interrupted", "driver-error", _iso(self._clock))
            runstore.write_record(directory, record)
        except Exception:  # noqa: BLE001 - never mask the original failure
            pass

    def _kill_now(self, handle: RuntimeHandle) -> None:
        try:
            os.killpg(handle.pgid if handle.pgid else handle.proc.pid, _signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                handle.proc.kill()
            except OSError:
                pass
        try:
            handle.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------- readers

    @staticmethod
    def _put(handle: RuntimeHandle, item: Tuple[Any, ...]) -> bool:
        while not handle.closing.is_set():
            try:
                handle.stdout_q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _read_stdout(self, handle: RuntimeHandle) -> None:
        stream = handle.proc.stdout
        limit = self._max_line_bytes
        try:
            while not handle.closing.is_set():
                chunk = stream.readline(limit + 1)
                if not chunk:
                    break
                if len(chunk) > limit and not chunk.endswith(b"\n"):
                    # Discard the rest of an over-long line without holding it.
                    while True:
                        rest = stream.readline(65536)
                        if not rest or rest.endswith(b"\n"):
                            break
                    if not self._put(handle, ("oversized",)):
                        return
                    continue
                if not self._put(handle, ("line", chunk)):
                    return
            self._put(handle, ("eof",))
        except (OSError, ValueError) as exc:
            self._put(handle, ("error", type(exc).__name__))

    def _read_stderr(self, handle: RuntimeHandle) -> None:
        stream = handle.proc.stderr
        limit, bound = 64 * 1024, 8 * 1024
        try:
            while not handle.closing.is_set():
                chunk = stream.readline(limit + 1)
                if not chunk:
                    return
                if self._stderr_read_delay_s:
                    time.sleep(self._stderr_read_delay_s)
                if len(chunk) > limit and not chunk.endswith(b"\n"):
                    while True:
                        rest = stream.readline(65536)
                        if not rest or rest.endswith(b"\n"):
                            break
                    handle.stderr_discarded += 1
                text = chunk.decode("utf-8", "replace")
                found = ev.parse_stderr_line(text.rstrip("\r\n"))
                if found is not None:
                    handle.signal_q.put(found)
                # Redact the whole chunk first, then bound it, so a secret that
                # straddles the bound is replaced before it can be cut.
                shown = handle.redactor(text.rstrip("\r\n"))
                shown = shown.encode("utf-8")[:bound].decode("utf-8", "ignore")
                handle.stderr_ring.append(shown)
        except (OSError, ValueError):
            return

    # ------------------------------------------------------------ events

    def _write_event(self, handle: RuntimeHandle, event: ev.RuntimeEvent) -> bool:
        """Append one event and update what the observer knows. Returns true
        when a checkpoint is due."""
        handle.writer.write(event)
        handle.last_sequence = event.sequence
        handle.event_count += 1
        handle.facts.update(event)
        if event.origin == "native":
            handle.ran_anything = True
            if handle.native_session_id is None and event.session_id:
                handle.native_session_id = event.session_id
        payload = event.payload
        if event.type is EventType.PROGRESS:
            if event.native is not None and event.native.id and payload.tool in WRITABLE_TOOLS:
                handle.state_changing.add(event.native.id)
            if payload.kind in ("step_start", "halted") or payload.delegation is not None:
                return True
        if event.type in (EventType.USAGE, EventType.APPROVAL_REQUIRED, EventType.ERROR):
            return True
        return self._checkpoint_every > 0 and handle.event_count % self._checkpoint_every == 0

    def _emit_driver(self, handle: RuntimeHandle, etype: EventType, payload: Any) -> Optional[ev.RuntimeEvent]:
        event = handle.pipeline.driver_event(etype, payload)
        if event is not None:
            self._write_event(handle, event)
        return event

    def _checkpoint(self, handle: RuntimeHandle) -> None:
        handle.writer.fsync()
        steps = handle.facts.steps()
        with handle.record_lock:
            runstore.write_checkpoint(
                handle.directory,
                runstore.new_checkpoint(
                    handle.run_id, handle.attempt, last_sequence=handle.last_sequence,
                    sidecar_offset=handle.writer.offset, native_session_id=handle.native_session_id,
                    repo_revisions=handle.prepared.repo_revisions, step_open=steps.open_step,
                    ran_anything=handle.ran_anything, state_changing_part_ids=handle.state_changing,
                    clock=self._clock,
                ),
            )

    def _handle_item(self, handle: RuntimeHandle, item: Tuple[Any, ...]) -> List[ev.RuntimeEvent]:
        kind = item[0]
        if kind == "line":
            events = handle.pipeline.feed_line(item[1])
            due = False
            for event in events:
                due = self._write_event(handle, event) or due
            if due:
                self._checkpoint(handle)
            return events
        if kind == "oversized":
            handle.pipeline.counters["lines"] += 1
            handle.pipeline.counters["oversized"] += 1
        elif kind == "eof":
            handle.stdout_eof = True
        elif kind == "error":
            handle.facts.driver_error = True
            handle.stdout_eof = True
            if not handle.terminated:
                self._terminate_once(handle, "driver-error")
        return []

    def _service_signals(self, handle: RuntimeHandle, *, allow_terminate: bool) -> List[ev.RuntimeEvent]:
        out: List[ev.RuntimeEvent] = []
        while True:
            try:
                found = handle.signal_q.get_nowait()
            except queue.Empty:
                return out
            if found.kind == "approval":
                payload = ev.approval_from_stderr(found, redact=handle.redactor)
                event = self._emit_driver(handle, EventType.APPROVAL_REQUIRED, payload)
                handle.facts.approval = True
                if event is not None:
                    out.append(event)
                if allow_terminate and not handle.terminated:
                    self._terminate_once(handle, "approval")
            elif found.kind == "agent_fallback":
                message = handle.redactor('agent "%s" is missing or not a primary agent; the runtime fell back' % found.name)
                event = self._emit_driver(
                    handle, EventType.ERROR,
                    ev.ErrorPayload(name="AgentFallback", message=message[:256], failure_kind="config"),
                )
                handle.facts.agent_fallback = True
                if event is not None:
                    out.append(event)
                if allow_terminate and not handle.terminated:
                    self._terminate_once(handle, "agent-fallback")

    # ----------------------------------------------------------- observe

    def observe(self, handle: RuntimeHandle) -> Iterator[ev.RuntimeEvent]:
        """Yield the attempt's events as they arrive and finish the attempt.

        Runs the deadline, the approval and cancel checks, the descendant
        rescans and the end-of-attempt bookkeeping. Any exception, and the
        caller abandoning the generator, ends the attempt `interrupted`,
        stops the child and re-raises.
        """
        with handle.record_lock:
            if handle.finalizing or handle.finalized or handle.observer_active:
                return
            handle.observer_active = True
        try:
            yield from self._observe(handle)
        except BaseException as exc:
            self._abort(handle, exc)
            raise
        finally:
            handle.observer_active = False
            self._close(handle)

    def _observe(self, handle: RuntimeHandle) -> Iterator[ev.RuntimeEvent]:
        if not handle.started_yielded:
            handle.started_yielded = True
            if handle.started_event is not None:
                yield handle.started_event
        while True:
            if not handle.terminated:
                if handle.cancel_requested:
                    self._terminate_once(handle, "cancel")
                elif self._monotonic() >= handle.deadline:
                    self._terminate_once(handle, "timeout")
            for event in self._service_signals(handle, allow_terminate=True):
                yield event
            try:
                item = handle.stdout_q.get(timeout=self._poll_s)
            except queue.Empty:
                item = None
            if item is not None:
                for event in self._handle_item(handle, item):
                    yield event
                if handle.facts.approval and not handle.terminated:
                    self._terminate_once(handle, "approval")
            self._maybe_rescan(handle)
            if handle.proc.poll() is not None:
                break
            if handle.stdout_eof:
                now = self._monotonic()
                if handle.eof_at is None:
                    handle.eof_at = now
                elif now - handle.eof_at >= self._eof_grace_s:
                    if not handle.terminated:
                        self._terminate_once(handle, "eof-without-exit")
                    break
        with handle.record_lock:
            handle.finalizing = True
        for event in self._finish(handle):
            yield event

    # ------------------------------------------------- end of an attempt

    def _finish(self, handle: RuntimeHandle) -> List[ev.RuntimeEvent]:
        """Everything after the child stops: drain, join, reap leftovers,
        classify, write the terminal records. The caller has set
        `finalizing`."""
        out: List[ev.RuntimeEvent] = []
        facts, proc = handle.facts, handle.proc
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=self._grace_s + self._kill_grace_s)
            except subprocess.TimeoutExpired:
                code = None
        if code is not None:
            if code < 0:
                facts.exit_signal = -code
            else:
                facts.exit_code = code
        out.extend(self._drain_stdout(handle))
        self._join(handle.stderr_thread, 2.0)
        out.extend(self._service_signals(handle, allow_terminate=False))
        reaped = self._reap_leftovers(handle)
        if reaped:
            handle.leftovers_count += reaped
            self._join(handle.stderr_thread, 1.0)
            out.extend(self._service_signals(handle, allow_terminate=False))
        if (
            handle.resume_mode == "session" and facts.exit_code == 1
            and facts.new_native_events == 0 and not facts.cancelled and not facts.timeout
        ):
            facts.session_lost = True
        after = self._hash_now(
            handle, runstore.SHORT_HASH_TOTAL_BYTES if (facts.timeout or facts.cancelled) else None
        )
        revisions = self._revisions_now(
            handle, runstore.SHORT_REVISIONS_BUDGET_S if (facts.timeout or facts.cancelled) else None
        )
        if after is not None:
            for payload in runstore.diff_hashes(handle.hashes_before, after):
                event = self._emit_driver(handle, EventType.ARTIFACT_REFERENCE, payload)
                if event is not None:
                    out.append(event)
        outcome = classify(facts)
        stopped = self._emit_driver(
            handle, EventType.STOPPED,
            ev.StoppedPayload(
                state=outcome.state, exit_code=outcome.exit_code, signal=outcome.signal,
                reason=outcome.reason, evidence=outcome.evidence or "none",
                step_summary=facts.steps().to_dict(),
            ),
        )
        if stopped is not None:
            out.append(stopped)
        self._write_final(handle, outcome, after, revisions)
        return out

    def _drain_stdout(self, handle: RuntimeHandle) -> List[ev.RuntimeEvent]:
        """Read what is left on stdout. It can stay open after the child exits
        because a descendant inherited it: the wait is bounded, leftovers are
        reaped once, then the wait is bounded again."""
        out: List[ev.RuntimeEvent] = []
        deadline = self._monotonic() + self._exit_drain_s
        reaped_once = False
        while not handle.stdout_eof:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                if not reaped_once:
                    reaped_once = True
                    handle.leftovers_count += self._reap_leftovers(handle)
                    deadline = self._monotonic() + self._exit_drain_s
                    continue
                break
            try:
                item = handle.stdout_q.get(timeout=min(self._poll_s, remaining))
            except queue.Empty:
                continue
            out.extend(self._handle_item(handle, item))
        if not handle.stdout_eof:
            counters = handle.pipeline.counters
            counters["truncated-after-exit"] = counters.get("truncated-after-exit", 0) + 1
            handle.closing.set()
        return out

    @staticmethod
    def _join(thread: Optional[threading.Thread], timeout: float) -> None:
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _hash_now(
        self, handle: RuntimeHandle, max_total_bytes: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        extra = {} if max_total_bytes is None else {"max_total_bytes": max_total_bytes}
        try:
            return runstore.hash_inputs(
                self.project_root, handle.task, handle.prepared.request.context_refs, **extra
            )
        except (runstore.RunStoreError, OSError):
            return None

    def _revisions_now(
        self, handle: RuntimeHandle, budget_s: Optional[float] = None
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            if budget_s is None:
                return runstore.repo_revisions(self.project_root)
            return runstore.repo_revisions(self.project_root, budget_s=budget_s)
        except Exception:  # noqa: BLE001 - revisions are advisory
            return None

    def _write_final(
        self, handle: RuntimeHandle, outcome: AttemptOutcome,
        after_hashes: Optional[Mapping[str, Any]], revisions: Optional[List[Dict[str, Any]]],
    ) -> None:
        """Write the terminal attempt, record and checkpoint once."""
        try:
            handle.writer.fsync()
            everything = runstore.read_sidecar(handle.writer.path).events
            totals = ev.usage_totals(everything)
            usage = ev.to_retry_usage(totals)
        except (OSError, runstore.RunStoreError):
            totals, usage = ev.UsagePayload(), retry.Usage(None, None)
        with handle.record_lock:
            if handle.finalized:
                return
            record, entry = handle.record, handle.entry
            now = _iso(self._clock)
            with handle.tracked_lock:
                descendants = sorted(
                    ({"pid": i.pid, "start": i.start} for i in handle.tracked.values() if i.pid != handle.proc.pid),
                    key=lambda d: d["pid"],
                )
            entry.update(
                ended_at=now, exit_code=outcome.exit_code, signal=outcome.signal,
                state=outcome.state, reason=outcome.reason, evidence=outcome.evidence,
                counters=dict(handle.pipeline.counters), descendants=descendants,
                leftovers_reaped=handle.leftovers_count > 0,
                leftovers_reaped_count=handle.leftovers_count,
                input_hashes_after=dict(after_hashes) if after_hashes is not None else None,
                repo_revisions_after=[dict(r) for r in revisions] if revisions is not None else None,
            )
            record["counters_total"] = runstore.add_counters(handle.counters_base, handle.pipeline.counters)
            record["usage_totals"] = totals.to_dict()
            record["retry_usage"] = {
                "tokens": usage.tokens, "cost": None if usage.cost is None else str(usage.cost),
            }
            record["stderr_tail"] = handle.redactor("\n".join(list(handle.stderr_ring)))
            record["resume_blocked"] = outcome.resume_blocked
            record["outcome"] = {
                "state": outcome.state, "evidence": outcome.evidence, "reason": outcome.reason,
                "exit_code": outcome.exit_code, "signal": outcome.signal,
                "new_native_events": outcome.new_native_events,
            }
            if after_hashes is not None:
                record["input_hashes"] = dict(after_hashes)
            if revisions is not None:
                record["repo_revisions"] = [dict(r) for r in revisions]
            if record.get("state") == "running":
                transition(record, outcome.state, outcome.reason, now)
            else:
                record["updated_at"] = now
            steps = handle.facts.steps()
            runstore.write_record(handle.directory, record)
            runstore.write_checkpoint(
                handle.directory,
                runstore.new_checkpoint(
                    handle.run_id, handle.attempt, last_sequence=handle.last_sequence,
                    sidecar_offset=handle.writer.offset, native_session_id=handle.native_session_id,
                    repo_revisions=revisions if revisions is not None else handle.prepared.repo_revisions,
                    step_open=steps.open_step, ran_anything=handle.ran_anything,
                    state_changing_part_ids=handle.state_changing, clock=self._clock,
                ),
            )
            handle.outcome = outcome
            handle.finalized = True

    def _abort(self, handle: RuntimeHandle, exc: BaseException) -> None:
        """Stop the child and record the attempt `interrupted` after an
        exception or an abandoned observer. Never raises."""
        reason = "observer-closed" if isinstance(exc, GeneratorExit) else "driver-error"
        try:
            self._terminate_once(handle, "abort")
        except Exception:  # noqa: BLE001
            self._kill_now(handle)
        if handle.finalized:
            return
        code = handle.proc.poll()
        exit_code = code if code is not None and code >= 0 else None
        exit_signal = -code if code is not None and code < 0 else None
        outcome = AttemptOutcome(
            state="interrupted", reason=reason, exit_code=exit_code, signal=exit_signal,
            new_native_events=handle.facts.new_native_events,
        )
        try:
            self._emit_driver(
                handle, EventType.STOPPED,
                ev.StoppedPayload(
                    state="interrupted", exit_code=exit_code, signal=exit_signal, reason=reason,
                    evidence="none", step_summary=handle.facts.steps().to_dict(),
                ),
            )
        except Exception:  # noqa: BLE001 - the sidecar may be the thing that failed
            pass
        try:
            self._write_final(handle, outcome, None, None)
        except Exception:  # noqa: BLE001
            handle.outcome = outcome

    def _close(self, handle: RuntimeHandle) -> None:
        """Release pipes, threads and the sidecar. Never raises."""
        handle.closing.set()
        try:
            for thread in (handle.stdout_thread, handle.stderr_thread):
                self._join(thread, 1.0)
            for thread, stream in ((handle.stdout_thread, handle.proc.stdout), (handle.stderr_thread, handle.proc.stderr)):
                if stream is not None and (thread is None or not thread.is_alive()):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            try:
                handle.writer.close()
            except OSError:
                pass
            handle.proc.poll()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------- descendants

    def _expand_tracked(self, handle: RuntimeHandle, table: Mapping[int, Any]) -> List[Identity]:
        """Add every descendant of the child and of tracked live processes,
        and every live member of the run group, to the tracked set."""
        added: List[Identity] = []
        me = os.getpid()

        def track(info: Any) -> None:
            if info.pid == me or info.pid <= 1:
                return
            known = handle.tracked.get(info.pid)
            if known is None or known.start != info.start:
                ident = Identity(info.pid, info.start)
                handle.tracked[info.pid] = ident
                added.append(ident)

        with handle.tracked_lock:
            roots: List[int] = []
            child = handle.proc
            if child.poll() is None:
                info = table.get(child.pid)
                if info is not None and (handle.child_start is None or info.start == handle.child_start):
                    track(info)
                    roots.append(child.pid)
            for ident in list(handle.tracked.values()):
                if ident.pid != child.pid and self._proc.alive(ident, table):
                    roots.append(ident.pid)
            for root in roots:
                for info in self._proc.descendants(root, table):
                    track(info)
            if not handle.finalized and handle.pgid:
                for info in self._proc.group_members(handle.pgid, table):
                    track(info)
        return added

    def _maybe_rescan(self, handle: RuntimeHandle) -> None:
        now = self._monotonic()
        if now - handle.last_scan < self._descendant_scan_s:
            return
        handle.last_scan = now
        table = self._proc.snapshot()
        if table is None:
            return
        if self._expand_tracked(handle, table):
            with handle.record_lock:
                if handle.finalized:
                    return
                with handle.tracked_lock:
                    handle.entry["descendants"] = sorted(
                        ({"pid": i.pid, "start": i.start} for i in handle.tracked.values() if i.pid != handle.proc.pid),
                        key=lambda d: d["pid"],
                    )
                runstore.write_record(handle.directory, handle.record)

    def _reap_leftovers(self, handle: RuntimeHandle) -> int:
        """Terminate anything of this attempt still alive after the child
        exited. Returns how many processes were found."""
        table = self._proc.snapshot()
        if table is None:
            return 0
        self._expand_tracked(handle, table)
        remaining = self._remaining(handle, table)
        if not remaining:
            return 0
        self._terminate(handle, self._leftover_grace_s)
        return len(remaining)

    # ------------------------------------------------------ termination

    def _terminate_once(
        self, handle: RuntimeHandle, reason: str, grace_s: Optional[float] = None
    ) -> CancellationResult:
        """The single reaper. The first caller terminates and stores the
        result; every later caller gets the same result."""
        with handle.cancel_lock:
            if handle.cancel_result is not None:
                return handle.cancel_result
            flag = _REASON_FLAGS.get(reason)
            if flag is not None:
                setattr(handle.facts, flag, True)
            result = self._terminate(handle, self._grace_s if grace_s is None else grace_s)
            handle.terminated_reason = reason
            handle.cancel_result = result
            return result

    def _remaining(self, handle: RuntimeHandle, table: Mapping[int, Any]) -> List[int]:
        with handle.tracked_lock:
            live = {i.pid for i in handle.tracked.values() if self._proc.alive(i, table)}
        if not handle.finalized and handle.pgid:
            live.update(m.pid for m in self._proc.group_members(handle.pgid, table))
        live.discard(os.getpid())
        return sorted(live)

    def _killpg(self, pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _send(self, handle: RuntimeHandle, table: Mapping[int, Any], sig: int,
              only: Optional[Sequence[Identity]] = None) -> bool:
        """Signal every group that still holds a live tracked process, and
        the run group while it has live members. A process whose group is the
        driver's own is signalled by pid."""
        with handle.tracked_lock:
            idents = list(handle.tracked.values()) if only is None else list(only)
        groups: Dict[int, List[Identity]] = {}
        for ident in idents:
            if ident.pid != os.getpid() and self._proc.alive(ident, table):
                groups.setdefault(table[ident.pid].pgid, []).append(ident)
        if only is None and not handle.finalized and handle.pgid and self._proc.group_members(handle.pgid, table):
            groups.setdefault(handle.pgid, [])
        own = os.getpgrp()
        for pgid, members in groups.items():
            if pgid == own or pgid <= 1:
                for ident in members:
                    self._kill(ident.pid, sig)
            else:
                self._killpg(pgid, sig)
        return bool(groups)

    def _terminate(
        self, handle: RuntimeHandle, grace_s: float = 5.0, kill_grace_s: Optional[float] = None
    ) -> CancellationResult:
        """Stop the child and every descendant found by walking the process
        table: SIGTERM, wait up to `grace_s`, then SIGKILL what remains.

        Zombies count as dead. A group is signalled only while a live member
        is verified in a fresh snapshot, so a recycled group id is not hit.
        With no trustworthy process table it falls back to killing the run
        group and reports the remaining descendants as unknown.

        Residual: once the leader has been reaped its group id could in
        principle be reused by an unrelated group. The group is therefore
        signalled before the leader is reaped, and never after the handle is
        finalized; a reuse within the grace window remains theoretically
        possible and is accepted.
        """
        kill_grace = self._kill_grace_s if kill_grace_s is None else kill_grace_s
        proc = handle.proc
        started = self._monotonic()
        table = self._proc.snapshot()
        if table is None:
            return self._terminate_group_only(handle, grace_s, kill_grace, started)
        proc.poll()
        self._expand_tracked(handle, table)
        signalled = self._send(handle, table, _signal.SIGTERM)
        found = self._descendants_found(handle)
        started = self._monotonic()
        deadline = started + grace_s
        escalated = False
        remaining: List[int] = self._remaining(handle, table)
        while remaining:
            time.sleep(self._poll_s)
            proc.poll()
            table = self._proc.snapshot()
            if table is None:
                break
            added = self._expand_tracked(handle, table)
            if added:
                self._send(handle, table, _signal.SIGTERM, only=added)
            remaining = self._remaining(handle, table)
            if not remaining or self._monotonic() >= deadline:
                break
        if table is None:
            # No trustworthy process table: kill the run group and the child
            # outright rather than waiting on a view that cannot be refreshed.
            escalated = True
            self._force_kill(handle)
        elif remaining:
            escalated = True
            self._send(handle, table, _signal.SIGKILL)
            kill_deadline = self._monotonic() + kill_grace
            while remaining and self._monotonic() < kill_deadline:
                time.sleep(self._poll_s)
                proc.poll()
                table = self._proc.snapshot()
                if table is None:
                    self._force_kill(handle)
                    break
                self._expand_tracked(handle, table)
                remaining = self._remaining(handle, table)
                if remaining:
                    self._send(handle, table, _signal.SIGKILL)
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            pass
        final = self._proc.snapshot()
        if final is None:
            return CancellationResult(
                signalled_term=signalled, escalated_kill=escalated,
                group_empty=not self._group_alive(handle.pgid),
                descendants_found=self._descendants_found(handle), descendants_remaining=None,
                exit_code=proc.returncode, duration_s=self._monotonic() - started,
            )
        self._expand_tracked(handle, final)
        left = [pid for pid in self._remaining(handle, final) if pid != proc.pid]
        return CancellationResult(
            signalled_term=signalled,
            escalated_kill=escalated,
            group_empty=not self._proc.group_members(handle.pgid, final) if handle.pgid else True,
            descendants_found=max(found, self._descendants_found(handle)),
            descendants_remaining=len(left),
            exit_code=proc.returncode,
            duration_s=self._monotonic() - started,
        )

    def _descendants_found(self, handle: RuntimeHandle) -> int:
        with handle.tracked_lock:
            return len([p for p in handle.tracked if p != handle.proc.pid])

    def _group_alive(self, pgid: Optional[int]) -> bool:
        """Whether the group still has a member. Probes the group itself, so
        it stays correct after the direct child has exited."""
        if not pgid or pgid <= 1 or pgid == os.getpgrp():
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group exists but cannot be probed: treat it as alive.
            return True
        return True

    def _force_kill(self, handle: RuntimeHandle) -> None:
        if handle.pgid and handle.pgid > 1 and handle.pgid != os.getpgrp() and not handle.finalized:
            self._killpg(handle.pgid, _signal.SIGKILL)
        try:
            handle.proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _terminate_group_only(
        self, handle: RuntimeHandle, grace_s: float, kill_grace: float, started: float
    ) -> CancellationResult:
        proc = handle.proc
        signalled = False
        escalated = False
        if self._group_alive(handle.pgid):
            signalled = True
            self._killpg(handle.pgid, _signal.SIGTERM)
            proc.poll()
            deadline = self._monotonic() + grace_s
            while self._group_alive(handle.pgid) and self._monotonic() < deadline:
                time.sleep(self._poll_s)
                proc.poll()
            if self._group_alive(handle.pgid):
                escalated = True
        if escalated or (not handle.pgid and proc.poll() is None):
            self._force_kill(handle)
        try:
            proc.wait(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            pass
        return CancellationResult(
            signalled_term=signalled, escalated_kill=escalated, group_empty=not self._group_alive(handle.pgid),
            descendants_found=self._descendants_found(handle), descendants_remaining=None,
            exit_code=proc.returncode, duration_s=self._monotonic() - started,
        )

    def cancel(self, handle: RuntimeHandle, grace_s: float = 5.0) -> CancellationResult:
        """Stop the attempt and return how it went. Thread-safe and
        idempotent: a second call returns the stored first result.

        While an observer is active the observer writes the terminal record;
        otherwise this call does. It blocks for up to `grace_s` plus the kill
        grace, so signal handlers must use `handle.request_cancel()`.
        Releasing any phase lock stays with the caller.
        """
        result = self._terminate_once(handle, "cancel", grace_s)
        if handle.finalized:
            return result
        take = False
        with handle.record_lock:
            if not handle.observer_active and not handle.finalizing and not handle.finalized:
                handle.finalizing = True
                take = True
        if take:
            try:
                self._finish(handle)
            except BaseException as exc:
                self._abort(handle, exc)
                raise
            finally:
                self._close(handle)
        return result

    # ------------------------------------------------- orphans and resume

    def reconcile_run(self, record: Dict[str, Any]) -> bool:
        """Deal with a run recorded as running whose driver process is gone.

        The recorded child, its descendants and the members of its group are
        stopped (SIGTERM, a grace period, then SIGKILL), the open attempt is
        marked `driver_lost` and the attempt and the run become
        `interrupted(driver-lost)`. A run whose driver is alive, or a process
        listing that cannot be trusted, changes nothing. Returns true when the
        run was reconciled.
        """
        table = self._proc.snapshot()
        if runstore.orphan_state(record, table) != "driver-lost":
            return False
        self._reap_identities(runstore.live_identities(record, table))
        directory = runstore.store_dir(self.project_root)
        with self._record_lock:
            if record.get("state") != "running":
                return False
            now = _iso(self._clock)
            attempt = next(
                (a for a in reversed(record.get("attempts") or []) if a.get("state") == "running"), None
            )
            if attempt is not None:
                attempt.update(driver_lost=True, state="interrupted", reason="driver-lost", ended_at=now)
            transition(record, "interrupted", "driver-lost", now)
            runstore.write_record(directory, record)
        return True

    def reconcile_task(self, task: str) -> Optional[Dict[str, Any]]:
        """Reconcile the run the task's pointer names. Returns the record, or
        `None` when the task has no run."""
        directory = runstore.store_dir(self.project_root)
        pointer = runstore.load_pointer(directory, task)
        if not pointer:
            return None
        record = runstore.load_record(directory, pointer["run_id"])
        if record is None:
            return None
        self.reconcile_run(record)
        return record

    def _reap_identities(self, identities: Sequence[Identity]) -> None:
        """Terminate recorded processes. A group is signalled only while a
        member whose identity still matches is alive in a fresh snapshot."""
        if not identities:
            return
        for sig, wait in ((_signal.SIGTERM, self._grace_s), (_signal.SIGKILL, self._kill_grace_s)):
            table = self._proc.snapshot()
            if table is None:
                return
            live = [i for i in identities if self._proc.alive(i, table)]
            if not live:
                return
            groups: Dict[int, List[Identity]] = {}
            for ident in live:
                groups.setdefault(table[ident.pid].pgid, []).append(ident)
            own = os.getpgrp()
            for pgid, members in groups.items():
                if pgid == own or pgid <= 1:
                    for ident in members:
                        self._kill(ident.pid, sig)
                else:
                    self._killpg(pgid, sig)
            end = self._monotonic() + wait
            while self._monotonic() < end:
                time.sleep(self._poll_s)
                table = self._proc.snapshot()
                if table is None or not any(self._proc.alive(i, table) for i in identities):
                    break
        return

    def _block_resume(self, record: Dict[str, Any], directory: Path, reason: str) -> ResumeBlocked:
        """Record why a resume was refused, without changing the run state."""
        with self._record_lock:
            now = _iso(self._clock)
            record["resume_blocked"] = reason
            record["updated_at"] = now
            attempts = record.get("attempts") or []
            if attempts and attempts[-1].get("state") == _STAGED:
                attempts[-1]["reason"] = "resume-blocked: " + reason
            try:
                runstore.write_record(directory, record)
            except (OSError, runstore.RunStoreError):
                pass
        return ResumeBlocked(reason, record.get("run_id"))

    def resume(
        self, handoff: Handoff, prepared: PreparedRun, *, deadline_s: Optional[float] = None
    ) -> RuntimeHandle:
        """Start the next attempt of an interrupted or failed run.

        `prepared` comes from `prepare(..., resume_run_id=...)`, which staged
        the attempt. The choice, in order: a run that ever lost its driver, a
        run with an open step, an unverified step-settling capability or a
        missing session is blocked (`ResumeBlocked`, nothing spawned); a run in
        which nothing ran starts over as a fresh attempt; anything else
        continues the native session. Continuation attempts carry the whole
        run's history: the deduper, the next sequence number, the counters and
        the evidence-downgrading facts.
        """
        if prepared.artifact_paths is None or prepared.launch_env is None:
            raise ValueError("the prepared run has no launch environment")
        if prepared.run_id != handoff.run_id:
            raise ValueError("the handoff belongs to a different run")
        directory = prepared.artifact_paths.sidecar.parent
        record = runstore.load_record(directory, prepared.run_id)
        if record is None:
            raise ValueError("the run has no record")
        self.reconcile_run(record)
        if record.get("state") not in ("interrupted", "failed"):
            raise IllegalTransition("a run in state %s cannot be resumed" % record.get("state"))

        read = runstore.read_sidecar(prepared.artifact_paths.sidecar, repair=True)
        if read.torn_tail:
            counters = record.setdefault("counters_total", {})
            counters["torn-tail"] = counters.get("torn-tail", 0) + 1
        events = read.events
        last_sequence = events[-1].sequence if events else 0
        if last_sequence < handoff.last_sequence:
            raise self._block_resume(record, directory, "sidecar-behind-checkpoint")

        facts = runstore.run_facts(events)
        session = handoff.native_session_id
        if session is None:
            session = next((e.session_id for e in reversed(events) if e.origin == "native" and e.session_id), None)
        session_invalid = session is not None and not NATIVE_SESSION_RE.fullmatch(session)
        if session_invalid:
            session = None
        blocked: Optional[str] = None
        if any(a.get("driver_lost") for a in record.get("attempts") or []):
            blocked = "effect-uncertain"
        elif record.get("resume_blocked") == "session-lost":
            blocked = "session-lost"
        elif facts.ran_anything:
            if facts.last_attempt_step_open or handoff.step_open:
                blocked = "effect-uncertain"
            elif not STEP_SETTLING_VERIFIED:
                blocked = "effect-uncertain"
            elif session_invalid:
                blocked = "session-invalid"
            elif session is None:
                blocked = "effect-uncertain"
        if blocked is not None:
            raise self._block_resume(record, directory, blocked)

        if facts.ran_anything:
            argv = [
                prepared.argv[0], "run", "--format", "json", "--session", session,
                "--agent", prepared.agent or names.role_agent_name(prepared.role), "--", RESUME_MESSAGE,
            ]
            mode = "session"
        else:
            argv = list(prepared.argv)
            mode = "fresh"
            session = None
        checkpoint = runstore.load_checkpoint(directory, prepared.run_id) or {}
        seed = AttemptSeed(
            resume_mode=mode, start_sequence=last_sequence + 1, deduper=ev.Deduper.from_events(events),
            facts=AttemptFacts.seed(facts), native_session_id=session, ran_anything=facts.ran_anything,
            record=record, state_changing=checkpoint.get("state_changing_part_ids") or (),
        )
        launch_env.verify_compiled(prepared.config_path, prepared.native_sha256)
        with self._record_lock:
            record["resume_blocked"] = None
            transition(record, "running", None, _iso(self._clock))
            runstore.write_record(directory, record)
        return self._spawn_attempt(prepared, argv, deadline_s=deadline_s, seed=seed)
