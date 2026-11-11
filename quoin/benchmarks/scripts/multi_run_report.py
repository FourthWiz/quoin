"""
multi_run_report.py - Compare the arms of a tiered paired benchmark across several runs.

Usage (from a source checkout of the quoin repo):

    python3 quoin/benchmarks/scripts/multi_run_report.py RUN_PATH [RUN_PATH ...]
        [--suite PATH] [--allow-mixed] [--json OUT]

Each RUN_PATH is one run directory (`<run-dir>/<run-id>`), for example the
repeats `quoin bench tiered --repeat N` writes as `<run-id>-r1` ... `-rN`.
Per arm and task the report gives passes / runs, then per tier and overall the
mean per-task pass rate of each arm, the mean per-task difference (quoin minus
plain), a sign test and a Wilcoxon signed-rank test over the per-task
differences, and which tasks each arm solved in every run, in none, or in
some. When the runs record the route the quoin arm took on each task, the same
figures are split by that route.

A run contributes to a task only when both arms ran it and neither row is
invalid, so every count is paired. Runs whose suite sha, models or quoin route
differ are refused unless `--allow-mixed` is given; the refusal lists what
differs. Read-only over the run directories.

Exit codes: 0 report written, 2 refused (unreadable run, mixed runs, options).
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
import json
import math
import sys
from fractions import Fraction
from pathlib import Path
from typing import Optional

from quoin.benchmarks.harness.tiered_report import (
    PLAIN_CELL, QUOIN_CELL, _tier_order, build_tiered_report, read_manifest,
)

# Manifest keys that must agree across the runs being combined.
MATCH_KEYS = ("suite_sha", "simple_claude_model", "quoin_claude_model", "quoin_route")
ARM_LABELS = {PLAIN_CELL: "plain", QUOIN_CELL: "quoin"}
MIXED_ROUTE = "mixed"


# --- statistics ---------------------------------------------------------------

def sign_test(diffs) -> dict:
    """Exact two-sided sign test over non-zero differences."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    if n == 0:
        return {"n": 0, "positive": 0, "negative": 0, "p_value": None}
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return {"n": n, "positive": pos, "negative": neg, "p_value": min(1.0, 2 * tail)}


def _midranks(values):
    """Ranks of `values` (ascending, 1-based), ties sharing their mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def wilcoxon_signed_rank(diffs) -> dict:
    """Exact two-sided Wilcoxon signed-rank test, zeros dropped, ties at mid-rank.

    The null distribution is enumerated over the observed (doubled, so integer)
    mid-ranks, which stays exact with ties for any task count this suite has.
    """
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "w_minus": 0.0, "p_value": None}
    ranks = _midranks([abs(d) for d in nonzero])
    w_plus = float(sum(r for r, d in zip(ranks, nonzero) if d > 0))
    w_minus = float(sum(r for r, d in zip(ranks, nonzero) if d < 0))
    doubled = [int(round(2 * r)) for r in ranks]
    counts = {0: 1}
    for r in doubled:
        nxt = dict(counts)
        for total, ways in counts.items():
            nxt[total + r] = nxt.get(total + r, 0) + ways
        counts = nxt
    observed = int(round(2 * w_plus))
    lower = sum(w for t, w in counts.items() if t <= observed)
    upper = sum(w for t, w in counts.items() if t >= observed)
    p = min(1.0, 2 * min(lower, upper) / 2 ** n)
    return {"n": n, "w_plus": w_plus, "w_minus": w_minus, "p_value": p}


# --- loading and checks ---------------------------------------------------------

def _suite_for(run_path: Path, manifest: dict, suite_arg: Optional[Path]):
    candidates = [suite_arg] if suite_arg else [
        manifest.get("suite_path_resolved"), manifest.get("suite_path"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(Path(candidate).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def load_run(run_path: Path, suite_arg: Optional[Path] = None):
    """{path, manifest, suite, pairs} for one run, or the reason it cannot be read."""
    run_path = Path(run_path)
    manifest = read_manifest(run_path)
    if not manifest:
        return f"{run_path}: no readable run-manifest.yaml"
    suite = _suite_for(run_path, manifest, suite_arg)
    if suite is None:
        return f"{run_path}: suite file not found; pass --suite"
    report = build_tiered_report(run_path, suite, manifest, manifest.get("suite_sha"))
    return {"path": run_path, "manifest": manifest, "suite": suite, "pairs": report["pairs"]}


def run_identity(manifest: dict) -> dict:
    """The manifest values that must agree; a missing route is the default route."""
    identity = {key: manifest.get(key) for key in MATCH_KEYS}
    identity["quoin_route"] = identity["quoin_route"] or "default"
    return identity


def differences(runs: list) -> dict:
    """{key: {run path: value}} for every identity key whose values differ."""
    out = {}
    for key in MATCH_KEYS:
        values = {str(run["path"]): run_identity(run["manifest"])[key] for run in runs}
        if len(set(values.values())) > 1:
            out[key] = values
    return out


# --- aggregation ------------------------------------------------------------------

def _counted(entry) -> bool:
    verdicts = (entry[PLAIN_CELL]["verdict"], entry[QUOIN_CELL]["verdict"])
    return not any(v in ("not_run", "invalid") for v in verdicts)


def _route(entry) -> Optional[str]:
    route = (entry[QUOIN_CELL].get("workflow_route") or {}).get("route")
    return route if isinstance(route, str) and route else None


def per_task(runs: list) -> list:
    """One record per task: tier, per-arm passes and runs, routes the quoin arm took."""
    tasks = {}
    for run in runs:
        for entry in run["pairs"]:
            rec = tasks.setdefault(entry["task_id"], {
                "task_id": entry["task_id"], "tier": entry["tier"], "runs": 0,
                "passes": {PLAIN_CELL: 0, QUOIN_CELL: 0}, "routes": [], "not_counted": 0,
            })
            if not _counted(entry):
                rec["not_counted"] += 1
                continue
            rec["runs"] += 1
            for arm in (PLAIN_CELL, QUOIN_CELL):
                rec["passes"][arm] += entry[arm]["verdict"] == "pass"
            route = _route(entry)
            if route:
                rec["routes"].append(route)
    out = []
    for rec in tasks.values():
        n = rec["runs"]
        rec["rate"] = {arm: (rec["passes"][arm] / n if n else None) for arm in rec["passes"]}
        rec["diff"] = rec["rate"][QUOIN_CELL] - rec["rate"][PLAIN_CELL] if n else None
        routes = set(rec["routes"])
        rec["route"] = (next(iter(routes)) if len(routes) == 1
                        else MIXED_ROUTE if routes else None)
        out.append(rec)
    return out


def _stability(records, arm) -> dict:
    out = {"every": [], "none": [], "some": []}
    for rec in records:
        passes, n = rec["passes"][arm], rec["runs"]
        key = "every" if passes == n else "none" if passes == 0 else "some"
        out[key].append(rec["task_id"])
    return out


def summarize(records) -> dict:
    """Mean per-task pass rates, the paired tests and stability over `records`.

    Differences are exact fractions, so two tasks that differ by the same
    amount through different pass counts tie in the signed-rank test.
    """
    used = [r for r in records if r["runs"]]
    n = len(used)
    mean = {arm: (sum(r["rate"][arm] for r in used) / n if n else None)
            for arm in (PLAIN_CELL, QUOIN_CELL)}
    diffs = [Fraction(r["passes"][QUOIN_CELL] - r["passes"][PLAIN_CELL], r["runs"]) for r in used]
    return {
        "n_tasks": n,
        "runs": sum(r["runs"] for r in used),
        "mean_pass_rate": mean,
        "mean_diff": float(sum(diffs) / n) if n else None,
        "sign_test": sign_test(diffs),
        "wilcoxon": wilcoxon_signed_rank(diffs),
        "stability": {arm: _stability(used, arm) for arm in (PLAIN_CELL, QUOIN_CELL)},
    }


def build_report(runs: list) -> dict:
    records = per_task(runs)
    order = _tier_order(runs[0]["suite"], [{"tier": r["tier"]} for r in records])
    tier_of = {t: i for i, t in enumerate(order)}
    records.sort(key=lambda r: (tier_of.get(r["tier"], len(order)), r["task_id"]))
    tiers = {}
    for tier in order:
        in_tier = [r for r in records if r["tier"] == tier]
        if in_tier:
            tiers[tier] = summarize(in_tier)
    by_route = {}
    for route in sorted({r["route"] for r in records if r["route"]}):
        by_route[route] = summarize([r for r in records if r["route"] == route])
    return {
        "runs": [str(run["path"]) for run in runs],
        "identity": [run_identity(run["manifest"]) for run in runs],
        "tasks": records,
        "tiers": tiers,
        "overall": summarize(records),
        "by_route": by_route,
    }


# --- rendering ----------------------------------------------------------------------

def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _pp(value) -> str:
    return "n/a" if value is None else f"{100 * value:+.1f} pp"


def _p(value) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _summary_rows(label, s) -> str:
    sign, wil = s["sign_test"], s["wilcoxon"]
    return (f"| {label} | {s['n_tasks']} | {_pct(s['mean_pass_rate'][PLAIN_CELL])} | "
            f"{_pct(s['mean_pass_rate'][QUOIN_CELL])} | {_pp(s['mean_diff'])} | "
            f"{sign['positive']}+/{sign['negative']}- p={_p(sign['p_value'])} | "
            f"p={_p(wil['p_value'])} |")


def _summary_table(title, groups) -> list:
    lines = [f"## {title}", "",
             "| Group | Tasks | Plain mean | Quoin mean | Diff (quoin - plain) | Sign test | Wilcoxon |",
             "|---|---|---|---|---|---|---|"]
    lines += [_summary_rows(label, s) for label, s in groups]
    return lines + [""]


def render_markdown(report: dict, mixed: dict) -> str:
    lines = ["# Multi-run comparison", ""]
    lines.append(f"Runs ({len(report['runs'])}):")
    lines += [f"- {path}" for path in report["runs"]]
    lines.append("")
    if mixed:
        lines.append("**Mixed runs (--allow-mixed):** " + "; ".join(
            f"{key} differs" for key in mixed))
        lines.append("")
    lines += ["## Per task", "",
              "| Task | Tier | Plain passes/runs | Quoin passes/runs | Diff | Quoin route |",
              "|---|---|---|---|---|---|"]
    for rec in report["tasks"]:
        n = rec["runs"]
        note = f" ({rec['not_counted']} not counted)" if rec["not_counted"] else ""
        lines.append(
            f"| {rec['task_id']} | {rec['tier']} | {rec['passes'][PLAIN_CELL]}/{n} | "
            f"{rec['passes'][QUOIN_CELL]}/{n}{note} | {_pp(rec['diff'])} | {rec['route'] or '-'} |")
    lines.append("")
    groups = [(tier, s) for tier, s in report["tiers"].items()] + [("overall", report["overall"])]
    lines += _summary_table("Pass rate by tier", groups)
    lines += ["Mean of per-task pass rates over the runs. The sign test counts tasks where "
              "quoin's rate is higher (+) or lower (-); both tests are exact, two-sided, over "
              "per-task differences, and drop ties.", ""]
    lines += ["## Stability", "",
              "| Arm | Solved in every run | Solved in none | Solved in some |", "|---|---|---|---|"]
    for arm in (PLAIN_CELL, QUOIN_CELL):
        st = report["overall"]["stability"][arm]
        lines.append(f"| {ARM_LABELS[arm]} | " + " | ".join(
            f"{len(st[key])}: {', '.join(st[key]) or '-'}" for key in ("every", "none", "some")) + " |")
    lines.append("")
    if report["by_route"]:
        lines += _summary_table("By quoin route", list(report["by_route"].items()))
        lines += [f"A task whose quoin arm took different routes across runs is `{MIXED_ROUTE}`.", ""]
    else:
        lines += ["## By quoin route", "", "The runs record no quoin route per task.", ""]
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------------------

def _refuse(clause: str, detail: str) -> int:
    print(f"REFUSED: {clause}: {detail}", file=sys.stderr)
    return 2


def build_parser(prog: str = "multi_run_report.py") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, allow_abbrev=False,
        description="Compare plain and quoin arms across several runs of the same tiered suite.",
    )
    parser.add_argument("runs", nargs="+", type=Path, help="Run directories (<run-dir>/<run-id>)")
    parser.add_argument("--suite", type=Path, default=None,
                        help="Suite file (default: the path each run's manifest records)")
    parser.add_argument("--allow-mixed", action="store_true",
                        help="Combine runs whose suite sha, models or quoin route differ")
    parser.add_argument("--json", type=Path, default=None, help="Also write the report as JSON here")
    return parser


def main(argv=None, prog: str = "multi_run_report.py") -> int:
    args = build_parser(prog).parse_args(argv)
    paths = [p.resolve() for p in args.runs]
    if len(set(paths)) != len(paths):
        return _refuse("runs", "the same run directory is given more than once")
    runs = []
    for path in paths:
        run = load_run(path, args.suite)
        if isinstance(run, str):
            return _refuse("run", run)
        runs.append(run)
    mixed = differences(runs)
    if mixed:
        for key, values in mixed.items():
            print(f"{key} differs:", file=sys.stderr)
            for path, value in values.items():
                print(f"  {path}: {value}", file=sys.stderr)
        if not args.allow_mixed:
            return _refuse("mixed-runs", f"{', '.join(mixed)} differ; pass --allow-mixed to combine them")
    report = build_report(runs)
    print(render_markdown(report, mixed))
    if args.json:
        report["mixed"] = mixed
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
