"""Shared, non-collected helpers for the cost and run-telemetry tests.

`Synth` builds a project with a real run store and writes synthetic run
records, sidecars and ledgers through `runstore`, so the cost and hook code
reads what it reads in production. `CostProject` wraps the installed
end-to-end world with a task folder and a seeded ledger.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter import runstore

import _opencode_helpers as helpers

SOURCE_DIR = helpers.SOURCE_DIR
TASK = "demo"
HEADER = "# Cost Ledger — %s\n" % TASK
SEED_ROW = "seed-row-1 | 2026-01-01 | plan | seed-model | task | seeded | 0\n"
FIXED_NOW = 1_800_000_000.0


def clock() -> float:
    return FIXED_NOW


def stamp(offset: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FIXED_NOW + offset))


def usage_event(
    run_id: str, sequence: int, part_id: Optional[str], *, input_tokens: Any = 10,
    output_tokens: Any = 2, reasoning_tokens: Any = 0, cost: Any = "0.001", revision: int = 1,
    session: str = "ses_1", attempt: int = 1,
) -> ev.RuntimeEvent:
    payload = ev.UsagePayload(
        input_tokens=input_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
        cache_read_tokens=0, cache_write_tokens=0, cost=cost, finish_reason="stop",
    )
    native = ev.NativeRef(type="step-finish", id=part_id, revision=revision) if part_id else ev.NativeRef(
        type="step-finish", id=None, revision=revision)
    return ev.RuntimeEvent(
        schema_version=ev.SCHEMA_VERSION, run_id=run_id, attempt=attempt, sequence=sequence,
        session_id=session, parent_id=None, timestamp="2026-01-01T00:00:00.000Z",
        observed_at="2026-01-01T00:00:00.000Z", type=ev.EventType.USAGE, origin="native",
        native=native, payload=payload, revision=revision,
    )


def attempt(number: int = 1, *, pid: Optional[int] = 4242, state: str = "completed",
            started: int = 0, ended: Optional[int] = 5, before: Any = None, after: Any = None,
            **extra: Any) -> Dict[str, Any]:
    entry = runstore.new_attempt(
        number, pid=pid, pgid=pid, child_start="t0" if pid else None, driver_pid=1234,
        driver_start="d0", resume_mode="fresh", input_hashes_before=before, clock=lambda: FIXED_NOW + started,
    )
    entry["state"] = state
    entry["ended_at"] = stamp(ended) if ended is not None else None
    entry["input_hashes_after"] = after
    entry.update(extra)
    return entry


class Synth:
    """A project root with a task folder, a ledger and a run store."""

    def __init__(self, tmp_path: Path, *, task_folder: bool = True, ledger: bool = True,
                 root: Optional[Path] = None, task: str = TASK) -> None:
        self.root = Path(root) if root is not None else Path(tmp_path) / "proj"
        self.root.mkdir(parents=True, exist_ok=True)
        self.task = task
        if task_folder:
            (self.root / ".workflow_artifacts" / task).mkdir(parents=True, exist_ok=True)
            if ledger:
                self.ledger.write_text("# Cost Ledger \u2014 %s\n" % task + SEED_ROW, encoding="utf-8")
        self.directory = runstore.store_dir(self.root, create=True)

    @property
    def ledger(self) -> Path:
        return self.root / ".workflow_artifacts" / self.task / "cost-ledger.md"

    def rows(self) -> List[str]:
        if not self.ledger.exists():
            return []
        return [ln for ln in self.ledger.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]

    def rows_for(self, run_id: str) -> List[str]:
        return [r for r in self.rows() if r.split("|")[0].strip() == run_id]

    def seed(
        self, state: str = "completed", *, phase: str = "plan", stage: Any = None,
        attempts: Optional[Sequence[Dict[str, Any]]] = None, events: Optional[Sequence[Any]] = None,
        prepared: Optional[Dict[str, Any]] = None, task: Optional[str] = None, pointer: bool = True,
        resume_blocked: Optional[str] = None, request: Optional[dict] = None,
        usage: bool = True, extra: Optional[dict] = None,
    ) -> str:
        task = task or self.task
        run_id, _paths = runstore.reserve_run_id(self.directory)
        req = {"task": task, "stage": stage, "phase": phase, "profile": "work", "effort": None}
        req.update(request or {})
        record = runstore.new_run_record(run_id, task, req, dict(self.prepared_summary(), **(prepared or {})))
        record["state"] = state
        record["resume_blocked"] = resume_blocked
        record["attempts"] = list(attempts if attempts is not None else [attempt()])
        if state not in ("prepared", "running"):
            record["outcome"] = {
                "state": state, "evidence": "full" if state == "completed" else "none", "reason": None,
                "exit_code": 0, "signal": None, "new_native_events": 1,
            }
        record.update(extra or {})
        runstore.write_record(self.directory, record)
        if events is None and usage:
            events = [usage_event(run_id, 1, "prt_f1")]
        with runstore.SidecarWriter(runstore.run_paths(self.directory, run_id).sidecar) as writer:
            for item in events or ():
                writer.write(item)
        if pointer:
            runstore.write_pointer(self.directory, runstore.new_pointer(task, run_id))
        return run_id

    @staticmethod
    def prepared_summary() -> Dict[str, Any]:
        return {
            "effective_model": "quoin-p/some-model", "provider": "p", "native_provider": "quoin-p",
            "configured_effort": "high", "effort_origin": "default", "variant": "quoin-high",
            "effort_diagnostic": None, "model_priced": False, "effort": None,
        }

    def record(self, run_id: str) -> Dict[str, Any]:
        record = runstore.load_record(self.directory, run_id)
        assert record is not None
        return record

    def set_pointer(self, run_id: str, task: Optional[str] = None) -> None:
        runstore.write_pointer(self.directory, runstore.new_pointer(task or self.task, run_id))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class HookWorld:
    """A git project holding a complete multi-stage task (`t1`) with a seeded
    cost ledger, for the hook and gate tests."""

    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:
        import _opencode_gate_helpers as gh

        self.gh = gh
        self.fx = gh.Fixture(tmp_path, monkeypatch)
        self.root = self.fx.root
        self.task = self.fx.task
        self.synth = Synth(tmp_path, root=self.root, task=self.task)
        self.stage_rel = ".workflow_artifacts/%s/stage-1" % self.task
        self.mark = cost_mark(self)

    def path(self, rel: str) -> Path:
        return self.root / ".workflow_artifacts" / self.task / rel

    def exec_run(
        self, phase: str, *, stage: Any = 1, writes: Optional[Dict[str, str]] = None,
        edit: Any = None, state: str = "completed", attempts: Optional[list] = None, **seed_kw: Any,
    ) -> str:
        """Write a run record whose before and after hashes bracket `writes`
        (task-relative paths) and `edit` (a callable run between them)."""
        before = runstore.hash_inputs(self.root, self.task)
        for rel, text in (writes or {}).items():
            self.gh.write(self.path(rel), text)
        if edit is not None:
            edit()
        after = runstore.hash_inputs(self.root, self.task)
        items = attempts if attempts is not None else [attempt(before=before, after=after, state=state)]
        return self.synth.seed(
            state, phase=phase, stage=stage if stage is None else str(stage), attempts=items, **seed_kw)

    def result(self, run_id: str, outcome: str = "COMPLETED", blocked: Optional[str] = None) -> Any:
        from quoin.opencode_adapter import phase_loop

        return phase_loop.PhaseResult(outcome=outcome, run_id=run_id, resume_blocked=blocked)

    def hook(self, run_id: str, *, outcome: str = "COMPLETED", blocked: Optional[str] = None,
             candidate: Optional[str] = None, source_dir: Any = SOURCE_DIR, mark: Any = "default") -> Any:
        from quoin.opencode_adapter import run_hooks

        return run_hooks.after_phase_run(
            self.root, self.task, self.result(run_id, outcome, blocked),
            mark=self.mark if mark == "default" else mark, source_dir=source_dir,
            superseded_candidate=candidate, clock=clock)

    def entries(self) -> List[Dict[str, Any]]:
        directory = runstore.store_dir(self.root)
        state = runstore.load_workflow_state(directory, self.task)
        return list((state or {}).get("entries") or [])

    def live(self, phase: str, stage: Optional[int] = 1) -> Optional[Dict[str, Any]]:
        directory = runstore.store_dir(self.root)
        state = runstore.load_workflow_state(directory, self.task)
        return runstore.current_entry(state, stage, phase) if state else None

    def evaluate(self, phase: str, stage: Optional[int] = 1) -> Any:
        return self.fx.evaluate(phase, stage=stage)


def cost_mark(world: "HookWorld") -> Dict[str, Any]:
    from quoin.opencode_adapter import cost

    return cost.ledger_mark(world.root, world.task, clock)
