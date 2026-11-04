#!/usr/bin/env python3
"""quoin/core/scripts/status_graph.py — workflow pipeline status graph.

Public API:
  detect_phase(task_dir: Path, probe_git: bool = False) -> PhaseResult
  render_graph(phase: PhaseResult, task_name: str, compact: bool = False) -> str
  main(argv: list[str] | None = None) -> int
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Google Drive conflict-copy filter (IVG-75)
# Conservative anchored regex: matches only Drive's " N" / " N.ext" suffix.
# ---------------------------------------------------------------------------

_DRIVE_CONFLICT_RE = re.compile(r" \d{1,3}(\.[^ ]*)?$")


def _is_drive_conflict(name: str) -> bool:
    """Return True iff *name* looks like a Google Drive sync-conflict copy."""
    return _DRIVE_CONFLICT_RE.search(name) is not None


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    phase: str          # canonical phase name (see detect_phase docstring)
    critic_rounds: int  # number of completed critic rounds (0 if none)
    review_rounds: int  # number of completed review rounds (0 if none)
    task_dir: Optional[Path] = None


def _max_n(pattern: str, filenames: list[str]) -> int:
    """Return max N where a file matches pattern (must contain {n} placeholder)."""
    regex = re.compile(pattern)
    ns = [int(m.group(1)) for f in filenames for m in [regex.match(f)] if m]
    return max(ns, default=0)


def _has_glob(filenames: list[str], prefix: str) -> bool:
    """Return True if any filename starts with prefix."""
    return any(f.startswith(prefix) for f in filenames)


def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy (D-06) — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _git_diff_nonempty(task_dir: Path) -> bool:
    """Return True if there are uncommitted or committed changes on the task branch."""
    try:
        root_result = subprocess.run(
            ["git", "-C", str(task_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=_subprocess_timeout(),
        )
        if root_result.returncode != 0:
            return False
        git_root = root_result.stdout.strip()
        diff_result = subprocess.run(
            ["git", "-C", git_root, "diff", "--quiet", "HEAD"],
            capture_output=True, timeout=_subprocess_timeout(),
        )
        return diff_result.returncode != 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def detect_phase(task_dir: Path, probe_git: bool = False) -> PhaseResult:
    """Infer the current workflow phase from artifacts in task_dir.

    Phase precedence (highest completed phase wins):
      done           — task_dir is under .workflow_artifacts/finalized/
      review-gated   — gate-review-* or gate-post-review-* present
      review         — review-N.md present (max N ≥ 1)
      implement-gated — gate-implement-* or gate-post-implement-* present
      implement      — only under --probe-git: uncommitted changes detected
      plan-gated     — gate-post-plan-* or gate-plan-* present
      planning       — current-plan.md + critic-response-N.md present
      planning       — current-plan.md present alone
      architecture   — architecture.md present
      discover       — none of the above

    Known accuracy ceiling (D-03): a task mid-implement (code written, gate not
    yet run) is indistinguishable from plan-gated/planning without --probe-git.
    This is documented degraded behavior, not a bug.

    gate-thorough_plan-* prefixes are intentionally unmapped (they only appear
    in finalized tasks, which short-circuit to "done").
    """
    # Check if under finalized/
    try:
        parts = task_dir.parts
        if "finalized" in parts:
            return PhaseResult(phase="done", critic_rounds=0, review_rounds=0, task_dir=task_dir)
    except Exception:
        pass

    # Gather filenames (top-level only), excluding Drive conflict copies.
    try:
        filenames = [
            f.name for f in task_dir.iterdir()
            if f.is_file() and not _is_drive_conflict(f.name)
        ]
    except (OSError, PermissionError):
        return PhaseResult(phase="discover", critic_rounds=0, review_rounds=0, task_dir=task_dir)

    crit = _max_n(r"^critic-response-(\d+)\.md$", filenames)
    rev = _max_n(r"^review-(\d+)\.md$", filenames)

    if _has_glob(filenames, "gate-review-") or _has_glob(filenames, "gate-post-review-"):
        return PhaseResult(phase="review-gated", critic_rounds=crit, review_rounds=rev, task_dir=task_dir)
    if rev >= 1:
        return PhaseResult(phase="review", critic_rounds=crit, review_rounds=rev, task_dir=task_dir)
    if _has_glob(filenames, "gate-implement-") or _has_glob(filenames, "gate-post-implement-"):
        return PhaseResult(phase="implement-gated", critic_rounds=crit, review_rounds=0, task_dir=task_dir)
    if probe_git and _git_diff_nonempty(task_dir):
        return PhaseResult(phase="implement", critic_rounds=crit, review_rounds=0, task_dir=task_dir)
    if _has_glob(filenames, "gate-post-plan-") or _has_glob(filenames, "gate-plan-"):
        return PhaseResult(phase="plan-gated", critic_rounds=crit, review_rounds=0, task_dir=task_dir)
    if "current-plan.md" in filenames:
        return PhaseResult(phase="planning", critic_rounds=crit, review_rounds=0, task_dir=task_dir)
    if "architecture.md" in filenames:
        return PhaseResult(phase="architecture", critic_rounds=crit, review_rounds=0, task_dir=task_dir)
    return PhaseResult(phase="discover", critic_rounds=0, review_rounds=0, task_dir=task_dir)


# ---------------------------------------------------------------------------
# Active-task selection
# ---------------------------------------------------------------------------

def _max_artifact_mtime(task_dir: Path) -> float:
    """Return the max mtime of non-empty artifact files inside task_dir.

    Returns 0.0 if no non-empty artifact files exist.
    Scans top-level files only; also checks stage-N/ subfolders.
    """
    mtimes: list[float] = []
    try:
        for entry in task_dir.iterdir():
            if _is_drive_conflict(entry.name):
                continue  # skip Drive conflict-copy files/dirs at task root
            if entry.is_file() and entry.stat().st_size > 0:
                mtimes.append(entry.stat().st_mtime)
            elif entry.is_dir() and re.match(r"^stage-\d+$", entry.name):
                # Stage subfolder: check its artifact files too
                try:
                    for sub in entry.iterdir():
                        if _is_drive_conflict(sub.name):
                            continue  # skip Drive conflict copies inside stage dirs
                        if sub.is_file() and sub.stat().st_size > 0:
                            mtimes.append(sub.stat().st_mtime)
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return max(mtimes, default=0.0)


_EXCLUDED_NAMES = frozenset({"memory", "cache", "finalized", "security-review", "trash"})


def pick_active_task(root: Path) -> Optional[Path]:
    """Return the most-recently-modified non-finalized task dir under root/.workflow_artifacts/.

    Returns None if no qualifying task dir is found.
    """
    artifacts = root / ".workflow_artifacts"
    if not artifacts.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    try:
        for entry in artifacts.iterdir():
            if not entry.is_dir():  # excludes stray top-level files
                continue
            if entry.name in _EXCLUDED_NAMES:
                continue
            if _is_drive_conflict(entry.name):
                continue  # skip Drive conflict-copy task dirs
            mtime = _max_artifact_mtime(entry)
            if mtime > 0.0:  # requires at least one non-empty artifact
                candidates.append((mtime, entry))
    except (OSError, PermissionError):
        pass

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# ASCII graph rendering
# ---------------------------------------------------------------------------

# Canonical pipeline nodes (for the full graph)
_PIPELINE = [
    ("discover",       "discover"),
    ("architect",      "architect"),
    ("thorough_plan",  "thorough_plan"),
    ("implement",      "implement"),
    ("review",         "review"),
    ("end_of_task",    "end_of_task"),
]

# Map detect_phase output → which pipeline node is "active"
_PHASE_TO_NODE = {
    "discover":        "discover",
    "architecture":    "architect",
    "planning":        "thorough_plan",
    "plan-gated":      "thorough_plan",
    "implement":       "implement",
    "implement-gated": "implement",
    "review":          "review",
    "review-gated":    "review",
    "done":            "end_of_task",
}

def build_nodes(result: PhaseResult) -> list[dict]:
    """Build the pipeline node list with per-node state for --emit-nodes output."""
    active_node = _PHASE_TO_NODE.get(result.phase, "discover")
    node_names = [n for _, n in _PIPELINE]
    active_idx = node_names.index(active_node)
    nodes = []
    for i, (_, node) in enumerate(_PIPELINE):
        if result.phase == "done":
            state = "done"
        elif i < active_idx:
            state = "done"
        elif i == active_idx:
            state = "active"
        else:
            state = "future"
        obj: dict = {"node": node, "state": state}
        if node == "thorough_plan" and result.critic_rounds > 0:
            obj["critic_rounds"] = result.critic_rounds
        if node == "review" and result.review_rounds > 0:
            obj["review_rounds"] = result.review_rounds
        nodes.append(obj)
    return nodes


# Map detect_phase output → display label used in "you are here" annotation
_PHASE_LABELS = {
    "discover":        "discover",
    "architecture":    "architect (architecture done)",
    "planning":        "planning",
    "plan-gated":      "plan-gated (ready to implement)",
    "implement":       "implement (in-progress, git)",
    "implement-gated": "implement-gated (ready to review)",
    "review":          "review",
    "review-gated":    "review-gated (ready to finalize)",
    "done":            "done",
}


def render_graph(result: PhaseResult, task_name: str, compact: bool = False) -> str:
    """Render an ASCII pipeline graph with the active phase marked.

    compact=True: single-column layout, max 40 chars per line.
    compact=False: horizontal pipeline with ▲ annotation.
    """
    phase = result.phase
    active_node = _PHASE_TO_NODE.get(phase, "discover")
    label = _PHASE_LABELS.get(phase, phase)

    if compact:
        return _render_compact(result, task_name, active_node, label)
    return _render_full(result, task_name, active_node, label)


def _render_full(result: PhaseResult, task_name: str, active_node: str, label: str) -> str:
    lines: list[str] = []
    lines.append(f"quoin pipeline -- {task_name}")
    lines.append("")

    # Build pipeline row with active node marked
    parts: list[str] = []
    for _, node in _PIPELINE:
        if node == active_node:
            parts.append(f"[{node}]")
        else:
            parts.append(node)
    pipeline_line = " -> ".join(parts)
    lines.append(pipeline_line)

    # Build "you are here" pointer under the active node
    prefix = ""
    used = 0
    for _, node in _PIPELINE:
        display = f"[{node}]" if node == active_node else node
        if node == active_node:
            # Center the pointer under the display token
            center = used + len(display) // 2
            prefix = " " * center + "^"
            break
        used += len(display) + 4  # 4 = len(" -> ")
    lines.append(prefix + " you are here")
    lines.append("")

    # Phase detail
    lines.append(f"phase: {label}")
    if result.phase == "planning" and result.critic_rounds > 0:
        # Show the plan→critic loop progress
        loop_parts = []
        for i in range(1, result.critic_rounds + 1):
            loop_parts.append(f"plan{'' if i == 1 else i}")
            loop_parts.append(f"critic{'' if i == 1 else i}")
        loop_str = " -> ".join(loop_parts)
        lines.append(f"loop:  {loop_str}  [round {result.critic_rounds}]")

    return "\n".join(lines)


def _render_compact(result: PhaseResult, task_name: str, active_node: str, label: str) -> str:
    """Single-column layout, max 40 chars per line."""
    lines: list[str] = []
    task_trunc = task_name[:32] if len(task_name) > 32 else task_name
    lines.append(f"[{task_trunc}]")

    for _, node in _PIPELINE:
        if node == active_node:
            marker = ">>>"
        else:
            marker = "   "
        line = f"{marker} {node}"[:40]
        lines.append(line)

    lines.append("")
    phase_line = f"phase: {label}"[:40]
    lines.append(phase_line)

    if result.phase == "planning" and result.critic_rounds > 0:
        round_line = f"round {result.critic_rounds}"[:40]
        lines.append(round_line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-stage and autonomous-run position
# ---------------------------------------------------------------------------

# Phase strings in pipeline order; a later entry means the task got further.
_PHASE_ORDER = [
    "discover", "architecture", "planning", "plan-gated", "implement",
    "implement-gated", "review", "review-gated", "done",
]

# Per-stage progress sentinels written by autonomous runs:
#   {phase}.stage-N.done        -> the phase finished for stage N
#   {phase}.stage-N.<sub>.done  -> part of the phase finished (e.g. tasks, fix1)
_STAGE_SENTINEL_RE = re.compile(r"^([a-z_]+)\.stage-(\d+)(?:\.([\w-]+))?\.done$")
_SENTINEL_DONE_PHASE = {
    "thorough_plan": "plan-gated",
    "implement": "implement-gated",
    "review": "review-gated",
}
_SENTINEL_PARTIAL_PHASE = {
    "thorough_plan": "planning",
    "implement": "implement",
    "review": "review",
}

# Run-state "start <phase>" -> the phase string shown while that phase runs.
_IN_FLIGHT_PHASE = {
    "discover": "discover",
    "enrich": "discover",
    "specify": "discover",
    "fast_path_triage": "discover",
    "architect": "architecture",
    "thorough_plan": "planning",
    "implement": "implement",
    "review": "review",
    "end_of_task": "review-gated",
}

_STAGE_DIR_RE = re.compile(r"^stage-(\d+)$")
# Same stage-row and section patterns as path_resolve.py; keep them in sync.
_ROW_RE = re.compile(r"^[0-9]+\.\s+(?:[✅✓✗⏳⛔⚠️\s])*S-([0-9]+):\s*(.+?)\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## Stage decomposition\s*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)


@dataclass
class RunPosition:
    stage: Optional[int] = None   # current stage of a multi-stage task
    stage_count: int = 0          # stages listed in architecture.md (0 if none)
    next_action: Optional[str] = None  # from a fresh, active run-state record


def _phase_rank(phase: str) -> int:
    try:
        return _PHASE_ORDER.index(phase)
    except ValueError:
        return -1


def _stage_count(task_dir: Path) -> int:
    try:
        text = (task_dir / "architecture.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    section = _SECTION_RE.search(text)
    if not section:
        return 0
    body = text[section.end():]
    next_h2 = _NEXT_H2_RE.search(body)
    if next_h2:
        body = body[:next_h2.start()]
    return max((int(m.group(1)) for m in _ROW_RE.finditer(body)), default=0)


def _sentinel_stage_phases(progress_dir: Path) -> dict:
    """Return {stage: furthest phase} from stage-suffixed progress sentinels."""
    phases: dict = {}
    try:
        names = [p.name for p in progress_dir.iterdir() if p.is_file()]
    except (OSError, PermissionError):
        return phases
    for name in names:
        m = _STAGE_SENTINEL_RE.match(name)
        if not m:
            continue
        table = _SENTINEL_PARTIAL_PHASE if m.group(3) else _SENTINEL_DONE_PHASE
        phase = table.get(m.group(1))
        if phase is None:
            continue
        stage = int(m.group(2))
        if _phase_rank(phase) > _phase_rank(phases.get(stage, "")):
            phases[stage] = phase
    return phases


def _run_state_stale_days() -> int:
    """Same knob and fallback as run_state.py; 0 or less means no age limit."""
    try:
        return int(os.environ.get("QUOIN_RUN_STATE_STALE_DAYS", "1"))
    except ValueError:
        return 1


def _fresh_next_action(memory_dir: Path, task: str) -> Optional[str]:
    """Return next_action from an active run-state record younger than the stale window."""
    path = memory_dir / f"run-state-{task}.json"
    try:
        age = time.time() - path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("active") is not True:
        return None
    max_days = _run_state_stale_days()
    if max_days > 0 and age > max_days * 86400:
        return None
    next_action = data.get("next_action")
    if not isinstance(next_action, str):
        return None
    m = re.match(r"^start\s+([a-z_]+)\s*$", next_action.strip())
    if not m or m.group(1) not in _IN_FLIGHT_PHASE:
        return None
    return next_action.strip()


def resolve_position(
    task_dir: Path, project_root: Path, base: PhaseResult, probe_git: bool = False,
) -> tuple:
    """Refine *base* (from detect_phase on the task root) with stage and run data.

    Multi-stage tasks keep plans and reviews under stage-N/, and autonomous runs
    record per-stage progress as stage-suffixed sentinels, so the task root alone
    only shows "architecture". This picks the furthest stage with any evidence and
    the furthest phase within it; a finished stage that is not the last one moves
    the position to planning for the next stage. A fresh, active run-state record
    then overrides the phase with the one it is running.

    Returns (PhaseResult, RunPosition).
    """
    pos = RunPosition()
    if base.phase == "done":
        return base, pos

    task = task_dir.name
    memory_dir = project_root / ".workflow_artifacts" / "memory"
    pos.stage_count = _stage_count(task_dir)

    per_stage: dict = {}
    if pos.stage_count:
        try:
            stage_dirs = [
                d for d in task_dir.iterdir()
                if d.is_dir() and _STAGE_DIR_RE.match(d.name) and not _is_drive_conflict(d.name)
            ]
        except (OSError, PermissionError):
            stage_dirs = []
        for d in stage_dirs:
            n = int(_STAGE_DIR_RE.match(d.name).group(1))
            stage_result = detect_phase(d, probe_git=probe_git)
            if stage_result.phase != "discover":  # empty or pre-created stage dir
                per_stage[n] = stage_result
    for n, phase in _sentinel_stage_phases(memory_dir / f"autonomous-progress-{task}").items():
        current = per_stage.get(n)
        if current is None or _phase_rank(phase) > _phase_rank(current.phase):
            rounds = (current.critic_rounds, current.review_rounds) if current else (0, 0)
            per_stage[n] = PhaseResult(
                phase=phase, critic_rounds=rounds[0], review_rounds=rounds[1],
                task_dir=task_dir / f"stage-{n}",
            )

    result = base
    if per_stage:
        stage = max(per_stage)
        result = per_stage[stage]
        if result.phase == "review-gated" and stage < pos.stage_count:
            stage += 1
            result = PhaseResult(
                phase="planning", critic_rounds=0, review_rounds=0,
                task_dir=task_dir / f"stage-{stage}",
            )
        pos.stage = stage

    pos.next_action = _fresh_next_action(memory_dir, task)
    if pos.next_action:
        in_flight = _IN_FLIGHT_PHASE[pos.next_action.split()[1]]
        result = PhaseResult(
            phase=in_flight, critic_rounds=result.critic_rounds,
            review_rounds=result.review_rounds, task_dir=result.task_dir,
        )
    return result, pos


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------

def _find_project_root(start: Path) -> Optional[Path]:
    """Walk up from start to find a directory containing .workflow_artifacts/."""
    current = start.resolve()
    for _ in range(20):
        if (current / ".workflow_artifacts").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:  # type: ignore[assignment]  # argv: list[str] | None
    parser = argparse.ArgumentParser(
        prog="status_graph",
        description="Show quoin workflow pipeline status for the active task.",
    )
    parser.add_argument("--task", metavar="NAME",
                        help="task name (default: auto-select most-recently-modified)")
    parser.add_argument("--project-root", metavar="PATH", default=None,
                        help="path to project root containing .workflow_artifacts/ (default: cwd walk-up)")
    parser.add_argument("--stage", metavar="N-or-NAME", default=None,
                        help="stage number or name for multi-stage tasks")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON object instead of ASCII graph")
    parser.add_argument("--emit-nodes", action="store_true",
                        help="include pipeline node list in --json output (implies --json)")
    parser.add_argument("--watch", nargs="?", const=5, type=int, metavar="SECONDS",
                        help="refresh mode: clear+redraw every N seconds (default 5)")
    parser.add_argument("--compact", action="store_true",
                        help="narrow-pane render, max 40 chars per line (for agentdesk)")
    parser.add_argument("--probe-git", action="store_true",
                        help="detect in-progress implement via git diff (requires git)")
    args = parser.parse_args(argv)

    # Resolve project root
    project_root_str = args.project_root
    if project_root_str:
        project_root = Path(project_root_str).resolve()
        if not (project_root / ".workflow_artifacts").is_dir():
            print(f"No .workflow_artifacts/ directory found under {project_root}", file=sys.stderr)
            return 1
    else:
        project_root = _find_project_root(Path.cwd())
        if project_root is None:
            print("No .workflow_artifacts/ directory found. Run from a quoin project root.", file=sys.stderr)
            return 1

    if args.watch is not None:
        interval = args.watch
        _watch_loop(args, project_root, interval)
        return 0

    output, code = _run_once(args, project_root)
    print(output)
    return code


def _resolve_task_dir(args: argparse.Namespace, project_root: Path) -> tuple[Optional[Path], str]:
    """Return (task_dir, error_message). error_message is empty string on success."""
    if args.task:
        # Use path_resolve to find the task dir (handles stage resolution)
        try:
            _core_dir = Path(__file__).resolve().parent
            _spec_pr = __import__("importlib.util", fromlist=["util"]).util.spec_from_file_location(
                "_pr", _core_dir / "path_resolve.py"
            )
            _pr = __import__("importlib.util", fromlist=["util"]).util.module_from_spec(_spec_pr)
            _spec_pr.loader.exec_module(_pr)
            task_path_fn = _pr.task_path
        except Exception:
            task_path_fn = None

        if task_path_fn is not None:
            try:
                task_dir = task_path_fn(
                    task=args.task,
                    stage=args.stage,
                    project_root=str(project_root),
                )
                return Path(task_dir), ""
            except Exception:
                pass
        # Fallback: direct construction
        task_dir = project_root / ".workflow_artifacts" / args.task
        if not task_dir.is_dir():
            return None, f"Task directory not found: {task_dir}"
        return task_dir, ""
    else:
        task_dir = pick_active_task(project_root)
        if task_dir is None:
            return None, ""
        return task_dir, ""


def _run_once(args: argparse.Namespace, project_root: Path) -> tuple[str, int]:
    task_dir, err = _resolve_task_dir(args, project_root)
    if err:
        return err, 1
    if task_dir is None:
        return "No active task found under .workflow_artifacts/", 0

    result = detect_phase(task_dir, probe_git=args.probe_git)
    pos = RunPosition()
    if not args.stage:
        result, pos = resolve_position(task_dir, project_root, result, probe_git=args.probe_git)
    task_name = task_dir.name

    if args.json or args.emit_nodes:
        data = {
            "task": task_name,
            "phase": result.phase,
            "critic_rounds": result.critic_rounds,
            "review_rounds": result.review_rounds,
            "stage": args.stage,
            "current_stage": pos.stage,
            "stage_count": pos.stage_count,
            "next_action": pos.next_action,
            "task_dir": str(task_dir),
        }
        if args.emit_nodes:
            data["nodes"] = build_nodes(result)
        return json.dumps(data, indent=2), 0

    graph = render_graph(result, task_name, compact=args.compact)
    extra: list[str] = []
    if pos.stage is not None:
        total = f"/{pos.stage_count}" if pos.stage_count else ""
        extra.append(f"stage: {pos.stage}{total}")
    if pos.next_action:
        extra.append(f"next:  {pos.next_action}")
    if extra:
        if args.compact:
            extra = [line[:40] for line in extra]
        graph = graph + "\n" + "\n".join(extra)
    if not args.compact:
        footer = f"\n(showing {task_name}; use --task to pick another)"
        return graph + footer, 0
    return graph, 0


def _watch_loop(args: argparse.Namespace, project_root: Path, interval: int) -> None:
    try:
        while True:
            os.system("clear")
            output, _ = _run_once(args, project_root)
            print(output)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
