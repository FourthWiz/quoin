"""
vague_report.py - Vague-prompt suite: plain and quoin on vague and original prompts.

Usage (from a source checkout of the quoin repo):

    python3 quoin/benchmarks/scripts/vague_report.py VAGUE_RUN [VAGUE_RUN ...]
        [--ceiling-run CEILING_RUN ...] [--vague-suite PATH] [--ceiling-suite PATH]
        [--allow-mixed] [--json OUT]

Each VAGUE_RUN is a paired run of `verified-vague` (prompts are short rewrites
that keep only the symptom); each CEILING_RUN, optional and repeatable, is a
paired run of `verified-vague-ceiling` (the same tasks with the original issue
text). Several runs of one suite, such as `--repeat` siblings, are pooled. Up
to four cells come out of them: plain-vague and quoin-vague from the vague
runs, plain-original and quoin-original from the ceiling runs. Tasks are
joined by their upstream instance id; a paired difference compares the two
arms within one run.

Per cell, overall and per tier: tasks counted, passes, pass rate with a 95%
Wilson interval, mean cost and mean wall time. Then the headline, quoin-vague
minus plain-vague, paired per task with a sign test; with a ceiling run, the
pass rate plain loses to vagueness (plain-original minus plain-vague) and the
share of it the quoin arm recovers, and the difference in differences. Last,
how /run's prompt triage classed each counted quoin-vague session (C1 clear,
C2 unstructured, C3 underspecified), read from the brief the session wrote,
with the share not classed C1 and pass rates per class.

A row counts only when it ran, is not invalid and is not awaiting judging; a
paired difference needs both rows. The runs must use the same models and
quoin route, their suites must be the vague and the original variant of the
same task set, and each run's recorded suite sha must match the suite file
read now and the other runs of that suite, unless `--allow-mixed` is given.
With pooled repeats the sign test counts each (run, task) pair, so its p-value
is optimistic. Read-only over the run
directories.

Exit codes: 0 report written, 2 refused (unreadable run, wrong suite, mismatch).
"""

from __future__ import annotations

# Path bootstrap: mirrors run_benchmark.py so the script works when invoked directly.
import sys as _sys
from pathlib import Path as _Path
_repo_root = _Path(__file__).resolve().parent.parent.parent.parent  # .../quoin/
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
try:
    import quoin as _q
    _inner = str(_repo_root / "quoin")
    if _inner not in _q.__path__:
        _q.__path__.append(_inner)
except Exception:
    pass

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

from quoin.benchmarks.harness.tiered_report import PLAIN_CELL, QUOIN_CELL, wilson_interval
from quoin.benchmarks.scripts.multi_run_report import load_run, sign_test

CELLS = ("plain_vague", "quoin_vague", "plain_original", "quoin_original")
TIERS = ("easy", "medium", "large")
IDENTITY_KEYS = ("simple_claude_model", "quoin_claude_model", "quoin_route")
TRIAGE_CLASSES = ("C1", "C2", "C3")
_TRIAGE_LINE = re.compile(r"^##\s*Triage\s*\n+\s*class:\s*(C[123])\b", re.MULTILINE)


def _counted(cell: Optional[dict]) -> bool:
    return (cell is not None and cell["verdict"] not in ("not_run", "invalid")
            and not cell.get("judge_pending"))


def _variant(run: dict) -> Optional[str]:
    return (run["suite"].get("tiered") or {}).get("prompt_variant")


def check_runs(vague_runs: list, ceiling_runs: list) -> list:
    """Reasons the runs cannot be combined; empty when they can."""
    problems = []
    for runs, want in ((vague_runs, "vague"), (ceiling_runs, "original")):
        for run in runs:
            if _variant(run) != want:
                problems.append(f"{run['path']}: suite prompt_variant is {_variant(run)!r}, not {want!r}")
    runs = vague_runs + ceiling_runs
    task_sets = {frozenset(t["source_id"] for t in run["suite"]["tasks"]) for run in runs}
    if len(task_sets) > 1:
        problems.append("the runs' suites hold different tasks")
    for key in IDENTITY_KEYS:
        values = {str(run["path"]): run["manifest"].get(key) for run in runs}
        if key == "quoin_route":
            values = {path: value or "default" for path, value in values.items()}
        if len(set(values.values())) > 1:
            problems.append(f"manifest {key} differs: {values}")
    for runs in (vague_runs, ceiling_runs):
        shas = {str(run["path"]): run["manifest"].get("suite_sha") for run in runs}
        if len(set(shas.values())) > 1:
            problems.append(f"runs of one suite were made from different suite files: {shas}")
        for run in runs:
            current = run.get("suite_file_sha")
            if current and run["manifest"].get("suite_sha") != current:
                problems.append(f"{run['path']}: its suite file changed since the run (suite_sha"
                                f" {run['manifest'].get('suite_sha')!r}, file now {current!r})")
    return problems


def suite_file_sha(run_path: Path, manifest: dict, suite_arg: Optional[Path]) -> Optional[str]:
    """The runner's suite_sha (sha256, first 16 hex) of the suite file the report reads."""
    candidates = [suite_arg] if suite_arg else [manifest.get("suite_path_resolved"), manifest.get("suite_path")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return hashlib.sha256(Path(candidate).read_bytes()).hexdigest()[:16]
        except OSError:
            continue
    return None


def triage_class(run_path: Path, task_id: str) -> Optional[str]:
    """The class /run's prompt triage wrote into the quoin session's brief."""
    task_dir = Path(run_path) / QUOIN_CELL / task_id
    if not task_dir.is_dir():
        return None
    for brief in sorted(task_dir.rglob("brief.md")):
        try:
            match = _TRIAGE_LINE.search(brief.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if match:
            return match.group(1)
    return None


def cell_rows(vague_runs: list, ceiling_runs: list) -> dict:
    """{cell: {(run index, source_id): row}}, plus each task's tier under '_tier'.

    Rows of different runs (repeats) are kept apart, so a paired difference
    always compares the two arms of the same run.
    """
    out = {cell: {} for cell in CELLS}
    out["_tier"] = {}
    for variant, runs in (("vague", vague_runs), ("original", ceiling_runs)):
        for index, run in enumerate(runs):
            for entry in run["pairs"]:
                sid = entry["source_id"]
                out["_tier"][sid] = entry["tier"]
                out[f"plain_{variant}"][(index, sid)] = entry[PLAIN_CELL]
                out[f"quoin_{variant}"][(index, sid)] = entry[QUOIN_CELL]
    return out


def _stats(cells: list) -> dict:
    counted = [c for c in cells if _counted(c)]
    n = len(counted)
    passes = sum(1 for c in counted if c["verdict"] == "pass")
    lo, hi = wilson_interval(passes, n)
    usd = [float(c["usd"]) for c in counted
           if c.get("usd") is not None and c.get("usd_source") != "unavailable"]
    wall = [float(c["wall_clock_s"]) for c in counted if c.get("wall_clock_s") is not None]
    return {
        "n": n,
        "passes": passes,
        "pass_rate": passes / n if n else None,
        "wilson_95": [lo, hi] if n else None,
        "mean_usd": sum(usd) / len(usd) if usd else None,
        "mean_wall_clock_s": sum(wall) / len(wall) if wall else None,
    }


def cell_stats(rows: dict) -> dict:
    out = {}
    for cell in CELLS:
        if not rows[cell]:
            continue
        by_tier = {t: _stats([r for (_, sid), r in rows[cell].items() if rows["_tier"].get(sid) == t])
                   for t in TIERS}
        out[cell] = {"overall": _stats(list(rows[cell].values())), "tiers": by_tier}
    return out


def paired(rows: dict, a: str, b: str) -> Optional[dict]:
    """Per-task differences a minus b over tasks where both rows count."""
    if not rows[a] or not rows[b]:
        return None
    passes, usd, wall = [], [], []
    for key in sorted(set(rows[a]) & set(rows[b])):
        ca, cb = rows[a][key], rows[b][key]
        if not (_counted(ca) and _counted(cb)):
            continue
        passes.append(float(ca["verdict"] == "pass") - float(cb["verdict"] == "pass"))
        if ca.get("usd") is not None and cb.get("usd") is not None and \
                "unavailable" not in (ca.get("usd_source"), cb.get("usd_source")):
            usd.append(float(ca["usd"]) - float(cb["usd"]))
        if ca.get("wall_clock_s") is not None and cb.get("wall_clock_s") is not None:
            wall.append(float(ca["wall_clock_s"]) - float(cb["wall_clock_s"]))

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "n": len(passes),
        "pass_rate_diff": mean(passes),
        "sign_test": sign_test(passes),
        "mean_usd_diff": mean(usd),
        "mean_wall_clock_s_diff": mean(wall),
    }


def recovery(stats: dict, headline: Optional[dict], original_edge: Optional[dict]) -> Optional[dict]:
    """How much of the pass rate plain loses to vagueness the quoin arm wins back.

    The quoin arm differs from plain in all of /run, not only prompt triage, so
    `difference_in_differences` subtracts the paired edge the arms show on the
    original prompts: what is left is the part of the quoin edge that appears
    only when the prompt is vague. Both edges are paired within each run.
    """
    pv = (stats.get("plain_vague") or {}).get("overall", {}).get("pass_rate")
    po = (stats.get("plain_original") or {}).get("overall", {}).get("pass_rate")
    vague_edge = (headline or {}).get("pass_rate_diff")
    orig_edge = (original_edge or {}).get("pass_rate_diff")
    if None in (pv, po, vague_edge):
        return None
    loss = po - pv
    return {
        "vagueness_loss": loss,
        "recovered_share": vague_edge / loss if loss > 0 else None,
        "difference_in_differences": vague_edge - orig_edge if orig_edge is not None else None,
    }


def triage_breakdown(vague_runs: list, rows: dict) -> dict:
    """Prompt-triage classes over the quoin-vague sessions that count."""
    classes = {}
    for index, run in enumerate(vague_runs):
        for entry in run["pairs"]:
            if _counted(entry[QUOIN_CELL]):
                classes[(index, entry["source_id"])] = triage_class(run["path"], entry["task_id"])
    out = {
        "per_task": [{"run": str(vague_runs[i]["path"]), "source_id": sid, "class": cls}
                     for (i, sid), cls in sorted(classes.items())],
        "sessions": len(classes),
        "not_c1_share": (sum(1 for c in classes.values() if c in ("C2", "C3")) / len(classes)
                         if classes else None),
        "classes": {},
    }
    for cls in (*TRIAGE_CLASSES, None):
        keys = [key for key, c in classes.items() if c == cls]
        out["classes"][cls or "unknown"] = {
            "tasks": len(keys),
            "quoin_vague": _stats([rows["quoin_vague"].get(k) for k in keys]),
            "plain_vague": _stats([rows["plain_vague"].get(k) for k in keys]),
        }
    return out


def build_report(vague_runs: list, ceiling_runs: list) -> dict:
    rows = cell_rows(vague_runs, ceiling_runs)
    stats = cell_stats(rows)
    headline = paired(rows, "quoin_vague", "plain_vague")
    original_edge = paired(rows, "quoin_original", "plain_original")
    return {
        "vague_runs": [str(run["path"]) for run in vague_runs],
        "ceiling_runs": [str(run["path"]) for run in ceiling_runs],
        "judge_pending_excluded": {
            cell: sum(1 for r in rows[cell].values() if r.get("judge_pending")) for cell in CELLS
        },
        "cells": stats,
        "headline": headline,
        "quoin_original_vs_plain_original": original_edge,
        "recovery": recovery(stats, headline, original_edge),
        "triage": triage_breakdown(vague_runs, rows),
    }


def _fmt(value, kind) -> str:
    if value is None:
        return "-"
    if kind == "pct":
        return f"{100 * value:.0f}%"
    if kind == "dpct":
        return f"{100 * value:+.0f} pts"
    if kind == "usd":
        return f"${value:.2f}"
    if kind == "dusd":
        return f"{'+' if value >= 0 else '-'}${abs(value):.2f}"
    if kind == "s":
        return f"{value:.0f}s"
    if kind == "ds":
        return f"{value:+.0f}s"
    return str(value)


def _ci(stats: dict) -> str:
    ci = stats.get("wilson_95")
    return f"{100 * ci[0]:.0f}-{100 * ci[1]:.0f}%" if ci else "-"


def render_markdown(report: dict) -> str:
    ceiling = ", ".join(f"`{p}`" for p in report["ceiling_runs"]) or "not given"
    lines = ["# Vague-prompt report", "",
             "Vague runs: " + ", ".join(f"`{p}`" for p in report["vague_runs"]),
             f"Ceiling runs: {ceiling}", ""]
    pending = {k: v for k, v in report["judge_pending_excluded"].items() if v}
    if pending:
        lines += ["Left out, still awaiting judging (run `--judge-only`): "
                  + ", ".join(f"{cell} {n}" for cell, n in pending.items()), ""]
    lines += ["## Cells", "", "| Cell | Scope | n | Pass rate | 95% CI | Mean cost | Mean time |",
              "|---|---|---|---|---|---|---|"]
    for cell, data in report["cells"].items():
        for scope, s in [("overall", data["overall"])] + list(data["tiers"].items()):
            lines.append(f"| {cell} | {scope} | {s['n']} | {_fmt(s['pass_rate'], 'pct')} | {_ci(s)} | "
                         f"{_fmt(s['mean_usd'], 'usd')} | {_fmt(s['mean_wall_clock_s'], 's')} |")
    lines += ["", "## Headline: quoin-vague minus plain-vague", ""]
    h = report["headline"]
    if h:
        st = h["sign_test"]
        p = "-" if st["p_value"] is None else f"{st['p_value']:.3f}"
        lines += [f"- paired tasks: {h['n']}",
                  f"- pass rate: {_fmt(h['pass_rate_diff'], 'dpct')} (sign test: {st['positive']} tasks"
                  f" only quoin passed, {st['negative']} only plain passed, p = {p})",
                  f"- mean cost: {_fmt(h['mean_usd_diff'], 'dusd')}",
                  f"- mean time: {_fmt(h['mean_wall_clock_s_diff'], 'ds')}"]
    else:
        lines.append("No paired rows.")
    rec = report["recovery"]
    if rec:
        lines += ["", "## Against the original prompts", "",
                  f"- pass rate plain loses to vagueness: {_fmt(rec['vagueness_loss'], 'dpct')}",
                  f"- share of that the quoin arm recovers: {_fmt(rec['recovered_share'], 'pct')}",
                  "- quoin edge on vague prompts minus quoin edge on original prompts: "
                  f"{_fmt(rec['difference_in_differences'], 'dpct')}"]
    qo = report["quoin_original_vs_plain_original"]
    if qo:
        lines.append(f"- quoin-original minus plain-original, pass rate: {_fmt(qo['pass_rate_diff'], 'dpct')}"
                     f" over {qo['n']} tasks")
    lines += ["", "## Prompt triage on the vague prompts", "",
              f"Sessions classed C2 or C3: {_fmt(report['triage']['not_c1_share'], 'pct')}"
              f" of {report['triage']['sessions']} quoin-vague sessions that count.", "",
              "| Class | Sessions | quoin-vague pass | plain-vague pass |", "|---|---|---|---|"]
    for cls, d in report["triage"]["classes"].items():
        lines.append(f"| {cls} | {d['tasks']} | {_fmt(d['quoin_vague']['pass_rate'], 'pct')} | "
                     f"{_fmt(d['plain_vague']['pass_rate'], 'pct')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("vague_runs", type=Path, nargs="+")
    parser.add_argument("--ceiling-run", type=Path, action="append", default=[], dest="ceiling_runs")
    parser.add_argument("--vague-suite", type=Path, default=None)
    parser.add_argument("--ceiling-suite", type=Path, default=None)
    parser.add_argument("--allow-mixed", action="store_true")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    loaded = {"vague": [], "original": []}
    for variant, paths, suite in (("vague", args.vague_runs, args.vague_suite),
                                  ("original", args.ceiling_runs, args.ceiling_suite)):
        for path in paths:
            run = load_run(path, suite)
            if isinstance(run, str):
                print(f"ERROR: {run}", file=sys.stderr)
                return 2
            run["suite_file_sha"] = suite_file_sha(path, run["manifest"], suite)
            loaded[variant].append(run)
    problems = check_runs(loaded["vague"], loaded["original"])
    if problems and not args.allow_mixed:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print("Pass --allow-mixed to combine them anyway.", file=sys.stderr)
        return 2
    report = build_report(loaded["vague"], loaded["original"])
    report["mixed"] = problems
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
