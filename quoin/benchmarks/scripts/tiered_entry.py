"""
tiered_entry.py - One command for the tiered paired SWE-bench benchmark.

Usage (from a source checkout of the quoin repo):

    quoin bench tiered [options]

The command prepares the repo mirrors when they are missing, prints a worst-case
estimate and asks once, runs the pilot tier, compares permission denials between
the arms, runs the remaining tiers on the same run id and ledger, then judges the
sessions or prints the command that does it on a host with Docker. Running it
again with the same options continues an interrupted run.

Every spend guard is the one `run_benchmark.py` already applies: each leg is a
normal paired run built from the same argument parser.
"""

from __future__ import annotations

# Path bootstrap: mirrors run_benchmark.py so the script works when invoked directly
# (without pytest adding src/ and . to sys.path). Must run before any quoin imports.
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
import platform
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITE = "tiered-small"
PAIR = "simple-claude,quoin-claude"
EXIT_PARITY = 4
EXIT_INVALID_PILOT = 5
EXIT_UNPRICED = 6
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RERUN_HINT = "re-run `quoin bench tiered` with the same options to continue"
NO_RESULT_REASON = "no result event recorded (for example a wall-clock timeout), so no denial count"
NO_METRICS_REASON = "metrics.json missing or unreadable"


def _rb():
    """The run_benchmark module; always called through the module object."""
    from quoin.benchmarks.scripts import run_benchmark

    return run_benchmark


# --- options ---------------------------------------------------------------

def build_parser(prog: str = "quoin bench tiered") -> argparse.ArgumentParser:
    from quoin.benchmarks.scripts import spend_ledger

    parser = argparse.ArgumentParser(
        prog=prog,
        allow_abbrev=False,
        description=(
            "Run the tiered paired SWE-bench benchmark end to end: prepare repo mirrors, "
            "confirm the worst-case spend once, run a pilot tier, check permission-denial "
            "parity between the arms, run the remaining tiers, then judge or print the "
            "deferred judge command. Re-running with the same options continues the run."
        ),
        epilog=(
            "exit codes:\n"
            "  0  finished (including judging deferred, and nothing left to do)\n"
            "  1  interrupted, aborted, judging failed, or the spend prompt was declined\n"
            "  2  refused before spending anything (options, auth, spend ceilings, checkout)\n"
            "  3  a spend guard stopped the run\n"
            "  4  parity stop: the arms differ in permission denials, or denial data is missing\n"
            "  5  the quoin arm did not run the pipeline in a pilot row (stopped, or refused on a re-run)\n"
            "  6  finished, but a model in the run has no pricing entry (results are invalid)\n"
            "\n"
            "with --clean:\n"
            "  0  deleted, or nothing to delete\n"
            "  1  declined, or a deletion failed\n"
            "  2  refused (options, no terminal without --yes)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--suite", default=DEFAULT_SUITE,
                        help="Suite alias or file path (default: %(default)s)")
    parser.add_argument("--run-id", default=DEFAULT_SUITE,
                        help="Run identifier; the same id continues the same run (default: %(default)s)")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="Root directory for run outputs (default: "
                             "<repo-root>/.workflow_artifacts/quoin-benchmarks/runs)")
    parser.add_argument("--spend-ledger", type=Path, default=None,
                        help="Spend ledger file (default: <run-dir>/<run-id>.spend-ledger.jsonl)")
    parser.add_argument("--authorised-usd", type=spend_ledger.positive_finite_float, default=None,
                        help="Spend ceiling across the whole ledger (default: the suite's worst-case ceiling)")
    parser.add_argument("--max-budget-usd-per-task", type=spend_ledger.positive_finite_float,
                        default=None,
                        help="Per-session spend cap (default: the suite's documented cap)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the spend prompt; needed for unattended runs")
    parser.add_argument("--pilot-tier", default="easy",
                        help="Tier run first and checked for denial parity (default: %(default)s)")
    parser.add_argument("--parity-min-gap", type=int, default=3,
                        help="Stop when the arms differ by at least this many permission denials "
                             "(and the ratio test also holds) (default: %(default)s)")
    parser.add_argument("--parity-min-ratio", type=float, default=2.0,
                        help="Ratio between the arms' denial totals that, with the gap, triggers "
                             "the parity stop (default: %(default)s)")
    parser.add_argument("--ignore-parity", action="store_true",
                        help="Continue past a parity stop or missing denial data after reviewing the pilot")
    parser.add_argument("--judge-emulation", action="store_true",
                        help="Judge on an arm64 host through x86_64 emulation instead of deferring")
    parser.add_argument("--accept-invalid-pilot", action="store_true",
                        help="Continue past recorded pilot rows where the quoin arm did not run the "
                             "pipeline; give it on every re-run, it is not remembered")
    parser.add_argument("--skip-judge", action="store_true",
                        help="Run the sessions and leave judging for later")
    parser.add_argument("--wall-clock-seconds", type=int, default=None,
                        help="Per-session wall-clock budget override, passed to every leg")
    parser.add_argument("--arm-root", type=Path, default=None,
                        help="Arm worktree root, passed to every leg")
    parser.add_argument("--expected-quoin-commit", default=None,
                        help="Commit the quoin arm must run, passed to every leg")
    parser.add_argument("--repo-root", type=Path, default=CHECKOUT_ROOT,
                        help="Quoin checkout root (default: %(default)s)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete this run's leftovers after listing them and asking once")
    parser.add_argument("--clean-mirrors", action="store_true",
                        help="With --clean, also delete the repo mirror cache")
    parser.add_argument("--clean-explicit-ledger", action="store_true",
                        help="With --clean, also delete a ledger given with --spend-ledger")
    return parser


# Options that only make sense for a run; --clean refuses them.
RUN_ONLY_OPTIONS = (
    "--ignore-parity", "--judge-emulation", "--parity-min-gap", "--parity-min-ratio",
    "--skip-judge", "--pilot-tier", "--wall-clock-seconds", "--arm-root",
    "--expected-quoin-commit", "--accept-invalid-pilot",
)


def _explicit(argv: list, names: tuple) -> list:
    """Names given on the command line, as an exact token or as --name=value.

    Parsed values cannot tell a default from an explicit one for options with
    non-None defaults; with abbreviations disabled these are the only spellings.
    """
    return [name for name in names
            if any(tok == name or tok.startswith(name + "=") for tok in argv)]


# --- resolution -------------------------------------------------------------

@dataclass
class Resolved:
    args: argparse.Namespace
    suite_path: Path
    alias_used: bool
    doc: dict
    tasks: list
    tier_names: list
    pilot_tier: str
    run_id: str
    repo_root: Path
    run_dir: Path
    run_path: Path
    ledger: Path
    cap: float
    authorised: float
    arms: list = field(default_factory=lambda: PAIR.split(","))


def resolve(args):
    """Resolve defaults and validate the options; returns Resolved or an exit code."""
    rb = _rb()
    alias_used = "/" not in args.suite and os.sep not in args.suite and not args.suite.endswith(".json")
    try:
        suite_path = rb._resolve_suite_arg(args.suite)
    except SystemExit:
        return rb._refuse("suite", f"unknown suite alias {args.suite!r}")
    suite_path = suite_path.resolve()
    if not suite_path.is_file():
        return rb._refuse("suite", f"suite file not found: {suite_path}")
    try:
        doc = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return rb._refuse("suite", f"suite file unreadable: {exc}")
    if not rb._is_paired(doc):
        return rb._refuse("suite", "quoin bench tiered needs a paired tiered suite")
    tiered = doc["tiered"]
    tier_names = list((tiered.get("tiers") or {}).keys())
    if args.pilot_tier not in tier_names:
        return rb._refuse(
            "pilot-tier",
            f"unknown pilot tier {args.pilot_tier!r}; known tiers: {', '.join(tier_names)}",
        )
    if not RUN_ID_RE.match(args.run_id) or ".." in args.run_id:
        return rb._refuse("run-id", f"{args.run_id!r} is not a valid run id")
    repo_root = Path(args.repo_root).resolve()
    run_dir = Path(
        args.run_dir or repo_root / ".workflow_artifacts" / "quoin-benchmarks" / "runs"
    ).resolve()
    ledger = Path(args.spend_ledger or run_dir / f"{args.run_id}.spend-ledger.jsonl").resolve()
    from quoin.benchmarks.scripts import spend_ledger

    cap = args.max_budget_usd_per_task or tiered.get("documented_cap_usd")
    if cap is None:
        return rb._refuse("cap-set", "the suite has no documented cap; pass --max-budget-usd-per-task")
    authorised = args.authorised_usd or tiered.get("worst_case_ceiling_usd")
    if authorised is None:
        return rb._refuse("authorised-set", "the suite has no ceiling; pass --authorised-usd")
    try:
        cap = spend_ledger.positive_finite_float(str(cap))
        authorised = spend_ledger.positive_finite_float(str(authorised))
    except argparse.ArgumentTypeError as exc:
        return rb._refuse("suite", f"spend figure is not a positive finite number: {exc}")
    return Resolved(
        args=args, suite_path=suite_path, alias_used=alias_used, doc=doc, tasks=doc["tasks"],
        tier_names=tier_names, pilot_tier=args.pilot_tier, run_id=args.run_id,
        repo_root=repo_root, run_dir=run_dir, run_path=run_dir / args.run_id, ledger=ledger,
        cap=cap, authorised=authorised,
    )


def check_pilot_tier(res: Resolved) -> Optional[int]:
    """Refuse a pilot tier that differs from the one a partly run pilot recorded."""
    from quoin.benchmarks.harness.tiered_report import read_manifest

    stored = set(read_manifest(res.run_path).get("tiers") or [])
    if not stored or stored == set(res.tier_names) or stored == {res.pilot_tier}:
        return None
    if len(stored) == 1:
        advice = f"re-run with --pilot-tier {next(iter(stored))} or start a new --run-id"
    else:
        advice = "start a new --run-id"
    return _rb()._refuse(
        "pilot-tier", f"this run's pilot recorded tiers {sorted(stored)}; {advice}",
    )


def check_resume_ledger(res: Resolved) -> Optional[int]:
    """Refuse a resume that points at a different spend ledger than the run started with."""
    from quoin.benchmarks.harness.tiered_report import read_manifest

    stored = read_manifest(res.run_path).get("spend_ledger")
    if not stored or Path(stored).resolve() == res.ledger:
        return None
    return _rb()._refuse(
        "spend-ledger",
        f"this run recorded its spend in {stored}; re-run with that --spend-ledger "
        "or start a new --run-id",
    )


def leg_argv(res: Resolved, *, tiers: Optional[str] = None, skip_judge: bool,
             stop_on_invalid: bool = False) -> list:
    """Arguments for one paired leg, parsed by run_benchmark's own parser."""
    args = res.args
    argv = [
        "--suite", str(res.suite_path), "--cells", PAIR, "--run-id", res.run_id,
        "--run-dir", str(res.run_dir), "--max-budget-usd-per-task", repr(res.cap),
        "--spend-ledger", str(res.ledger), "--authorised-usd", repr(res.authorised),
        "--repo-root", str(res.repo_root), "--max-parallel", "1", "--resume",
    ]
    if tiers:
        argv += ["--tiers", tiers]
    if skip_judge:
        argv.append("--skip-judge")
    if stop_on_invalid:
        argv.append("--stop-on-invalid-quoin")
    if args.judge_emulation:
        argv.append("--judge-emulation")
    if args.wall_clock_seconds is not None:
        argv += ["--wall-clock-seconds", str(args.wall_clock_seconds)]
    if args.arm_root is not None:
        argv += ["--arm-root", str(args.arm_root)]
    if args.expected_quoin_commit:
        argv += ["--expected-quoin-commit", args.expected_quoin_commit]
    return argv


def leg_namespace(res: Resolved, **kw) -> argparse.Namespace:
    return _rb().build_parser().parse_args(leg_argv(res, **kw))


def recorded_invalid_pilot_rows(res: Resolved) -> list:
    """Pilot-tier task ids whose recorded quoin session did not run the pipeline."""
    from quoin.benchmarks.harness.session_state import INVALID_REASONS, read_session_json

    found = []
    for task in res.tasks:
        if task.get("tier") != res.pilot_tier:
            continue
        session = read_session_json(res.run_path / "quoin-claude" / task["id"])
        if session and session.get("failure_reason") in INVALID_REASONS:
            found.append(task["id"])
    return found


def check_invalid_pilot(res: Resolved) -> Optional[int]:
    """Refuse to spawn anything while a recorded pilot row is invalid.

    This runs before every spawning leg, not inside the pilot leg: once the last
    pilot pair is recorded the pilot leg is skipped, and a plain re-run would
    otherwise spend the full leg on a quoin arm that is known not to work.
    """
    ids = recorded_invalid_pilot_rows(res)
    if not ids or res.args.accept_invalid_pilot:
        return None
    print("pilot stopped earlier: the quoin arm did not run /run on "
          f"{', '.join(ids)}; inspect the rows' transcripts and workflow_artifacts_evidence, "
          "fix the cause, or re-run with --accept-invalid-pilot (give it on every re-run)")
    return EXIT_INVALID_PILOT


# --- estimate and confirmation ----------------------------------------------

def estimate_rows(doc: dict, run_dir: Path, run_id: str, pilot_tier: str):
    """(rows still to spawn overall, rows still to spawn in the pilot tier), or None."""
    rb = _rb()
    arms = PAIR.split(",")
    rows_all = rb._paired_rows_to_spawn(Path(run_dir), run_id, doc["tasks"], arms)
    if rows_all is None:
        return None
    pilot_tasks = [t for t in doc["tasks"] if t.get("tier") == pilot_tier]
    rows_pilot = rb._paired_rows_to_spawn(Path(run_dir), run_id, pilot_tasks, arms)
    if rows_pilot is None:
        return None
    return rows_all, rows_pilot


def print_estimate(res: Resolved, rows_all: int, rows_pilot: int, suite_ceiling: float) -> float:
    from quoin.benchmarks.harness import paired_spend
    from quoin.benchmarks.scripts import spend_ledger

    worst = paired_spend.worst_case(res.cap, rows_all, 1)
    print(f"estimate: worst case ${worst:.2f} for {rows_all} sessions still to spawn "
          f"(pilot {rows_pilot}, rest {rows_all - rows_pilot})")
    print(f"  per-session cap: ${res.cap:.2f}")
    print(f"  suite ceiling: ${suite_ceiling:.2f}")
    print(f"  authorised ceiling: ${res.authorised:.2f}")
    print(f"  recorded for run {res.run_id}: ${spend_ledger.recorded_total(res.ledger, res.run_id):.2f}")
    print(f"  run directory: {res.run_dir}")
    print(f"  spend ledger: {res.ledger}")
    print(f"  checkout: {res.repo_root}")
    return worst


def confirm(worst: float, *, yes: bool, stdin=None, out=None) -> Optional[int]:
    """None to proceed, otherwise the exit code."""
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if out is None else out
    if yes:
        print("confirmed by --yes", file=out)
        return None
    if not stdin.isatty():
        return _rb()._refuse("confirm", "no terminal to confirm on; pass --yes for an unattended run")
    out.write(f"Spend up to ${worst:.2f} on this run? [y/N] ")
    out.flush()
    try:
        answer = stdin.readline()
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return None
    print("declined", file=out)
    return 1


# --- parity -------------------------------------------------------------------

@dataclass
class Verdict:
    plain: int = 0
    quoin: int = 0
    pairs: int = 0
    stop: bool = False
    missing: bool = False
    notes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return "missing" if self.missing else ("stop" if self.stop else "ok")


def _metrics_reason(row_dir: Path) -> str:
    try:
        metrics = json.loads((row_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NO_METRICS_REASON
    return NO_RESULT_REASON if isinstance(metrics, dict) else NO_METRICS_REASON


def parity(res: Resolved) -> Verdict:
    """Compare permission denials between the arms over the pilot tier's recorded rows."""
    from quoin.benchmarks.harness import paired_run
    from quoin.benchmarks.harness.session_state import read_session_json
    from quoin.benchmarks.harness.tiered_report import load_row

    verdict = Verdict()
    plain_cell, quoin_cell = paired_run.PAIR_ORDER
    plain_total = quoin_total = 0
    missing = []
    for task in (t for t in res.tasks if t.get("tier") == res.pilot_tier):
        rows = {cell: res.run_path / cell / task["id"] for cell in paired_run.PAIR_ORDER}
        state = {
            cell: paired_run.row_state(
                rows[cell],
                superseded_dir=paired_run._superseded_dir(res.run_dir, res.run_id, cell, task["id"]),
            )
            for cell in paired_run.PAIR_ORDER
        }
        for cell in paired_run.PAIR_ORDER:
            if state[cell] == "respawn" and read_session_json(rows[cell]) is not None:
                verdict.notes.append(f"excluded {task['id']} {cell}: will be re-spawned")
        ran = {
            cell: state[cell] == "done" and read_session_json(rows[cell]) is not None
            for cell in paired_run.PAIR_ORDER
        }
        if not any(ran.values()):
            continue
        if not all(ran.values()):
            only = next(c for c in paired_run.PAIR_ORDER if ran[c])
            verdict.notes.append(f"excluded {task['id']}: only {only} ran a session")
            continue
        counts = {cell: load_row(rows[cell], None, {})["permission_denials"]
                  for cell in paired_run.PAIR_ORDER}
        gaps = [cell for cell in paired_run.PAIR_ORDER if counts[cell] is None]
        for cell in gaps:
            missing.append((cell, task["id"], _metrics_reason(rows[cell])))
        if not gaps:
            verdict.pairs += 1
            plain_total += counts[plain_cell]
            quoin_total += counts[quoin_cell]
    for cell, task_id, reason in missing:
        verdict.notes.append(f"missing {task_id} {cell}: {reason}")
    verdict.plain, verdict.quoin = plain_total, quoin_total
    verdict.missing = bool(missing) or verdict.pairs == 0
    if verdict.pairs == 0 and not missing:
        verdict.notes.append(f"no comparable {res.pilot_tier} pairs recorded")
    high, low = max(plain_total, quoin_total), min(plain_total, quoin_total)
    args = res.args
    verdict.stop = (not verdict.missing and high - low >= args.parity_min_gap
                    and (low == 0 or high >= args.parity_min_ratio * low))
    return verdict


def report_parity(res: Resolved) -> Verdict:
    verdict = parity(res)
    print(f"parity: plain {verdict.plain}, quoin {verdict.quoin} -> {verdict.label}")
    for note in verdict.notes:
        print(f"  {note}")
    return verdict


def print_parity_next_steps(verdict: Verdict) -> None:
    if verdict.missing:
        print("parity: denial data is missing for the rows above. Review the pilot rows; "
              "--ignore-parity after review is the way forward.")
    else:
        print("parity: the arms differ in permission denials (thresholds "
              "--parity-min-gap / --parity-min-ratio). Review the pilot rows, then re-run "
              "with --ignore-parity to continue.")


# --- judging ----------------------------------------------------------------

def judge_possible(args) -> tuple:
    """(can judge here, reason it cannot)."""
    from quoin.benchmarks.harness import judge

    if args.skip_judge:
        return False, "--skip-judge"
    ok, clause, detail, _version = _rb()._judge_prereqs()
    if not ok:
        return False, f"{clause}: {detail}"
    x86 = platform.machine().lower() in {"x86_64", "amd64"}
    if x86 or os.environ.get(judge.SWEBENCH_NAMESPACE_ENV) == "" or args.judge_emulation:
        return True, ""
    return False, "arm64 host without local image build or --judge-emulation"


def deferred_command(res: Resolved) -> str:
    rb = _rb()
    suite = res.args.suite if res.alias_used else str(res.suite_path)
    argv = [
        "python3", str(Path(rb.__file__).resolve()), "--suite", suite, "--cells", PAIR,
        "--run-dir", str(res.run_dir), "--run-id", res.run_id, "--judge-only",
    ]
    if res.args.judge_emulation:
        argv.append("--judge-emulation")
    return shlex.join(argv)


def print_deferred(res: Resolved, why: str) -> None:
    from quoin.benchmarks.harness import judge

    print(f"judge: deferred ({why})")
    print(deferred_command(res))
    print("  re-running `quoin bench tiered` also judges once the prerequisites hold")
    print("  on another host, copy the run directory and adjust --run-dir")
    probed = os.environ.get(judge.SWEBENCH_PYTHON_ENV) or sys.executable
    print(f"  the interpreter running it needs swebench installed; this host probed {probed}")


def pending_rows(res: Resolved) -> int:
    from quoin.benchmarks.harness.session_state import is_judge_pending

    return sum(
        1 for cell in res.arms for task in res.tasks
        if (res.run_path / cell / task["id"]).is_dir()
        and is_judge_pending(res.run_path / cell / task["id"])
    )


def unpriced_from_summary(run_path) -> list:
    """Models the run's summary lists as unpriced; `[]` when the summary is
    missing, unreadable, carries no tiered report, or predates the field."""
    try:
        summary = json.loads((Path(run_path) / "summary.json").read_text(encoding="utf-8"))
        models = summary["tiered"]["validity"]["unpriced_models"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str)]


def _refresh_summary(res: Resolved) -> None:
    """Re-aggregate the recorded rows under the current code and pricing card.

    No judging, no session, no spend: it keeps a judge-only re-run from
    reporting a summary that older code wrote.
    """
    from quoin.benchmarks.harness.aggregate import aggregate_run
    from quoin.benchmarks.scripts.check_invariants import check_invariants

    started_at = finished_at = None
    try:
        previous = json.loads((res.run_path / "summary.json").read_text(encoding="utf-8"))
        started_at, finished_at = previous.get("started_at"), previous.get("finished_at")
    except (OSError, ValueError):
        pass
    aggregate_run(
        res.run_dir, res.run_id, started_at=started_at, finished_at=finished_at,
        suite_path=res.suite_path,
    )
    report = check_invariants(res.run_dir, res.run_id)
    (res.run_path / "invariants-report.md").write_text(report.to_markdown(), encoding="utf-8")


def review_command(res: Resolved) -> str:
    script = Path(__file__).resolve().with_name("pairwise_review.py")
    argv = ["python3", str(script), "--run-id", res.run_id]
    default_dir = (res.repo_root / ".workflow_artifacts" / "quoin-benchmarks" / "runs").resolve()
    if res.run_dir != default_dir:
        argv += ["--run-dir", str(res.run_dir)]
    if res.ledger != (res.run_dir / f"{res.run_id}.spend-ledger.jsonl").resolve():
        argv += ["--spend-ledger", str(res.ledger)]
    return shlex.join(argv)


def print_review_hint(res: Resolved, rc: int) -> None:
    """Print the review command once the run finished cleanly and is fully judged.

    The counts are taken fresh here, so the hint never rests on a stale
    remaining-session figure. The recount runs with stderr captured: a foreign
    row has already been reported once and must not be reported again.
    """
    import contextlib
    import io

    if rc != 0:
        return
    with contextlib.redirect_stderr(io.StringIO()):
        remaining = _rb()._paired_rows_to_spawn(res.run_dir, res.run_id, res.tasks, res.arms)
    if remaining is None or remaining > 0 or pending_rows(res) > 0:
        return
    print("review: optional blind pairwise review of the patches (a secondary measure, never "
          "changes `resolved`; it spends model budget under its own caps):")
    print(review_command(res))


def finish(res: Resolved, rc: int) -> int:
    """Print the final report; a clean run that used an unpriced model exits 6."""
    final_report(res)
    print_review_hint(res, rc)
    unpriced = unpriced_from_summary(res.run_path)
    if unpriced and rc == 0:
        print(f"unpriced model in this run: {', '.join(unpriced)}; add it to pricing.json "
              "and cost_from_jsonl.PRICES, then re-aggregate")
        return EXIT_UNPRICED
    return rc


def final_report(res: Resolved) -> None:
    from quoin.benchmarks.scripts import spend_ledger

    pending = pending_rows(res)
    print(f"summary: {res.run_path / 'summary.md'}")
    print("results: final" if not pending else f"results: provisional ({pending} rows judge-pending)")
    print(f"recorded spend for run {res.run_id}: "
          f"${spend_ledger.recorded_total(res.ledger, res.run_id):.2f}")


# --- orchestration --------------------------------------------------------------

def pilot_phase(res: Resolved, rows_pilot: int) -> bool:
    """True while the pilot tier is the only thing this run has started."""
    from quoin.benchmarks.harness.tiered_report import read_manifest

    stored = set(read_manifest(res.run_path).get("tiers") or [])
    return (not stored or stored == {res.pilot_tier}) and rows_pilot > 0


def _mirrors_ready(res: Resolved) -> bool:
    from quoin.benchmarks.harness.repo_prep import pin_ref_status_offline

    verified = _rb()._verified_tasks(res.tasks)
    return all(pin_ref_status_offline(t) == t["base_commit"] for t in verified)


def _emulate_x86(res: Resolved) -> bool:
    from quoin.benchmarks.harness import judge

    return judge.emulation_active(res.args.judge_emulation)


def _judge_pending_or_defer(res: Resolved) -> int:
    rb = _rb()
    pending = pending_rows(res)
    ok, why = judge_possible(res.args)
    rc = 0
    if pending and ok:
        rc = rb._judge_only_main(
            res.suite_path, res.tasks, res.run_dir, res.run_id, emulate_x86=_emulate_x86(res),
        )
        if rc:
            print(RERUN_HINT)
    elif pending:
        print_deferred(res, why)
    if not (pending and ok):
        try:
            _refresh_summary(res)
        except Exception as exc:  # noqa: BLE001 - the stored summary is still reported
            print(f"summary: could not refresh ({exc})")
    return rc


def orchestrate(args) -> int:
    rb = _rb()
    res = resolve(args)
    if isinstance(res, int):
        return res
    rc = check_pilot_tier(res)
    if rc is not None:
        return rc
    rc = check_resume_ledger(res)
    if rc is not None:
        return rc
    rows = estimate_rows(res.doc, res.run_dir, res.run_id, res.pilot_tier)
    if rows is None:
        return 2
    rows_all, rows_pilot = rows

    if res.run_path.is_dir() and rows_all == 0:
        rc = _judge_pending_or_defer(res)
        return finish(res, rc)

    rc = check_invalid_pilot(res)
    if rc is not None:
        return rc

    in_pilot = pilot_phase(res, rows_pilot)
    if not in_pilot and not args.ignore_parity:
        verdict = report_parity(res)
        if verdict.stop or verdict.missing:
            print_parity_next_steps(verdict)
            return EXIT_PARITY

    if _mirrors_ready(res):
        print("prepare: skipped (repo mirrors already pinned)")
    else:
        print("prepare: building repo mirrors")
        if rb._prepare_repos_main(res.suite_path, res.tasks):
            print("prepare: failed")
            return 2
        print("prepare: done")

    judge_ok, why = judge_possible(args)
    # The estimate needs only the spawn count; judging was probed just above, and the
    # full leg's own preflight checks it again before anything is spent.
    pre = rb._paired_preflight(leg_namespace(res, skip_judge=True), res.suite_path, res.doc)
    if isinstance(pre, int):
        return pre
    worst = print_estimate(res, pre.n_spawns, rows_pilot, pre.suite_ceiling)
    rc = confirm(worst, yes=args.yes)
    if rc is not None:
        return rc
    confirmed_worst = res.cap * pre.n_spawns
    pilot_rows_before_leg = rows_pilot if in_pilot else 0

    if in_pilot:
        print(f"pilot: {res.pilot_tier} tier, {rows_pilot} sessions")
        rc = rb._paired_main(
            leg_namespace(res, tiers=res.pilot_tier, skip_judge=True, stop_on_invalid=True),
            res.suite_path, res.doc, quiet_deferred_hint=True,
        )
        if rc == EXIT_INVALID_PILOT:
            stopped = recorded_invalid_pilot_rows(res)
            print("pilot stopped: the quoin arm did not run /run on "
                  f"{', '.join(stopped) or 'a pilot task'}; inspect the row's transcript and "
                  "workflow_artifacts_evidence before spending more; re-running refuses until "
                  "fixed or --accept-invalid-pilot is given (on every re-run)")
            final_report(res)
            return rc
        if rc:
            print(RERUN_HINT)
            return rc
        if not args.ignore_parity:
            verdict = report_parity(res)
            if verdict.stop or verdict.missing:
                print_parity_next_steps(verdict)
                return EXIT_PARITY

    remaining = rb._paired_rows_to_spawn(res.run_dir, res.run_id, res.tasks, res.arms)
    if remaining is None:
        return 2
    if res.cap * remaining > confirmed_worst - res.cap * pilot_rows_before_leg:
        again = res.cap * remaining
        print(f"estimate: ${again:.2f} for the {remaining} remaining sessions is more than the "
              "amount confirmed")
        rc = confirm(again, yes=args.yes)
        if rc is not None:
            return rc

    if in_pilot:
        # The pilot can take long enough for the daemon to go away, so look again.
        judge_ok, why = judge_possible(args)
    judged = False
    if remaining:
        print(f"full: {remaining} sessions")
        rc = rb._paired_main(leg_namespace(res, skip_judge=not judge_ok), res.suite_path, res.doc)
        judged = judge_ok and rc == 0
    elif pending_rows(res) and judge_ok:
        rc = rb._judge_only_main(
            res.suite_path, res.tasks, res.run_dir, res.run_id, emulate_x86=_emulate_x86(res),
        )
        judged = rc == 0
    else:
        rc = 0
    if rc:
        # The run is unfinished, so neither a judge status nor the deferred command applies yet.
        print(RERUN_HINT)
    elif pending_rows(res) and not judge_ok:
        print_deferred(res, why)
    elif judged:
        print("judge: ran")
    return finish(res, rc)


def main(argv=None, prog: str = "quoin bench tiered") -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser(prog).parse_args(argv)
    if (args.clean_mirrors or args.clean_explicit_ledger) and not args.clean:
        return _rb()._refuse("clean", "--clean-mirrors and --clean-explicit-ledger need --clean")
    if args.clean:
        conflicts = _explicit(argv, RUN_ONLY_OPTIONS)
        if conflicts:
            return _rb()._refuse("clean", f"--clean cannot be combined with {', '.join(conflicts)}")
        from quoin.benchmarks.scripts import tiered_clean

        try:
            return tiered_clean.run_clean(args)
        except KeyboardInterrupt:
            print("interrupted")
            return 1
    try:
        return orchestrate(args)
    except KeyboardInterrupt:
        print("interrupted")
        print(RERUN_HINT)
        return 1


if __name__ == "__main__":
    sys.exit(main(prog="tiered_entry.py"))
