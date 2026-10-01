"""Shared, non-collected helpers for the phase-loop and `quoin run` tests.

`ScriptedDriver` stands in for the OpenCode driver: each attempt pops the
next scripted outcome (or an exception to raise), while the run record,
pointer and checkpoint live in a real run store so the code that reads them
runs for real.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from quoin.opencode_adapter import driver, launch_env, phase_loop, retry, runstore

TASK = "demo"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecToken(phase_loop.CancelToken):
    """A cancel token that records waits, attaches and detaches."""

    def __init__(self, clock: Optional[FakeClock] = None, on_sleep: Optional[Callable[[float], None]] = None):
        self.clock = clock or FakeClock()
        self.delays: List[float] = []
        self.sleeps: List[float] = []
        self.events: List[str] = []
        self._on_sleep = on_sleep

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.advance(seconds)
            if self._on_sleep is not None:
                self._on_sleep(seconds)

        super().__init__(sleep=sleep, monotonic=self.clock)

    def wait(self, seconds: float) -> bool:
        self.delays.append(seconds)
        return super().wait(seconds)

    def attach(self, handle: Any) -> None:
        self.events.append("attach")
        super().attach(handle)

    def detach(self) -> None:
        self.events.append("detach")
        super().detach()


@dataclass
class Boom:
    """Raise `exc` instead of returning an outcome, at `where`."""

    exc: BaseException
    where: str = "observe"


def outcome(state: str = "completed", **kwargs: Any) -> "driver.AttemptOutcome":
    if state == "completed":
        kwargs.setdefault("evidence", "full")
    kwargs.setdefault("new_native_events", 3)
    return driver.AttemptOutcome(state=state, **kwargs)


class SpyPolicy:
    """Records calls to a real retry policy."""

    def __init__(self, inner: "retry.RetryPolicy") -> None:
        self.inner = inner
        self.starts = 0
        self.decisions: List[tuple] = []

    def start(self) -> float:
        self.starts += 1
        return self.inner.start()

    def decide(self, attempt: int, started_at: float, failure: Any) -> Any:
        self.decisions.append((attempt, started_at, failure))
        return self.inner.decide(attempt, started_at, failure)


class ScriptedDriver:
    def __init__(
        self,
        project_root: Path,
        script: Any = (),
        *,
        clock: Optional[FakeClock] = None,
        limits: Optional[Dict[str, int]] = None,
        events_per_attempt: int = 2,
        attempt_seconds: float = 0.0,
        redactor: Optional["launch_env.Redactor"] = None,
        base: float = 1.0,
        cap: float = 30.0,
    ) -> None:
        self.project_root = Path(project_root)
        self.script = list(script)
        self.clock = clock or FakeClock()
        self.limits = dict(limits or {})
        self.events_per_attempt = events_per_attempt
        self.attempt_seconds = attempt_seconds
        self.redactor = redactor or launch_env.Redactor()
        self.policy = SpyPolicy(
            retry.RetryPolicy.from_limits(
                self.limits, base=base, cap=cap, rng=lambda: 0.5, clock=self.clock
            )
        )
        self.calls: List[tuple] = []
        self.deadlines: List[Optional[float]] = []
        self.handoffs: List[Any] = []
        self.handles: List[Any] = []
        self.on_prepare: Optional[Callable[["ScriptedDriver", int], None]] = None
        self.on_observe: Optional[Callable[["ScriptedDriver", Any], None]] = None
        self.reconcile_error: Optional[BaseException] = None
        self.prepare_errors: Dict[int, BaseException] = {}
        self.prepare_count = 0
        self.last_request: Any = None

    # -- store helpers ----------------------------------------------------
    @property
    def directory(self) -> Path:
        return runstore.store_dir(self.project_root, create=True)

    def seed(self, state: str, *, attempts: Optional[list] = None, request: Optional[dict] = None,
             task: str = TASK, phase: str = "plan", profile: str = "work",
             stage: Optional[str] = None) -> str:
        directory = self.directory
        run_id, _ = runstore.reserve_run_id(directory)
        req = {"task": task, "stage": stage, "phase": phase, "profile": profile}
        req.update(request or {})
        record = runstore.new_run_record(run_id, task, req, {})
        record["state"] = state
        record["attempts"] = list(attempts or [])
        runstore.write_record(directory, record)
        runstore.write_pointer(directory, runstore.new_pointer(task, run_id))
        return run_id

    def record(self, run_id: str) -> Optional[Dict[str, Any]]:
        return runstore.load_record(self.directory, run_id)

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]

    # -- driver surface ---------------------------------------------------
    def reconcile_task(self, task: str) -> Optional[Dict[str, Any]]:
        self.calls.append(("reconcile_task", task))
        if self.reconcile_error is not None:
            raise self.reconcile_error
        pointer = runstore.load_pointer(self.directory, task)
        if not pointer:
            return None
        return runstore.load_record(self.directory, pointer["run_id"])

    def prepare(self, request: Any, *, resume_run_id: Optional[str] = None) -> Any:
        n = self.prepare_count
        self.prepare_count += 1
        self.last_request = request
        self.calls.append(("prepare", resume_run_id))
        if self.on_prepare is not None:
            self.on_prepare(self, n)
        if n in self.prepare_errors:
            raise self.prepare_errors[n]
        directory = self.directory
        if resume_run_id is None:
            run_id, _ = runstore.reserve_run_id(directory)
            req = {"task": request.task, "stage": request.stage, "phase": request.phase,
                   "profile": request.profile}
            runstore.write_record(directory, runstore.new_run_record(run_id, request.task, req, {}))
            runstore.write_pointer(directory, runstore.new_pointer(request.task, run_id))
        else:
            run_id = resume_run_id
        return SimpleNamespace(
            run_id=run_id,
            retry=self.policy,
            policy={"profile": request.profile, "limits": self.limits},
            launch_env=SimpleNamespace(redactor=self.redactor),
        )

    def _attempt(self, kind: str, prepared: Any, deadline_s: Optional[float]) -> Any:
        self.deadlines.append(deadline_s)
        step = self.script.pop(0)
        if isinstance(step, Boom) and step.where in ("start", "resume"):
            raise step.exc
        record = runstore.load_record(self.directory, prepared.run_id)
        if record is not None:
            runstore.set_state(record, "running", None)
            runstore.write_record(self.directory, record)
        handle = SimpleNamespace(
            run_id=prepared.run_id, step=step, cancel_requests=0, outcome=None, kind=kind,
        )

        def request_cancel() -> None:
            handle.cancel_requests += 1

        handle.request_cancel = request_cancel
        self.handles.append(handle)
        return handle

    def start(self, prepared: Any, deadline_s: Optional[float] = None) -> Any:
        self.calls.append(("start", prepared.run_id))
        return self._attempt("start", prepared, deadline_s)

    def resume(self, handoff: Any, prepared: Any, deadline_s: Optional[float] = None) -> Any:
        self.calls.append(("resume", prepared.run_id))
        self.handoffs.append(handoff)
        return self._attempt("resume", prepared, deadline_s)

    def observe(self, handle: Any) -> Any:
        self.calls.append(("observe", handle.run_id))
        if self.on_observe is not None:
            self.on_observe(self, handle)
        for index in range(self.events_per_attempt):
            yield index
        step = handle.step
        self.clock.advance(self.attempt_seconds)
        record = runstore.load_record(self.directory, handle.run_id)
        if isinstance(step, Boom):
            if record is not None:
                runstore.set_state(record, "interrupted", "driver-error")
                runstore.write_record(self.directory, record)
            raise step.exc
        handle.outcome = step
        if record is not None:
            runstore.set_state(record, step.state, step.reason)
            runstore.write_record(self.directory, record)
