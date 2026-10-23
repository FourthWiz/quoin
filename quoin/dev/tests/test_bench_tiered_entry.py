"""Offline tests of `quoin bench tiered`: options, parity, the CLI group and end-to-end runs.

Every end-to-end case drives `tiered_entry.main()` in-process against a two-tier
stand-in suite with a fake `claude` on PATH, a fake quoin installer, and the judge,
Docker and SWE-bench probes stubbed. No model, network or real config is touched.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import quoin
from quoin import cli
from quoin.benchmarks.harness import arm_sandbox, judge, paired_spend, runner
from quoin.benchmarks.harness.judge import JudgeResult
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.harness.tiered_report import read_manifest
from quoin.benchmarks.scripts import run_benchmark, spend_ledger, tiered_entry
from fakes.fake_arm import fake_install_quoin
from fakes.fake_claude import make_paired_fake_claude, read_paired_log
from test_run_benchmark_paired_cli import _no_network, arm_repo, auth, sandbox_parent  # noqa: F401
from verified_standin import build_tiered_standin, git

REPO = Path(__file__).resolve().parents[3]
PAIR = "simple-claude,quoin-claude"
ARMS = ("simple-claude", "quoin-claude")


class Tty(io.StringIO):
    def isatty(self):
        return True


class NoRead(Tty):
    def readline(self, *a):
        raise AssertionError("the spend prompt must not be shown")

    read = readline


# --- fixture -------------------------------------------------------------------

@pytest.fixture
def tw(tmp_path, monkeypatch, arm_repo, auth, sandbox_parent):  # noqa: F811
    st = build_tiered_standin(tmp_path, monkeypatch)
    cache = tmp_path / "mirrors"
    prepare_mirrors(st["tasks"], log=lambda *_: None)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(["init", "-q", "-b", "main"], checkout)

    state = {"docker_rc": 0, "swebench": judge.SWEBENCH_PINNED_VERSION}
    real_run = subprocess.run

    def fake_docker(argv, *a, **k):
        if argv and argv[0] == "docker":
            return subprocess.CompletedProcess(argv, state["docker_rc"], b"", b"")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_docker)
    monkeypatch.setattr(judge, "probe_swebench_version", lambda *a, **k: state["swebench"])
    monkeypatch.setattr(arm_sandbox, "check_arm_tree", lambda *a, **k: [])
    monkeypatch.setattr(arm_sandbox, "install_quoin", fake_install_quoin)
    monkeypatch.setattr(tiered_entry.platform, "machine", lambda: "x86_64")
    monkeypatch.delenv(judge.SWEBENCH_NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(judge.SWEBENCH_PYTHON_ENV, raising=False)

    judged = []

    def judge_stub(task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
                   task_spec=None, judge_context=None):
        judged.append((Path(task_dir).parent.name, task_id))
        return JudgeResult(task_id, "swebench_verified", "pass", "ev", 0.0, extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", judge_stub)

    calls = []
    original = run_benchmark._paired_main

    def spy(args, suite_path, doc, **kw):
        calls.append({"tiers": args.tiers, "skip_judge": args.skip_judge,
                      "quiet": kw.get("quiet_deferred_hint", False)})
        rc = original(args, suite_path, doc, **kw)
        for hook in hooks:
            hook(kw)
        return rc

    hooks = []
    monkeypatch.setattr(run_benchmark, "_paired_main", spy)

    bin_dir, log = make_paired_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runs = tmp_path / "runs"
    return {
        "tmp": tmp_path, "suite": st["suite"], "tasks": st["tasks"], "standin": st,
        "arm_repo": arm_repo, "runs": runs, "ledger": runs / "R.spend-ledger.jsonl",
        "checkout": checkout, "cache": cache, "bin": bin_dir, "log": log, "state": state,
        "judged": judged, "calls": calls, "hooks": hooks, "monkeypatch": monkeypatch,
        "sandbox_parent": sandbox_parent,
    }


def _argv(w, *extra, **over):
    args = {
        "--suite": str(w["suite"]), "--run-id": "R", "--run-dir": str(w["runs"]),
        "--arm-root": str(w["arm_repo"]["root"]),
        "--expected-quoin-commit": w["arm_repo"]["head"], "--repo-root": str(w["checkout"]),
    }
    args.update(over)
    out = []
    for key, value in args.items():
        if value is not None:
            out += [key, value]
    return out + list(extra)


def _go(w, *extra, **over):
    return tiered_entry.main(_argv(w, *extra, **over))


def _scenario(w, scenario):
    make_paired_fake_claude(w["tmp"], scenario)


def _starts(w):
    return [e for e in read_paired_log(w["log"]) if e["event"] == "start"]


def _ledger(w):
    return [json.loads(l) for l in w["ledger"].read_text().splitlines() if l.strip()]


def _no_leftovers(w):
    assert list(w["sandbox_parent"].rglob("qbench_sandbox_*")) == []
    assert list(w["sandbox_parent"].glob("qbench_run_*")) == []


def _kill_after(w, starts):
    def killer():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(_starts(w)) < starts:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=killer, daemon=True)
    thread.start()
    return thread


def _interrupted(w, starts, scenario_run):
    """Run until the given spawn is killed by SIGTERM; returns the exit code."""
    previous = signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(
        AssertionError("the paired run did not install its own SIGTERM handler")))
    try:
        _scenario(w, {"runs": {str(scenario_run): {"sleep_forever": True}}})
        thread = _kill_after(w, starts)
        code = _go(w, "--yes")
        thread.join(5)
        _scenario(w, {})
        return code
    finally:
        signal.signal(signal.SIGTERM, previous)


def _parsed(*argv):
    return tiered_entry.build_parser().parse_args(list(argv))


# --- T-03: fake claude knob ----------------------------------------------------------

def test_denials_knob_reaches_metrics(tw, capsys):
    w = tw
    _scenario(w, {"denials": {"plain": 2, "quoin": 0}})
    argv = ["--suite", str(w["suite"]), "--cells", PAIR, "--run-id", "K", "--run-dir", str(w["runs"]),
            "--max-budget-usd-per-task", "5.5", "--spend-ledger", str(w["tmp"] / "k.jsonl"),
            "--authorised-usd", "200", "--arm-root", str(w["arm_repo"]["root"]),
            "--expected-quoin-commit", w["arm_repo"]["head"], "--tiers", "medium", "--skip-judge"]
    w["monkeypatch"].setattr(sys, "argv", ["run_benchmark.py", *argv])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    assert exc.value.code == 0
    row = w["runs"] / "K"
    plain = json.loads((row / "simple-claude" / "verified_medium_001" / "metrics.json").read_text())
    quoin_m = json.loads((row / "quoin-claude" / "verified_medium_001" / "metrics.json").read_text())
    assert plain["permission_denial_count"] == 2 and quoin_m["permission_denial_count"] == 0


# --- T-02: tier widening through run_benchmark.main ----------------------------------------

def _rb_main(w, *argv, run_id="R"):
    base = ["--suite", str(w["suite"]), "--cells", PAIR, "--run-id", run_id, "--run-dir", str(w["runs"]),
            "--max-budget-usd-per-task", "5.5", "--spend-ledger", str(w["ledger"]),
            "--authorised-usd", "200", "--arm-root", str(w["arm_repo"]["root"]),
            "--expected-quoin-commit", w["arm_repo"]["head"], "--skip-judge"]
    w["monkeypatch"].setattr(sys, "argv", ["run_benchmark.py", *base, *argv])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    return exc.value.code


def test_resume_may_widen_tiers_but_not_narrow_them(tw, capsys):
    w = tw
    assert _rb_main(w, "--tiers", "easy") == 0
    assert len(_starts(w)) == 4
    assert read_manifest(w["runs"] / "R")["tiers"] == ["easy"]
    assert _rb_main(w, "--resume") == 0
    assert len(_starts(w)) == 6
    assert read_manifest(w["runs"] / "R")["tiers"] == ["easy", "medium"]
    capsys.readouterr()
    assert _rb_main(w, "--resume", "--tiers", "easy") == 2
    assert "REFUSED: resume" in capsys.readouterr().err
    assert len(_starts(w)) == 6


# --- T-04: resolution and up-front checks --------------------------------------------------

def test_resolve_defaults(tw):
    w = tw
    res = tiered_entry.resolve(_parsed("--suite", str(w["suite"]), "--repo-root", str(w["checkout"])))
    expected = (w["checkout"] / ".workflow_artifacts" / "quoin-benchmarks" / "runs").resolve()
    assert res.run_dir == expected
    assert res.ledger == expected / "tiered-small.spend-ledger.jsonl"
    assert res.cap == 5.5 and res.authorised == 44.0
    assert res.pilot_tier == "easy" and res.tier_names == ["easy", "medium"]


def test_relative_explicit_suite_is_made_absolute(tw, monkeypatch):
    w = tw
    monkeypatch.chdir(w["suite"].parent)
    res = tiered_entry.resolve(_parsed("--suite", w["suite"].name))
    assert res.suite_path.is_absolute() and res.suite_path.samefile(w["suite"])
    assert not res.alias_used
    assert str(res.suite_path) in tiered_entry.leg_argv(res, skip_judge=False)


@pytest.mark.parametrize("over,clause", [
    ({"--suite": "nowhere.json"}, "suite"),
    ({"--suite": "no-such-alias"}, "suite"),
    ({"--run-id": "../x"}, "run-id"),
    ({"--pilot-tier": "huge"}, "pilot-tier"),
])
def test_resolve_refusals(tw, capsys, over, clause):
    w = tw
    args = _parsed(*_argv(w, **over))
    assert tiered_entry.resolve(args) == 2
    assert f"REFUSED: {clause}:" in capsys.readouterr().err


def test_pilot_tier_change_is_refused_before_anything_else(tw, capsys, monkeypatch):
    w = tw
    monkeypatch.setattr(run_benchmark, "_git_sha", lambda root: "abc1234")
    monkeypatch.setattr(run_benchmark, "_probe_claude_model", lambda: "model-x")
    run_benchmark._write_manifest(
        w["runs"], "R", w["suite"], list(ARMS), 1, False, w["checkout"],
        extra={"tiers": ["medium"], "max_budget_usd_per_task": 5.5})
    monkeypatch.setattr(sys, "stdin", NoRead())
    assert _go(w, "--pilot-tier", "easy") == 2
    err = capsys.readouterr().err
    assert "REFUSED: pilot-tier" in err and "--pilot-tier medium" in err
    assert _starts(w) == [] and not w["ledger"].exists()


def test_pilot_tier_hint_does_not_suggest_a_multi_tier_value(tmp_path, monkeypatch, arm_repo, capsys):
    st = build_tiered_standin(tmp_path, monkeypatch, tiers={"easy": 1, "medium": 1, "large": 1})
    monkeypatch.setattr(run_benchmark, "_git_sha", lambda root: "abc1234")
    monkeypatch.setattr(run_benchmark, "_probe_claude_model", lambda: "model-x")
    runs = tmp_path / "runs"
    run_benchmark._write_manifest(
        runs, "R", st["suite"], list(ARMS), 1, False, tmp_path,
        extra={"tiers": ["easy", "medium"]})
    res = tiered_entry.resolve(_parsed("--suite", str(st["suite"]), "--run-id", "R",
                                       "--run-dir", str(runs), "--repo-root", str(tmp_path)))
    assert tiered_entry.check_pilot_tier(res) == 2
    err = capsys.readouterr().err
    assert "start a new --run-id" in err and "--pilot-tier easy,medium" not in err
    assert "--pilot-tier easy" not in err.replace("pilot recorded", "")


def test_confirm_branches():
    out = io.StringIO()
    assert tiered_entry.confirm(33.0, yes=True, out=out) is None
    assert "confirmed by --yes" in out.getvalue()
    for answer, expected in (("y\n", None), ("YES\n", None), ("n\n", 1), ("\n", 1), ("", 1)):
        out = io.StringIO()
        assert tiered_entry.confirm(33.0, yes=False, stdin=Tty(answer), out=out) == expected
        assert "Spend up to $33.00 on this run? [y/N] " in out.getvalue()
    assert tiered_entry.confirm(33.0, yes=False, stdin=io.StringIO("y\n"), out=io.StringIO()) == 2


def test_real_suite_estimate_is_ninety_nine_dollars(tmp_path):
    doc = json.loads((REPO / "quoin" / "benchmarks" / "suite-tiered-small.json").read_text())
    rows_all, rows_pilot = tiered_entry.estimate_rows(doc, tmp_path / "runs", "x", "easy")
    assert rows_all == 18 and rows_pilot == 8
    assert f"${paired_spend.worst_case(5.5, rows_all, 1):.2f}" == "$99.00"


def test_leg_argv_parses_with_the_benchmark_parser(tw):
    w = tw
    res = tiered_entry.resolve(_parsed(*_argv(w, "--wall-clock-seconds", "30")))
    ns = run_benchmark.build_parser().parse_args(tiered_entry.leg_argv(res, tiers="easy", skip_judge=True))
    assert ns.tiers == "easy" and ns.skip_judge and ns.resume and ns.max_parallel == 1
    assert ns.max_budget_usd_per_task == 5.5 and ns.authorised_usd == 44.0
    assert ns.wall_clock_seconds == 30 and ns.cells == PAIR
    assert ns.spend_ledger == res.ledger and ns.run_dir == res.run_dir


# --- T-05: parity ----------------------------------------------------------------------

def _rows(res, spec):
    """spec maps (task index, arm index) to a count, 'null', 'nometrics', 'respawn' or 'absent'."""
    easy = [t for t in res.tasks if t["tier"] == "easy"]
    for (ti, ai), value in spec.items():
        d = res.run_path / ARMS[ai] / easy[ti]["id"]
        if value == "absent":
            continue
        d.mkdir(parents=True, exist_ok=True)
        session = {"judge_required": False}
        if value == "respawn":
            session["failure_reason"] = "budget-cap-unarmed"
        (d / "session.json").write_text(json.dumps(session))
        if value == "nometrics" or value == "respawn":
            continue
        count = None if value == "null" else value
        (d / "metrics.json").write_text(json.dumps({"permission_denial_count": count}))


def _pair(res_args, spec, tw):
    w = tw
    res = tiered_entry.resolve(_parsed(*_argv(w), *res_args))
    _rows(res, spec)
    return tiered_entry.parity(res)


@pytest.mark.parametrize("spec,label,plain,quoin_n", [
    ({(0, 0): 1, (0, 1): 1, (1, 0): 1, (1, 1): 1}, "ok", 2, 2),
    ({(0, 0): 3, (0, 1): 0, (1, 0): 0, (1, 1): 0}, "stop", 3, 0),
    ({(0, 0): 6, (0, 1): 4, (1, 0): 6, (1, 1): 5}, "ok", 12, 9),
    ({(0, 0): 4, (0, 1): 1, (1, 0): 0, (1, 1): 0}, "stop", 4, 1),
    ({(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 0}, "ok", 0, 0),
])
def test_parity_verdicts(tw, spec, label, plain, quoin_n):
    verdict = _pair([], spec, tw)
    assert (verdict.label, verdict.plain, verdict.quoin) == (label, plain, quoin_n)


def test_parity_excludes_a_task_only_one_arm_ran(tw):
    verdict = _pair([], {(0, 0): 1, (0, 1): 1, (1, 0): 5, (1, 1): "absent"}, tw)
    assert verdict.label == "ok" and verdict.pairs == 1
    assert any("only simple-claude ran a session" in n for n in verdict.notes)


def test_parity_excludes_a_row_that_will_be_respawned(tw):
    verdict = _pair([], {(0, 0): 1, (0, 1): 1, (1, 0): 1, (1, 1): "respawn"}, tw)
    assert verdict.label == "ok" and verdict.pairs == 1
    assert any("will be re-spawned" in n for n in verdict.notes)
    assert not any(n.startswith("missing") for n in verdict.notes)


def test_parity_reports_missing_denial_data(tw):
    verdict = _pair([], {(0, 0): "null", (0, 1): 0, (1, 0): "nometrics", (1, 1): 0}, tw)
    assert verdict.label == "missing"
    notes = " | ".join(verdict.notes)
    assert tiered_entry.NO_RESULT_REASON in notes and tiered_entry.NO_METRICS_REASON in notes


def test_parity_with_no_comparable_pairs_is_missing(tw):
    assert _pair([], {}, tw).label == "missing"


def test_parity_thresholds_change_the_verdict(tw):
    spec = {(0, 0): 3, (0, 1): 0, (1, 0): 0, (1, 1): 0}
    assert _pair(["--parity-min-gap", "4"], spec, tw).label == "ok"
    spec = {(0, 0): 4, (0, 1): 1, (1, 0): 0, (1, 1): 0}
    assert _pair(["--parity-min-ratio", "5"], spec, tw).label == "ok"


@pytest.mark.parametrize("machine,ns,emulation,expected", [
    ("arm64", None, False, False),
    ("x86_64", None, False, True),
    ("AMD64", None, False, True),
    ("arm64", "", False, True),
    ("arm64", "foo", False, False),
    ("arm64", None, True, True),
])
def test_judge_possible_architecture_rule(tw, monkeypatch, machine, ns, emulation, expected):
    monkeypatch.setattr(tiered_entry.platform, "machine", lambda: machine)
    if ns is not None:
        monkeypatch.setenv(judge.SWEBENCH_NAMESPACE_ENV, ns)
    args = _parsed(*(["--judge-emulation"] if emulation else []))
    ok, why = tiered_entry.judge_possible(args)
    assert ok is expected
    assert expected or "arm64 host" in why


def test_judge_possible_reports_why_not(tw):
    w = tw
    assert tiered_entry.judge_possible(_parsed("--skip-judge")) == (False, "--skip-judge")
    w["state"]["docker_rc"] = 1
    assert tiered_entry.judge_possible(_parsed())[1].startswith("docker: ")
    w["state"]["docker_rc"] = 0
    w["state"]["swebench"] = "0.0.1"
    assert tiered_entry.judge_possible(_parsed())[1].startswith("swebench: ")


# --- T-06: the bench group -------------------------------------------------------------

@pytest.fixture
def cli_path(monkeypatch):
    monkeypatch.setattr(quoin, "__path__", list(quoin.__path__))


def test_bench_tiered_forwards_the_remaining_arguments(cli_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tiered_entry, "main",
                        lambda argv=None, prog=None: seen.append((argv, prog)) or 7)
    rc = cli.main(["bench", "tiered", "--yes", "--run-id", "x", "--pilot-tier", "easy"])
    assert rc == 7
    assert seen == [(["--yes", "--run-id", "x", "--pilot-tier", "easy"], "quoin bench tiered")]


FLAGS = ["--yes", "--run-id", "--run-dir", "--spend-ledger", "--authorised-usd",
         "--max-budget-usd-per-task", "--parity-min-gap", "--parity-min-ratio",
         "--ignore-parity", "--judge-emulation"]


def test_bench_tiered_help_lists_the_options(cli_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bench", "tiered", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert all(flag in out for flag in FLAGS) and "quoin bench tiered" in out


def test_bench_group_help_lists_the_options(cli_path, capsys):
    assert cli.main(["bench", "--help"]) == 0
    out = capsys.readouterr().out
    assert all(flag in out for flag in FLAGS)
    assert "usage: quoin bench tiered [options]" in out


def test_top_level_help_lists_bench(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "bench" in capsys.readouterr().out


def test_unknown_bench_subcommand_is_a_usage_error(cli_path, capsys):
    assert cli.main(["bench", "other"]) == 2


@pytest.mark.parametrize("rest", [["tiered"], ["tiered", "--help"], ["--help"]])
def test_bench_without_a_checkout_says_so_and_spawns_nothing(cli_path, monkeypatch, tmp_path, capsys, rest):
    def boom(*a, **k):
        raise AssertionError("no process may start")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(cli, "_bench_candidates", lambda rest: [tmp_path])
    assert cli.main(["bench", *rest]) == 2
    assert cli._BENCH_CHECKOUT_MSG in capsys.readouterr().err


def test_bench_candidates_peek_at_repo_root():
    assert cli._bench_candidates(["--repo-root=/x/y", "--yes"])[0] == Path("/x/y")
    assert cli._bench_candidates(["--yes", "--repo-root", "/x/z"])[0] == Path("/x/z")


def test_importing_the_cli_does_not_import_the_benchmarks():
    out = subprocess.run(
        [sys.executable, "-c",
         "import quoin.cli, sys; print(any(m.startswith('quoin.benchmarks') for m in sys.modules))"],
        capture_output=True, text=True, cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert out.stdout.strip() == "False", out.stderr


# --- T-07: end to end ---------------------------------------------------------------------

def test_fresh_run_goes_prepare_estimate_confirm_pilot_parity_full_judge(tw, capsys):
    w = tw
    assert _go(w, "--yes") == 0
    out = capsys.readouterr().out
    marks = ["prepare:", "estimate:", "confirmed by --yes", "pilot:", "parity:", "full:", "judge: ran"]
    positions = [out.index(m) for m in marks]
    assert positions == sorted(positions)
    assert "worst case $33.00 for 6 sessions" in out
    assert w["calls"] == [
        {"tiers": "easy", "skip_judge": True, "quiet": True},
        {"tiers": None, "skip_judge": False, "quiet": False},
    ]
    starts = _starts(w)
    assert len(starts) == 6
    rows = _ledger(w)
    assert len([r for r in rows if r["kind"] == "reservation"]) == 6
    assert len([r for r in rows if r["kind"] == "settlement"]) == 6
    assert read_manifest(w["runs"] / "R")["tiers"] == ["easy", "medium"]
    summary = json.loads((w["runs"] / "R" / "summary.json").read_text())
    assert "medium" in json.dumps(summary["tiered"]) and "easy" in json.dumps(summary["tiered"])
    assert "results: final" in out and "summary:" in out
    assert len(w["judged"]) == 6
    _no_leftovers(w)


def test_prepare_runs_when_mirrors_are_missing(tw, capsys):
    w = tw
    shutil.rmtree(w["cache"])
    seen = []

    def fake_prepare(suite_path, tasks):
        seen.append(suite_path)
        prepare_mirrors(tasks, log=lambda *_: None)
        return 0

    w["monkeypatch"].setattr(run_benchmark, "_prepare_repos_main", fake_prepare)
    assert _go(w, "--yes") == 0
    assert len(seen) == 1 and "prepare: building" in capsys.readouterr().out


def test_prepare_is_skipped_when_mirrors_are_ready(tw, capsys):
    w = tw

    def boom(*a, **k):
        raise AssertionError("prepare must not run")

    w["monkeypatch"].setattr(run_benchmark, "_prepare_repos_main", boom)
    assert _go(w, "--yes") == 0
    assert "prepare: skipped" in capsys.readouterr().out


def test_prepare_failure_refuses_before_any_session(tw, capsys):
    w = tw
    shutil.rmtree(w["cache"])
    w["monkeypatch"].setattr(run_benchmark, "_prepare_repos_main", lambda *a: 1)
    assert _go(w, "--yes") == 2
    assert _starts(w) == []


def test_declining_the_prompt_spends_nothing(tw, monkeypatch):
    w = tw
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert _go(w) == 1
    assert _starts(w) == [] and not w["ledger"].exists()


def test_accepting_the_prompt_runs(tw, monkeypatch):
    w = tw
    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert _go(w) == 0
    assert len(_starts(w)) == 6


def test_no_terminal_without_yes_is_refused(tw, capsys):
    w = tw
    assert _go(w) == 2
    assert "REFUSED: confirm:" in capsys.readouterr().err
    assert _starts(w) == []


@pytest.mark.parametrize("case", ["auth-none", "auth-both", "cap", "ledger-other-run"])
def test_refusals_happen_before_the_prompt(tw, monkeypatch, capsys, case):
    w = tw
    monkeypatch.setattr(sys, "stdin", NoRead())
    extra = []
    clause = "auth"
    if case == "auth-none":
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    elif case == "auth-both":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    elif case == "cap":
        extra = ["--max-budget-usd-per-task", "8.0"]
        clause = "suite-ceiling"
    else:
        w["runs"].mkdir(parents=True)
        attempt = paired_spend.reserve(w["ledger"], "other", "t", "simple-claude", 5.5)
        paired_spend.settle(w["ledger"], attempt, "other", "t", "simple-claude", 5.5, 20.0)
        clause = "authorised-ceiling"
    assert _go(w, *extra) == 2
    assert f"REFUSED: {clause}:" in capsys.readouterr().err
    assert _starts(w) == []


def test_parity_stop_after_the_pilot_then_stays_stopped(tw, monkeypatch, capsys):
    w = tw
    _scenario(w, {"denials": {"plain": 3, "quoin": 0}})
    assert _go(w, "--yes") == 4
    out = capsys.readouterr().out
    assert "parity: plain 6, quoin 0 -> stop" in out
    assert len(_starts(w)) == 4
    monkeypatch.setattr(sys, "stdin", NoRead())
    assert _go(w) == 4
    assert len(_starts(w)) == 4
    assert _go(w, "--yes", "--ignore-parity") == 0
    assert len(_starts(w)) == 6


def test_ignore_parity_continues_from_a_fresh_run(tw):
    w = tw
    _scenario(w, {"denials": {"plain": 3, "quoin": 0}})
    assert _go(w, "--yes", "--ignore-parity") == 0
    assert len(_starts(w)) == 6


def test_missing_denial_data_stops_with_the_reason(tw, capsys):
    w = tw
    _scenario(w, {"runs": {"1": {"no_result": True}}})
    code = _go(w, "--yes")
    out = capsys.readouterr().out
    assert code == 4, out
    assert tiered_entry.NO_RESULT_REASON in out and "verified_easy_001" in out
    n = len(_starts(w))
    assert _go(w, "--yes") == 4 and len(_starts(w)) == n
    _scenario(w, {})
    assert _go(w, "--yes", "--ignore-parity") == 0


def test_interrupt_during_the_pilot_then_rerun_finishes_without_repaying(tw, capsys):
    w = tw
    assert _interrupted(w, 2, 2) == 1
    out = capsys.readouterr().out
    assert tiered_entry.RERUN_HINT in out
    assert spend_ledger.recorded_total(w["ledger"], "R") == pytest.approx(0.25 + 5.5)
    before = len(_starts(w))
    assert _go(w, "--yes") == 0
    out = capsys.readouterr().out
    assert "worst case $27.50 for 5 sessions" in out
    assert len(_starts(w)) - before == 5
    assert spend_ledger.recorded_total(w["ledger"], "R") == pytest.approx(7.0)
    assert read_manifest(w["runs"] / "R")["tiers"] == ["easy", "medium"]
    _no_leftovers(w)


def test_interrupt_during_the_full_leg_resumes_without_a_pilot(tw, capsys):
    w = tw
    assert _interrupted(w, 6, 6) == 1
    w["calls"].clear()
    assert _go(w, "--yes") == 0
    assert [c["tiers"] for c in w["calls"]] == [None]
    assert "parity: plain 0, quoin 0 -> ok" in capsys.readouterr().out
    _no_leftovers(w)


def test_nothing_to_do_does_not_prompt_or_spawn(tw, monkeypatch, capsys):
    w = tw
    assert _go(w, "--yes") == 0
    n = len(_starts(w))
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", NoRead())
    assert _go(w) == 0
    assert len(_starts(w)) == n
    out = capsys.readouterr().out
    assert "summary:" in out and "results: final" in out


def test_rerun_from_another_directory_finds_the_same_run(tw, monkeypatch, tmp_path):
    w = tw
    base = ["--run-dir", None]
    assert _go(w, "--yes", **{"--run-dir": None}) == 0
    default_dir = w["checkout"] / ".workflow_artifacts" / "quoin-benchmarks" / "runs" / "R"
    assert default_dir.is_dir()
    n = len(_starts(w))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _go(w, **{"--run-dir": None}) == 0
    assert len(_starts(w)) == n


def test_judging_is_deferred_with_a_runnable_command(tw, monkeypatch, capsys):
    w = tw
    w["state"]["docker_rc"] = 1
    assert _go(w, "--yes") == 0
    out = capsys.readouterr().out
    assert "judge: deferred (docker: docker info failed" in out
    commands = [l for l in out.splitlines() if l.startswith("python3 ")]
    assert len(commands) == 1
    argv = shlex.split(commands[0])
    assert Path(argv[1]).samefile(Path(run_benchmark.__file__)) and "--judge-only" in argv
    assert "results: provisional" in out
    w["state"]["docker_rc"] = 0
    monkeypatch.setattr(sys, "argv", argv[1:])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    assert exc.value.code == 0
    assert len(w["judged"]) == 6
    assert _go(w) == 0
    assert "results: final" in capsys.readouterr().out


def test_wrong_swebench_version_defers_too(tw, capsys):
    w = tw
    w["state"]["swebench"] = "0.0.1"
    assert _go(w, "--yes") == 0
    assert "judge: deferred (swebench: swebench" in capsys.readouterr().out


def test_arm64_host_defers_unless_a_namespace_or_emulation_is_given(tw, monkeypatch, capsys):
    w = tw
    monkeypatch.setattr(tiered_entry.platform, "machine", lambda: "arm64")
    assert _go(w, "--yes") == 0
    assert "judge: deferred (arm64 host without local image build or --judge-emulation)" in capsys.readouterr().out


def test_arm64_with_an_empty_namespace_judges(tw, monkeypatch, capsys):
    w = tw
    monkeypatch.setattr(tiered_entry.platform, "machine", lambda: "arm64")
    monkeypatch.setenv(judge.SWEBENCH_NAMESPACE_ENV, "")
    assert _go(w, "--yes") == 0
    assert "judge: ran" in capsys.readouterr().out


def test_arm64_with_emulation_judges(tw, monkeypatch, capsys):
    w = tw
    monkeypatch.setattr(tiered_entry.platform, "machine", lambda: "arm64")
    assert _go(w, "--yes", "--judge-emulation") == 0
    assert "judge: ran" in capsys.readouterr().out


def test_losing_the_judge_prerequisites_after_the_pilot_defers_instead_of_refusing(tw, capsys):
    w = tw
    w["hooks"].append(lambda kw: w["state"].update(docker_rc=1) if kw.get("quiet_deferred_hint") else None)
    assert _go(w, "--yes") == 0
    captured = capsys.readouterr()
    assert w["calls"][1]["skip_judge"] is True
    assert "REFUSED: docker" not in captured.err
    assert "judge: deferred" in captured.out


def test_resume_refusals_run_against_an_incomplete_run(tw, capsys):
    w = tw
    assert _interrupted(w, 2, 2) == 1
    n = len(_starts(w))
    capsys.readouterr()
    assert _go(w, "--yes", "--max-budget-usd-per-task", "5.0") == 2
    assert "REFUSED: resume:" in capsys.readouterr().err
    assert _go(w, "--yes", **{"--expected-quoin-commit": "0" * 40}) == 2
    assert "REFUSED: resume:" in capsys.readouterr().err
    suite = json.loads(w["suite"].read_text())
    suite["tiered"]["dataset_revision"] = "other"
    w["suite"].write_text(json.dumps(suite))
    assert _go(w, "--yes") == 2
    assert "REFUSED: resume: suite_sha differs" in capsys.readouterr().err
    assert len(_starts(w)) == n


def test_no_second_prompt_past_the_pilot_phase_for_a_respawnable_easy_row(tw, monkeypatch):
    w = tw
    assert _go(w, "--yes") == 0
    shutil.rmtree(w["runs"] / "R" / "quoin-claude" / "verified_easy_001")
    n = len(_starts(w))
    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert _go(w) == 0
    assert len(_starts(w)) == n + 1


def test_a_grown_remainder_in_the_pilot_phase_asks_again(tw, monkeypatch, capsys):
    w = tw
    grown = []
    w["hooks"].append(lambda kw: grown.append(True) if kw.get("quiet_deferred_hint") else None)
    real = run_benchmark._paired_rows_to_spawn
    monkeypatch.setattr(
        run_benchmark, "_paired_rows_to_spawn",
        lambda *a, **k: (lambda n: n + 1 if grown and n is not None else n)(real(*a, **k)))
    monkeypatch.setattr(sys, "stdin", Tty("y\nn\n"))
    assert _go(w) == 1
    out = capsys.readouterr().out
    assert out.count("Spend up to") == 2 and "declined" in out
    assert [c["tiers"] for c in w["calls"]] == ["easy"]
