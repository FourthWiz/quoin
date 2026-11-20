#!/usr/bin/env python3
# analyze_cost_ledger.py — cost analytics across all .workflow_artifacts/*/cost-ledger.md files.
#
# Pure stdlib by default. Reuses cost_from_jsonl.py for JSONL parsing and the
# static PRICES table. The Anthropic SDK is an optional lazy-import used ONLY for
# the --list-models flag (Task 3) — never for cost computation.
#
# Usage:
#   python3 analyze_cost_ledger.py [--project-root PATH] [--since YYYY-MM-DD]
#                                  [--ledger PATH] [--write-md] [--top N]
#                                  [--home PATH] [--list-models]
#
# Exit codes:
#   0  — success
#   1  — IO or runtime error
#   2  — bad CLI args

import argparse
import ast
import importlib.util
import pathlib
import sys
from datetime import date, datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Sibling import: cost_from_jsonl.py
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = pathlib.Path(__file__).parent
_CFJ_PATH = _SCRIPTS_DIR / "cost_from_jsonl.py"


def _load_cost_from_jsonl():
    """Import cost_from_jsonl from sibling path — works at source and deployed."""
    spec = importlib.util.spec_from_file_location("cost_from_jsonl", _CFJ_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cfj = _load_cost_from_jsonl()
project_hash = _cfj.project_hash
jsonl_path_for = _cfj.jsonl_path_for
parse_session = _cfj.parse_session


# ---------------------------------------------------------------------------
# Sibling import: core/scripts/cost_event.py (adapter -> core, allowed)
# ---------------------------------------------------------------------------
_CORE_SCRIPTS_DIR = _SCRIPTS_DIR.parent / "core" / "scripts"
_COST_EVENT_PATH = _CORE_SCRIPTS_DIR / "cost_event.py"


def _load_core_cost_event():
    """Import cost_event from core/scripts/ — works at source (relative
    traversal) and deployed (flat sibling dir, same shape as cost_from_jsonl).
    Fail-open: returns None on any load failure — callers treat every row
    as legacy, never crash.
    """
    try:
        path = _COST_EVENT_PATH if _COST_EVENT_PATH.exists() else _SCRIPTS_DIR / "cost_event.py"
        if not path.exists():
            return None
        module_key = "_analyze_cost_ledger_cost_event"
        if module_key in sys.modules:
            return sys.modules[module_key]
        spec = importlib.util.spec_from_file_location(module_key, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError):
        return None


_ce = _load_core_cost_event()
classify_attribution = _ce.classify_attribution if _ce is not None else None
cohort_attribution = _ce.cohort_attribution if _ce is not None else None


# ---------------------------------------------------------------------------
# Optional Anthropic SDK — lazy-imported ONLY for --list-models
# ---------------------------------------------------------------------------
def _list_models() -> Optional[list]:
    """Return list of model IDs from Anthropic SDK, or None if not installed.

    Uses client.models.list(limit=100) — limit=100 covers current model
    counts; the API paginates but current catalogs are well under 100 models.
    The SDK exposes id, display_name, and created_at — NO pricing data.
    Cost computation always uses the static PRICES table, never the SDK.
    """
    try:
        import anthropic  # lazy-import — only if installed
        client = anthropic.Anthropic()
        return [m.id for m in client.models.list(limit=100)]
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Ledger discovery
# ---------------------------------------------------------------------------
def discover_ledgers(project_root: pathlib.Path) -> list:
    """Walk project_root/.workflow_artifacts/ for all cost-ledger.md files,
    excluding any path that contains a 'finalized' component."""
    wa = project_root / ".workflow_artifacts"
    if not wa.exists():
        return []
    ledgers = []
    for p in wa.rglob("cost-ledger.md"):
        if "finalized" not in p.parts:
            ledgers.append(p)
    return sorted(ledgers)


# ---------------------------------------------------------------------------
# Ledger row parsing
# ---------------------------------------------------------------------------
def _parse_row(line: str) -> Optional[dict]:
    """Parse a single ledger row. Returns None to skip (blank, comment, non-task).
    Returns a dict with keys: uuid, date_str, phase, model, category, note,
    fallback_fires, attribution. Emits a warning to stderr on malformed rows
    (non-fatal).

    Tolerates 6-column and 7-column rows per the CLAUDE.md spec. The 8th
    column (attribution) is optional and first-class when present.
    Skips rows with UUID containing '$(' (template artifacts).
    Skips non-'task' category rows silently.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [p.strip() for p in stripped.split("|")]

    # Need at least 6 columns: UUID | DATE | PHASE | MODEL | CATEGORY | NOTE
    if len(parts) < 6:
        print(
            f"analyze_cost_ledger: skipping malformed row (fewer than 6 columns): {stripped!r}",
            file=sys.stderr,
        )
        return None

    uuid_val = parts[0]
    date_str = parts[1]
    phase = parts[2]
    model = parts[3]
    category = parts[4]
    note = parts[5]
    fallback_fires = int(parts[6]) if len(parts) >= 7 and parts[6].isdigit() else 0
    attribution = parts[7].strip() if len(parts) >= 8 else ""

    # Skip template artifacts
    if "$(" in uuid_val:
        return None

    # Skip non-task category rows
    if category != "task":
        return None

    return {
        "uuid": uuid_val,
        "date_str": date_str,
        "phase": phase,
        "model": model,
        "note": note,
        "fallback_fires": fallback_fires,
        "attribution": attribution,
    }


def parse_ledger_file(ledger_path: pathlib.Path, task_name: str = "") -> list:
    """Parse all rows from a ledger file. Returns list of row dicts."""
    rows = []
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except (IOError, OSError) as exc:
        print(f"analyze_cost_ledger: error reading {ledger_path}: {exc}", file=sys.stderr)
        return []

    for line in text.splitlines():
        row = _parse_row(line)
        if row is not None:
            row["task_name"] = task_name
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# JSONL cost lookup
# ---------------------------------------------------------------------------
def lookup_session_cost(
    uuid: str, proj_hash: str, home: pathlib.Path
) -> tuple:
    """Return (cost_usd, has_jsonl) for a given UUID.
    has_jsonl=False means unresolvable — either the JSONL file is missing OR
    it exists but every model in it is unpriceable; both cases are labeled
    unresolvable, never folded to a silent $0 (cost returned as 0.0).
    """
    jpath = jsonl_path_for(uuid, proj_hash, home=home)
    if not jpath.exists():
        return 0.0, False
    try:
        result = parse_session(jpath)
        if not result.get("priceable", True):
            return 0.0, False
        return result.get("totalCost", 0.0), True
    except (IOError, OSError) as exc:
        print(f"analyze_cost_ledger: error reading {jpath}: {exc}", file=sys.stderr)
        return 0.0, False


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def _row_verdict(row: dict) -> tuple:
    """classify_attribution wrapper with the fail-open "no classifier" default."""
    if classify_attribution is None:
        return ("legacy", None)
    return classify_attribution(row.get("attribution", ""))


def _legacy_build_report_core(
    rows: list, proj_hash: str, home: pathlib.Path, top_n: int
) -> dict:
    """Pre-cohort per-row accumulation (fail-open fallback when the cohort
    helper is unavailable, e.g. the core module failed to load). Kept
    byte-for-byte equivalent to the original stage-4 implementation."""
    by_phase: dict = {}
    by_model: dict = {}
    top_candidates = []
    resolved_total = 0.0
    total_fallback_fires = 0
    sessions_with_fires = 0
    no_jsonl_count = 0
    unresolvable_count = 0

    for row in rows:
        uuid = row["uuid"]
        phase = row["phase"]
        model = row["model"]
        date_str = row["date_str"]
        task_name = row.get("task_name", "")
        ff = row.get("fallback_fires", 0)

        verdict, inline_usd = _row_verdict(row)

        if verdict == "resolved":
            cost = inline_usd or 0.0
        elif verdict == "unresolvable":
            unresolvable_count += 1
            continue  # never contributes to resolved_total/by_phase/by_model/top-N
        else:  # legacy
            cost, has_jsonl = lookup_session_cost(uuid, proj_hash, home)
            if not has_jsonl:
                no_jsonl_count += 1
                unresolvable_count += 1
                continue  # missing, empty, or unpriced JSONL is unresolvable, not a silent $0

        resolved_total += cost

        if phase not in by_phase:
            by_phase[phase] = {"cost": 0.0, "count": 0}
        by_phase[phase]["cost"] += cost
        by_phase[phase]["count"] += 1

        if model not in by_model:
            by_model[model] = {"cost": 0.0, "count": 0}
        by_model[model]["cost"] += cost
        by_model[model]["count"] += 1

        top_candidates.append((cost, task_name, phase, uuid[:8], date_str))

        total_fallback_fires += ff
        if ff > 0:
            sessions_with_fires += 1

    top_candidates.sort(key=lambda x: x[0], reverse=True)
    top_sessions = top_candidates[:top_n]

    return {
        "total_cost": resolved_total,
        "resolved_total": resolved_total,
        "unresolvable_count": unresolvable_count,
        "by_phase": by_phase,
        "by_model": by_model,
        "shared_bucket": {},
        "top_sessions": top_sessions,
        "total_fallback_fires": total_fallback_fires,
        "sessions_with_fires": sessions_with_fires,
        "no_jsonl_count": no_jsonl_count,
        "session_count": len(rows),
    }


def _build_top_sessions(
    rows: list, resolve_session_cost, top_n: int
) -> list:
    """One entry per solo/resolved row, PLUS one combined entry per distinct
    shared UUID (never N per-phase rows for a shared session)."""
    legacy_uuid_counts: dict = {}
    for row in rows:
        verdict, _ = _row_verdict(row)
        if verdict == "legacy":
            legacy_uuid_counts[row["uuid"]] = legacy_uuid_counts.get(row["uuid"], 0) + 1

    candidates = []
    seen_shared_uuids: set = set()

    for row in rows:
        uuid = row["uuid"]
        phase = row["phase"]
        date_str = row["date_str"]
        task_name = row.get("task_name", "")

        verdict, inline_usd = _row_verdict(row)

        if verdict == "resolved":
            candidates.append((inline_usd or 0.0, task_name, phase, uuid[:8], date_str))
        elif verdict == "unresolvable":
            continue
        else:  # legacy
            if legacy_uuid_counts.get(uuid, 0) == 1:
                cost, has_cost = resolve_session_cost(uuid)
                if not has_cost:
                    continue
                candidates.append((cost, task_name, phase, uuid[:8], date_str))
            else:
                if uuid in seen_shared_uuids:
                    continue
                seen_shared_uuids.add(uuid)
                cost, has_cost = resolve_session_cost(uuid)
                if not has_cost:
                    continue
                candidates.append(
                    (cost, task_name, "shared-session (multi-phase)", uuid[:8], date_str)
                )

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:top_n]


def _fallback_fire_tally(rows: list, resolve_session_cost) -> tuple:
    """MIN-1: recompute total_fallback_fires/sessions_with_fires over the SAME
    resolved-row set the cohort helper attributed (solo-legacy with a resolved
    cost + resolved-inline rows) — shared-cohort rows and unresolvable/no-cost
    rows are excluded, mirroring today's pre-tally `continue` semantics."""
    legacy_uuid_counts: dict = {}
    for row in rows:
        verdict, _ = _row_verdict(row)
        if verdict == "legacy":
            legacy_uuid_counts[row["uuid"]] = legacy_uuid_counts.get(row["uuid"], 0) + 1

    total_fallback_fires = 0
    sessions_with_fires = 0
    for row in rows:
        verdict, _ = _row_verdict(row)
        if verdict == "unresolvable":
            continue
        if verdict == "legacy":
            if legacy_uuid_counts.get(row["uuid"], 0) != 1:
                continue  # shared — excluded from the tally
            _, has_cost = resolve_session_cost(row["uuid"])
            if not has_cost:
                continue
        ff = row.get("fallback_fires", 0)
        total_fallback_fires += ff
        if ff > 0:
            sessions_with_fires += 1
    return total_fallback_fires, sessions_with_fires


def build_report(
    rows: list,
    project_root: pathlib.Path,
    proj_hash: str,
    home: pathlib.Path,
    top_n: int = 10,
    since_date: Optional[date] = None,
) -> dict:
    """
    Compute cost for each row and aggregate into a report dict.

    Precedence rule (D-1/D-2, classify_attribution): a row whose col-8
    attribution classifies as "resolved" uses the inline usd directly (no
    JSONL lookup); "unresolvable" contributes NOTHING to the resolved total
    and is counted in unresolvable_count (never folded into $0); "legacy"
    (no col 8) is grouped into session cohorts by `cohort_attribution`
    (IVG-157): a UUID used by exactly one legacy row is resolved and
    attributed to that row's phase exactly as before (byte-identical);
    a UUID shared by two or more legacy rows (e.g. an inline checkpoint
    sharing its parent phase's session UUID) is NEVER charged in full to
    each participating phase — instead its cost is resolved once and shown
    as a single labeled `shared_bucket` ("not separately attributable"). A
    missing session cost (solo or shared) counts into unresolvable_count,
    never a silent $0.

    Returns:
      {
        "total_cost": float,           # alias of resolved_total
        "resolved_total": float,       # each distinct session counted ONCE
        "unresolvable_count": int,     # col-8-unresolved + no-cost rows/UUIDs
        "by_phase": {phase: {"cost": float, "count": int}},   # solo+resolved only
        "by_model": {model: {"cost": float, "count": int}},   # solo+resolved only
        "shared_bucket": {"cost": float, "phases": {...}, "uuids": int},
        "top_sessions": [(cost, task_name, phase, uuid_short, date_str), ...],
        "total_fallback_fires": int,
        "sessions_with_fires": int,
        "no_jsonl_count": int,
        "session_count": int,
      }
    """
    # Apply --since filter FIRST — cohorts form over the filtered row set.
    if since_date is not None:
        filtered = []
        for row in rows:
            try:
                row_date = datetime.strptime(row["date_str"], "%Y-%m-%d").date()
                if row_date >= since_date:
                    filtered.append(row)
            except ValueError:
                filtered.append(row)  # keep rows with unparseable dates
        rows = filtered

    if cohort_attribution is None:
        return _legacy_build_report_core(rows, proj_hash, home, top_n)

    memo: dict = {}

    def resolve_session_cost(uuid: str) -> tuple:
        if uuid in memo:
            return memo[uuid]
        result = lookup_session_cost(uuid, proj_hash, home)
        memo[uuid] = result
        return result

    cohort_result = cohort_attribution(rows, resolve_session_cost)

    if cohort_result is None:
        return _legacy_build_report_core(rows, proj_hash, home, top_n)

    top_sessions = _build_top_sessions(rows, resolve_session_cost, top_n)
    total_fallback_fires, sessions_with_fires = _fallback_fire_tally(
        rows, resolve_session_cost
    )

    return {
        "total_cost": cohort_result.resolved_total,
        "resolved_total": cohort_result.resolved_total,
        "unresolvable_count": cohort_result.unresolvable_count,
        "by_phase": cohort_result.by_phase,
        "by_model": cohort_result.by_model,
        "shared_bucket": cohort_result.shared_bucket,
        "top_sessions": top_sessions,
        "total_fallback_fires": total_fallback_fires,
        "sessions_with_fires": sessions_with_fires,
        "no_jsonl_count": cohort_result.unpriced_count,
        "session_count": len(rows),
    }


def format_report(
    report: dict,
    project_root: pathlib.Path,
    ledger_count: int,
    top_n: int,
    report_date: str,
) -> str:
    """Format the report dict into a human-readable string."""
    lines = []
    sep_full = "=" * 74
    sep_part = "-" * 74

    lines.append(f"Cost Analysis — {project_root} — {report_date}")
    lines.append(sep_full)
    unresolvable_count = report.get("unresolvable_count", 0)
    if unresolvable_count > 0:
        total_line = (
            f"Ledgers scanned: {ledger_count}  |  "
            f"Sessions: {report['session_count']}  |  "
            f"Resolved cost: ${report['resolved_total']:.2f}  |  "
            f"Unresolvable: {unresolvable_count} (labeled, not $0)  |  "
            f"Total: ~${report['resolved_total']:.2f} (partial)"
        )
    else:
        total_line = (
            f"Ledgers scanned: {ledger_count}  |  "
            f"Sessions: {report['session_count']}  |  "
            f"Resolved cost: ${report['resolved_total']:.2f}"
        )
    lines.append(total_line)
    lines.append(sep_part)

    lines.append("By phase:")
    for phase, data in sorted(report["by_phase"].items()):
        lines.append(f"  {phase:<20} ${data['cost']:.2f}  ({data['count']} sessions)")

    lines.append("")

    # Shared sessions (cohort attribution, IVG-157) — additive, not a
    # replacement for the solo By-phase block above.
    shared_bucket = report.get("shared_bucket") or {}
    if shared_bucket.get("uuids", 0) > 0:
        phases = shared_bucket.get("phases", {})
        phase_list = ", ".join(sorted(phases.keys()))
        lines.append("Shared sessions (multi-phase — not separately attributable):")
        lines.append(
            f"  shared-session (multi-phase)   ${shared_bucket['cost']:.2f}   "
            f"({shared_bucket['uuids']} session(s), {phase_list})"
        )
        for phase, counts in sorted(phases.items()):
            if phase == "checkpoint":
                lines.append(
                    f"    checkpoint: save x {counts['save']}, restore x {counts['restore']}"
                    "  (not separately attributable)"
                )
            else:
                lines.append(
                    f"    {phase}: {counts['count']}  (not separately attributable)"
                )
        lines.append("")

    lines.append("By model:")
    for model, data in sorted(report["by_model"].items()):
        lines.append(f"  {model:<30} ${data['cost']:.2f}  ({data['count']} sessions)")

    lines.append("")

    lines.append(f"Top {top_n} most expensive sessions:")
    for i, (cost, task_name, phase, uuid_short, date_str) in enumerate(
        report["top_sessions"], start=1
    ):
        label = f"{task_name}/{phase}" if task_name else phase
        lines.append(f"  {i:>2}. ${cost:.2f}  {label}  {uuid_short}  {date_str}")

    lines.append("")

    lines.append("Fallback-fire summary:")
    lines.append(
        f"  Total fallback fires: {report['total_fallback_fires']}"
        f"  (sessions with fires: {report['sessions_with_fires']})"
    )

    lines.append("")
    lines.append(
        f"Sessions unresolvable (missing, empty, or unpriced JSONL), not $0: {report['no_jsonl_count']}"
    )
    lines.append(sep_full)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_cost_ledger.py",
        description=(
            "Cost analytics across .workflow_artifacts/*/cost-ledger.md files. "
            "Pure stdlib. Reuses cost_from_jsonl.py for JSONL parsing. "
            "The Anthropic SDK is optional (used only for --list-models)."
        ),
    )
    parser.add_argument(
        "--project-root",
        metavar="PATH",
        default=None,
        help="Project root for ledger discovery + JSONL hash computation (default: cwd)",
    )
    parser.add_argument(
        "--ledger",
        metavar="PATH",
        default=None,
        help="Single ledger file path (overrides discovery)",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Filter sessions to this date or later",
    )
    parser.add_argument(
        "--write-md",
        action="store_true",
        help=(
            "Write report to "
            "<project-root>/.workflow_artifacts/memory/cost-analysis-<date>.md"
        ),
    )
    parser.add_argument(
        "--top",
        metavar="N",
        type=int,
        default=10,
        help="Number of sessions to show in Top N list (default: 10)",
    )
    parser.add_argument(
        "--home",
        metavar="PATH",
        default=None,
        help=(
            "Override base path for JSONL lookup (default: pathlib.Path.home()). "
            "JSONL files are resolved as <home>/.claude/projects/<hash>/<uuid>.jsonl. "
            "Use in tests to intercept JSONL lookup without touching ~/.claude/."
        ),
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help=(
            "List available Anthropic model IDs (requires anthropic SDK). "
            "Prints one model ID per line and exits 0. Does NOT run ledger analysis."
        ),
    )

    args = parser.parse_args()

    # --list-models: standalone sub-mode, does not run ledger analysis
    if args.list_models:
        model_ids = _list_models()
        if model_ids is None:
            print("anthropic SDK not installed")
        else:
            for mid in model_ids:
                print(mid)
        return 0

    project_root = pathlib.Path(args.project_root).resolve() if args.project_root else pathlib.Path.cwd()

    home = pathlib.Path(args.home).resolve() if args.home else pathlib.Path.home()

    proj_hash = project_hash(str(project_root))

    # Discover / load ledger(s)
    if args.ledger:
        ledger_path = pathlib.Path(args.ledger).resolve()
        if not ledger_path.exists():
            print(
                f"analyze_cost_ledger: ledger not found: {ledger_path}",
                file=sys.stderr,
            )
            return 1
        task_name = ledger_path.parent.name
        all_rows = parse_ledger_file(ledger_path, task_name)
        ledger_count = 1
    else:
        ledger_paths = discover_ledgers(project_root)
        all_rows = []
        for lp in ledger_paths:
            task_name = lp.parent.name
            all_rows.extend(parse_ledger_file(lp, task_name))
        ledger_count = len(ledger_paths)

    # Apply --since filter (parse date)
    since_date: Optional[date] = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(
                f"analyze_cost_ledger: invalid --since date {args.since!r} "
                "(expected YYYY-MM-DD)",
                file=sys.stderr,
            )
            return 2

    # Build and format report
    report_date_str = date.today().isoformat()
    report = build_report(
        all_rows,
        project_root=project_root,
        proj_hash=proj_hash,
        home=home,
        top_n=args.top,
        since_date=since_date,
    )
    output = format_report(
        report,
        project_root=project_root,
        ledger_count=ledger_count,
        top_n=args.top,
        report_date=report_date_str,
    )

    print(output)

    if args.write_md:
        out_dir = project_root / ".workflow_artifacts" / "memory"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"cost-analysis-{report_date_str}.md"
            out_path.write_text(output, encoding="utf-8")
            print(f"Report written to {out_path}", file=sys.stderr)
        except (IOError, OSError) as exc:
            print(
                f"analyze_cost_ledger: error writing report: {exc}",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
