"""Whole-task coordinator for the OpenCode runtime.

The coordinator walks a task through discover, architect, and for each stage
plan (with its critic loop), implement and review. Every phase is its own
fresh ``--command`` run driven through ``phase_loop``; the coordinator never
shares a session between phases. It runs under the task lock held by the
caller, records every phase through ``run_hooks``, runs the configured tests
itself after implement, and gates each phase in-process.

This module never imports ``quoin.cli``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import (
    driver, evidence, gate, handoff, phase_loop, run_hooks, runstore, snapshot, testrun, window,
)

DEFAULT_MAX_CRITIC_ROUNDS = 2
MAX_CRITIC_ROUNDS = 5
THROUGH_PHASES: Tuple[str, ...] = ("discover", "architect", "plan", "implement", "review")
RERUN_PHASES: Tuple[str, ...] = ("plan", "implement", "review")


class WorkflowRefused(Exception):
    """The coordinator refused before (or instead of) running a phase."""

    def __init__(self, code: str, message: str = "", exit_code: int = 3) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.exit_code = exit_code


@dataclass
class WorkflowOptions:
    profile: str = "work"
    no_pause: bool = False
    through: Optional[str] = None
    from_discover: bool = False
    max_critic_rounds: Optional[int] = None
    rerun_from: Optional[str] = None
    adopt: Optional[str] = None
    continue_: bool = False
    max_relaunch: int = 3
    halt_on_abort: bool = False
    budget: Optional[str] = None
    test_command: Optional[Sequence[str]] = None
    test_include: Sequence[str] = ()
    test_timeout: Optional[float] = None

    def critic_cap(self) -> int:
        value = self.max_critic_rounds
        if value is None:
            return DEFAULT_MAX_CRITIC_ROUNDS
        return max(1, min(int(value), MAX_CRITIC_ROUNDS))


# ---------------------------------------------------------------------------
# settings and sequence
# ---------------------------------------------------------------------------


def discover_files_missing(project_root) -> bool:
    """True when any of the three discover files is not a plain file."""
    for rel in evidence.DISCOVER_FILES:
        try:
            info = os.lstat(os.path.join(str(project_root), rel))
        except OSError:
            return True
        if not stat.S_ISREG(info.st_mode):
            return True
    return False


def workflow_block(state: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    block = ((state or {}).get("settings") or {}).get("workflow")
    return block if isinstance(block, Mapping) else None


def has_workflow_state(state: Optional[Mapping[str, Any]]) -> bool:
    """True when the state holds an entry or a workflow block; a state that
    only carries test settings does not count."""
    if state is None:
        return False
    return bool(state.get("entries")) or workflow_block(state) is not None


def write_settings(
    state: Dict[str, Any], options: WorkflowOptions, *, include_architect: bool, from_discover: bool,
    clock: Callable[[], float] = time.time,
) -> None:
    """Store the coordinator settings on `state` (the caller writes it)."""
    settings = state["settings"]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    settings["workflow"] = {
        "profile": options.profile,
        "started_at": started,
        "through": options.through,
        "no_pause": bool(options.no_pause),
        "from_discover": bool(from_discover),
        "include_architect": bool(include_architect),
    }
    settings["max_critic_rounds"] = options.critic_cap()
    settings["critic_required"] = True


def stored_cap(state: Optional[Mapping[str, Any]], options: WorkflowOptions) -> int:
    """The critic round cap the loop and the gate both use: the stored one."""
    value = ((state or {}).get("settings") or {}).get("max_critic_rounds")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return min(value, MAX_CRITIC_ROUNDS)
    return options.critic_cap()


def sequence(
    project_root, task: str, state: Optional[Mapping[str, Any]], source_dir, options: WorkflowOptions,
) -> List[Tuple[str, Optional[int]]]:
    """The (phase, stage) items in order. The critic is part of the plan item."""
    block = workflow_block(state)
    include_architect = block.get("include_architect") is not False if block else True
    want_discover = bool(options.from_discover) or (
        bool(block and block.get("from_discover") is True) or discover_files_missing(project_root)
    )
    items: List[Tuple[str, Optional[int]]] = []
    if want_discover:
        items.append(("discover", None))
    if include_architect:
        items.append(("architect", None))
    stages = handoff.listed_stages(project_root, task, source_dir)
    for stage in ([None] if stages is None else stages):
        for phase in ("plan", "implement", "review"):
            items.append((phase, stage))
    return items


def stage_text(stage: Optional[int]) -> Optional[str]:
    """The stage string `RunRequest.stage` carries, as the CLI passes it."""
    return None if stage is None else str(stage)


# ---------------------------------------------------------------------------
# core script loading
# ---------------------------------------------------------------------------


def load_core_script(source_dir, name: str):
    """A core script loaded by file path with its module registered, so
    dataclasses in it resolve their annotations."""
    path = Path(source_dir) / "core" / "scripts" / (name + ".py")
    key = "_quoin_core_" + name
    existing = sys.modules.get(key)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise WorkflowRefused("source-unavailable", "a core script cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - any load failure is the same refusal
        sys.modules.pop(key, None)
        raise WorkflowRefused("source-unavailable", "a core script cannot be loaded") from None
    finally:
        sys.dont_write_bytecode = old
    return module


def protected_branch_repos(project_root, source_dir) -> List[str]:
    """Repositories carrying task commits on a protected branch."""
    if os.environ.get("QUOIN_DISABLE_BRANCH_HYGIENE", "").strip() == "1":
        return []
    mod = load_core_script(source_dir, "branch_hygiene")
    flagged: List[str] = []
    for repo in mod.discover_repos(Path(project_root)):
        result = mod.check_repo(repo)
        if result.has_task_commits:
            flagged.append(os.path.relpath(result.repo, str(project_root)).replace(os.sep, "/"))
    return flagged


# ---------------------------------------------------------------------------
# step results
# ---------------------------------------------------------------------------


@dataclass
class GateOutcome:
    verdict: Optional[str] = None  # PASS or FAIL; None when the artifact could not be written
    reasons: Tuple[str, ...] = ()
    artifact: Optional[str] = None
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


@dataclass
class StepResult:
    """One executed phase run: its loop result, the hook outcome and the gate."""

    phase: str
    stage: Optional[int]
    result: phase_loop.PhaseResult
    hook: Optional[run_hooks.HookOutcome] = None
    gate: Optional[GateOutcome] = None
    tests_ran: bool = False

    @property
    def completed(self) -> bool:
        return self.result.outcome == "COMPLETED"

    @property
    def exit_code(self) -> int:
        return phase_loop.exit_code(self.result)


# ---------------------------------------------------------------------------
# the coordinator
# ---------------------------------------------------------------------------


class Coordinator:
    """State shared by one invocation. The caller holds the task lock for the
    coordinator's whole life."""

    def __init__(
        self, drv: Any, project_root, task: str, options: WorkflowOptions, *, source_dir,
        cancel: phase_loop.CancelToken, scope: Mapping[str, Any], scope_source: str,
        backoff_fn: Optional[Callable[[int], float]] = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self.drv = drv
        self.project_root = Path(project_root)
        self.task = runstore.check_task_name(task)
        self.options = options
        self.source_dir = source_dir
        self.cancel = cancel
        self.scope = scope
        self.scope_source = scope_source
        self.backoff_fn = backoff_fn
        self.clock = clock
        self.state_root = drv.state_root
        self.window = window.WindowStore(self.state_root, self.project_root, self.task)
        self.errors: List[str] = []

    # -- state ---------------------------------------------------------------
    def load_state(self) -> Optional[Dict[str, Any]]:
        directory = runstore.store_dir(self.project_root, create=True)
        return runstore.load_workflow_state(directory, self.task)

    def save_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        runstore.write_workflow_state(runstore.store_dir(self.project_root, create=True), state)

    def sequence(self, state: Optional[Mapping[str, Any]]) -> List[Tuple[str, Optional[int]]]:
        return sequence(self.project_root, self.task, state, self.source_dir, self.options)

    # -- continuation record -------------------------------------------------
    def refresh_record(self) -> Optional[str]:
        """Rewrite the continuation record from state and the tree. While a run
        of the current item is open the artifact shas come from state, so the
        record never moves ahead of it, and both written files are noted in
        that run's window. Best effort: a failure is kept in `errors`."""
        try:
            open_id = run_hooks.open_run(self.project_root, self.task)
            previous = None
            try:
                previous = handoff.core(self.source_dir).load_record(
                    str(handoff.record_path(self.project_root, self.task))
                )
            except Exception:  # noqa: BLE001 - an unusable previous record only loses its decisions
                previous = None
            record = handoff.build_record(
                self.project_root, self.task, scope=self.scope, scope_source=self.scope_source,
                source_dir=self.source_dir, previous=previous, clock=self.clock,
                pin_to_state=open_id is not None,
            )
            path = handoff.write(self.project_root, self.task, record, source_dir=self.source_dir)
            if open_id is not None:
                self._note_record_writes(open_id, path)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            self.errors.append("record-refresh: " + type(exc).__name__)
            return None

    def _note_record_writes(self, run_id: str, path: Path) -> None:
        candidates = [path, path.with_name(path.stem + ".prev.json")]
        for item in candidates:
            try:
                digest = hashlib.sha256(item.read_bytes()).hexdigest()
            except OSError:
                continue
            rel = os.path.relpath(str(item), str(self.project_root)).replace(os.sep, "/")
            self.window.note_write(run_id, rel, digest)

    # -- gate ----------------------------------------------------------------
    def gate(self, stage: Optional[int], phase: str) -> GateOutcome:
        """Evaluate, write the audit artifact and record the verdict."""
        result = gate.evaluate(
            self.project_root, self.task, stage, phase, source_dir=self.source_dir, clock=self.clock
        )
        out = GateOutcome(verdict=result.verdict, reasons=tuple(result.reasons))
        try:
            sdir = gate.stage_dir(self.project_root, self.task, stage, self.source_dir)
            path = gate.write_artifact(
                self.project_root, result, sdir, source_dir=self.source_dir, clock=self.clock
            )
        except gate.PathUnresolved:
            return dataclasses.replace(out, verdict=None, error="path-unresolved")
        except gate.GateArtifactError as exc:
            return dataclasses.replace(out, verdict=None, error=exc.code)
        except OSError as exc:
            return dataclasses.replace(out, verdict=None, error="artifact-write-failed: " + type(exc).__name__)
        out.artifact = os.path.relpath(str(path), str(self.project_root)).replace(os.sep, "/")
        try:
            gate.record_gate(self.project_root, self.task, stage, phase, result, path, clock=self.clock)
        except (runstore.RunStoreError, OSError) as exc:
            return dataclasses.replace(out, verdict=None, error=getattr(exc, "code", type(exc).__name__))
        return out

    # -- plain phases --------------------------------------------------------
    def _resumable_run(self, request: "driver.RunRequest") -> Optional[Dict[str, Any]]:
        """The interrupted, unclosed run of the same request, which `run_phase`
        would resume."""
        existing = self.drv.reconcile_task(request.task)
        if not existing or existing.get("state") != "interrupted":
            return None
        if not phase_loop._same_request(existing, request):  # noqa: SLF001 - same comparison as the loop
            return None
        telemetry = existing.get("telemetry")
        if isinstance(telemetry, dict) and telemetry.get("final") is True:
            return None
        return existing

    def run_plain_phase(
        self, phase: str, stage: Optional[int], *, compose: bool = False,
        context_refs: Sequence[str] = (), new_run: bool = False, gate_after: bool = True,
    ) -> StepResult:
        """Discover, architect, a plan round or implement, in the executor's
        fixed order. A result other than COMPLETED stops after the hook and
        the record refresh: nothing is tested and nothing is gated."""
        root, task, opts = self.project_root, self.task, self.options
        entry_phase = runstore.entry_phase_for_run(phase) or phase

        # 1. the record names the phase about to run
        self.refresh_record()

        # 2. implement preconditions
        if entry_phase == "implement":
            flagged = protected_branch_repos(root, self.source_dir)
            if flagged:
                raise WorkflowRefused(
                    "protected-branch",
                    "task commits are on a protected branch in: %s; move them to a feature branch"
                    % ", ".join(flagged),
                )
            snapshot.record_base_tree(root, task, stage, clock=self.clock)
            problem = testrun.settings_status(root, task, state_root=self.state_root)
            if problem == "tests-settings-changed":
                raise WorkflowRefused(
                    "tests-settings-changed",
                    "the test settings changed since they were configured; configure them again with "
                    "--test-command before running implement",
                )

        request = driver.RunRequest(
            project_root=root, task=task, stage=stage_text(stage), phase=phase, profile=opts.profile,
            budget=opts.budget, non_interactive=True, context_refs=tuple(context_refs),
        )
        resumed = None if new_run else self._resumable_run(request)
        if resumed is not None:
            stored = (resumed.get("request") or {}).get("context_refs")
            request = dataclasses.replace(
                request, context_refs=tuple(stored) if isinstance(stored, (list, tuple)) else (),
            )

        # 3. ledger mark, the run a new one could supersede, the pre-run listing
        mark = run_hooks.before_run(root, task)
        candidate = run_hooks.open_run(root, task)
        listing, complete = self._pre_run_listing(stage, phase, resumed)

        # 4. run
        def bind(prepared: Any) -> None:
            try:
                self.window.bind(prepared.run_id)
            except Exception:  # noqa: BLE001 - a missing window only leaves the boundary partial
                pass

        result = phase_loop.run_phase(
            self.drv, request, max_relaunch=opts.max_relaunch, cancel=self.cancel, new_run=new_run,
            backoff_fn=self.backoff_fn, on_prepared=bind,
        )

        # 5. record
        hook = run_hooks.after_phase_run(
            root, task, result, mark=mark, source_dir=self.source_dir, superseded_candidate=candidate,
            boundary_before=listing, state_root=self.state_root, origin="coordinator", compose=compose,
            other_task_policy="violation", window_complete=complete,
        )
        if hook.violation and result.outcome not in ("CANCELLED", "REFUSED"):
            result = dataclasses.replace(result, outcome="FAILED", reason="boundary-violation")

        # 6. the record follows the entry; a phase that did not complete stops here
        self.refresh_record()
        step = StepResult(phase=entry_phase, stage=stage, result=result, hook=hook)
        if result.run_id and result.outcome == "COMPLETED":
            self.window.drop(result.run_id)
        if result.outcome != "COMPLETED":
            return step

        # 7. implement: the coordinator runs the configured tests itself
        if entry_phase == "implement" and self._tests_configured():
            self.run_implement_tests(stage)
            step.tests_ran = True
            self.refresh_record()

        # 8. gate
        if gate_after:
            step.gate = self.gate(stage, entry_phase)
            self.refresh_record()
        return step

    def _pre_run_listing(self, stage, phase, resumed):
        """`(listing, window_complete)`: the saved window of a resumed run
        reconciled with the coordinator's own writes, else a fresh listing that
        is also saved for the run about to start."""
        root = self.project_root
        if resumed is not None:
            loaded = self.window.load(resumed["run_id"])
            if loaded is not None:
                saved, writes = loaded
                try:
                    now = run_hooks.before_boundary(root, self.task)
                except Exception:  # noqa: BLE001
                    now = None
                if now is not None:
                    return window.reconcile(saved, now, writes, root), True
        listing = run_hooks.before_boundary(root, self.task)
        if listing is not None and resumed is None:
            try:
                self.window.save_pending(stage, phase, listing)
            except Exception as exc:  # noqa: BLE001
                self.errors.append("window-save: " + type(exc).__name__)
        return listing, False

    # -- tests ---------------------------------------------------------------
    def _tests_configured(self) -> bool:
        try:
            return testrun.settings_status(
                self.project_root, self.task, state_root=self.state_root
            ) != "tests-not-configured"
        except Exception:  # noqa: BLE001
            return False

    def run_implement_tests(self, stage: Optional[int]) -> None:
        """Run the configured tests and store the outcome on the live implement
        entry: the result as `tests`, or the reason it could not be had."""
        run = testrun.run_tests(self.project_root, self.task, stage, state_root=self.state_root)
        directory = runstore.store_dir(self.project_root, create=True)
        state = runstore.load_workflow_state(directory, self.task)
        if state is None:
            return
        entry = runstore.current_entry(state, stage, "implement")
        if entry is None:
            return
        data = None
        if run.outcome != testrun.REFUSED:
            data = testrun.read_result(self.state_root, self.project_root, self.task, stage)
        if data is not None:
            entry["tests"] = data
            entry["tests_source"] = "coordinator"
            if data.get("outcome") == testrun.PASSED:
                entry.pop("tests_reason", None)
            else:
                reason = data.get("reason")
                entry["tests_reason"] = reason if isinstance(reason, str) and reason else "tests-failed"
        else:
            entry.pop("tests", None)
            entry["tests_source"] = "coordinator"
            entry["tests_reason"] = run.reason or "tests-not-run"
        self.save_state(state)
