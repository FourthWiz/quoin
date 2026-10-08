#!/usr/bin/env python3
# CLAUDE-ADAPTER-OWNED — this file reads Claude Code JSONL transcripts and
# Claude model pricing to total a task's cost ledger. Do NOT import this module
# from any file in quoin/core/. The portable cost-event schema is at
# quoin/core/scripts/cost_event.py and the portable total normalizer is
# quoin/core/scripts/cost_summary.py; runtime-neutral functionality belongs
# there, not here.
#
# cost_ledger_summary.py — builds cost-summary.json for a task from its cost
# ledger. Ledger rows written while a transcript was not yet readable carry
# only `tok=<n>;src=unresolved`; the transcripts are on disk by finalize time,
# so this script locates and prices them now. The ledger itself is never
# rewritten.
#
# Row handling, in order:
#   1. A row with an inline usd value is taken as is.
#   2. A row whose id is a subagent id (a + 16 hex) is located: first through
#      each session id listed in the same ledger, then through a project-wide
#      search accepted only when exactly one transcript matches. A located,
#      flushed, fully priced transcript is re-priced.
#   3. A row that is still unpriced but carries a token count is estimated from
#      the token mix and model alias, and labelled as an estimate.
#   4. Anything else is counted as unresolvable.
#   5. Rows identified only by a session id are priced from that session's
#      transcript; a session shared by several phases is counted once.
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOK_RE = re.compile(r"(?:^|;)\s*tok=(\d+)")


def _load_module(key: str, path: pathlib.Path):
    if key in sys.modules:
        return sys.modules[key]
    if not path.exists():
        raise ImportError("Cannot load %s: %s not found" % (key, path))
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError("Cannot create spec for %s at %s" % (key, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_sibling(name: str):
    return _load_module("_cost_ledger_summary_" + name, _SCRIPTS_DIR / (name + ".py"))


def _load_cost_event():
    core = _SCRIPTS_DIR.parent / "core" / "scripts" / "cost_event.py"
    path = core if core.exists() else _SCRIPTS_DIR / "cost_event.py"
    return _load_module("_cost_ledger_summary_cost_event", path)


_cfj = _load_sibling("cost_from_jsonl")
_atc = _load_sibling("agent_transcript_cost")
_ce = _load_cost_event()


def _session_cost(home: pathlib.Path, proj_hash: str, sid: str) -> Tuple[float, bool]:
    """Whole-session cost for the cohort resolver: (cost, has_cost).

    A session counts as costed only when its transcript exists, has usage rows
    and every model in it is priced, so a partly unpriced session is never
    reported as a smaller real number."""
    path = _cfj.jsonl_path_for(sid, proj_hash, home)
    if not path.exists():
        return 0.0, False
    summary = _cfj.parse_session(path)
    has_cost = bool(summary["entries"]) and not summary["unknown_models"]
    return float(summary["totalCost"]), has_cost


def _tok_of(attribution: str) -> Optional[int]:
    match = _TOK_RE.search(attribution or "")
    return int(match.group(1)) if match else None


def _locate_agent_transcript(
    agent_id: str,
    session_ids: List[str],
    project_path: str,
    home: pathlib.Path,
    proj_hash: str,
) -> Optional[pathlib.Path]:
    for sid in session_ids:
        found = _atc.resolve_by_agent_id(sid, agent_id, project_path, home)
        if found is not None:
            return found
    pattern = str(home / ".claude" / "projects" / proj_hash / "*" / "subagents"
                  / ("agent-%s.jsonl" % agent_id))
    import glob

    matches = glob.glob(pattern)
    if len(matches) == 1:
        return pathlib.Path(matches[0])
    return None


def build_summary(
    ledger: pathlib.Path,
    project_path: str,
    home: pathlib.Path,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return (summary, per-row dispositions) for a ledger. Never raises for a
    bad row: the row is counted unresolvable and noted instead."""
    proj_hash = _cfj.project_hash(project_path)
    notes: List[str] = []
    rows: List[Any] = []
    try:
        with open(ledger, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                try:
                    event = _ce.parse_row(raw, source=str(ledger), lineno=lineno)
                except Exception as exc:  # noqa: BLE001
                    notes.append("line %d unreadable: %s" % (lineno, exc))
                    continue
                if event is not None:
                    rows.append(event)
    except OSError as exc:
        notes.append("ledger not readable: %s" % exc)

    session_ids = [r.uuid for r in rows if _SESSION_ID_RE.match(r.uuid)]
    dispositions: List[Dict[str, Any]] = []

    per_phase: Dict[str, Dict[str, float]] = {}
    per_model: Dict[str, Dict[str, float]] = {}
    estimated_per_phase: Dict[str, float] = {}
    resolved_total = 0.0
    estimated_total = 0.0
    estimated_count = 0
    repriced_count = 0
    inline_count = 0
    unresolvable_count = 0
    legacy_rows: List[Any] = []

    def add_priced(phase: str, model: str, usd: float) -> None:
        nonlocal resolved_total
        resolved_total += usd
        pp = per_phase.setdefault(phase, {"cost": 0.0, "count": 0})
        pp["cost"] += usd
        pp["count"] += 1
        pm = per_model.setdefault(model, {"cost": 0.0, "count": 0})
        pm["cost"] += usd
        pm["count"] += 1

    for row in rows:
        try:
            verdict, inline_usd = _ce.classify_attribution(row.attribution)
            if verdict == "resolved":
                add_priced(row.phase, row.model_or_effort, float(inline_usd or 0.0))
                inline_count += 1
                dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                     "model": row.model_or_effort,
                                     "disposition": "resolved-inline", "usd": inline_usd})
                continue

            is_agent = bool(_AGENT_ID_RE.match(row.uuid))
            if is_agent:
                transcript = _locate_agent_transcript(
                    row.uuid, session_ids, project_path, home, proj_hash)
                if transcript is not None and _atc.last_row_usage_present(transcript):
                    priced = _atc.price_agent_jsonl(transcript)
                    if priced["priceable"] and priced["usd"] is not None:
                        add_priced(row.phase, row.model_or_effort, float(priced["usd"]))
                        repriced_count += 1
                        dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                             "model": row.model_or_effort,
                                             "disposition": "repriced",
                                             "usd": priced["usd"]})
                        continue

            if verdict == "legacy" and not is_agent and _SESSION_ID_RE.match(row.uuid):
                legacy_rows.append(row)
                continue

            tok = _tok_of(row.attribution)
            rate = _cfj.estimate_rate_for_alias(row.model_or_effort) if tok else None
            if tok and rate is not None:
                est = tok * rate
                estimated_total += est
                estimated_count += 1
                estimated_per_phase[row.phase] = estimated_per_phase.get(row.phase, 0.0) + est
                dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                     "model": row.model_or_effort,
                                     "disposition": "estimated", "usd": est})
                continue

            unresolvable_count += 1
            dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                 "model": row.model_or_effort,
                                 "disposition": "unresolvable", "usd": None})
        except Exception as exc:  # noqa: BLE001
            unresolvable_count += 1
            notes.append("row %s skipped: %s" % (getattr(row, "uuid", "?"), exc))
            dispositions.append({"uuid": getattr(row, "uuid", "?"),
                                 "phase": getattr(row, "phase", ""),
                                 "model": getattr(row, "model_or_effort", ""),
                                 "disposition": "unresolvable", "usd": None})

    shared_sessions_total = 0.0
    cohort_priced_rows = 0
    if legacy_rows:
        cohort = _ce.cohort_attribution(
            legacy_rows, lambda sid: _session_cost(home, proj_hash, sid))
        if cohort is None:
            unresolvable_count += len(legacy_rows)
            notes.append("session-cohort attribution failed; legacy rows left unresolved")
            for row in legacy_rows:
                dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                     "model": row.model_or_effort,
                                     "disposition": "unresolvable", "usd": None})
        else:
            resolved_total += cohort.resolved_total
            for phase, entry in cohort.by_phase.items():
                pp = per_phase.setdefault(phase, {"cost": 0.0, "count": 0})
                pp["cost"] += entry["cost"]
                pp["count"] += entry["count"]
                cohort_priced_rows += entry["count"]
            for model, entry in cohort.by_model.items():
                pm = per_model.setdefault(model, {"cost": 0.0, "count": 0})
                pm["cost"] += entry["cost"]
                pm["count"] += entry["count"]
            if cohort.shared_bucket:
                shared_sessions_total += cohort.shared_bucket.get("cost", 0.0)
                for entry in cohort.shared_bucket.get("phases", {}).values():
                    cohort_priced_rows += entry.get("count", 0)
            unresolvable_count += cohort.unresolvable_count
            for row in legacy_rows:
                dispositions.append({"uuid": row.uuid, "phase": row.phase,
                                     "model": row.model_or_effort,
                                     "disposition": "legacy-session", "usd": None})

    phases = sorted({r.phase for r in rows})
    phases_excluded = [] if "end-of-task" in phases else ["end-of-task"]
    priced_rows = inline_count + repriced_count + cohort_priced_rows

    summary: Dict[str, Any] = {
        "per_phase": {k: {"cost": round(v["cost"], 6), "count": int(v["count"])}
                      for k, v in sorted(per_phase.items())},
        "per_model": {k: {"cost": round(v["cost"], 6), "count": int(v["count"])}
                      for k, v in sorted(per_model.items())},
        "estimated_per_phase": {k: round(v, 6) for k, v in sorted(estimated_per_phase.items())},
        "task_total": round(resolved_total, 6),
        # Rows whose category is not "task" are dropped by the row parser, so
        # there is never an off-topic amount; the key keeps the summary shape.
        "off_topic_total": 0.0,
        "resolved_total": round(resolved_total, 6),
        "estimated_total": round(estimated_total, 6),
        "estimated_count": estimated_count,
        "repriced_count": repriced_count,
        "priced_rows": priced_rows,
        "rows_total": len(rows),
        "unresolvable_count": unresolvable_count,
        "shared_sessions_total": round(shared_sessions_total, 6),
        "price_table_updated": _cfj.LAST_UPDATED,
        "grand_total": round(resolved_total + estimated_total, 6),
        "phases_covered": phases,
        "phases_excluded": phases_excluded,
        "phases_note": (
            "No end-of-task row is in the ledger: the finalize phase itself is not "
            "included in this total." if phases_excluded else
            "The ledger includes an end-of-task row."
        ),
    }
    if estimated_count > 0 or unresolvable_count > 0:
        summary["fallback_used"] = True
        summary["fallback_note"] = (
            "%d row(s) estimated from token counts, %d row(s) unresolvable"
            % (estimated_count, unresolvable_count)
        )
    if notes:
        summary["notes"] = notes
    return summary, dispositions


def _format_text(summary: Dict[str, Any], dispositions: List[Dict[str, Any]],
                 projects_dir: pathlib.Path) -> str:
    lines = ["projects_dir: %s" % projects_dir,
             "%-20s %-16s %-8s %-16s %s" % ("uuid", "phase", "model", "disposition", "usd")]
    for d in dispositions:
        usd = "" if d["usd"] is None else "%.4f" % d["usd"]
        lines.append("%-20s %-16s %-8s %-16s %s" % (
            d["uuid"][:20], d["phase"], d["model"], d["disposition"], usd))
    for key in ("resolved_total", "estimated_total", "grand_total", "rows_total",
                "priced_rows", "repriced_count", "estimated_count", "unresolvable_count",
                "shared_sessions_total", "phases_excluded"):
        lines.append("%s: %s" % (key, summary[key]))
    if summary.get("fallback_note"):
        lines.append("fallback_note: %s" % summary["fallback_note"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Total a task cost ledger into cost-summary.json, re-pricing "
                    "ledger rows whose transcripts are now readable.")
    parser.add_argument("--ledger", required=True, help="Path to cost-ledger.md")
    parser.add_argument(
        "--project-path", default=None,
        help="Project root the sessions ran in (default: current directory). The "
             "transcript folder name is derived from this path, so a finalize step "
             "must pass the project root explicitly.")
    parser.add_argument("--home", default=None, help="Home directory holding .claude/projects")
    parser.add_argument("--out", default=None,
                        help="Where to write the summary (default: cost-summary.json beside the ledger)")
    parser.add_argument("--format", choices=("text", "json"), default="json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the summary and write nothing")
    args = parser.parse_args(argv)

    ledger = pathlib.Path(args.ledger)
    if args.out is None and "finalized" in ledger.parts:
        print("cost_ledger_summary: refusing to write beside a finalized ledger; "
              "pass --out to choose where the summary goes", file=sys.stderr)
        return 2
    out = pathlib.Path(args.out) if args.out else ledger.parent / "cost-summary.json"
    project_path = args.project_path if args.project_path is not None else os.getcwd()
    home = pathlib.Path(args.home) if args.home else pathlib.Path.home()

    summary, dispositions = build_summary(ledger, project_path, home)
    projects_dir = home / ".claude" / "projects" / _cfj.project_hash(project_path)

    if args.format == "text":
        print(_format_text(summary, dispositions, projects_dir))
    else:
        print(json.dumps(summary, indent=2))

    if not args.dry_run:
        tmp = out.with_name(out.name + ".tmp")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
