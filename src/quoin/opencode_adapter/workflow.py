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
import shlex
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
    harvested: Optional[str] = None
    regated: bool = False

    @property
    def completed(self) -> bool:
        return self.result.outcome == "COMPLETED"

    @property
    def exit_code(self) -> int:
        return phase_loop.exit_code(self.result)


@dataclass
class Resolution:
    """What to do about one sequence item: `pass` (skip), `run`, `gate` (the
    entry exists and only needs its gate), `report` (a stored non-completed
    outcome), `gate-fail` (a stored FAIL verdict) or `refuse`."""

    kind: str
    outcome: Optional[str] = None
    exit_code: int = 0
    reasons: Tuple[str, ...] = ()
    run_id: Optional[str] = None
    gate_artifact: Optional[str] = None
    code: str = ""
    message: str = ""
    origin: Optional[str] = None


_STORED_OUTCOMES = {
    "awaiting_approval": "AWAITING_APPROVAL",
    "failed": "FAILED",
    "interrupted": "INTERRUPTED",
    "cancelled": "CANCELLED",
    "running": "INTERRUPTED",
    "prepared": "INTERRUPTED",
}
_SNAPSHOT_KINDS = ("critic", "review")


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
        self.seeded: List[Tuple[str, Optional[int]]] = []
        self.rerun_info: Optional[Dict[str, Any]] = None
        self._new_run_for: Optional[Tuple[str, Optional[int]]] = None
        self._rerun_refs: Tuple[str, ...] = ()

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

    def run_implement_tests(self, stage: Optional[int], phase: str = "implement") -> None:
        """Run the configured tests and store the outcome on the live entry of
        `phase` (implement or review): the result as `tests`, or the reason it
        could not be had."""
        run = testrun.run_tests(self.project_root, self.task, stage, state_root=self.state_root)
        directory = runstore.store_dir(self.project_root, create=True)
        state = runstore.load_workflow_state(directory, self.task)
        if state is None:
            return
        entry = runstore.current_entry(state, stage, phase)
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


    # -- snapshot phases -----------------------------------------------------
    def run_snapshot_phase(self, kind: str, stage: Optional[int]) -> StepResult:
        """A critic or review run in a fresh snapshot, recorded through
        `after_snapshot_run`. A harvest error on a completed run turns it into
        FAILED with the harvest reason; a snapshot refusal raises."""
        root, task, opts = self.project_root, self.task, self.options
        self.refresh_record()
        request = driver.RunRequest(
            project_root=root, task=task, stage=stage_text(stage), phase=kind, profile=opts.profile,
            budget=opts.budget, non_interactive=True,
        )
        mark = run_hooks.before_run(root, task)
        candidate = run_hooks.open_run(root, task)
        isolated = snapshot.run_in_snapshot(
            self.drv, request, kind=kind, source_dir=self.source_dir, cancel=self.cancel,
            max_relaunch=opts.max_relaunch, backoff_fn=self.backoff_fn, clock=self.clock,
        )
        if isolated.result is None:
            raise WorkflowRefused(isolated.refusal or "snapshot-refused", "the %s snapshot could not be made: %s" % (
                kind, isolated.refusal))
        hook = run_hooks.after_snapshot_run(
            root, task, isolated, kind=kind, stage=stage, mark=mark, source_dir=self.source_dir,
            superseded_candidate=candidate, origin="coordinator", clock=self.clock,
        )
        result = isolated.result
        if result.outcome == "COMPLETED":
            if hook.violation or (isolated.boundary is not None and getattr(isolated.boundary, "status", "") == "violation"):
                result = dataclasses.replace(result, outcome="FAILED", reason="boundary-violation")
            elif isolated.harvest is None or isolated.harvest.error or isolated.harvest.path_rel is None:
                reason = (isolated.harvest.error if isolated.harvest is not None else None) or isolated.harvest_skipped or "harvest-none"
                result = dataclasses.replace(result, outcome="FAILED", reason=reason)
        self.refresh_record()
        step = StepResult(phase="plan" if kind == "critic" else "review", stage=stage, result=result, hook=hook)
        step.harvested = isolated.harvest.path_rel if isolated.harvest is not None else None
        return step

    def classify_response(self, stage: Optional[int], path_rel: str) -> None:
        """Record the advisory issue counts of a harvested critic response on the
        plan entry. A failure is stored as an error and never stops the loop."""
        try:
            counts: Any = classify_counts(self.source_dir, self.project_root / path_rel)
            item: Dict[str, Any] = {"path": path_rel, "counts": counts}
        except Exception as exc:  # noqa: BLE001 - advisory only
            item = {"path": path_rel, "error": type(exc).__name__}
        state = self.load_state()
        if state is None:
            return
        entry = runstore.current_entry(state, stage, "plan")
        if entry is None:
            return
        classes = [c for c in entry.get("critic_classes") or [] if isinstance(c, dict) and c.get("path") != path_rel]
        classes.append(item)
        entry["critic_classes"] = classes
        self.save_state(state)

    def _read_text(self, path_rel: str) -> Optional[str]:
        path = self.project_root / path_rel
        try:
            info = os.lstat(str(path))
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _last_run_phase(self, entry: Mapping[str, Any]) -> Optional[str]:
        runs = [r for r in entry.get("runs") or [] if isinstance(r, str)]
        if not runs:
            return None
        directory = runstore.store_dir(self.project_root, create=True)
        record = runstore.load_record(directory, runs[-1])
        request = (record or {}).get("request") or {}
        phase = request.get("phase")
        return phase if isinstance(phase, str) else None

    def plan_item(self, stage: Optional[int]) -> List[StepResult]:
        """Plan round, critic, and further rounds while the critic says REVISE
        and the cap allows; the plan gate follows. Stops at the first run that
        does not complete. The returned list ends with the gated step."""
        steps: List[StepResult] = []
        cap = stored_cap(self.load_state(), self.options)
        state = self.load_state()
        entry = runstore.current_entry(state, stage, "plan") if state is not None else None
        if entry is None or entry.get("origin") != "coordinator":
            step = self.run_plain_phase(
                "plan", stage, gate_after=False, new_run=self._take_new_run("plan", stage),
            )
            steps.append(step)
            if not step.completed:
                return steps
        while True:
            state = self.load_state()
            entry = runstore.current_entry(state, stage, "plan") if state is not None else None
            if entry is None:
                break
            last = self._last_run_phase(entry)
            responses = [r for r in entry.get("critic_responses") or [] if isinstance(r, str)]
            if last in ("plan", "thorough_plan"):
                step = self.run_snapshot_phase("critic", stage)
                steps.append(step)
                if not step.completed:
                    return steps
                continue
            if last != "critic" or not responses:
                break
            latest = responses[-1]
            if not any(isinstance(c, dict) and c.get("path") == latest for c in entry.get("critic_classes") or []):
                self.classify_response(stage, latest)
            text = self._read_text(latest)
            verdict = gate.parse_verdict(text, gate.CRITIC_VERDICTS) if text is not None else None
            if verdict == "REVISE" and len(responses) < cap:
                step = self.run_plain_phase(
                    "plan", stage, compose=True, context_refs=(latest,), gate_after=False,
                )
                steps.append(step)
                if not step.completed:
                    return steps
                continue
            break
        gated = steps[-1] if steps else StepResult("plan", stage, phase_loop.PhaseResult(outcome="COMPLETED"))
        gated.gate = self.gate(stage, "plan")
        self.refresh_record()
        if gated not in steps:
            steps.append(gated)
        return steps

    def review_item(self, stage: Optional[int]) -> StepResult:
        """Review in a snapshot, its test result, then the review gate."""
        step = self.run_snapshot_phase("review", stage)
        if not step.completed:
            return step
        if self._tests_configured():
            self._review_tests(stage)
            step.tests_ran = True
        step.gate = self.gate(stage, "review")
        self.refresh_record()
        return step

    def _review_tests(self, stage: Optional[int]) -> None:
        """Copy the stage's implement test result onto the review entry when the
        repositories are unchanged since implement, otherwise run the tests."""
        state = self.load_state()
        impl = runstore.current_entry(state, stage, "implement") if state is not None else None
        review = runstore.current_entry(state, stage, "review") if state is not None else None
        if impl is None or review is None:
            self.run_implement_tests(stage, "review")
            return
        copied = False
        tests = impl.get("tests")
        if isinstance(tests, dict) and isinstance(impl.get("evidence"), dict):
            try:
                fresh = evidence.take_snapshot(self.project_root, self.task, "implement", clock=self.clock)
                _, repo_findings = evidence.compare(impl["evidence"], fresh)
            except Exception:  # noqa: BLE001 - fall back to a rerun
                repo_findings = [object()]
            if not repo_findings:
                review["tests"] = dict(tests)
                review["tests_source"] = "implement-entry"
                review.pop("tests_reason", None)
                self.save_state(state)
                copied = True
        if not copied:
            self.run_implement_tests(stage, "review")

    # -- start: new run, continuation, seeding, rerun and adopt ----------------
    def _take_new_run(self, phase: str, stage: Optional[int]) -> bool:
        if self._new_run_for == (phase, stage):
            self._new_run_for = None
            return True
        return False

    def _record_exists(self) -> bool:
        try:
            return os.path.lexists(str(handoff.record_path(self.project_root, self.task)))
        except handoff.HandoffRefused as exc:
            raise WorkflowRefused("continuation-invalid", exc.message) from None

    def begin(self) -> None:
        """Resolve how the invocation starts, in this order: a new run or the
        continuation precedence (agreement with lag, or seeding), the stored
        settings, the test configuration, the seeded entries' test result, then
        `--rerun-from` and `--adopt`. Raises `WorkflowRefused`."""
        opts = self.options
        state = self.load_state()
        if not opts.continue_:
            if opts.rerun_from or opts.adopt:
                raise WorkflowRefused("continue-required", "--rerun-from and --adopt need --continue")
            if has_workflow_state(state) or self._record_exists():
                raise WorkflowRefused(
                    "workflow-started",
                    "this task already has workflow state or a continuation record; continue it with: %s"
                    % self.resume_hint(),
                )
            state = state or runstore.new_workflow_state(self.task, self.clock)
            write_settings(state, opts, include_architect=True, from_discover=opts.from_discover, clock=self.clock)
            self.save_state(state)
        else:
            self._begin_continue(state)
        self._configure_tests()
        self._seed_tests()
        if opts.rerun_from:
            self._apply_rerun()
        if opts.adopt:
            self._apply_adopt()

    def _begin_continue(self, state: Optional[Dict[str, Any]]) -> None:
        opts = self.options
        try:
            record = handoff.load_for_continuation(
                self.project_root, self.task, source_dir=self.source_dir, requested_scope=self.scope,
            )
        except handoff.HandoffRefused as exc:
            raise WorkflowRefused(exc.code, exc.message) from None
        foreign = record.get("origin_runtime") != handoff.ORIGIN_RUNTIME
        if not foreign and has_workflow_state(state):
            problems = handoff.state_agreement(record, state, allow_lag=True)
            if problems:
                raise WorkflowRefused(
                    "continuation-state-mismatch",
                    "the continuation record and the workflow state disagree (%s); restart the item with "
                    "--rerun-from or record it with --adopt" % ", ".join(problems[:5]),
                )
            if handoff.state_agreement(record, state):
                self.refresh_record()  # the record was behind state; bring it level before running anything
        else:
            self._seed(record)
        state = self.load_state() or runstore.new_workflow_state(self.task, self.clock)
        block = workflow_block(state)
        if block is None:
            pairs = {(c["phase"], c.get("stage")) for c in record.get("completed") or []}
            pairs |= handoff._completed_pairs(state)  # noqa: SLF001 - same package, same definition
            include = (
                ("architect", None) in pairs
                or handoff._primary_artifact_present(self.project_root, self.task, None, "architect", self.source_dir)  # noqa: SLF001
                or not any(phase == "plan" for phase, _ in pairs)
            )
            prior = state["settings"].get("max_critic_rounds")
            write_settings(state, opts, include_architect=include, from_discover=opts.from_discover, clock=self.clock)
            if opts.max_critic_rounds is None and isinstance(prior, int) and not isinstance(prior, bool) and prior >= 1:
                state["settings"]["max_critic_rounds"] = min(prior, MAX_CRITIC_ROUNDS)
        else:
            state["settings"]["workflow"] = dict(block, through=opts.through, no_pause=bool(opts.no_pause))
            if opts.max_critic_rounds is not None:
                state["settings"]["max_critic_rounds"] = opts.critic_cap()
        self.save_state(state)

    def _seed(self, record: Mapping[str, Any]) -> None:
        changed = handoff.artifact_changes(self.project_root, record)
        if changed:
            raise WorkflowRefused(
                "continuation-artifact-changed",
                "artifacts changed since the continuation record was written: %s" % ", ".join(changed[:5]),
            )
        validation = {(v["phase"], v.get("stage")): v for v in record.get("validation") or []}
        for item in record.get("completed") or []:
            phase, stage = item["phase"], item.get("stage")
            found = validation.get((phase, stage))
            snapshot_data = evidence.take_snapshot(self.project_root, self.task, phase, clock=self.clock)
            evidence.record_evidence(
                self.project_root, self.task, stage, phase, "continuation", snapshot_data, clock=self.clock,
                continuation_validation=(found or {}).get("verdict"),
            )
            self.seeded.append((phase, stage))

    def _configure_tests(self) -> None:
        opts = self.options
        if not opts.test_command:
            return
        try:
            testrun.configure(
                self.project_root, self.task, command=list(opts.test_command), include=list(opts.test_include),
                timeout_s=opts.test_timeout if opts.test_timeout is not None else testrun.DEFAULT_TIMEOUT_S,
                state_root=self.state_root, clock=self.clock,
            )
        except testrun.TestRunError as exc:
            raise WorkflowRefused(exc.code, str(exc)) from None

    def _seed_tests(self) -> None:
        """One test run attached to every seeded implement and review entry."""
        targets = [(p, s) for p, s in self.seeded if p in ("implement", "review")]
        if not targets:
            return
        problem = testrun.settings_status(self.project_root, self.task, state_root=self.state_root)
        if problem == "tests-settings-changed":
            raise WorkflowRefused("tests-settings-changed", "the test settings changed since they were configured")
        if problem is not None:
            return
        stage = targets[0][1]
        run = testrun.run_tests(self.project_root, self.task, stage, state_root=self.state_root)
        data = None
        if run.outcome != testrun.REFUSED:
            data = testrun.read_result(self.state_root, self.project_root, self.task, stage)
        fields: Dict[str, Any] = {"tests_source": "continuation-seed"}
        if data is not None:
            fields["tests"] = data
            if data.get("outcome") != testrun.PASSED:
                reason = data.get("reason")
                fields["tests_reason"] = reason if isinstance(reason, str) and reason else "tests-failed"
        else:
            fields["tests"] = None
            fields["tests_reason"] = run.reason or "tests-not-run"
        state = self.load_state()
        if state is None:
            return
        for phase, st in targets:
            entry = runstore.current_entry(state, st, phase)
            if entry is not None:
                entry.update(fields)
        self.save_state(state)

    def _stage_items(self, state) -> List[Tuple[str, Optional[int]]]:
        return [item for item in self.sequence(state) if item[0] in RERUN_PHASES]

    def _apply_rerun(self) -> None:
        phase = self.options.rerun_from
        state = self.load_state()
        stage_items = self._stage_items(state)
        if not stage_items:
            raise WorkflowRefused("rerun-unavailable", "the task has no stage to rerun")
        first = next((it for it in stage_items if not self._item_passed(state, *it)), None)
        stage = first[1] if first is not None else stage_items[-1][1]
        if (phase, stage) not in stage_items:
            raise WorkflowRefused("rerun-unavailable", "there is no %s item for stage %s" % (phase, stage))
        refs: Tuple[str, ...] = ()
        if phase == "implement":
            review = runstore.current_entry(state, stage, "review")
            for item in reversed((review or {}).get("harvested") or []):
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    refs = (item["path"],)
                    break
        runstore.supersede_items(state, stage, RERUN_PHASES[RERUN_PHASES.index(phase):], self.clock)
        self.save_state(state)
        self._new_run_for = (phase, stage)
        self._rerun_refs = refs
        self.rerun_info = {"phase": phase, "stage": stage}
        sys.stderr.write("rerunning %s for stage %s\n" % (phase, "-" if stage is None else stage))

    def _apply_adopt(self) -> None:
        phase = self.options.adopt
        state = self.load_state()
        found = next(
            (it for it in self.sequence(state) if it[0] == phase and not self._item_passed(state, *it)), None,
        )
        if found is None:
            raise WorkflowRefused("adopt-unavailable", "there is no pending %s item to adopt" % phase)
        snapshot_data = evidence.take_snapshot(self.project_root, self.task, phase, clock=self.clock)
        evidence.record_evidence(
            self.project_root, self.task, found[1], phase, "adopted", snapshot_data, clock=self.clock,
        )

    # -- next-item resolution --------------------------------------------------
    def _stored_outcome(self, run_id: str, entry: Mapping[str, Any]) -> Optional[Tuple[str, int, str]]:
        """`(outcome, exit code, reason)` of a last run that did not complete,
        or whose boundary is a stored violation."""
        directory = runstore.store_dir(self.project_root, create=True)
        record = runstore.load_record(directory, run_id) or {}
        state = record.get("state")
        outcome = _STORED_OUTCOMES.get(state)
        signum = None
        if state == "cancelled":
            signal_name = (record.get("outcome") or {}).get("signal")
            signum = 2 if signal_name in ("SIGINT", 2) else 15
        if outcome is None and state == "completed" and (record.get("outcome") or {}).get("evidence") not in (None, "full"):
            outcome = "COMPLETED_UNVERIFIED"
        if outcome is None and entry.get("boundary") == "violation":
            return "FAILED", 2, "boundary-violation"
        if outcome is None:
            return None
        code = phase_loop.exit_code(phase_loop.PhaseResult(outcome=outcome, signum=signum))
        reason = (record.get("outcome") or {}).get("reason")
        return outcome, code, reason if isinstance(reason, str) and reason else outcome.lower()

    def _unrecorded(self, phase: str, stage: Optional[int], what: str) -> Resolution:
        label = phase if stage is None else "%s (stage %s)" % (phase, stage)
        return Resolution(
            "refuse", code="phase-unrecorded",
            message="%s has %s with no recorded entry; record it with --adopt %s, or restart it with "
            "--rerun-from %s (a run started by a single-phase command is finished or restarted with that "
            "command)" % (label, what, phase, phase if phase in RERUN_PHASES else "plan"),
        )

    def resolve(self, state: Optional[Mapping[str, Any]], phase: str, stage: Optional[int]) -> Resolution:
        entry = runstore.current_entry(state, stage, phase) if state is not None else None
        if entry is not None:
            gate_info = entry.get("gate")
            if isinstance(gate_info, Mapping) and gate_info.get("verdict"):
                if gate_info["verdict"] == "PASS":
                    return Resolution("pass")
                return Resolution(
                    "gate-fail", "GATE_REFUSED", 7, tuple(str(r) for r in gate_info.get("reasons") or []),
                    gate_artifact=gate_info.get("artifact"),
                )
            runs = [r for r in entry.get("runs") or [] if isinstance(r, str)]
            if runs:
                stored = self._stored_outcome(runs[-1], entry)
                if stored is not None:
                    return Resolution("report", stored[0], stored[1], (stored[2],), run_id=runs[-1])
            if phase == "plan" and entry.get("origin") == "coordinator":
                return Resolution("run")
            return Resolution("gate", run_id=runs[-1] if runs else None, origin=entry.get("origin"))
        known = [e for e in (state or {}).get("entries") or [] if e.get("phase") == phase and e.get("stage") == stage]
        recorded_runs = {r for e in (state or {}).get("entries") or [] for r in e.get("runs") or []}
        pointer = self.drv.reconcile_task(self.task)
        if pointer and pointer.get("run_id") not in recorded_runs:
            req = pointer.get("request") or {}
            if (
                runstore.entry_phase_for_run(req.get("phase")) == phase
                and req.get("stage") == stage_text(stage)
            ):
                telemetry = pointer.get("telemetry")
                closed = isinstance(telemetry, dict) and telemetry.get("final") is True
                if pointer.get("state") == "interrupted" and not closed:
                    if runstore.normalize_phase(req.get("phase")) in _SNAPSHOT_KINDS or req.get("non_interactive"):
                        return Resolution("run")
                    return self._unrecorded(phase, stage, "an interrupted run started by a single-phase command")
                return self._unrecorded(phase, stage, "a finished run")
        if not known and handoff._primary_artifact_present(  # noqa: SLF001 - the advice engine's own test
            self.project_root, self.task, stage, phase, self.source_dir
        ):
            return self._unrecorded(phase, stage, "its output already present")
        return Resolution("run")

    def gate_existing(self, phase: str, stage: Optional[int], res: Resolution) -> List[StepResult]:
        """Gate an entry whose run completed (or that was seeded or adopted)."""
        if res.origin == "coordinator" and self._tests_configured():
            state = self.load_state()
            entry = runstore.current_entry(state, stage, phase) if state is not None else None
            if entry is not None and not entry.get("tests") and not entry.get("tests_reason"):
                if phase == "implement":
                    self.run_implement_tests(stage)
                elif phase == "review":
                    self._review_tests(stage)
        result = phase_loop.PhaseResult(outcome="COMPLETED", run_id=res.run_id)
        step = StepResult(phase, stage, result, regated=res.origin != "coordinator")
        step.gate = self.gate(stage, phase)
        self.refresh_record()
        return [step]

    # -- the loop ------------------------------------------------------------
    def resume_hint(self) -> str:
        return "quoin run --runtime opencode --profile %s --workflow --continue %s --project-root %s" % (
            shlex.quote(self.options.profile), shlex.quote(self.task), shlex.quote(str(self.project_root)))

    def _item_passed(self, state: Optional[Mapping[str, Any]], phase: str, stage: Optional[int]) -> bool:
        entry = runstore.current_entry(state, stage, phase) if state is not None else None
        gate_info = (entry or {}).get("gate")
        return isinstance(gate_info, Mapping) and gate_info.get("verdict") == "PASS"

    def run_item(self, phase: str, stage: Optional[int]) -> List[StepResult]:
        if phase == "plan":
            return self.plan_item(stage)
        if phase == "review":
            return [self.review_item(stage)]
        new_run = self._take_new_run(phase, stage)
        refs = self._rerun_refs if new_run else ()
        return [self.run_plain_phase(phase, stage, new_run=new_run, context_refs=refs)]

    def run_items(self, items: Sequence[Tuple[str, Optional[int]]]) -> Dict[str, Any]:
        """Walk `items` in order, stopping at the first refusal, failure, failed
        gate, pause or `--through` boundary, and return the summary."""
        opts = self.options
        phases: List[Dict[str, Any]] = []
        gates: List[GateOutcome] = []
        outcome, exit_code, reasons = "COMPLETED", 0, []  # type: str, int, List[str]
        stopped = False
        try:
            for phase, stage in items:
                res = self.resolve(self.load_state(), phase, stage)
                if res.kind == "pass":
                    continue
                if res.kind == "refuse":
                    raise WorkflowRefused(res.code, res.message)
                if res.kind in ("report", "gate-fail"):
                    phases.append({
                        "phase": phase, "stage": stage, "run_ids": [res.run_id] if res.run_id else [],
                        "outcome": res.outcome, "gate": "FAIL" if res.kind == "gate-fail" else None,
                        "gate_artifact": res.gate_artifact,
                    })
                    stopped, outcome, exit_code = True, res.outcome or "FAILED", res.exit_code
                    reasons.extend(res.reasons)
                    break
                steps = self.gate_existing(phase, stage, res) if res.kind == "gate" else self.run_item(phase, stage)
                phases.append({
                    "phase": phase, "stage": stage,
                    "run_ids": [st.result.run_id for st in steps if st.result.run_id],
                    "outcome": steps[-1].result.outcome if steps else None,
                    "gate": None, "gate_artifact": None,
                })
                last = steps[-1]
                if not last.completed:
                    stopped = True
                    outcome = last.result.outcome
                    exit_code = last.exit_code
                    if last.result.reason:
                        reasons.append(last.result.reason)
                    break
                outcome_gate = last.gate
                if outcome_gate is None:
                    continue
                phases[-1]["gate"] = outcome_gate.verdict
                phases[-1]["gate_artifact"] = outcome_gate.artifact
                if outcome_gate.verdict is None:
                    stopped, outcome, exit_code = True, "GATE_ARTIFACT_FAILED", 8
                    reasons.append(outcome_gate.error or "gate-artifact-failed")
                    break
                gates.append(outcome_gate)
                if not outcome_gate.passed:
                    stopped, outcome, exit_code = True, "GATE_REFUSED", 7
                    reasons.extend(outcome_gate.reasons)
                    break
                if opts.through == phase:
                    stopped = True
                    break
                if (
                    not opts.no_pause and phase in ("plan", "review") and (phase, stage) != items[-1]
                    and not last.regated
                ):
                    stopped, outcome = True, "PAUSED_AT_GATE"
                    break
        except WorkflowRefused as exc:
            outcome, exit_code = "REFUSED", exc.exit_code
            reasons.append(exc.code)
            self.errors.append(exc.message)
        record = self.refresh_record()
        return {
            "runtime": "opencode", "mode": "workflow", "task": self.task, "profile": opts.profile,
            "outcome": outcome, "exit_code": exit_code, "phases": phases, "reasons": reasons,
            "resume_hint": None if outcome == "COMPLETED" else self.resume_hint(),
            "record": os.path.relpath(record, str(self.project_root)).replace(os.sep, "/") if record else None,
            **({"rerun": self.rerun_info} if self.rerun_info else {}),
            "workflow_validated": bool(gates) and all(g.passed for g in gates) and outcome in (
                "COMPLETED", "PAUSED_AT_GATE"),
        }


def classify_counts(source_dir, path) -> Dict[str, int]:
    """Issue counts of a critic response from the core classifier."""
    mod = load_core_script(source_dir, "classify_critic_issues")
    issues = mod.parse_critic_response(str(path))
    serious = [i for i in issues if i.severity in ("CRITICAL", "MAJOR")]
    mechanical = [i for i in serious if mod._is_mechanical(i)]  # noqa: SLF001 - the classifier's own rule
    return {"issues": len(issues), "structural": len(serious) - len(mechanical), "mechanical": len(mechanical)}
