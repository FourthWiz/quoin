"""Evaluate OpenCode qualification rows against the frozen pilot thresholds.

Input is either a finished run directory written by ``run_benchmark.py``
(``<run>/<cell>/<task>/metrics.json`` and ``judge.json``), or a JSON Lines file
with one row per run (unsuccessful runs included):

    {"cell": "quoin-opencode", "scenario": "scoped-code-change", "verdict": "pass",
     "failure_reason": null, "review_findings": 1, "setup_seconds": 4.2,
     "wall_seconds": 310.0, "turns": 18, "tool_errors": 0, "tokens": null,
     "cost_usd": null, "provenance": {...}}

``provenance`` is the record the OpenCode cells write. The report refuses the
paired comparison when the cells used different models or endpoints, and
prints every threshold as pass, fail or pending with the actual counts.

Exit status: 0 when no threshold failed and pairing holds, 1 otherwise,
2 on unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

# Path bootstrap, as in run_benchmark.py, so the script runs without pytest.
_repo_root = Path(__file__).resolve().parents[3]
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import quoin as _q

    if str(_repo_root / "quoin") not in _q.__path__:
        _q.__path__.append(str(_repo_root / "quoin"))
except ImportError:
    pass

from quoin.benchmarks.harness.opencode_cells import build_report, render_markdown, rows_from_run_dir  # noqa: E402


def read_rows(path: Path) -> List[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"line {number}: expected an object")
        rows.append(row)
    return rows


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rows", type=Path, help="run directory, or JSON Lines file with one row per run")
    parser.add_argument("--invariants", choices=("pass", "fail", "pending"), default="pending",
                        help="outcome of the deterministic safety and artifact suite")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    try:
        rows = rows_from_run_dir(args.rows) if args.rows.is_dir() else read_rows(args.rows)
    except (OSError, ValueError) as exc:
        print(f"opencode_report: cannot read {args.rows}: {exc}", file=sys.stderr)
        return 2
    invariants = {"pass": True, "fail": False, "pending": None}[args.invariants]
    report = build_report(rows, invariants)
    print(json.dumps(report, indent=2) if args.format == "json" else render_markdown(report))
    failed = any(t["status"] == "fail" for t in report["thresholds"])
    return 1 if failed or report["pairing"]["status"] != "paired" else 0


if __name__ == "__main__":
    sys.exit(main())
