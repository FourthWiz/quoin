"""
pairwise_review.py - Blind pairwise review of a finished tiered run.

Usage, after the run is judged:

    python3 quoin/benchmarks/scripts/pairwise_review.py --run-id <run-id>

The review is a secondary measure: it never changes `resolved`. It spends
through the run's spend ledger under three checks and can be resumed; see
`harness/pairwise_review.py`.
"""

from __future__ import annotations

# Path bootstrap: mirrors tiered_entry.py so the script works when invoked directly.
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
import os
import shlex
import sys
from pathlib import Path
from typing import Optional

CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    from quoin.benchmarks.harness import pairwise_review as core
    from quoin.benchmarks.scripts import spend_ledger

    money = spend_ledger.positive_finite_float
    parser = argparse.ArgumentParser(
        prog="pairwise_review.py",
        allow_abbrev=False,
        description=(
            "Blind pairwise review of the plain and quoin patches of a judged tiered run. "
            "Secondary measure; never changes the resolved result."
        ),
        epilog=(
            "exit codes:\n"
            "  0  done\n"
            "  1  some tasks ended in error (run again to retry them)\n"
            "  2  refused before spending anything\n"
            "  3  a spend guard stopped the review"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--run-id", required=True, help="Run identifier of the finished run")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="Root directory of run outputs (default: <repo-root>/.workflow_artifacts/quoin-benchmarks/runs)")
    parser.add_argument("--suite", default=None,
                        help="Suite alias or file path (default: the run manifest's suite_path)")
    parser.add_argument("--spend-ledger", type=Path, default=None,
                        help="Spend ledger (default: the manifest's spend_ledger, else <run-dir>/<run-id>.spend-ledger.jsonl)")
    parser.add_argument("--authorised-usd", type=money, default=None,
                        help="Ceiling across the whole ledger (default: the manifest's authorised_usd, else the suite ceiling)")
    parser.add_argument("--max-review-usd", type=money, default=None,
                        help="Cap per review call (default: the suite's pairwise_review_cap_usd)")
    parser.add_argument("--max-review-total-usd", type=money, default=None,
                        help="Cap on all review spend of the run (default: the suite's pairwise_review_total_cap_usd)")
    parser.add_argument("--seed", type=int, default=core.DEFAULT_SEED,
                        help="Seed for the A/B order (default: %(default)s)")
    parser.add_argument("--model", default=None, help="Reviewer model (default: the manifest's simple_claude_model)")
    parser.add_argument("--effort", default=None, help="Reviewer effort (default: the manifest's effort)")
    parser.add_argument("--timeout-s", type=float, default=core.DEFAULT_REVIEW_TIMEOUT_S,
                        help="Per-review timeout in seconds (default: %(default)s)")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Review the rows that exist even though sessions remain to spawn")
    parser.add_argument("--yes", action="store_true", help="Skip the spend prompt")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print eligibility and the A/B assignment; spend nothing")
    return parser


def _refuse(clause: str, detail: str) -> int:
    print(f"REFUSED: {clause}: {detail}", file=sys.stderr)
    return 2


def make_reviewer(effort: str, run_id: str):
    """The real reviewer; tests replace this factory with a fake."""
    from quoin.benchmarks.harness import arm_sandbox, pairwise_review as core

    auth = arm_sandbox.select_auth(os.environ)
    return core.ClaudeCliReviewer(effort, run_id, auth)


def _money(value, clause, what):
    from quoin.benchmarks.scripts import spend_ledger

    try:
        return spend_ledger.positive_finite_float(str(value))
    except argparse.ArgumentTypeError:
        return _refuse(clause, f"{what} {value!r} is not a positive finite number")


def _refresh_summary(run_dir: Path, run_id: str, suite_path: Path) -> None:
    """Re-aggregate so the review lands in summary.json and summary.md."""
    from quoin.benchmarks.harness.aggregate import aggregate_run
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    run_path = run_dir / run_id
    started_at = finished_at = None
    try:
        previous = json.loads((run_path / "summary.json").read_text(encoding="utf-8"))
        started_at, finished_at = previous.get("started_at"), previous.get("finished_at")
    except (OSError, ValueError):
        pass
    aggregate_run(run_dir, run_id, started_at=started_at, finished_at=finished_at, suite_path=suite_path)
    report = check_invariants(run_dir, run_id)
    (run_path / "invariants-report.md").write_text(report.to_markdown(), encoding="utf-8")


def main(argv=None) -> int:
    from quoin.benchmarks.harness import pairwise_review as core
    from quoin.benchmarks.harness.tiered_report import read_manifest
    from quoin.benchmarks.scripts import run_benchmark as rb

    args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    run_dir = Path(
        args.run_dir or CHECKOUT_ROOT / ".workflow_artifacts" / "quoin-benchmarks" / "runs"
    ).resolve()
    run_path = run_dir / args.run_id
    if not run_path.is_dir():
        return _refuse("run-dir", f"no run directory at {run_path}")
    manifest = read_manifest(run_path)

    suite_arg = args.suite or manifest.get("suite_path")
    if not suite_arg:
        return _refuse("suite", "no --suite given and the run manifest records none")
    try:
        suite_path = rb._resolve_suite_arg(suite_arg).resolve()
    except SystemExit:
        return _refuse("suite", f"unknown suite alias {suite_arg!r}")
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _refuse("suite", f"suite file unreadable: {exc}")
    tiered = suite.get("tiered") if isinstance(suite.get("tiered"), dict) else {}
    tasks = [t for t in suite.get("tasks", []) if isinstance(t, dict) and t.get("id")]

    ledger = Path(
        args.spend_ledger or manifest.get("spend_ledger") or run_dir / f"{args.run_id}.spend-ledger.jsonl"
    ).resolve()
    model = args.model or manifest.get("simple_claude_model")
    if not model:
        return _refuse("model-set", "no --model given and the run manifest records no simple_claude_model")
    effort = args.effort or manifest.get("effort")
    if not effort:
        return _refuse("effort-set", "no --effort given and the run manifest records no effort")

    cap = args.max_review_usd if args.max_review_usd is not None else tiered.get("pairwise_review_cap_usd")
    if cap is None:
        return _refuse("cap-set", "the suite has no review cap; pass --max-review-usd")
    total_cap = (args.max_review_total_usd if args.max_review_total_usd is not None
                 else tiered.get("pairwise_review_total_cap_usd"))
    if total_cap is None:
        return _refuse("total-cap-set", "the suite has no review total cap; pass --max-review-total-usd")
    cap = _money(cap, "cap-set", "review cap")
    if isinstance(cap, int):
        return cap
    total_cap = _money(total_cap, "total-cap-set", "review total cap")
    if isinstance(total_cap, int):
        return total_cap

    if args.authorised_usd is not None:
        authorised, source = args.authorised_usd, "flag"
    elif manifest.get("authorised_usd") is not None:
        authorised = _money(manifest["authorised_usd"], "authorised-set", "manifest authorised_usd")
        source = "manifest"
        if isinstance(authorised, int):
            return authorised
    elif tiered.get("worst_case_ceiling_usd") is not None:
        authorised = _money(tiered["worst_case_ceiling_usd"], "authorised-set", "suite worst_case_ceiling_usd")
        source = "suite"
        if isinstance(authorised, int):
            return authorised
    else:
        return _refuse("authorised-set", "no ceiling: pass --authorised-usd")

    if not args.allow_partial:
        remaining = rb._paired_rows_to_spawn(run_dir, args.run_id, tasks, ["simple-claude", "quoin-claude"])
        if remaining is None or remaining > 0:
            return _refuse(
                "run-unfinished",
                "sessions remain to spawn; finish and judge the run first, or pass --allow-partial",
            )

    plan = []
    for task in tasks:
        ok, reason = core.eligibility(run_path, task["id"])
        plan.append((task["id"], ok, reason, core.quoin_is_a(args.seed, task["id"])))
    reviewable = sum(1 for _, ok, _, _ in plan if ok)
    worst = min(cap * reviewable, total_cap)
    print(f"reviewable tasks: {reviewable} of {len(tasks)}")
    print(f"WORST CASE review spend: ${worst:.2f} (cap ${cap:.2f} x {reviewable}, bounded by ${total_cap:.2f})")
    print(f"authorised ceiling: ${authorised:.2f} (from {source})")
    if args.dry_run:
        for task_id, ok, reason, is_a in plan:
            print(f"  {task_id}: " + (f"quoin is {'A' if is_a else 'B'}" if ok else f"ineligible ({reason})"))
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            return _refuse("confirm", "no terminal to confirm the spend; pass --yes")
        sys.stdout.write(f"Spend up to ${worst:.2f} on reviews? [y/N] ")
        sys.stdout.flush()
        try:
            answer = sys.stdin.readline()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("declined")
            return 2

    try:
        reviewer = make_reviewer(effort, args.run_id)
    except (ValueError, OSError) as exc:
        return _refuse("auth", str(exc))

    rc, _state = core.run_review(
        run_path, suite, reviewer=reviewer, ledger=ledger, run_id=args.run_id, seed=args.seed,
        model=model, effort=effort, cap=cap, total_cap=total_cap, authorised=authorised,
        timeout_s=args.timeout_s, allow_partial=args.allow_partial,
    )
    if rc in (0, 1, 3):
        try:
            _refresh_summary(run_dir, args.run_id, suite_path)
        except Exception as exc:  # noqa: BLE001 - the review file is already written
            print(f"summary: could not refresh ({exc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
