"""
sequence_report.py - Arms of a sequence suite by position in the sequence.

Usage (from a source checkout of the quoin repo):

    python3 quoin/benchmarks/scripts/sequence_report.py MEMORY_RUN [--lean-run LEAN_RUN]
        [--suite PATH] [--allow-mixed] [--json OUT]

MEMORY_RUN is a run of a sequence suite (`tiered.sequence_mode`) with
QUOIN_BENCH_QUOIN_ROUTE=memory; LEAN_RUN, optional, is a run of the same
suite with QUOIN_BENCH_QUOIN_ROUTE=lean. Three arms come out of them: plain
(the plain arm of MEMORY_RUN), memory (its quoin arm) and lean (the quoin arm
of LEAN_RUN).

Per arm, the report gives pass rate, mean cost and mean wall time over the
first, middle and last third of each sequence, and the least-squares slope of
each measure against relative position (0 for a sequence's first task, 1 for
its last), fitted within each sequence, so the slope is the fitted change from
first to last task. Rows with no known cost are left out of the cost figures. Then the
paired per-task differences memory minus lean and memory minus plain, with a
sign test; and per task, what memory the memory arm inherited (from which
earlier task, how many files, how many lessons) and how many lessons it wrote.

A row counts for an arm only when it ran and is not invalid; a paired
difference needs both rows. The runs must share the suite sha and models, and
their manifests must record the memory and lean routes, unless `--allow-mixed`
is given. Read-only over the run directories.

Exit codes: 0 report written, 2 refused (unreadable run, wrong suite or route).
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
import sys
from pathlib import Path
from typing import Optional

from quoin.benchmarks.harness import sequence_memory
from quoin.benchmarks.harness.tiered_report import PLAIN_CELL, QUOIN_CELL
from quoin.benchmarks.scripts.multi_run_report import load_run, run_identity, sign_test

ARMS = ("plain", "memory", "lean")
THIRDS = ("first", "middle", "last")
MEASURES = ("pass", "usd", "wall_clock_s")
PAIRS = (("memory", "lean"), ("memory", "plain"))
EXPECTED_ROUTE = {"memory": "memory", "lean": "lean"}


def third_of(position: int, length: int) -> str:
    return THIRDS[min(2, (3 * position) // max(length, 1))]


def relative_position(position: int, length: int) -> float:
    return position / (length - 1) if length > 1 else 0.0


def slope(points) -> Optional[float]:
    """Within-sequence least-squares slope of y on x.

    `points` are (sequence id, x, y). Each sequence is centred on its own
    means before the pooled fit, so a level difference between sequences
    (one repo easier than another) cannot leak into the slope when rows are
    missing. None when no sequence has two distinct x.
    """
    groups = {}
    for seq, x, y in points:
        groups.setdefault(seq, []).append((x, y))
    sxx = sxy = 0.0
    for pts in groups.values():
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        sxx += sum((x - mx) ** 2 for x, _ in pts)
        sxy += sum((x - mx) * (y - my) for x, y in pts)
    return sxy / sxx if sxx else None


def _value(cell: dict, measure: str):
    if measure == "pass":
        return 1.0 if cell["verdict"] == "pass" else 0.0
    return float(cell.get(measure) or 0.0)


def _counted(cell: Optional[dict], measure: str = "pass") -> bool:
    """A row that ran and is not invalid; for cost, also one with a known cost."""
    if cell is None or cell["verdict"] in ("not_run", "invalid"):
        return False
    return measure != "usd" or cell.get("usd_source") != "unavailable"


def arm_rows(memory_run: dict, lean_run: Optional[dict]) -> dict:
    """{arm: {task_id: pair-entry cell}} for the three arms."""
    out = {arm: {} for arm in ARMS}
    for entry in memory_run["pairs"]:
        out["plain"][entry["task_id"]] = entry[PLAIN_CELL]
        out["memory"][entry["task_id"]] = entry[QUOIN_CELL]
    if lean_run is not None:
        for entry in lean_run["pairs"]:
            out["lean"][entry["task_id"]] = entry[QUOIN_CELL]
    return out


def by_position(suite: dict, rows: dict) -> dict:
    tasks = [t for t in suite["tasks"] if t.get("sequence_id") is not None]
    out = {}
    for arm in ARMS:
        groups = {third: [] for third in THIRDS}
        points = {m: [] for m in MEASURES}
        for task in tasks:
            cell = rows[arm].get(task["id"])
            if not _counted(cell):
                continue
            pos, length = task["sequence_position"], task["sequence_length"]
            groups[third_of(pos, length)].append(cell)
            for m in MEASURES:
                if _counted(cell, m):
                    points[m].append((task["sequence_id"], relative_position(pos, length),
                                      _value(cell, m)))

        def mean(cells, m):
            vals = [_value(c, m) for c in cells if _counted(c, m)]
            return sum(vals) / len(vals) if vals else None

        arm_out = {}
        for third, cells in groups.items():
            arm_out[third] = {
                "n": len(cells),
                "pass_rate": mean(cells, "pass"),
                "mean_usd": mean(cells, "usd"),
                "mean_wall_clock_s": mean(cells, "wall_clock_s"),
            }
        arm_out["slope"] = {m: slope(points[m]) for m in MEASURES}
        out[arm] = arm_out
    return out


def paired(suite: dict, rows: dict) -> dict:
    out = {}
    for a, b in PAIRS:
        diffs = {m: [] for m in MEASURES}
        per_task = []
        for task in suite["tasks"]:
            ca, cb = rows[a].get(task["id"]), rows[b].get(task["id"])
            if not (_counted(ca) and _counted(cb)):
                continue
            d = {m: (_value(ca, m) - _value(cb, m)) if _counted(ca, m) and _counted(cb, m) else None
                 for m in MEASURES}
            per_task.append({"task_id": task["id"], **d})
            for m in MEASURES:
                if d[m] is not None:
                    diffs[m].append(d[m])
        n = len(per_task)
        out[f"{a}_vs_{b}"] = {
            "n": n,
            "mean": {m: (sum(diffs[m]) / len(diffs[m]) if diffs[m] else None) for m in MEASURES},
            "sign_test": {m: sign_test(diffs[m]) for m in MEASURES},
            "per_task": per_task,
        }
    return out


def inherited_memory(suite: dict, memory_run_path: Path) -> list:
    out = []
    for task in suite["tasks"]:
        record_path = memory_run_path / QUOIN_CELL / task["id"] / sequence_memory.RECORD_FILE
        try:
            rec = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        out.append({
            "task_id": task["id"],
            "sequence_id": task.get("sequence_id"),
            "sequence_position": task.get("sequence_position"),
            "recorded": rec is not None,
            "inherited_from": rec.get("inherited_from") if rec else None,
            "inherited_files": len(rec.get("inherited_files") or []) if rec else 0,
            "lessons_inherited": rec.get("lessons_inherited", 0) if rec else 0,
            "lessons_written": rec.get("lessons_written", 0) if rec else 0,
        })
    return out


def build_report(memory_run: dict, lean_run: Optional[dict]) -> dict:
    suite = memory_run["suite"]
    rows = arm_rows(memory_run, lean_run)
    return {
        "memory_run": str(memory_run["path"]),
        "lean_run": str(lean_run["path"]) if lean_run else None,
        "by_position": by_position(suite, rows),
        "paired": paired(suite, rows),
        "memory": inherited_memory(suite, Path(memory_run["path"])),
    }


def _fmt(value, kind) -> str:
    if value is None:
        return "-"
    if kind == "pct":
        return f"{100 * value:.0f}%"
    if kind == "usd":
        return f"${value:.2f}"
    if kind == "s":
        return f"{value:.0f}s"
    return f"{value:+.3f}"


def render_markdown(report: dict) -> str:
    lines = ["# Sequence report", "", f"Memory run: `{report['memory_run']}`",
             f"Lean run: `{report['lean_run'] or 'not given'}`", "",
             "## By position in the sequence", "",
             "| Arm | Third | n | Pass rate | Mean cost | Mean time |", "|---|---|---|---|---|---|"]
    for arm in ARMS:
        data = report["by_position"][arm]
        for third in THIRDS:
            d = data[third]
            lines.append(f"| {arm} | {third} | {d['n']} | {_fmt(d['pass_rate'], 'pct')} | "
                         f"{_fmt(d['mean_usd'], 'usd')} | {_fmt(d['mean_wall_clock_s'], 's')} |")
    lines += ["", "Slope against relative position (first task 0, last task 1):", "",
              "| Arm | Pass rate | Cost ($) | Time (s) |", "|---|---|---|---|"]
    for arm in ARMS:
        s = report["by_position"][arm]["slope"]
        lines.append(f"| {arm} | {_fmt(s['pass'], 'num')} | {_fmt(s['usd'], 'num')} | "
                     f"{_fmt(s['wall_clock_s'], 'num')} |")
    lines += ["", "## Paired per-task differences", "",
              "| Pair | n | Pass (mean, sign p) | Cost (mean) | Time (mean) |", "|---|---|---|---|---|"]
    for name, d in report["paired"].items():
        p = d["sign_test"]["pass"]["p_value"]
        lines.append(f"| {name.replace('_vs_', ' - ')} | {d['n']} | {_fmt(d['mean']['pass'], 'num')}, "
                     f"p={'-' if p is None else f'{p:.3f}'} | {_fmt(d['mean']['usd'], 'num')} | "
                     f"{_fmt(d['mean']['wall_clock_s'], 'num')} |")
    lines += ["", "## Memory per task (memory arm)", "",
              "| Task | Inherited from | Files | Lessons inherited | Lessons written |",
              "|---|---|---|---|---|"]
    for m in report["memory"]:
        if not m["recorded"]:
            lines.append(f"| {m['task_id']} | not recorded | - | - | - |")
            continue
        lines.append(f"| {m['task_id']} | {m['inherited_from'] or 'none'} | {m['inherited_files']} | "
                     f"{m['lessons_inherited']} | {m['lessons_written']} |")
    return "\n".join(lines) + "\n"


def _refuse(clause: str, detail: str) -> int:
    print(f"REFUSED: {clause}: {detail}", file=sys.stderr)
    return 2


def build_parser(prog: str = "sequence_report.py") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, allow_abbrev=False,
        description="Compare plain, memory and lean arms of a sequence suite by sequence position.",
    )
    parser.add_argument("memory_run", type=Path, help="Run directory with QUOIN_BENCH_QUOIN_ROUTE=memory")
    parser.add_argument("--lean-run", type=Path, default=None,
                        help="Run directory of the same suite with QUOIN_BENCH_QUOIN_ROUTE=lean")
    parser.add_argument("--suite", type=Path, default=None,
                        help="Suite file (default: the path each run's manifest records)")
    parser.add_argument("--allow-mixed", action="store_true",
                        help="Accept runs whose suite sha, models or routes do not match")
    parser.add_argument("--json", type=Path, default=None, help="Also write the report as JSON here")
    return parser


def main(argv=None, prog: str = "sequence_report.py") -> int:
    args = build_parser(prog).parse_args(argv)
    memory_run = load_run(args.memory_run.resolve(), args.suite)
    if isinstance(memory_run, str):
        return _refuse("run", memory_run)
    lean_run = None
    if args.lean_run is not None:
        lean_run = load_run(args.lean_run.resolve(), args.suite)
        if isinstance(lean_run, str):
            return _refuse("run", lean_run)
    if not (memory_run["suite"].get("tiered") or {}).get("sequence_mode"):
        return _refuse("suite", "not a sequence suite (tiered.sequence_mode is not set)")
    problems = []
    for arm, run in (("memory", memory_run), ("lean", lean_run)):
        if run is None:
            continue
        route = run_identity(run["manifest"])["quoin_route"]
        if route != EXPECTED_ROUTE[arm]:
            problems.append(f"{arm} run records quoin route {route!r}, expected {EXPECTED_ROUTE[arm]!r}")
    if lean_run is not None:
        a, b = run_identity(memory_run["manifest"]), run_identity(lean_run["manifest"])
        for key in ("suite_sha", "simple_claude_model", "quoin_claude_model"):
            if a[key] != b[key]:
                problems.append(f"{key} differs: {a[key]!r} vs {b[key]!r}")
    if problems and not args.allow_mixed:
        return _refuse("runs", "; ".join(problems) + "; pass --allow-mixed to report anyway")
    report = build_report(memory_run, lean_run)
    report["mixed"] = problems
    print(render_markdown(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
