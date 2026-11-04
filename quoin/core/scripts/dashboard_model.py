#!/usr/bin/env python3
"""quoin/core/scripts/dashboard_model.py — portable workflow cost dashboard model.

Runtime-neutral cost-aggregation and task-enumeration for the dashboard (Stage 1).

This is a PORTABLE CORE module (quoin/quoin/core/scripts/).
The doubled quoin/quoin/ prefix is INTENTIONAL: repo-root quoin/ + package quoin/.
Do NOT collapse this to a single quoin/ — it is correct.

CRITICAL: This module does NOT import from quoin/scripts/ (adapter layer).
NO cost_from_jsonl, NO analyze_cost_ledger, NO adapter-specific session handling.
All USD/token resolution is deferred to the injected cost_provider (Stage 2).

Public API:
  scan_tasks(root, include_finalized=False, cost_provider=None) -> dict
  task_detail(root, name, cost_provider=None) -> dict
  main(argv=None) -> int
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Callable, Optional

# CostProvider interface (Python 3.8-safe alias):
#   provider(task_name: str, rows: list[dict]) -> Optional[dict]
# rows: list of ledger-row dicts with frozen key set
#   {uuid, date, phase, model_or_effort, note, fallback_fires, attribution}
# return: {"mode": "usd"|"tokens", "usd": float|None, "tokens": int|None,
#          "by_phase": {phase: {"usd"|"tokens": ...}}}
#   or None (caller stays in counts mode)
CostProvider = Callable[[str, list], Optional[dict]]


# ---------------------------------------------------------------------------
# Sibling-core module loaders
# ---------------------------------------------------------------------------

def _load_core(name: str):
    """Load a sibling core module (status_graph, path_resolve, cost_event).

    Uses spec_from_file_location to avoid package-import assumptions.
    Registers in sys.modules before exec_module for dataclass compatibility.
    """
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        raise ImportError(f"Cannot load {name}: {path} not found")

    module_key = f"_dashboard_model_{name}"
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {path}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


# Load sibling modules (this happens at import time)
_status_graph = _load_core("status_graph")
_path_resolve = _load_core("path_resolve")
_cost_event = _load_core("cost_event")
_memory_select = _load_core("memory_select")

# Bind exported names from sibling modules
detect_phase = _status_graph.detect_phase
pick_active_task = _status_graph.pick_active_task
_EXCLUDED_NAMES = _status_graph._EXCLUDED_NAMES
_max_artifact_mtime = _status_graph._max_artifact_mtime
_PHASE_LABELS = _status_graph._PHASE_LABELS
resolve_position = _status_graph.resolve_position
_sentinel_stage_phases = _status_graph._sentinel_stage_phases
_phase_rank = _status_graph._phase_rank

task_path = _path_resolve.task_path
_lookup_stage_by_name = _path_resolve._lookup_stage_by_name
SECTION_RE = _path_resolve.SECTION_RE
ROW_RE = _path_resolve.ROW_RE

iter_events = _cost_event.iter_events

# memory_select exports used by memory browsing (T-02/T-03)
parse_entries = _memory_select.parse_entries


# ---------------------------------------------------------------------------
# ETag / conditional-GET support
# ---------------------------------------------------------------------------

def compute_version_token(root: Path, scope: str) -> str:
    """Compute a strong ETag string for the artifacts tree at root.

    Walks root/.workflow_artifacts/ (stat-only, no content reads) and
    aggregates three cheap signals: max(st_mtime_ns), file_count,
    sum(st_size).  The ``scope`` string is an opaque caller-supplied key
    (e.g. ``"tasks:fin=False|cj=12345"``) that partitions tokens so that
    different endpoints/query-params never share an ETag.

    Returns a quoted strong-ETag value (RFC 7232 entity-tag syntax):
      '"<16-char sha1-hex>"'

    The surrounding double-quotes are part of the returned string and must
    be preserved verbatim by HTTP clients and echoed back in If-None-Match.

    Error policy:
    - OSError/PermissionError on individual entries are swallowed.
    - On total failure (e.g. artifacts dir absent) the token is derived
      from scope alone — always deterministic, always non-empty.
    - The function NEVER raises.

    Core-purity contract: this function imports ONLY stdlib modules (hashlib,
    pathlib).  It must NEVER import from quoin/scripts/ (adapter layer).
    """
    artifacts = root / ".workflow_artifacts"
    max_mtime_ns: int = 0
    count: int = 0
    total: int = 0

    if artifacts.is_dir():
        for p in artifacts.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    count += 1
                    total += st.st_size
                    if st.st_mtime_ns > max_mtime_ns:
                        max_mtime_ns = st.st_mtime_ns
            except (OSError, PermissionError):
                continue  # vanished or permission-denied mid-walk — skip, never raise

    raw = f"{scope}|{max_mtime_ns}|{count}|{total}"
    return '"' + hashlib.sha1(raw.encode()).hexdigest()[:16] + '"'


# ---------------------------------------------------------------------------
# Cost provider interface
# ---------------------------------------------------------------------------

# CostProvider: Callable[[str, list[dict]], Optional[dict]]
# Cost provider interface.
#
# Receives:
#   - task_name: str — name of the task
#   - rows: list[dict] — list of ledger-row dicts with the FROZEN key set
#     {uuid, date, phase, model_or_effort, note, fallback_fires, attribution}
#
# Returns:
#   - None: no enrichment, use counts mode
#   - dict with keys:
#     - mode: "usd" | "tokens"
#     - usd: float | None
#     - tokens: int | None
#     - by_phase: {phase: {...}} | {}
#
# Note: The architecture's 'model' shorthand (architecture.md:116) maps to
# the 'model_or_effort' key — there is NO 'model' field on CostEvent.
# The provider contract is bound to model_or_effort, not model.


# ---------------------------------------------------------------------------
# Ledger reading and cost computation
# ---------------------------------------------------------------------------

def _read_ledger_rows(task_dir: Path) -> list:
    """Read and parse ledger rows from the task root's cost-ledger.md.

    Returns a list of JSON-serializable dicts with the FROZEN key set:
    {uuid, date, phase, model_or_effort, note, fallback_fires, attribution}

    Gracefully handles missing files (returns []).
    """
    # Resolve ledger path: always at the task ROOT, never in stage subfolders
    ledger_path = task_dir / "cost-ledger.md"

    # Guard: if ledger doesn't exist, return empty list
    if not ledger_path.exists():
        return []

    rows = []
    try:
        for event in iter_events(ledger_path):
            rows.append({
                "uuid": event.uuid,
                "date": event.date,
                "phase": event.phase,
                "model_or_effort": event.model_or_effort,
                "note": event.note,
                "fallback_fires": event.fallback_fires,
                "attribution": event.attribution,
            })
    except FileNotFoundError:
        # iter_events raises this if file disappears mid-read (race condition)
        pass

    return rows


def _counts_by_phase(rows: list) -> dict:
    """Compute counts mode: per-phase session-row counts.

    Returns dict like {"architect": 1, "critic": 2, "plan": 1}.
    Note: does NOT include a "total" key — callers that need total add it separately.
    """
    counts = {}
    for row in rows:
        phase = row.get("phase", "unknown")
        counts[phase] = counts.get(phase, 0) + 1

    return dict(sorted(counts.items()))  # alphabetical


def _min_artifact_mtime(task_dir: Path) -> float:
    """Return the MIN mtime of non-empty artifact files inside task_dir.

    Returns 0.0 if no non-empty artifact files exist.
    Scans top-level files + one level into stage-N/ subfolders (mirroring
    _max_artifact_mtime from status_graph.py:126-147).
    """
    mtimes: list[float] = []
    try:
        for entry in task_dir.iterdir():
            if entry.is_file() and entry.stat().st_size > 0:
                mtimes.append(entry.stat().st_mtime)
            elif entry.is_dir() and re.match(r"^stage-\d+$", entry.name):
                # Stage subfolder: check its artifact files too
                try:
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.stat().st_size > 0:
                            mtimes.append(sub.stat().st_mtime)
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return min(mtimes, default=0.0)


# ---------------------------------------------------------------------------
# Stage information
# ---------------------------------------------------------------------------

def _stage_info(task_dir: Path) -> dict:
    """Detect multi-stage structure and enumerate stages.

    Reads architecture.md from the task ROOT.
    Returns:
      {
        "is_multi_stage": bool,
        "stages": [{"n": int, "name": str, "phase": str,
                    "critic_rounds": int, "review_rounds": int}, ...]
        (empty if single-stage)
      }
    """
    arch_path = task_dir / "architecture.md"

    if not arch_path.exists():
        return {"is_multi_stage": False, "stages": []}

    try:
        arch_text = arch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"is_multi_stage": False, "stages": []}

    # Detect multi-stage: presence of "## Stage decomposition"
    if not SECTION_RE.search(arch_text):
        return {"is_multi_stage": False, "stages": []}

    # For finalized tasks (<root>/.workflow_artifacts/finalized/<name>),
    # task_dir.parent.parent is .workflow_artifacts/ — one level too deep.
    # Detect finalized layout and go one extra level up.
    if "finalized" in task_dir.parts:
        _proj_root = task_dir.parent.parent.parent
    else:
        _proj_root = task_dir.parent.parent

    # Autonomous runs record per-stage progress as stage-suffixed sentinels
    # ({phase}.stage-N[.<sub>].done); they can be ahead of the stage folder.
    sentinel_phases = _sentinel_stage_phases(
        _proj_root / ".workflow_artifacts" / "memory" / f"autonomous-progress-{task_dir.name}"
    )

    # Multi-stage: enumerate stages via ROW_RE
    stages = []
    for match in ROW_RE.finditer(arch_text):
        stage_n = int(match.group(1))
        stage_name = match.group(2).strip()

        try:
            stage_dir = task_path(task_dir.name, stage=stage_n, project_root=_proj_root)
            phase_result = detect_phase(stage_dir)
            phase = phase_result.phase
            critic_rounds = phase_result.critic_rounds
            review_rounds = phase_result.review_rounds
            sentinel_phase = sentinel_phases.get(stage_n)
            if sentinel_phase and _phase_rank(sentinel_phase) > _phase_rank(phase):
                phase = sentinel_phase
        except Exception:
            # If resolution fails, skip this stage
            phase = "unknown"
            critic_rounds = 0
            review_rounds = 0

        stages.append({
            "n": stage_n,
            "name": stage_name,
            "phase": phase,
            "critic_rounds": critic_rounds,
            "review_rounds": review_rounds,
        })

    return {"is_multi_stage": True, "stages": stages}


# ---------------------------------------------------------------------------
# Per-task assembler
# ---------------------------------------------------------------------------

def _task_summary(
    root: Path,
    task_dir: Path,
    task_name: str,
    cost_provider,
) -> dict:
    """Assemble a single task summary.

    Includes: name, phase, phase_label, critic_rounds, review_rounds,
    is_multi_stage, stage, next_action, last_activity, finalized, and cost.
    """
    phase_result = detect_phase(task_dir)
    # A multi-stage task root holds only architecture.md; refine with the
    # stage folders, stage-suffixed sentinels and run-state, as /status does.
    try:
        phase_result, position = resolve_position(task_dir, root, phase_result)
    except Exception:
        print(
            f"dashboard_model: stage position failed for '{task_name}'; "
            f"using task-root phase",
            file=sys.stderr,
        )
        position = None
    phase = phase_result.phase
    phase_label = _PHASE_LABELS.get(phase, phase)  # UNCONDITIONAL (per D-07)

    # Compute counts mode
    rows = _read_ledger_rows(task_dir)
    counts_mode = _counts_by_phase(rows)

    default_cost = {
        "mode": "counts",
        "by_phase": counts_mode,
        "total": len(rows),
        "usd": None,
        "tokens": None,
        "partial": False,
    }

    # Apply provider enrichment if available
    cost = default_cost.copy()
    if cost_provider is not None:
        try:
            enrichment = cost_provider(task_name, rows)
            if enrichment is not None and isinstance(enrichment, dict):
                # Merge provider output over counts default. "partial"
                # propagates the never-silent-$0 signal into the emitted
                # dashboard JSON (MAJOR-1 fix — previously dropped here).
                for key in ["mode", "usd", "tokens", "by_phase", "partial"]:
                    if key in enrichment:
                        cost[key] = enrichment[key]
        except Exception:
            # Provider raised: log to stderr, stay in counts mode
            print(
                f"dashboard_model: cost_provider raised for '{task_name}'; "
                f"using counts mode",
                file=sys.stderr,
            )
            # cost stays as default_cost

    # Stage info
    stage_info = _stage_info(task_dir)

    # For multi-stage tasks, aggregate critic/review rounds across stages.
    # detect_phase(task_dir) only scans top-level files; critic/review artifacts
    # live in stage-N/ subdirs and are invisible from the root scan.
    if stage_info["is_multi_stage"] and stage_info["stages"]:
        critic_rounds = sum(s.get("critic_rounds", 0) for s in stage_info["stages"])
        review_rounds = sum(s.get("review_rounds", 0) for s in stage_info["stages"])
    else:
        critic_rounds = phase_result.critic_rounds
        review_rounds = phase_result.review_rounds

    # Last activity: ISO-8601 from max mtime, or None if 0.0
    max_mtime = _max_artifact_mtime(task_dir)
    last_activity = None
    if max_mtime > 0.0:
        last_activity = datetime.datetime.fromtimestamp(max_mtime).isoformat()

    # Finalized: check if task_dir is under finalized/
    finalized = "finalized" in task_dir.parts

    # Current stage (multi-stage tasks only): the stage the run is on, or None
    # before any stage has evidence.
    active_stage = None
    if stage_info["is_multi_stage"] and position is not None:
        active_stage = position.stage

    return {
        "name": task_name,
        "phase": phase,
        "phase_label": phase_label,
        "critic_rounds": critic_rounds,
        "review_rounds": review_rounds,
        "is_multi_stage": stage_info["is_multi_stage"],
        "stage": active_stage,
        "next_action": position.next_action if position is not None else None,
        "last_activity": last_activity,
        "finalized": finalized,
        "cost": cost,
    }


# ---------------------------------------------------------------------------
# Task enumeration and detail
# ---------------------------------------------------------------------------

def scan_tasks(
    root: Path,
    include_finalized: bool = False,
    cost_provider=None,
) -> dict:
    """Scan and summarize all tasks.

    Returns:
      {
        "project_root": str,
        "active_task": str | None,  (JSON-serializable name string, not Path)
        "tasks": [task_summary, ...],
      }
    """
    root = Path(root).resolve()

    # Resolve root: if root/.workflow_artifacts exists, use root; else bail
    artifacts_dir = root / ".workflow_artifacts"
    if not artifacts_dir.is_dir():
        # Try to find it by walking up
        current = root
        while current != current.parent:
            if (current / ".workflow_artifacts").is_dir():
                root = current
                artifacts_dir = current / ".workflow_artifacts"
                break
            current = current.parent
        else:
            # No .workflow_artifacts found
            return {
                "project_root": str(root),
                "active_task": None,
                "tasks": [],
            }

    tasks = []

    # Enumerate non-finalized tasks
    try:
        for entry in artifacts_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in _EXCLUDED_NAMES:
                continue

            task_name = entry.name
            summary = _task_summary(root, entry, task_name, cost_provider)
            tasks.append(summary)
    except (OSError, PermissionError):
        pass

    # Enumerate finalized tasks if requested (TOP-LEVEL finalized/ only)
    if include_finalized:
        finalized_dir = artifacts_dir / "finalized"
        if finalized_dir.is_dir():
            try:
                for entry in finalized_dir.iterdir():
                    if not entry.is_dir():
                        continue

                    task_name = entry.name
                    summary = _task_summary(root, entry, task_name, cost_provider)
                    tasks.append(summary)
            except (OSError, PermissionError):
                pass

    # Compute active_task: JSON-serializable NAME STRING (not Path)
    # pick_active_task returns Optional[Path]; take .name to get the string
    active_task_path = pick_active_task(root)
    active_task = active_task_path.name if active_task_path is not None else None

    return {
        "project_root": str(root),
        "active_task": active_task,
        "tasks": tasks,
    }


def task_detail(
    root: Path,
    name: str,
    cost_provider=None,
) -> dict:
    """Get detailed info for a specific task.

    Raises KeyError if task not found.

    Returns task_summary fields PLUS:
      - stages: full _stage_info (with per-stage phase)
      - ledger_rows: list of dicts (non-finalized only)
      - totals: counts/cost totals (finalized only)
      - dates: {"first_activity", "last_activity"} (ISO-8601 or None)
    """
    root = Path(root).resolve()

    # Resolve root if needed
    artifacts_dir = root / ".workflow_artifacts"
    if not artifacts_dir.is_dir():
        current = root
        while current != current.parent:
            if (current / ".workflow_artifacts").is_dir():
                root = current
                artifacts_dir = current / ".workflow_artifacts"
                break
            current = current.parent

    # Try to find task dir: first at top level, then in finalized/
    task_dir = artifacts_dir / name
    finalized_task = False

    if not task_dir.is_dir():
        task_dir = artifacts_dir / "finalized" / name
        finalized_task = True

    if not task_dir.is_dir():
        raise KeyError(name)

    # Build base summary
    summary = _task_summary(root, task_dir, name, cost_provider)

    # Add stage info
    summary["stages"] = _stage_info(task_dir)["stages"]
    # The current stage row should not lag the task-level phase, which also
    # counts a finished previous stage and the run-state's in-flight phase.
    for row in summary["stages"]:
        if row["n"] == summary["stage"] and _phase_rank(summary["phase"]) > _phase_rank(row["phase"]):
            row["phase"] = summary["phase"]

    # Add ledger rows (non-finalized) or totals (finalized)
    rows = _read_ledger_rows(task_dir)

    if finalized_task:
        # Finalized: include totals only, no ledger_rows
        totals = _counts_by_phase(rows)
        totals["total"] = len(rows)
        summary["totals"] = totals
    else:
        # Non-finalized: include ledger_rows
        summary["ledger_rows"] = rows

    # Add dates
    min_mtime = _min_artifact_mtime(task_dir)
    max_mtime = _max_artifact_mtime(task_dir)

    first_activity = None
    if min_mtime > 0.0:
        first_activity = datetime.datetime.fromtimestamp(min_mtime).isoformat()

    last_activity = None
    if max_mtime > 0.0:
        last_activity = datetime.datetime.fromtimestamp(max_mtime).isoformat()

    summary["dates"] = {
        "first_activity": first_activity,
        "last_activity": last_activity,
    }

    return summary


# ---------------------------------------------------------------------------
# Memory browser (T-02 / T-03 / T-04)
# ---------------------------------------------------------------------------

MEMORY_TYPES: frozenset = frozenset({"lessons", "sessions", "insights"})

# Canonical subpath prefixes within .workflow_artifacts/memory/ per type
_MEMORY_SUBPATH = {
    "lessons":  "memory/lessons-learned.md",
    "sessions": "memory/sessions",
    "insights": "memory/daily",
}


def _lessons_slug(header: str) -> str:
    """Stable slug for a lessons-learned entry header."""
    slug = header.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:80]
    return slug


def _parse_lessons_entries(text: str) -> list:
    """Parse lessons-learned.md into item dicts using memory_select.parse_entries."""
    items = []
    for entry in parse_entries(text):
        header = entry.header
        date = ""
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", header)
        if m:
            date = m.group(1)
        items.append({
            "id": _lessons_slug(header),
            "title": header,
            "date": date,
            "subpath": "memory/lessons-learned.md",
            "lineno": entry.lineno,
        })
    return items


def _parse_session_file(text: str, fname: str) -> dict:
    """Parse a session state file; return item dict with id, title, date, subpath."""
    lines = text.splitlines()
    # 3-condition frontmatter: (a) first non-blank line == "---",
    # (b) closing "---" in first 50 lines, (c) interior key: value line
    has_fm = False
    fm_end = -1
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:50], 1):
            if line.strip() == "---":
                interior = lines[1:i]
                if any(re.match(r"^\w[\w-]*:\s", il) for il in interior):
                    has_fm = True
                    fm_end = i
                    body_start = i + 1
                break
    date = ""
    if has_fm:
        for line in lines[1:fm_end]:
            m = re.match(r"^date:\s*(\d{4}-\d{2}-\d{2})", line)
            if m:
                date = m.group(1)
                break
    if not date:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", fname)
        if m:
            date = m.group(1)
    title = Path(fname).stem
    for line in lines[body_start:]:
        m = re.match(r"^#\s+(.+)", line)
        if m:
            title = m.group(1).strip()
            break
    return {
        "id": Path(fname).stem,
        "title": title,
        "date": date,
        "subpath": f"memory/sessions/{fname}",
    }


def _parse_insights_file(text: str, fname: str) -> dict:
    """Parse an insights scratchpad file; return item dict."""
    date = ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
    if m:
        date = m.group(1)
    title = Path(fname).stem
    for line in text.splitlines():
        m = re.match(r"^#\s+(.+)", line)
        if m:
            title = m.group(1).strip()
            break
    return {
        "id": Path(fname).stem,
        "title": title,
        "date": date,
        "subpath": f"memory/daily/{fname}",
    }


def memory_version_key(root: Path) -> int:
    """Fingerprint for the memory subtree at root/.workflow_artifacts/memory/.

    Uses max(st_mtime_ns) XOR (count * 1_000_000_000) XOR sum(st_size) over
    individual files — mirrors the _cost_jsonl_mtime_ns pattern. Only memory/
    files contribute; non-memory artifact changes do NOT affect this value.
    """
    memory_dir = root / ".workflow_artifacts" / "memory"
    if not memory_dir.is_dir():
        return 0
    max_mtime_ns = 0
    count = 0
    total = 0
    try:
        for p in memory_dir.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    count += 1
                    total += st.st_size
                    if st.st_mtime_ns > max_mtime_ns:
                        max_mtime_ns = st.st_mtime_ns
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return 0
    return max_mtime_ns ^ (count * 1_000_000_000) ^ total


def list_memory(root: Path, mtype: str) -> list:
    """Enumerate memory items of the given type.

    mtype must be one of MEMORY_TYPES ('lessons', 'sessions', 'insights').
    Returns list of dicts: {id, title, date, subpath}.
    Raises KeyError for unknown mtype.
    """
    if mtype not in MEMORY_TYPES:
        raise KeyError(f"Unknown memory type: {mtype!r}")

    memory_dir = root / ".workflow_artifacts" / "memory"

    if mtype == "lessons":
        path = memory_dir / "lessons-learned.md"
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            return []
        return _parse_lessons_entries(text)

    elif mtype == "sessions":
        sessions_dir = memory_dir / "sessions"
        if not sessions_dir.is_dir():
            return []
        items = []
        try:
            files = sorted(sessions_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
        except (OSError, PermissionError):
            return []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            items.append(_parse_session_file(text, f.name))
        return items

    else:  # insights
        daily_dir = memory_dir / "daily"
        if not daily_dir.is_dir():
            return []
        items = []
        try:
            files = sorted(daily_dir.glob("insights-*.md"), key=lambda p: p.name, reverse=True)
        except (OSError, PermissionError):
            return []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            items.append(_parse_insights_file(text, f.name))
        return items


def read_memory_item(root: Path, mtype: str, item_id: str) -> dict:
    """Return full content of a single memory item.

    SECURITY: never constructs a filesystem path from item_id. For sessions
    and insights, enumerates via list_memory() then resolves the stored
    subpath from the matched item dict. For lessons, the file path is
    hardcoded to lessons-learned.md and item_id is used only for entry
    matching after parse_entries().

    Raises KeyError for unknown mtype or unknown item_id.
    """
    if mtype not in MEMORY_TYPES:
        raise KeyError(f"Unknown memory type: {mtype!r}")

    memory_dir = root / ".workflow_artifacts" / "memory"

    if mtype == "lessons":
        # Re-parse entries to get body; file path is never derived from item_id
        path = memory_dir / "lessons-learned.md"
        if not path.is_file():
            raise KeyError(f"Memory item not found: {mtype!r}/{item_id!r}")
        try:
            full_text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as e:
            raise KeyError(f"Cannot read memory item: {e}") from e
        for entry in parse_entries(full_text):
            if _lessons_slug(entry.header) == item_id:
                date = ""
                dm = re.match(r"^(\d{4}-\d{2}-\d{2})", entry.header)
                if dm:
                    date = dm.group(1)
                return {
                    "id": item_id,
                    "title": entry.header,
                    "date": date,
                    "subpath": "memory/lessons-learned.md",
                    "body": "## " + entry.header + "\n" + entry.body,
                }
        raise KeyError(f"Memory item not found: {mtype!r}/{item_id!r}")

    else:
        # sessions / insights: each item is a whole file
        # subpath is determined by enumeration, NEVER by item_id
        items = list_memory(root, mtype)
        matched = None
        for item in items:
            if item["id"] == item_id:
                matched = item
                break
        if matched is None:
            raise KeyError(f"Memory item not found: {mtype!r}/{item_id!r}")
        file_path = root / ".workflow_artifacts" / matched["subpath"]
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as e:
            raise KeyError(f"Cannot read memory item: {e}") from e
        result = dict(matched)
        result["body"] = text
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    """CLI entry point.

    Flags:
      --json              dump scan_tasks result as JSON
      --task NAME         dump task_detail instead
      --include-finalized include finalized tasks in scan
      --project-root PATH project root (default: walk up from cwd)
    """
    parser = argparse.ArgumentParser(
        prog="dashboard_model.py",
        description="Workflow cost dashboard model — task enumeration and aggregation.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="dump scan_tasks result as indented JSON",
    )
    parser.add_argument(
        "--task",
        default=None,
        metavar="NAME",
        help="dump task_detail for a specific task (implies --json output)",
    )
    parser.add_argument(
        "--include-finalized",
        action="store_true",
        help="include finalized tasks in scan",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        metavar="PATH",
        help="project root directory (default: walk up from cwd to find .workflow_artifacts/)",
    )

    args = parser.parse_args(argv)

    # Resolve project root
    if args.project_root:
        root = Path(args.project_root).resolve()
    else:
        # Walk up from cwd
        root = Path.cwd().resolve()
        while root != root.parent:
            if (root / ".workflow_artifacts").is_dir():
                break
            root = root.parent
        else:
            print(
                f"error: no .workflow_artifacts found in {Path.cwd()} or parents",
                file=sys.stderr,
            )
            return 1

    try:
        if args.task:
            # Dump task_detail
            result = task_detail(root, args.task)
            print(json.dumps(result, indent=2))
            return 0
        else:
            # Dump scan_tasks
            result = scan_tasks(root, include_finalized=args.include_finalized)
            print(json.dumps(result, indent=2))
            return 0
    except KeyError as e:
        print(f"error: task not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
