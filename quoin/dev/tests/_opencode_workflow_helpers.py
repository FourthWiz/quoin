"""Shared, non-collected helpers for the whole-task coordinator tests.

`CoordWorld` is a git project holding a two-stage task (`t1`) with a seeded
cost ledger. `CoordDriver` stands in for the OpenCode driver: every run
completes by default, optionally after running a per-phase effect that writes
files the way an agent would, and it fills in the run record the hooks read
(request, prepared summary, attempt hashes, a usage event).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import _opencode_cost_helpers as ch
import _opencode_gate_helpers as gh
import _opencode_handoff_helpers as hh
import _opencode_run_helpers as rh
from quoin.opencode_adapter import phase_loop, runstore, workflow

SOURCE_DIR = ch.SOURCE_DIR


class CoordDriver(rh.ScriptedDriver):
    def __init__(self, world: "CoordWorld", state_root: Path) -> None:
        super().__init__(world.root)
        self.world = world
        self.state_root = Path(state_root)
        self.effects: Dict[str, Callable[["CoordDriver", Dict[str, Any]], None]] = {}
        self.requests: List[Any] = []
        self.arguments: List[str] = []
        self.default = rh.outcome()

    def prepare(self, request: Any, *, resume_run_id: Optional[str] = None) -> Any:
        prepared = super().prepare(request, resume_run_id=resume_run_id)
        self.requests.append(request)
        if resume_run_id is None:
            record = runstore.load_record(self.directory, prepared.run_id)
            record["request"] = {
                "task": request.task, "stage": request.stage, "phase": request.phase,
                "profile": request.profile, "effort": None, "workspace": None,
                "non_interactive": bool(request.non_interactive),
                "context_refs": list(request.context_refs),
            }
            record["prepared"] = ch.Synth.prepared_summary()
            runstore.write_record(self.directory, record)
            sidecar = runstore.run_paths(self.directory, prepared.run_id).sidecar
            with runstore.SidecarWriter(sidecar) as writer:
                writer.write(ch.usage_event(prepared.run_id, 1, "prt_" + prepared.run_id[-6:]))
        return prepared

    def _attempt(self, kind: str, prepared: Any, deadline_s: Optional[float]) -> Any:
        if not self.script:
            self.script.append(self.default)
        handle = super()._attempt(kind, prepared, deadline_s)
        handle.before = runstore.hash_inputs(self.project_root, self.world.task)
        return handle

    def observe(self, handle: Any) -> Any:
        record = runstore.load_record(self.directory, handle.run_id)
        effect = self.effects.get(record["request"]["phase"])
        if effect is not None:
            effect(self, record["request"])
        yield from super().observe(handle)
        record = runstore.load_record(self.directory, handle.run_id)
        after = runstore.hash_inputs(self.project_root, self.world.task)
        record["attempts"] = [
            ch.attempt(before=handle.before, after=after, state=handle.outcome.state)
        ]
        runstore.write_record(self.directory, record)


class CoordWorld(ch.HookWorld):
    """`HookWorld` plus a coordinator factory around a `CoordDriver`."""

    def __init__(self, tmp_path: Path, monkeypatch: Any) -> None:
        super().__init__(tmp_path, monkeypatch)
        import io

        from quoin.opencode_adapter import install

        home = Path(tmp_path) / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # the role boundary needs an install record, as in a real project
        code = install.run_install(str(self.root), SOURCE_DIR, None, False, io.StringIO(), io.StringIO())
        assert code == 0
        gh.git(self.root, "add", "-A")
        gh.git(self.root, "commit", "-q", "-m", "install")
        self.drv = CoordDriver(self, Path(tmp_path) / "state-root")
        self.cancel = rh.RecToken()

    def coordinator(self, **option_kw: Any) -> workflow.Coordinator:
        options = workflow.WorkflowOptions(profile="personal", **option_kw)
        return workflow.Coordinator(
            self.drv, self.root, self.task, options, source_dir=SOURCE_DIR, cancel=self.cancel,
            scope=hh.fixed_scope(), scope_source="workflow-state", backoff_fn=lambda n: 0,
        )

    def seed_workflow(self, **block: Any) -> None:
        """A workflow state with the coordinator settings block."""
        directory = runstore.store_dir(self.root, create=True)
        state = runstore.load_workflow_state(directory, self.task) or runstore.new_workflow_state(
            self.task, self.fx.clock)
        workflow.write_settings(
            state, workflow.WorkflowOptions(profile="personal"),
            include_architect=block.get("include_architect", True),
            from_discover=block.get("from_discover", False),
        )
        runstore.write_workflow_state(directory, state)

    def write_plan(self, driver: CoordDriver, request: Dict[str, Any]) -> None:
        stage = request.get("stage")
        rel = "stage-%s/current-plan.md" % stage if stage else "current-plan.md"
        gh.write(self.path(rel), gh.PLAN + "\nrevised by the run\n")

    def record_json(self) -> Optional[Dict[str, Any]]:
        import json

        path = self.root / ".workflow_artifacts" / "memory" / "continuation" / (self.task + ".json")
        return json.loads(path.read_text()) if path.exists() else None


__all__ = ["CoordDriver", "CoordWorld", "phase_loop"]
