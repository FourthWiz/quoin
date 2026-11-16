"""Fixed-overhead audit of quoin-claude benchmark sessions against plain ones.

For each task that both arms ran, this module reads the row's
`transcript.jsonl` and reports what the quoin session paid on top of the plain
one: the context of its first model call (system prompt, tool and skill
listings, CLAUDE.md and the expanded /run skill all land there), the init
listing sizes, the number of assistant turns, token totals and billed cost.
Every later main-thread turn re-reads that first-call prefix from cache, so the
fixed overhead is charged once per turn; `prefix_reread_tokens` estimates that
charge as the first-call delta times the quoin session's main-thread turns
after the first (which writes the prefix rather than reading it).

Static sizes of the always-loaded sources come from a quoin source tree
(`--quoin-root`, the `quoin/` package dir): the slim CLAUDE.md the installer
deploys, the run skill files, and every skill's frontmatter description (the
harness lists them in each session). Token counts for those files are an
estimate at four characters per token.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .cost import load_pricing
from .phase_report import (
    _billed_usd, _messages, build_phase_report, read_transcript, summarize,
)
from .stream_metrics import _is_number, sum_model_usage

QUOIN_CELL = "quoin-claude"
PLAIN_CELL = "simple-claude"
CHARS_PER_TOKEN = 4
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
# Sources already in the first model call; only these can explain its delta.
AT_SESSION_START = ("every session", "every /run session")

_DESC_RE = re.compile(r"^description:\s*(.*)$", re.M)


def _first_context(tokens: dict) -> int:
    return tokens["input"] + tokens["cache_read"] + tokens["cache_write"]


def _hook_chars(events: list) -> int:
    total = 0
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "hook_response":
            # `output` already combines stdout and stderr when present.
            output = event.get("output")
            if isinstance(output, str) and output:
                total += len(output)
                continue
            for key in ("stdout", "stderr"):
                value = event.get(key)
                if isinstance(value, str):
                    total += len(value)
    return total


def _session_tokens(metrics: dict, messages: list) -> tuple:
    """Session token totals and where they came from.

    The stream's per-message `output_tokens` is a partial streaming count, so
    billed `modelUsage` totals win; per-message sums are only a fallback.
    """
    totals = metrics.get("model_usage_totals")
    if not (isinstance(totals, dict) and all(_is_number(totals.get(k)) for k in TOKEN_KEYS)):
        totals = sum_model_usage(metrics.get("model_usage"))
    if isinstance(totals, dict) and all(_is_number(totals.get(k)) for k in TOKEN_KEYS):
        return {k: totals[k] for k in TOKEN_KEYS}, "model_usage"
    summed = dict.fromkeys(TOKEN_KEYS, 0)
    for msg in messages:
        for key in TOKEN_KEYS:
            summed[key] += msg["tokens"][key]
    return summed, "transcript"


def _verdict(task_dir: Path) -> Optional[str]:
    try:
        judge = json.loads((task_dir / "judge.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = judge.get("verdict") if isinstance(judge, dict) else None
    return value if isinstance(value, str) else None


def session_profile(task_dir) -> Optional[dict]:
    """One row's overhead profile, or None when it has no transcript."""
    task_dir = Path(task_dir)
    events = read_transcript(task_dir / "transcript.jsonl")
    if not events:
        return None
    init = next((e for e in events
                 if e.get("type") == "system" and e.get("subtype") == "init"), {})
    messages = _messages(events)
    main = [m for m in messages if not m["parent"]]
    try:
        metrics = json.loads((task_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metrics = {}
    metrics = metrics if isinstance(metrics, dict) else {}
    totals, tokens_source = _session_tokens(metrics, messages)

    def count(key):
        value = init.get(key)
        return len(value) if isinstance(value, list) else None

    return {
        "task": task_dir.name,
        "verdict": _verdict(task_dir),
        "first_context_tokens": _first_context(main[0]["tokens"]) if main else 0,
        "skills_listed": count("skills"),
        "slash_commands_listed": count("slash_commands"),
        "main_turns": len(main),
        "subagent_turns": len(messages) - len(main),
        "tokens": totals,
        "tokens_source": tokens_source,
        "hooks_seen": sorted({e.get("hook_name") for e in events
                              if e.get("subtype") == "hook_response" and e.get("hook_name")}),
        "hook_output_chars": _hook_chars(events),
        "billed_cost_usd": _billed_usd(metrics, task_dir),
    }


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(values) / len(values) if values else None


def _ratio(a, b) -> Optional[float]:
    return a / b if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b else None


def pair_run(run_dir) -> list:
    """Profiles of every task present in both arms, as {task, quoin, plain}."""
    run_dir = Path(run_dir)
    pairs = []
    quoin_dir, plain_dir = run_dir / QUOIN_CELL, run_dir / PLAIN_CELL
    if not quoin_dir.is_dir() or not plain_dir.is_dir():
        return pairs
    for task_dir in sorted(p for p in quoin_dir.iterdir() if p.is_dir()):
        quoin = session_profile(task_dir)
        plain = session_profile(plain_dir / task_dir.name)
        if quoin and plain:
            delta = quoin["first_context_tokens"] - plain["first_context_tokens"]
            pairs.append({
                "task": task_dir.name, "quoin": quoin, "plain": plain,
                "first_context_delta": delta,
                # The first turn writes the prefix; only later turns re-read it.
                "prefix_reread_tokens": max(delta, 0) * max(quoin["main_turns"] - 1, 0),
            })
    return pairs


def summarize_pairs(pairs: list) -> Optional[dict]:
    """Arm means, quoin/plain ratios and pass counts over the paired tasks."""
    if not pairs:
        return None

    def arm(name):
        rows = [p[name] for p in pairs]
        return {
            "first_context_tokens": _mean([r["first_context_tokens"] for r in rows]),
            "skills_listed": _mean([r["skills_listed"] for r in rows]),
            "slash_commands_listed": _mean([r["slash_commands_listed"] for r in rows]),
            "main_turns": _mean([r["main_turns"] for r in rows]),
            "subagent_turns": _mean([r["subagent_turns"] for r in rows]),
            "tokens": {k: _mean([r["tokens"][k] for r in rows]) for k in TOKEN_KEYS},
            "hook_output_chars": _mean([r["hook_output_chars"] for r in rows]),
            "billed_cost_usd": _mean([r["billed_cost_usd"] for r in rows]),
            "passes": sum(1 for r in rows if r["verdict"] == "pass"),
            "hooks_seen": sorted({h for r in rows for h in r["hooks_seen"]}),
            "tokens_sources": sorted({r["tokens_source"] for r in rows}),
        }

    quoin, plain = arm("quoin"), arm("plain")
    reread = _mean([p["prefix_reread_tokens"] for p in pairs])
    return {
        "n": len(pairs),
        "quoin": quoin,
        "plain": plain,
        "ratio": {
            "billed_cost_usd": _ratio(quoin["billed_cost_usd"], plain["billed_cost_usd"]),
            "cache_read": _ratio(quoin["tokens"]["cache_read"], plain["tokens"]["cache_read"]),
            "main_turns": _ratio(quoin["main_turns"], plain["main_turns"]),
            "first_context_tokens": _ratio(quoin["first_context_tokens"],
                                           plain["first_context_tokens"]),
        },
        "first_context_delta": _mean([p["first_context_delta"] for p in pairs]),
        "prefix_reread_tokens": reread,
        "prefix_reread_share_of_quoin_cache_read": _ratio(reread, quoin["tokens"]["cache_read"]),
    }


def _description(skill_md: Path) -> str:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    match = _DESC_RE.search(text[: end if end != -1 else len(text)])
    return match.group(1).strip().strip("\"'") if match else ""


def static_sources(quoin_root) -> list:
    """Byte and estimated token size of each always-loaded or /run-loaded source."""
    root = Path(quoin_root)
    run_dir = root / "adapters" / "claude" / "skills" / "run"
    sources = [
        ("CLAUDE.md as deployed (slim)", "every session", root / "CLAUDE.slim.md"),
        ("run SKILL.md", "every /run session", run_dir / "SKILL.md"),
        # Companion files are listed when present, so older checkouts still audit.
        ("run reference.md", "on demand", run_dir / "reference.md"),
        ("run light-pipeline.md", "light route only", run_dir / "light-pipeline.md"),
        ("run prompt-triage.md", "triage subagent", run_dir / "prompt-triage.md"),
    ]
    rows = []
    for name, when, path in sources:
        if path.is_file():
            size = len(path.read_text(encoding="utf-8"))
            rows.append({"source": name, "loaded": when, "path": str(path.relative_to(root)),
                         "chars": size, "tokens_est": size // CHARS_PER_TOKEN})
    skills = sorted((root / "adapters" / "claude" / "skills").glob("*/SKILL.md"))
    listing = sum(len(p.parent.name) + len(_description(p)) for p in skills)
    rows.append({"source": f"skill listing ({len(skills)} quoin skills: name + description)",
                 "loaded": "every session", "path": "adapters/claude/skills/*/SKILL.md",
                 "chars": listing, "tokens_est": listing // CHARS_PER_TOKEN})
    return rows


def audit(run_dir, quoin_root=None, pricing=None) -> dict:
    """The full audit: paired summary, per-task rows, quoin phase shares, sources."""
    run_dir = Path(run_dir)
    pairs = pair_run(run_dir)
    pricing = load_pricing() if pricing is None else pricing
    phase_reports = [build_phase_report(run_dir / QUOIN_CELL / p["task"], pricing=pricing)
                     for p in pairs]
    summary = summarize_pairs(pairs)
    sources = static_sources(quoin_root) if quoin_root else []
    delta = summary["first_context_delta"] if summary else None
    for row in sources:
        at_start = row["loaded"] in AT_SESSION_START
        row["share_of_first_context_delta"] = _ratio(row["tokens_est"], delta) if at_start else None
    return {
        "run": run_dir.name,
        "summary": summary,
        "pairs": pairs,
        "phases": summarize(phase_reports),
        "sources": sources,
        "unattributed_share_of_first_context_delta": (
            1.0 - sum(s["share_of_first_context_delta"] or 0.0 for s in sources)
            if sources and delta else None),
    }


def _fmt(value, spec="%.0f") -> str:
    return "-" if value is None else spec % value


def _pct(value) -> str:
    return "-" if value is None else "%.0f%%" % (100.0 * value)


def _table(rows: list) -> list:
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def render_markdown(report: dict) -> str:
    lines = [f"## Overhead audit: {report['run']}", ""]
    summary = report["summary"]
    if summary is None:
        lines.append("No task ran in both arms.")
        return "\n".join(lines) + "\n"
    q, p, r = summary["quoin"], summary["plain"], summary["ratio"]
    lines += [f"Paired tasks: {summary['n']}. Passes: quoin {q['passes']}, plain {p['passes']}.", ""]
    lines += _table([
        ["metric (mean per session)", "quoin", "plain", "quoin / plain"],
        ["billed cost (USD)", _fmt(q["billed_cost_usd"], "%.3f"), _fmt(p["billed_cost_usd"], "%.3f"),
         _fmt(r["billed_cost_usd"], "%.2fx")],
        ["first-call context (tokens)", _fmt(q["first_context_tokens"]), _fmt(p["first_context_tokens"]),
         _fmt(r["first_context_tokens"], "%.2fx")],
        ["main-thread turns", _fmt(q["main_turns"], "%.1f"), _fmt(p["main_turns"], "%.1f"),
         _fmt(r["main_turns"], "%.2fx")],
        ["subagent turns", _fmt(q["subagent_turns"], "%.1f"), _fmt(p["subagent_turns"], "%.1f"), "-"],
        ["cache-read tokens", _fmt(q["tokens"]["cache_read"]), _fmt(p["tokens"]["cache_read"]),
         _fmt(r["cache_read"], "%.2fx")],
        ["cache-write tokens", _fmt(q["tokens"]["cache_write"]), _fmt(p["tokens"]["cache_write"]), "-"],
        ["output tokens", _fmt(q["tokens"]["output"]), _fmt(p["tokens"]["output"]), "-"],
        ["token totals from", "/".join(q["tokens_sources"]), "/".join(p["tokens_sources"]), "-"],
        ["skills listed", _fmt(q["skills_listed"]), _fmt(p["skills_listed"]), "-"],
        ["slash commands listed", _fmt(q["slash_commands_listed"]), _fmt(p["slash_commands_listed"]), "-"],
        ["hook output (chars)", _fmt(q["hook_output_chars"]), _fmt(p["hook_output_chars"]), "-"],
    ])
    lines += ["", f"First-call context delta: {_fmt(summary['first_context_delta'])} tokens. "
              f"Re-read on every quoin main-thread turn: about {_fmt(summary['prefix_reread_tokens'])} "
              f"cache-read tokens per session "
              f"({_pct(summary['prefix_reread_share_of_quoin_cache_read'])} of quoin's cache reads).", ""]
    if report["sources"]:
        lines += ["### Always-loaded and /run-loaded sources", ""]
        lines += _table([["source", "loaded", "chars", "tokens (est.)", "share of first-call delta"]] + [
            [s["source"], s["loaded"], str(s["chars"]), str(s["tokens_est"]),
             _pct(s.get("share_of_first_context_delta"))] for s in report["sources"]] + [
            ["hook output (%s)" % (", ".join(q["hooks_seen"]) or "none seen"), "every session",
             _fmt(q["hook_output_chars"]), _fmt(q["hook_output_chars"] / CHARS_PER_TOKEN
                                                if q["hook_output_chars"] is not None else None), "-"],
            ["not a quoin file (prompt prefix, listing format, wrappers)", "every session", "-", "-",
             _pct(report.get("unattributed_share_of_first_context_delta"))]])
        lines.append("")
    phases = report["phases"]
    if phases:
        lines += ["### quoin phase shares (estimated cost)", ""]
        lines += _table([["phase", "mean USD", "share", "sessions ran"]] + [
            [row["phase"], "%.3f" % row["usd_mean"], "%.0f%%" % row["usd_pct"],
             "%d/%d" % (row["sessions_ran"], phases["n"])] for row in phases["rows"]])
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    """CLI entry; run it through quoin/benchmarks/scripts/overhead_audit.py."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--quoin-root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="quoin source package dir for static sizes (default: this checkout)")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    report = audit(args.run_dir, args.quoin_root)
    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
