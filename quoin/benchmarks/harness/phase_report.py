"""Per-phase time, token and cost breakdown of a quoin-claude benchmark session.

A light /run session appends one line per step to
`.workflow_artifacts/<task>/phases.log` (`<UTC ts> <phase> [skipped]`, with a
`N:` stage prefix for staged tasks). The quoin cell copies that tree into the
row's `workflow_artifacts_evidence/`. This module joins those markers with the
row's `transcript.jsonl`:

- Time: the session timeline is every distinct event timestamp in the
  transcript. Each gap between two consecutive timestamps belongs to the phase
  active at the later one. When the gaps add up to more than the measured wall
  clock (a suspended machine, out-of-order stamps), the largest gaps are cut
  first until the sum equals the wall clock.
- Tokens: assistant messages are deduplicated by message id (the stream repeats
  a message once per content block). A main-thread message belongs to the phase
  active at its timestamp; a subagent message belongs to the phase that spawned
  its Agent call, even when the subagent runs on in the background.
- Cost: each message is priced from `pricing.json`. The reconstructed total
  undercounts the billed `modelUsage` total, so phase costs are also scaled to
  the billed total (per model when `modelUsage` bills every model seen, else by
  one factor) and labelled as an estimate.

Rows with no phases.log (every run before the markers existed) fall back to
inference from the Agent calls' descriptions, and the report says "inferred".
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Optional

from .cost import estimate_cost, load_pricing

PHASES = (
    "setup", "discover", "frame", "context", "decompose",
    "implement", "verify", "review", "review-fixes", "commit",
)
OTHER = "other"
TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")
EVIDENCE_DIR = "workflow_artifacts_evidence"
ESTIMATE_LABEL = "estimate: per-message usage priced and scaled to the billed total"

_LINE_RE = re.compile(r"^(\S+)\s+(?:(\d+):)?([a-z][a-z-]*)(\s+skipped)?\s*$")

# Agent descriptions seen in light /run transcripts, checked in this order.
_INFER_RULES = (
    ("review", re.compile(r"review", re.I)),
    ("discover", re.compile(r"discover|project memory|scan (the )?repo", re.I)),
    ("context", re.compile(r"context|explore|pack|locate|find ", re.I)),
    ("implement", re.compile(r"implement|fix|apply|opus implementer", re.I)),
)
# The orchestrator's own work after a subagent of each kind returns.
_FOLLOWS = {"discover": "frame", "context": "implement", "implement": "verify",
            "review": "review-fixes", OTHER: OTHER}


def _parse_ts(text) -> Optional[float]:
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()


def parse_phases_log(text: str) -> list:
    """Markers in file order: dicts with ts (epoch s), phase, stage, skipped.

    Lines that do not parse, or name an unknown phase, are dropped.
    """
    markers = []
    for raw in (text or "").splitlines():
        match = _LINE_RE.match(raw.strip())
        if not match:
            continue
        ts = _parse_ts(match.group(1))
        phase = match.group(3)
        if ts is None or phase not in PHASES:
            continue
        markers.append({
            "ts": ts, "phase": phase,
            "stage": int(match.group(2)) if match.group(2) else None,
            "skipped": bool(match.group(4)),
        })
    return markers


def find_phases_logs(evidence_dir: Path) -> list:
    """Every `<task>/phases.log` (or `<task>/stage-N/...`) under the evidence copy."""
    evidence_dir = Path(evidence_dir)
    if not evidence_dir.is_dir():
        return []
    return sorted(p for p in evidence_dir.rglob("phases.log")
                  if p.is_file() and not ({"memory", "cache", "finalized"} & set(p.relative_to(evidence_dir).parts)))


def load_markers(task_dir: Path) -> list:
    markers = []
    for path in find_phases_logs(Path(task_dir) / EVIDENCE_DIR):
        try:
            markers.extend(parse_phases_log(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    markers.sort(key=lambda m: m["ts"])
    return markers


def read_transcript(path: Path) -> list:
    events = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def _usage_tokens(usage) -> dict:
    usage = usage if isinstance(usage, dict) else {}

    def num(key):
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return {
        "input": num("input_tokens"),
        "output": num("output_tokens"),
        "cache_read": num("cache_read_input_tokens"),
        "cache_write": num("cache_creation_input_tokens"),
    }


def _messages(events: list) -> list:
    """Assistant messages, one per message id (last usage wins), in first-seen order."""
    by_id = {}
    order = []
    for index, event in enumerate(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        key = message.get("id") or f"_anon{index}"
        entry = by_id.get(key)
        if entry is None:
            entry = by_id[key] = {
                "id": key, "ts": _parse_ts(event.get("timestamp")),
                "parent": event.get("parent_tool_use_id"), "model": message.get("model"),
                "agents": [],
            }
            order.append(key)
        entry["tokens"] = _usage_tokens(message.get("usage"))
        if entry["ts"] is None:
            entry["ts"] = _parse_ts(event.get("timestamp"))
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                params = block.get("input") or {}
                entry["agents"].append({"id": block.get("id"), "description": str(params.get("description") or "")})
    return [by_id[k] for k in order]


def infer_agent_phase(description: str) -> str:
    for phase, pattern in _INFER_RULES:
        if pattern.search(description or ""):
            return phase
    return OTHER


class _Timeline:
    """Which phase is active at a given time, from markers or from Agent calls."""

    def __init__(self, markers, messages, events):
        self.markers = [m for m in markers if not m["skipped"]]
        self.source = "markers" if markers else "inferred"
        self.spawns = []  # (start, end, phase) for each main-thread Agent call
        if self.source == "inferred":
            last_seen = {}
            for event in events:
                parent = event.get("parent_tool_use_id")
                ts = _parse_ts(event.get("timestamp"))
                if parent and ts is not None:
                    last_seen[parent] = max(ts, last_seen.get(parent, ts))
                if event.get("type") == "user" and not parent:
                    for block in (event.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_result" and ts is not None:
                            tid = block.get("tool_use_id")
                            last_seen[tid] = max(ts, last_seen.get(tid, ts))
            for msg in messages:
                if msg["parent"] or msg["ts"] is None:
                    continue
                for agent in msg["agents"]:
                    end = last_seen.get(agent["id"], msg["ts"])
                    self.spawns.append((msg["ts"], max(end, msg["ts"]), infer_agent_phase(agent["description"])))
            self.spawns.sort()

    def phase_at(self, ts: Optional[float]) -> str:
        if self.source == "markers":
            if not self.markers:
                return OTHER
            if ts is None:
                return self.markers[-1]["phase"]
            current = self.markers[0]["phase"]
            for marker in self.markers:
                if marker["ts"] <= ts:
                    current = marker["phase"]
                else:
                    break
            return current
        if ts is None:
            return OTHER
        current = "setup"
        for start, end, phase in self.spawns:
            if start > ts:
                break
            current = phase if ts <= end else _FOLLOWS.get(phase, OTHER)
        return current


def _clamp(gaps: list, wall: Optional[float]) -> tuple:
    """Cut the largest gaps first until their sum is at most `wall`.

    `gaps` is a list of [seconds, phase]; returns (gaps, seconds_removed).
    """
    total = sum(g[0] for g in gaps)
    if wall is None or wall < 0 or total <= wall:
        return gaps, 0.0
    excess = total - wall
    removed = 0.0
    for gap in sorted(gaps, key=lambda g: g[0], reverse=True):
        if excess <= 0:
            break
        cut = min(gap[0], excess)
        gap[0] -= cut
        excess -= cut
        removed += cut
    return gaps, removed


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _billed_usd(metrics: dict, task_dir: Path) -> Optional[float]:
    totals = metrics.get("model_usage_totals")
    if isinstance(totals, dict) and _is_num(totals.get("cost_usd")):
        return float(totals["cost_usd"])
    if _is_num(metrics.get("cost_runtime_usd")):
        return float(metrics["cost_runtime_usd"])
    try:
        cost = json.loads((task_dir / "cost.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = cost.get("cost_runtime_usd") if isinstance(cost, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_phase_report(task_dir, metrics: Optional[dict] = None, pricing: Optional[dict] = None) -> Optional[dict]:
    """The per-phase breakdown of one row, or None when it has no transcript."""
    task_dir = Path(task_dir)
    events = read_transcript(task_dir / "transcript.jsonl")
    if not events:
        return None
    if metrics is None:
        try:
            metrics = json.loads((task_dir / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metrics = {}
    metrics = metrics if isinstance(metrics, dict) else {}
    pricing = load_pricing() if pricing is None else pricing
    markers = load_markers(task_dir)
    messages = _messages(events)
    timeline = _Timeline(markers, messages, events)

    phases = {}

    def slot(name):
        if name not in phases:
            phases[name] = {"seconds": 0.0, "tokens": dict.fromkeys(TOKEN_TYPES, 0),
                            "cost_usd_reconstructed": 0.0, "ran": False, "skipped": False,
                            "_by_model": {}}
        return phases[name]

    # Phase that owns each Agent call, so its subagent's messages follow it.
    spawn_phase = {}
    by_tool = {}
    for msg in messages:
        for agent in msg["agents"]:
            by_tool[agent["id"]] = (msg, agent)

    def owner(tool_id, depth=0):
        if tool_id in spawn_phase:
            return spawn_phase[tool_id]
        found = by_tool.get(tool_id)
        if found is None or depth > 20:
            return OTHER
        msg, agent = found
        if msg["parent"]:
            phase = owner(msg["parent"], depth + 1)
        elif timeline.source == "inferred":
            phase = infer_agent_phase(agent["description"])
        else:
            phase = timeline.phase_at(msg["ts"])
        spawn_phase[tool_id] = phase
        return phase

    reconstructed = 0.0
    recon_by_model = {}
    unpriced = set()
    for msg in messages:
        phase = owner(msg["parent"]) if msg["parent"] else timeline.phase_at(msg["ts"])
        entry = slot(phase)
        for key in TOKEN_TYPES:
            entry["tokens"][key] += msg["tokens"][key]
        tokens = msg["tokens"]
        amount = None
        if msg["model"] and any(tokens.values()):
            amount = estimate_cost(msg["model"], tokens["input"], tokens["output"],
                                   tokens["cache_write"], tokens["cache_read"], pricing, strict=False)
            if amount is None:
                unpriced.add(msg["model"])
        if amount is not None:
            entry["cost_usd_reconstructed"] += float(amount)
            entry["_by_model"][msg["model"]] = entry["_by_model"].get(msg["model"], 0.0) + float(amount)
            recon_by_model[msg["model"]] = recon_by_model.get(msg["model"], 0.0) + float(amount)
            reconstructed += float(amount)

    stamps = sorted({ts for ts in (_parse_ts(e.get("timestamp")) for e in events) if ts is not None})
    gaps = [[stamps[i] - stamps[i - 1], timeline.phase_at(stamps[i])] for i in range(1, len(stamps))]
    wall = metrics.get("wall_clock_seconds")
    wall = float(wall) if isinstance(wall, (int, float)) and not isinstance(wall, bool) else None
    raw_seconds = sum(g[0] for g in gaps)
    gaps, removed = _clamp(gaps, wall)
    for seconds, phase in gaps:
        slot(phase)["seconds"] += seconds

    for name, entry in phases.items():
        entry["ran"] = entry["seconds"] > 0 or any(entry["tokens"].values())
    if timeline.source == "markers":
        ran = {m["phase"] for m in markers if not m["skipped"]}
        for marker in markers:
            entry = slot(marker["phase"])
            if marker["phase"] in ran:
                entry["ran"] = True
            elif marker["skipped"]:
                entry["skipped"] = True

    billed = _billed_usd(metrics, task_dir)
    scale = billed / reconstructed if billed is not None and reconstructed > 0 else None
    usage = metrics.get("model_usage") if isinstance(metrics.get("model_usage"), dict) else {}
    by_model = {}
    for model, recon in recon_by_model.items():
        bucket = usage.get(model)
        if recon > 0 and isinstance(bucket, dict) and _is_num(bucket.get("costUSD")):
            by_model[model] = float(bucket["costUSD"]) / recon
    if scale is None or set(by_model) != set(recon_by_model):
        by_model = {}
    for entry in phases.values():
        split = entry.pop("_by_model")
        if by_model:
            estimate = sum(amount * by_model[model] for model, amount in split.items())
        else:
            estimate = entry["cost_usd_reconstructed"] * (scale if scale is not None else 1.0)
        entry["cost_usd_estimate"] = round(estimate, 6)
        entry["cost_usd_reconstructed"] = round(entry["cost_usd_reconstructed"], 6)
        entry["seconds"] = round(entry["seconds"], 3)

    order = {name: i for i, name in enumerate(PHASES + (OTHER,))}
    return {
        "source": timeline.source,
        "label": ESTIMATE_LABEL,
        "phases": {k: phases[k] for k in sorted(phases, key=lambda n: (order.get(n, 99), n))},
        "reconstructed_cost_usd": round(reconstructed, 6),
        "billed_cost_usd": billed,
        "cost_scale": round(scale, 6) if scale is not None else None,
        "cost_scale_by_model": {m: round(f, 6) for m, f in sorted(by_model.items())} or None,
        "unpriced_models": sorted(unpriced),
        "wall_clock_seconds": wall,
        "timeline_seconds": round(raw_seconds, 3),
        "clamped_seconds": round(removed, 3),
        "marker_count": len(markers),
    }


def summarize(reports: list) -> Optional[dict]:
    """Mean per-phase cost and time over rows with a report (one group)."""
    reports = [r for r in reports if isinstance(r, dict) and isinstance(r.get("phases"), dict)]
    if not reports:
        return None
    n = len(reports)
    names = []
    for report in reports:
        for name in report["phases"]:
            if name not in names:
                names.append(name)
    order = {name: i for i, name in enumerate(PHASES + (OTHER,))}
    names.sort(key=lambda x: (order.get(x, 99), x))
    total_usd = sum(p.get("cost_usd_estimate", 0.0) for r in reports for p in r["phases"].values())
    total_s = sum(p.get("seconds", 0.0) for r in reports for p in r["phases"].values())
    rows = []
    for name in names:
        usd = sum(r["phases"].get(name, {}).get("cost_usd_estimate", 0.0) for r in reports)
        secs = sum(r["phases"].get(name, {}).get("seconds", 0.0) for r in reports)
        ran = sum(1 for r in reports if r["phases"].get(name, {}).get("ran"))
        rows.append({
            "phase": name,
            "usd_mean": usd / n,
            "usd_pct": 100.0 * usd / total_usd if total_usd else 0.0,
            "seconds_mean": secs / n,
            "seconds_pct": 100.0 * secs / total_s if total_s else 0.0,
            "sessions_ran": ran,
        })
    billed = [r["billed_cost_usd"] for r in reports if isinstance(r.get("billed_cost_usd"), (int, float))]
    return {
        "n": n,
        "sources": {s: sum(1 for r in reports if r.get("source") == s) for s in ("markers", "inferred")},
        "reconstructed_usd_mean": sum(r.get("reconstructed_cost_usd") or 0.0 for r in reports) / n,
        "billed_usd_mean": sum(billed) / len(billed) if billed else None,
        "rows": rows,
    }


def render_markdown(groups: list, format_table) -> list:
    """Lines for the summary.md section; `groups` is [(label, summary_or_None)]."""
    groups = [(label, s) for label, s in groups if s]
    if not groups:
        return []
    lines = ["### Quoin arm: cost and time by /run phase (estimate)", ""]
    overall = groups[-1][1]
    lines.append(
        "Phase costs price each transcript message and are scaled to the billed total, so they are "
        "an estimate. Durations come from transcript timestamps, cut so a session never exceeds its "
        "wall clock. Rows with phases.log use its markers; older rows are inferred from Agent call "
        "descriptions (%d with markers, %d inferred). Mean reconstructed $%.2f/task vs billed %s/task."
        % (overall["sources"]["markers"], overall["sources"]["inferred"], overall["reconstructed_usd_mean"],
           "$%.2f" % overall["billed_usd_mean"] if overall["billed_usd_mean"] is not None else "n/a")
    )
    lines.append("")
    header = ["Tier", "Phase", "Mean $/task", "% of cost", "Mean s/task", "% of time", "Sessions ran"]
    table = [header, ["---"] * len(header)]
    for label, summary in groups:
        for row in summary["rows"]:
            table.append([
                label, row["phase"], "$%.2f" % row["usd_mean"], "%.0f%%" % row["usd_pct"],
                "%.0f" % row["seconds_mean"], "%.0f%%" % row["seconds_pct"],
                "%d/%d" % (row["sessions_ran"], summary["n"]),
            ])
    lines.append(format_table(table))
    return lines


def main(argv=None) -> int:
    """Print the phase table for a run directory: `python -m ... <run_path>`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_path", type=Path)
    parser.add_argument("--cell", default="quoin-claude")
    args = parser.parse_args(argv)
    pricing = load_pricing()
    reports = [build_phase_report(d, pricing=pricing)
               for d in sorted((args.run_path / args.cell).iterdir()) if d.is_dir()]

    def fmt(rows):
        return "\n".join("| " + " | ".join(r) + " |" for r in rows) + "\n"

    print("\n".join(render_markdown([("overall", summarize(reports))], fmt)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
