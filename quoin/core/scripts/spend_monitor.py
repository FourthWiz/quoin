#!/usr/bin/env python3
# CLAUDE-ADAPTER-OWNED — this file reads Claude Code JSONL sessions and/or
# Claude model pricing. Do NOT import this module from any file outside of
# quoin/quoin/. The portable cost-event schema is at
# quoin/core/scripts/cost_event.py. Adding runtime-neutral functionality
# belongs there, not here.
#
# spend_monitor.py — realtime token-spend terminal monitor for agentdesk.
# Pure stdlib. Walks ~/.claude/projects/**/*.jsonl, filters to today's rows,
# aggregates per-model cost, and renders a compact terminal pane.
#
# Reuses PRICES/cost_for_entry/jsonl_path_for/project_hash from cost_from_jsonl.py
# (single pricing source of truth — no second price dict in this file).
# The only cross-loaded module is cost_from_jsonl.py (via _load_sibling).
# dashboard_cost.py and dashboard_model.py are NOT imported here (D-06).
"""quoin/core/scripts/spend_monitor.py — realtime token-spend monitor.

Public API:
  parse_session_today(path, day_start_utc, day_end_utc) -> dict
  aggregate_today(home, now=None, scope="global", cache=None, project_root=None) -> SpendSnapshot
  render_compact(snap, width=38, interval=3, live=True) -> str
  main(argv=None) -> int
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Cross-load cost_from_jsonl (adapter sibling, same pattern as dashboard_cost.py)
# ---------------------------------------------------------------------------

def _load_sibling(name: str):
    """Load a sibling script from the scripts/ directory adjacent to core/scripts/."""
    # spend_monitor.py lives in core/scripts/; the sibling is in ../../../quoin/scripts/
    # i.e.  quoin/quoin/core/scripts/spend_monitor.py
    #       quoin/quoin/scripts/cost_from_jsonl.py
    core_dir = Path(__file__).resolve().parent  # quoin/quoin/core/scripts/
    # Try: quoin/quoin/scripts/<name>.py (sibling of core from quoin/quoin perspective)
    candidate = core_dir.parent.parent / "scripts" / f"{name}.py"
    if not candidate.exists():
        raise ImportError(f"Cannot load {name}: {candidate} not found")
    module_key = f"_spend_monitor_{name}"
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(module_key, candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {candidate}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_cfj = _load_sibling("cost_from_jsonl")

# Bind functions we need from cost_from_jsonl — single pricing source
PRICES = _cfj.PRICES          # single source of truth; no second price dict here
cost_for_entry = _cfj.cost_for_entry
jsonl_path_for = _cfj.jsonl_path_for
project_hash = _cfj.project_hash
is_priced = _cfj.is_priced
is_costable = _cfj.is_costable


def _load_core(name: str):
    """Load a core->core sibling module (allowed; stays within this directory).

    Mirrors dashboard_model.py's core loader shape. Unlike _load_sibling
    (which resolves the adapter directory), this stays core-local.
    """
    core_dir = Path(__file__).resolve().parent  # quoin/quoin/core/scripts/
    candidate = core_dir / f"{name}.py"
    if not candidate.exists():
        raise ImportError(f"Cannot load {name}: {candidate} not found")
    module_key = f"_spend_monitor_{name}"
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(module_key, candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {candidate}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    _ce = _load_core("cost_event")
    classify_attribution = _ce.classify_attribution
except ImportError:
    # Fail-open: inline-first precedence silently disabled; every row is
    # treated as legacy (today's behavior), never a crash.
    classify_attribution = None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SpendSnapshot:
    """Result of aggregate_today()."""
    today_usd: float = 0.0
    by_model: Dict[str, float] = field(default_factory=dict)      # short_name -> usd
    by_model_pct: Dict[str, float] = field(default_factory=dict)  # short_name -> pct
    by_task: Dict[str, float] = field(default_factory=dict)       # task_name -> usd
    by_task_partial: bool = False    # True if some ledger rows were na/unresolvable
    by_phase: Dict[str, float] = field(default_factory=dict)      # phase -> usd (D-04)
    by_phase_partial: bool = False   # True when unresolved-UUID condition applies
    today_partial: bool = False      # True if any today-file contributed unpriced rows (IVG-249 T-05 CRIT-1)
    stale: bool = False
    scope: str = "global"            # "global" or "project"


# ---------------------------------------------------------------------------
# Short-model-name mapping
# ---------------------------------------------------------------------------

# Ordered (substring, tier) pairs — first match wins. This is the single
# source of truth for both classification (_short_model) and display
# ordering (_model_order); do not duplicate this list anywhere else.
MODEL_TIERS = (
    ("fable", "fable"),
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
)
OTHER_TIER = "other"  # fallthrough default — never a row of MODEL_TIERS


def _short_model(full_id: str) -> str:
    """Map full model ID to short display name: fable / opus / sonnet / haiku / other."""
    fl = full_id.lower()
    for sub, tier in MODEL_TIERS:
        if sub in fl:
            return tier
    return OTHER_TIER


def _model_order() -> list:
    """Display order for per-model rows, derived from MODEL_TIERS on every call.

    Computed at call time (not cached at import) so the ordering can never
    drift from the classification table — see D-03 in the stage plan.
    """
    return [tier for _sub, tier in MODEL_TIERS] + [OTHER_TIER]


# ---------------------------------------------------------------------------
# T-01: per-row today filter
# ---------------------------------------------------------------------------

def parse_session_today(
    path: Path,
    day_start_utc: datetime,
    day_end_utc: datetime,
) -> dict:
    """Walk JSONL rows and return only those timestamped in [day_start_utc, day_end_utc).

    Row inclusion rule (deterministic parity):
      A row is included iff ALL three hold:
        1) row.get("message") is a non-empty dict
        2) message.get("usage") is a non-empty dict
        3) row.get("timestamp") is a string that parses to UTC datetime in [start, end)
      Rows missing timestamp, with non-string timestamp, or unparseable timestamp are
      EXCLUDED and counted in skipped_no_ts.

    Returns:
      {
        "per_model_cost": {model: float, ...},
        "per_model_tok":  {model: int, ...},
        "skipped_no_ts":  int,
        "unknown_models": [str, ...],  # sorted, deduped slugs not in PRICES (IVG-249 T-05)
        "priceable":      bool,        # True iff no unknown model was seen during the walk
      }
    """
    per_model_cost: Dict[str, float] = {}
    per_model_tok: Dict[str, int] = {}
    skipped_no_ts = 0
    unknown_models: set = set()

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Malformed line — skip (matches cost_from_jsonl behavior)
                    continue

                msg = row.get("message")
                if not msg or not isinstance(msg, dict):
                    continue

                usage = msg.get("usage")
                if not usage or not isinstance(usage, dict):
                    continue

                # Timestamp filter — condition 3
                ts_str = row.get("timestamp")
                if not ts_str or not isinstance(ts_str, str):
                    skipped_no_ts += 1
                    continue
                try:
                    ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    skipped_no_ts += 1
                    continue

                if not (day_start_utc <= ts < day_end_utc):
                    # Row is outside today's window — counted separately (not in skipped_no_ts)
                    continue

                model = msg.get("model") or ""
                if is_costable(model):
                    cost, tok = cost_for_entry(model, usage)
                    per_model_cost[model] = per_model_cost.get(model, 0.0) + cost
                    per_model_tok[model] = per_model_tok.get(model, 0) + tok
                    if not is_priced(model):
                        unknown_models.add(model)

    except (IOError, OSError):
        pass

    return {
        "per_model_cost": per_model_cost,
        "per_model_tok": per_model_tok,
        "skipped_no_ts": skipped_no_ts,
        "unknown_models": sorted(unknown_models),
        "priceable": not unknown_models,
    }


# ---------------------------------------------------------------------------
# T-02: per-(path,mtime) memo cache helper
# ---------------------------------------------------------------------------

_CACHE_MISS = object()  # sentinel


def _cache_get(cache: Optional[dict], path: Path) -> Any:
    """Return cached result for path, or _CACHE_MISS if not cached / no cache."""
    if cache is None:
        return _CACHE_MISS
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None
    key = (str(path), mtime)
    return cache.get(key, _CACHE_MISS)


def _cache_set(cache: Optional[dict], path: Path, result: Any) -> None:
    """Store result in cache keyed by (path_str, mtime_or_None)."""
    if cache is None:
        return
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None
    key = (str(path), mtime)
    cache[key] = result


# ---------------------------------------------------------------------------
# T-03: per-task ("by task") breakdown via ledgers
# ---------------------------------------------------------------------------

def _parse_ledger_today(ledger_path: Path, today_str: str) -> list:
    """Return list of {uuid, phase, attribution} for rows where date == today_str.

    Parses the cost-ledger.md pipe-separated format:
      UUID | DATE | PHASE | MODEL | task | NOTE | FALLBACK_FIRES | ATTRIBUTION
    Columns are 0-indexed. date is col 1, uuid is col 0, phase is col 2,
    attribution is col 7 (optional 8th column; "" when absent).
    """
    rows = []
    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("UUID"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue
                uuid = parts[0].strip()
                date_str = parts[1].strip()
                if date_str != today_str:
                    continue
                phase = parts[2].strip()
                attribution = parts[7].strip() if len(parts) >= 8 else ""
                rows.append({"uuid": uuid, "phase": phase, "attribution": attribution})
    except (IOError, OSError):
        pass
    return rows


def scan_ledgers_today(
    project_root: Path,
    day_start_utc: datetime,
    day_end_utc: datetime,
    home: Optional[Path] = None,
    proj_hash_str: Optional[str] = None,
) -> tuple:
    """Scan .workflow_artifacts/*/cost-ledger.md for today-dated UUID rows.

    For each row with a valid (non-na, non-empty) UUID:
      - Resolve UUID → JSONL path via jsonl_path_for
      - Call parse_session_today to get today's spend
      - Accumulate per task name AND per phase (D-04: direct PHASE-column attribution)

    UUID deduplication: maintain seen_uuids set per task-dir scan. When a UUID is
    encountered a second time (possibly under a different phase), skip parse_session_today
    — cost was already counted on the first occurrence. Phase is attributed to the first
    row for each UUID.

    Returns (by_task, by_phase, by_task_partial). Tuple shape is unchanged
    (IVG-249 T-05), but by_task_partial's meaning now additionally covers an
    unpriceable-but-present session (parse_session_today's priceable=False,
    i.e. at least one unknown model was seen in-window) — previously such a
    session silently contributed nothing without setting the flag. As of the
    IVG-249 fix round (F-02), a mixed (priced + unknown-model) session KEEPS
    its priced portion in by_task/by_phase while still setting the flag —
    aligned with aggregate_today's today-pane policy of labeling loudly
    rather than silently dropping. A priceable-but-zero result (including
    the routine empty-window case) still contributes nothing and does NOT
    set the flag.
    by_task: dict[str, float] — task_name -> usd
    by_phase: dict[str, float] — phase -> usd (flat, D-04)
    by_task_partial: bool — True if any rows had na-UUID or unresolvable JSONL.
    """
    if home is None:
        home = Path.home()
    if proj_hash_str is None:
        proj_hash_str = project_hash(str(project_root))

    today_str = day_start_utc.astimezone().strftime("%Y-%m-%d") if day_start_utc.tzinfo else date.today().isoformat()

    artifacts_dir = project_root / ".workflow_artifacts"
    if not artifacts_dir.is_dir():
        return {}, {}, False

    by_task: Dict[str, float] = {}
    by_phase: Dict[str, float] = {}
    by_task_partial = False
    seen_uuids: set = set()

    try:
        for task_dir in sorted(artifacts_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            if task_dir.name in {"memory", "cache", "finalized"}:
                continue
            ledger = task_dir / "cost-ledger.md"
            if not ledger.exists():
                continue

            task_name = task_dir.name
            rows = _parse_ledger_today(ledger, today_str)
            for row in rows:
                uuid = row.get("uuid", "")
                if not uuid or uuid.lower() == "na":
                    by_task_partial = True
                    continue

                # Inline-first precedence: col-8 attribution, if present and
                # classifiable, takes priority over legacy JSONL resolution.
                verdict, usd = ("legacy", None)
                if classify_attribution is not None:
                    attribution = row.get("attribution", "")
                    verdict, usd = classify_attribution(attribution)

                if verdict == "resolved":
                    # Inline usd — no JSONL lookup, no UUID-dedup gate (on-behalf
                    # rows carry a unique uuid=<agentId>; inline usd is already
                    # per-phase, so double-counting across rows cannot occur).
                    task_usd = usd or 0.0
                    by_task[task_name] = by_task.get(task_name, 0.0) + task_usd
                    phase = row.get("phase", "")
                    if phase:
                        by_phase[phase] = by_phase.get(phase, 0.0) + task_usd
                    continue

                if verdict == "unresolvable":
                    # Never fold into a $0 contribution.
                    by_task_partial = True
                    continue

                # verdict == "legacy": existing UUID-dedup + JSONL-resolve path.
                if uuid in seen_uuids:
                    continue
                jsonl_path = jsonl_path_for(uuid, proj_hash_str, home=home)
                if not jsonl_path.exists():
                    by_task_partial = True
                    continue
                seen_uuids.add(uuid)
                result = parse_session_today(jsonl_path, day_start_utc, day_end_utc)
                task_usd = sum(result["per_model_cost"].values())
                if not result["priceable"]:
                    # Unpriceable (unknown_models non-empty): label it (R2-MAJ-3)
                    # AND keep any priced portion of a mixed (priced + unknown)
                    # session rather than dropping it — mirrors aggregate_today's
                    # today-pane policy, which already keeps the priced portion
                    # here (IVG-249 fix-round F-02: label loudly, never silently
                    # drop). A priceable-but-zero result (including the routine
                    # empty-window case) keeps today's ordinary skip below,
                    # unflagged.
                    by_task_partial = True
                if task_usd > 0:
                    by_task[task_name] = by_task.get(task_name, 0.0) + task_usd
                    phase = row.get("phase", "")
                    if phase:
                        by_phase[phase] = by_phase.get(phase, 0.0) + task_usd
    except (OSError, PermissionError):
        pass

    return by_task, by_phase, by_task_partial


# ---------------------------------------------------------------------------
# T-01 + T-02: main aggregation
# ---------------------------------------------------------------------------

def _local_day_bounds(now: Optional[datetime] = None):
    """Return (day_start_utc, day_end_utc) for today in local timezone."""
    if now is None:
        now = datetime.now()
    # Local midnight today
    local_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_tomorrow = local_today + timedelta(days=1)
    # Convert to UTC via astimezone (handles DST correctly)
    tz_offset = datetime.now(timezone.utc).astimezone().utcoffset()
    if tz_offset is None:
        tz_offset = timedelta(0)
    start_utc = (local_today - tz_offset).replace(tzinfo=timezone.utc)
    end_utc = (local_tomorrow - tz_offset).replace(tzinfo=timezone.utc)
    return start_utc, end_utc


def aggregate_today(
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    scope: str = "global",
    cache: Optional[dict] = None,
    project_root: Optional[Path] = None,
) -> SpendSnapshot:
    """Aggregate today's token spend across all JSONL files.

    scope="global" (DEFAULT): glob ALL ~/.claude/projects/**/*.jsonl
    scope="project": glob only cwd-derived project hash dir
    """
    if home is None:
        home = Path.home()

    day_start_utc, day_end_utc = _local_day_bounds(now)

    # Determine which JSONL files to scan
    claude_dir = home / ".claude" / "projects"
    if scope == "project":
        if project_root is None:
            project_root = _find_project_root(Path.cwd())
        if project_root is not None:
            ph = project_hash(str(project_root))
        else:
            ph = project_hash(str(Path.cwd()))
        pattern = str(claude_dir / ph / "*.jsonl")
    else:
        # global: all project hashes
        pattern = str(claude_dir / "**" / "*.jsonl")

    try:
        jsonl_files = glob.glob(pattern, recursive=True)
    except (OSError, PermissionError):
        jsonl_files = []

    # Accumulate per full-model-id cost/tokens
    full_model_cost: Dict[str, float] = {}
    full_model_tok: Dict[str, int] = {}
    today_partial = False  # T-05 CRIT-1: True if any today-file contributed unpriced rows

    # Convert day_start_utc to float for mtime comparison
    day_start_ts = day_start_utc.timestamp()

    for fpath_str in jsonl_files:
        fpath = Path(fpath_str)

        # T-02: today-start mtime prefilter (stat-only skip for files before today)
        try:
            mtime = fpath.stat().st_mtime
        except OSError:
            continue
        if mtime < day_start_ts:
            continue  # File not touched today — skip without opening

        # T-02: (path, mtime) memo cache
        cached = _cache_get(cache, fpath)
        if cached is _CACHE_MISS:
            result = parse_session_today(fpath, day_start_utc, day_end_utc)
            _cache_set(cache, fpath, result)
        else:
            result = cached

        if result is None:
            continue

        if result.get("unknown_models"):
            today_partial = True

        for model, usd in result["per_model_cost"].items():
            full_model_cost[model] = full_model_cost.get(model, 0.0) + usd
        for model, tok in result["per_model_tok"].items():
            full_model_tok[model] = full_model_tok.get(model, 0) + tok

    # Aggregate to short names
    today_usd = 0.0
    by_model: Dict[str, float] = {}
    for full_id, usd in full_model_cost.items():
        short = _short_model(full_id)
        by_model[short] = by_model.get(short, 0.0) + usd
        today_usd += usd

    # Compute percentages; other bucket absorbs rounding remainder (sum approx 100 ±2)
    by_model_pct: Dict[str, float] = {}
    if today_usd > 0:
        for short, usd in by_model.items():
            by_model_pct[short] = round(usd / today_usd * 100, 1)

    # T-03: by-task breakdown; T-05: by-phase breakdown (D-04)
    by_task: Dict[str, float] = {}
    by_phase: Dict[str, float] = {}
    by_task_partial = False
    if project_root is not None:
        try:
            ph = project_hash(str(project_root))
            by_task, by_phase, by_task_partial = scan_ledgers_today(
                project_root, day_start_utc, day_end_utc,
                home=home, proj_hash_str=ph,
            )
        except Exception:
            by_task = {}
            by_phase = {}
            by_task_partial = False

    return SpendSnapshot(
        today_usd=today_usd,
        by_model=by_model,
        by_model_pct=by_model_pct,
        by_task=by_task,
        by_task_partial=by_task_partial,
        by_phase=by_phase,
        by_phase_partial=by_task_partial,
        today_partial=today_partial,
        stale=False,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# T-01: Render
# ---------------------------------------------------------------------------

def render_compact(
    snap: SpendSnapshot,
    width: int = 38,
    interval: int = 3,
    live: bool = True,
    task_limit: int = 3,
    show_task: bool = True,
) -> str:
    """Render a compact narrow-pane text matching the UI sketch:

    TOKEN SPEND (all)
    today   $4.21
    opus    $3.10  62%
    sonnet  $0.88  18%
    haiku   $0.23   5%
    ── by task (partial) ──
    ivg-62  $1.40
    ⟳ 3s   live
    """
    lines = []

    # Header: scope label
    scope_label = "(all)" if snap.scope == "global" else "(proj)"
    header = f"TOKEN SPEND {scope_label}"
    lines.append(header[:width])

    # Today total — label carries a "(partial)" suffix when today_partial is set
    # (IVG-249 T-05 Site 3(b)): a trailing marker after the formatted amount was
    # ruled out — _fmt_amount_line pads to width and render_compact's own join
    # truncates a second time, so any post-hoc suffix is unconditionally sliced
    # off before it can render. The label argument is the only safe landing spot.
    today_label = "today (partial)" if snap.today_partial else "today"
    today_line = _fmt_amount_line(today_label, snap.today_usd, None, width)
    lines.append(today_line)

    # Per-model rows (non-zero only, ordered per MODEL_TIERS — see _model_order)
    for short in _model_order():
        usd = snap.by_model.get(short, 0.0)
        if usd <= 0:
            continue
        pct = snap.by_model_pct.get(short)
        line = _fmt_amount_line(short, usd, pct, width)
        lines.append(line)

    # By-task block
    if show_task and (snap.by_task or snap.by_task_partial):
        if snap.by_task_partial and not snap.by_task:
            # All rows were na — omit block (just leave partial note out)
            pass
        else:
            # Divider
            divider_suffix = " (partial)" if snap.by_task_partial else ""
            divider = f"── by task{divider_suffix} ──"
            lines.append(divider[:width])

            # Top N tasks by today-USD
            sorted_tasks = sorted(snap.by_task.items(), key=lambda kv: kv[1], reverse=True)
            for task_name, usd in sorted_tasks[:task_limit]:
                if usd <= 0:
                    continue
                # Truncate task name to leave room for the amount
                line = _fmt_amount_line(task_name, usd, None, width)
                lines.append(line)

    # Footer
    interval_str = f"⟳ {interval}s"
    live_str = "live" if live else "once"
    footer = f"{interval_str}   {live_str}"
    lines.append(footer[:width])

    # Ensure every line fits in width
    return "\n".join(line[:width] for line in lines)


def _fmt_amount_line(label: str, usd: float, pct: Optional[float], width: int) -> str:
    """Format a line like:  label   $X.XX  NN%

    Right-aligns the dollar amount; appends percent if provided.
    Truncates label to ensure the line fits in width.
    """
    amount_str = f"${usd:.2f}"
    if pct is not None:
        pct_str = f"{int(round(pct)):3d}%"
        right_part = f"  {pct_str}"
    else:
        right_part = ""

    # Width allocation: amount is right-aligned; label fills the rest
    # Format: label<spaces>amount  pct
    # Minimum: "x  $0.00" = 8 chars
    right_total = len(amount_str) + len(right_part)
    label_budget = max(1, width - right_total - 2)  # 2 spaces gap
    label_trunc = label[:label_budget]

    # Build with padding
    spaces = width - len(label_trunc) - right_total
    spaces = max(1, spaces)
    line = f"{label_trunc}{' ' * spaces}{amount_str}{right_part}"
    return line[:width]


# ---------------------------------------------------------------------------
# Project root discovery (mirrors status_graph.py)
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

def _watch_loop(args: argparse.Namespace, effective_interval: int, project_root: Optional[Path]) -> None:
    """Clear + redraw + sleep loop. KeyboardInterrupt swallowed (mirrors status_graph lines 435-443)."""
    cache: dict = {}
    try:
        while True:
            os.system("clear")
            snap = aggregate_today(
                home=Path(args.home) if args.home else None,
                scope=args.scope,
                cache=cache,
                project_root=project_root,
            )
            output = render_compact(
                snap,
                width=args.width,
                interval=effective_interval,
                live=True,
                task_limit=args.task_limit,
                show_task=not args.no_task,
            )
            print(output)
            time.sleep(effective_interval)
    except KeyboardInterrupt:
        pass


def _run_once(args: argparse.Namespace, project_root: Optional[Path]) -> tuple:
    """Single-shot: return (output_str, exit_code)."""
    snap = aggregate_today(
        home=Path(args.home) if args.home else None,
        scope=args.scope,
        cache=None,
        project_root=project_root,
    )

    if args.json:
        data = {
            "today_usd": snap.today_usd,
            "by_model": snap.by_model,
            "by_model_pct": snap.by_model_pct,
            "by_task": snap.by_task,
            "by_task_partial": snap.by_task_partial,
            "by_phase": snap.by_phase,
            "by_phase_partial": snap.by_phase_partial,
            "today_partial": snap.today_partial,
            "scope": snap.scope,
            "stale": snap.stale,
        }
        return json.dumps(data, indent=2), 0

    # Resolve effective interval for footer display
    effective_interval = (
        args.interval if args.interval is not None
        else (args.watch if args.watch is not None else 3)
    )
    output = render_compact(
        snap,
        width=args.width,
        interval=effective_interval,
        live=False,
        task_limit=args.task_limit,
        show_task=not args.no_task,
    )
    return output, 0


def main(argv=None) -> int:  # type: ignore[assignment]
    parser = argparse.ArgumentParser(
        prog="spend_monitor",
        description="Realtime token-spend terminal monitor for agentdesk.",
    )
    parser.add_argument(
        "--watch", nargs="?", const=3, type=int, metavar="SECONDS",
        help="refresh mode: clear+redraw every N seconds (default 3)",
    )
    parser.add_argument(
        "--interval", type=int, default=None, metavar="N",
        help="refresh interval in seconds (overrides --watch if both given)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="single-shot: print once and exit (overrides --watch/--interval)",
    )
    parser.add_argument(
        "--compact", action="store_true",
        help="narrow-pane render (default-on for agentdesk; this flag is accepted for CLI compat)",
    )
    parser.add_argument(
        "--scope", choices=["global", "project"], default="global",
        help="global = all ~/.claude/projects/**/*.jsonl; project = cwd-derived project hash only",
    )
    parser.add_argument(
        "--width", type=int, default=38,
        help="render width in columns (default 38)",
    )
    parser.add_argument(
        "--no-task", action="store_true",
        help="suppress the by-task breakdown block",
    )
    parser.add_argument(
        "--task-limit", type=int, default=3, metavar="N",
        help="max tasks to show in by-task block (default 3)",
    )
    parser.add_argument(
        "--home", default=None, metavar="PATH",
        help="override HOME root for JSONL discovery (used in tests)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit snapshot as JSON (for tests/automation)",
    )

    args = parser.parse_args(argv)

    # Resolve effective_interval: --interval wins over --watch if both given (D-03)
    effective_interval = (
        args.interval if args.interval is not None
        else (args.watch if args.watch is not None else 3)
    )

    # Expose on args for _run_once footer
    args._effective_interval = effective_interval  # noqa: SLF001

    # Resolve project root for by-task breakdown
    if args.home:
        # When --home is given (test mode), skip by-task unless project_root is inferrable
        project_root = _find_project_root(Path.cwd())
    else:
        project_root = _find_project_root(Path.cwd())

    if args.once or (args.watch is None and args.interval is None):
        # Single-shot
        output, code = _run_once(args, project_root)
        print(output)
        return code

    # Watch mode
    _watch_loop(args, effective_interval, project_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
