"""CLI tests for the paired benchmark path, run in-process with no model, Docker or network."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from quoin.benchmarks.harness import arm_identity, arm_sandbox, judge, paired_run, repo_prep, runner
from quoin.benchmarks.harness.judge import JudgeResult
from quoin.benchmarks.harness.repo_prep import pin_ref_status_offline, prepare_mirrors
from quoin.benchmarks.harness.tiered_report import read_manifest
from quoin.benchmarks.scripts import run_benchmark
from fakes.fake_arm import fake_install_quoin
from verified_standin import build_upstream, configure_env, git

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "quoin" / "benchmarks"
PAIR = "simple-claude,quoin-claude"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def sandbox_parent(tmp_path, monkeypatch):
    parent = tmp_path / "sandbox-parent"
    parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(parent))
    return parent


@pytest.fixture
def arm_repo(tmp_path):
    """A throwaway git repository holding the installer and hook scripts of this checkout."""
    root = tmp_path / "arm"
    (root / "src" / "quoin").mkdir(parents=True)
    shutil.copy(REPO / "src" / "quoin" / "installer.py", root / "src" / "quoin" / "installer.py")
    shutil.copytree(REPO / "quoin" / "hooks", root / "quoin" / "hooks")
    git(["init", "-q", "-b", "main"], root)
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", "arm"], root)
    return {"root": root, "head": git(["rev-parse", "HEAD"], root)}


@pytest.fixture
def paired_arm_stubs(monkeypatch):
    monkeypatch.setattr(arm_sandbox, "install_quoin", fake_install_quoin)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")


@pytest.fixture
def standin(tmp_path, monkeypatch):
    up = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, up)
    prepare_mirrors([up["task"]], log=lambda *_: None)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "tiered": {
            "paired_run": True, "dataset_revision": "rev",
            "worst_case_target_usd": 100, "worst_case_ceiling_usd": 110,
            "tiers": {"easy": {"n": 1}},
        },
        "tasks": [up["task"]],
    }))
    return {**up, "cache": cache, "suite": suite}


@pytest.fixture
def docker_ok(monkeypatch):
    real = subprocess.run

    def fake(argv, *a, **k):
        if argv and argv[0] == "docker":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(judge, "probe_swebench_version", lambda *a, **k: judge.SWEBENCH_PINNED_VERSION)


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", *argv])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    return exc.value.code


def _args(tmp_path, arm_repo, suite="tiered-small", **over):
    args = {
        "--suite": str(suite), "--run-id": "R", "--run-dir": str(tmp_path / "runs"),
        "--cells": PAIR, "--max-budget-usd-per-task": "5.5",
        "--spend-ledger": str(tmp_path / "ledger.jsonl"), "--authorised-usd": "200",
        "--arm-root": str(arm_repo["root"]), "--expected-quoin-commit": arm_repo["head"],
    }
    args.update(over)
    out = []
    for key, value in args.items():
        if value is None:
            continue
        out += [key] if value is True else [key, value]
    return out


def _no_leftovers(sandbox_parent):
    assert list(sandbox_parent.rglob("qbench_sandbox_*")) == []
    assert list(sandbox_parent.glob("qbench_run_*")) == []


REFUSALS = [
    ("cap", {"--max-budget-usd-per-task": None}, "cap-set"),
    ("ledger", {"--spend-ledger": None}, "ledger-set"),
    ("authorised", {"--authorised-usd": None}, "authorised-set"),
    ("cap-too-high", {"--max-budget-usd-per-task": "100"}, "suite-ceiling"),
    ("authorised-low", {"--authorised-usd": "50"}, "authorised-ceiling"),
    ("codex-cell", {"--cells": "simple-claude,quoin-codex"}, "cells"),
    ("default-cells", {"--cells": "simple-claude,quoin-claude,simple-codex,quoin-codex"},
     "--cells simple-claude,quoin-claude"),
    ("fixture-repo", {"--fixture-repo": "/nowhere"}, "fixture-repo"),
]


@pytest.mark.parametrize("name,over,clause", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_paid_run_refusals_leave_no_trace(monkeypatch, capsys, tmp_path, arm_repo, auth,
                                          sandbox_parent, name, over, clause):
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, **over), "--skip-judge")
    assert code == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and clause in err
    _no_leftovers(sandbox_parent)
    assert not (tmp_path / "runs" / "R" / "run-manifest.yaml").exists()


@pytest.mark.parametrize("env", ["both", "none"])
def test_auth_refusals(monkeypatch, capsys, tmp_path, arm_repo, sandbox_parent, env):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if env == "both":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "b")
    assert _main(monkeypatch, *_args(tmp_path, arm_repo), "--skip-judge") == 2
    assert "REFUSED: auth" in capsys.readouterr().err
    _no_leftovers(sandbox_parent)


def test_missing_pin_ref_refuses(monkeypatch, capsys, tmp_path, arm_repo, auth, sandbox_parent):
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(tmp_path / "empty-cache"))
    assert _main(monkeypatch, *_args(tmp_path, arm_repo), "--skip-judge") == 2
    assert "REFUSED: pin-refs" in capsys.readouterr().err
    _no_leftovers(sandbox_parent)


def test_dirty_arm_tree_refuses_with_the_real_check(monkeypatch, capsys, tmp_path, arm_repo, auth,
                                                    standin, sandbox_parent):
    (arm_repo["root"] / "quoin" / "stray.txt").write_text("x")
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge")
    assert code == 2
    assert "REFUSED: arm-tree: dirty-tree" in capsys.readouterr().err
    _no_leftovers(sandbox_parent)


def _complete(monkeypatch, outcome=None):
    monkeypatch.setattr(
        paired_run, "run_paired",
        lambda plan, **kw: outcome or paired_run.PairedOutcome(pairs_completed=len(plan.tasks)),
    )


def test_skip_judge_complete_run_exits_zero(monkeypatch, capsys, tmp_path, arm_repo, auth, standin,
                                            paired_arm_stubs, sandbox_parent):
    _complete(monkeypatch)
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge")
    assert code == 0
    out = capsys.readouterr().out
    assert "Judging deferred: 0 rows pending" in out and "Max parallel: 1" in out
    manifest = read_manifest(tmp_path / "runs" / "R")
    assert manifest["paired_mode"] == "True" and manifest["judge_phase"] == "skipped"
    assert manifest["pair_order"] == ["simple-claude", "quoin-claude"]
    assert manifest["contamination_checks"] == "static-passed; runtime-passed"
    assert "test-token" not in (tmp_path / "runs" / "R" / "run-manifest.yaml").read_text()
    _no_leftovers(sandbox_parent)


def test_skip_judge_abort_exits_one(monkeypatch, tmp_path, arm_repo, auth, standin,
                                    paired_arm_stubs, sandbox_parent):
    _complete(monkeypatch, paired_run.PairedOutcome(stop_reason="abort", abort_detail=["auth-failure"]))
    assert _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge") == 1
    _no_leftovers(sandbox_parent)


def test_spend_stop_exits_three(monkeypatch, tmp_path, arm_repo, auth, standin, paired_arm_stubs,
                                sandbox_parent):
    _complete(monkeypatch, paired_run.PairedOutcome(stop_reason="usd-cap", abort_detail=["cap"]))
    assert _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge") == 3


def test_interrupt_exits_one_and_records_it(monkeypatch, tmp_path, arm_repo, auth, standin,
                                            paired_arm_stubs, sandbox_parent):
    def interrupted(plan, **kw):
        raise paired_run.PairedInterrupted("SIGTERM")

    monkeypatch.setattr(paired_run, "run_paired", interrupted)
    assert _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge") == 1
    assert read_manifest(tmp_path / "runs" / "R")["interrupted"] == "SIGTERM"
    _no_leftovers(sandbox_parent)


def _resume_setup(tmp_path, standin, stored_cap, commit=None):
    runs = tmp_path / "runs"
    run_benchmark._write_manifest(
        runs, "R", standin["suite"], list(paired_run.PAIR_ORDER), 1, False, REPO,
        extra={"max_budget_usd_per_task": stored_cap, "tiers": ["easy"],
               "arm_config_mode": "symmetric", "expected_quoin_commit": commit},
    )
    manifest = runs / "R" / "run-manifest.yaml"
    manifest.write_text(manifest.read_text().replace(
        next(l for l in manifest.read_text().splitlines() if l.startswith("suite_sha")),
        f"suite_sha: {run_benchmark._suite_sha(standin['suite'])}"))
    row = runs / "R" / "simple-claude" / standin["task"]["id"]
    row.mkdir(parents=True)
    (row / "session.json").write_text(json.dumps({"verdict": "pass"}))


def test_judge_phase_runs_and_every_row_is_final(monkeypatch, tmp_path, arm_repo, auth, standin,
                                                 paired_arm_stubs, docker_ok, sandbox_parent):
    task_id = standin["task"]["id"]
    _resume_setup(tmp_path, standin, "5.5", arm_repo["head"])
    runs = tmp_path / "runs" / "R"
    (runs / "simple-claude" / task_id / "session.json").write_text(
        json.dumps({"judge_required": True, "verdict": "error"}))
    quoin_row = runs / "quoin-claude" / task_id
    quoin_row.mkdir(parents=True)
    (quoin_row / "session.json").write_text(json.dumps({"judge_required": False}))
    _complete(monkeypatch)

    def stub(task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
             task_spec=None, judge_context=None):
        return JudgeResult(task_id, "swebench_verified", "pass", "ev", 0.0, extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", stub)
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--resume")
    assert code == 0


def test_resume_with_different_cap_refuses(monkeypatch, capsys, tmp_path, arm_repo, auth, standin,
                                           paired_arm_stubs, sandbox_parent):
    _resume_setup(tmp_path, standin, "5.50", arm_repo["head"])
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"],
                                     **{"--max-budget-usd-per-task": "5.0"}),
                 "--skip-judge", "--resume")
    assert code == 2 and "REFUSED: resume" in capsys.readouterr().err
    _no_leftovers(sandbox_parent)


def test_resume_with_the_same_cap_written_differently_continues(monkeypatch, tmp_path, arm_repo,
                                                                auth, standin, paired_arm_stubs,
                                                                sandbox_parent):
    _resume_setup(tmp_path, standin, "5.50", arm_repo["head"])
    _complete(monkeypatch)
    assert _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]),
                 "--skip-judge", "--resume") == 0


def test_resume_with_a_different_quoin_commit_refuses_before_any_side_effect(
        monkeypatch, capsys, tmp_path, arm_repo, auth, standin, paired_arm_stubs, sandbox_parent):
    _resume_setup(tmp_path, standin, "5.5", "f" * 40)
    before = (tmp_path / "runs" / "R" / "run-manifest.yaml").read_text()
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]),
                 "--skip-judge", "--resume")
    err = capsys.readouterr().err
    assert code == 2 and "REFUSED: resume" in err and "quoin commit" in err
    assert (tmp_path / "runs" / "R" / "run-manifest.yaml").read_text() == before
    _no_leftovers(sandbox_parent)


def test_tiers_flag_is_refused_on_a_suite_that_is_not_paired(monkeypatch, capsys, tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"tiered": {"tiers": {"easy": {"n": 1}}}, "tasks": []}))
    assert _main(monkeypatch, "--suite", str(suite), "--run-id", "R", "--tiers", "easy") == 2
    assert "--tiers applies only to paired suites" in capsys.readouterr().err


def test_existing_rows_without_resume_refuse(monkeypatch, capsys, tmp_path, arm_repo, auth, standin,
                                             sandbox_parent):
    _resume_setup(tmp_path, standin, "5.5", arm_repo["head"])
    code = _main(monkeypatch, *_args(tmp_path, arm_repo, suite=standin["suite"]), "--skip-judge")
    assert code == 2 and "REFUSED: run-dir" in capsys.readouterr().err


def test_skip_judge_on_a_legacy_suite_exits_two(monkeypatch):
    assert _main(monkeypatch, "--suite", str(BENCH / "suite-v1.json"), "--run-id", "R", "--skip-judge") == 2


def test_fake_installer_output_passes_the_real_sandbox_check(tmp_path, arm_repo):
    box = arm_sandbox.create_sandbox("quoin", parent=tmp_path)
    try:
        assert fake_install_quoin(box, arm_repo["root"], arm_repo["head"])["ok"]
        assert arm_identity.check_quoin_sandbox(box.home, arm_repo["root"]) == []
    finally:
        arm_sandbox.remove_sandbox(box)


def test_pin_ref_status_offline_reads_packed_refs(tmp_path, monkeypatch):
    task = {"repo": "org/repo", "source_id": "org__repo-1"}
    mirror = tmp_path / "org__repo.git"
    mirror.mkdir()
    sha = "a" * 40
    (mirror / "packed-refs").write_text(
        f"# pack-refs\n{sha} {repo_prep.PIN_REF_PREFIX}org__repo-1\n^{'b' * 40}\n")
    assert pin_ref_status_offline(task, tmp_path) == sha
    assert pin_ref_status_offline({"repo": "org/other", "source_id": "org__repo-1"}, tmp_path) is None


def _dry(monkeypatch, capsys, *extra, suite="tiered-small"):
    def boom(*a, **k):
        raise AssertionError("a process was started by the dry run")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    code = _main(monkeypatch, "--suite", suite, "--run-id", "D", "--dry-run", *extra)
    return code, capsys.readouterr()


def test_dry_run_prints_the_paired_matrix(monkeypatch, capsys):
    code, cap = _dry(monkeypatch, capsys, "--max-budget-usd-per-task", "90")
    assert code == 0
    assert "(9 tasks)" in cap.out
    assert sum(1 for l in cap.out.splitlines() if l.startswith("verified_")) == 18
    assert "WORST CASE: $1620.00" in cap.out
    assert "Target: $1620.00, enforced ceiling: $1790.00" in cap.out


def test_dry_run_tier_filter(monkeypatch, capsys):
    code, cap = _dry(monkeypatch, capsys, "--max-budget-usd-per-task", "90", "--tiers", "easy")
    assert code == 0
    assert sum(1 for l in cap.out.splitlines() if l.startswith("verified_")) == 8
    assert "WORST CASE: $720.00" in cap.out


def test_dry_run_without_cap_is_gate_stopped(monkeypatch, capsys):
    code, cap = _dry(monkeypatch, capsys)
    assert code == 2 and "UNBOUNDED" in cap.out and "GATE-STOP" in cap.err


def test_unknown_tier_exits_two(monkeypatch, capsys):
    code, cap = _dry(monkeypatch, capsys, "--tiers", "huge")
    assert code == 2 and "unknown tier" in cap.err


def test_tiers_on_a_legacy_suite_exits_two(monkeypatch, capsys):
    code, cap = _dry(monkeypatch, capsys, "--tiers", "easy", suite=str(BENCH / "suite-v1.json"))
    assert code == 2


def test_legacy_gate_check_keeps_its_ceiling():
    assert run_benchmark.dry_run_gate_check(1.0, 38.0)[0]
    assert not run_benchmark.dry_run_gate_check(1.0, 39.0)[0]
    assert run_benchmark.dry_run_gate_check(1.0, 99.0, worst_case_ceiling=110.0)[0]


def test_wall_clock_resolution_order():
    task = {"wall_clock_seconds": 1800}
    assert paired_run.resolve_wall_clock(120, task) == 120.0
    assert paired_run.resolve_wall_clock(None, task) == 1800.0
    assert paired_run.resolve_wall_clock(None, {}) == 600.0


def _manifest_args(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(run_benchmark, "_git_sha", lambda root: "abc1234")
    monkeypatch.setattr(run_benchmark, "_probe_claude_model", lambda: "model-x")
    monkeypatch.setattr(run_benchmark, "_probe_codex_model", lambda: "codex-x")
    suite = tmp_path / "suite.json"
    suite.write_text("{}")
    return run_benchmark._write_manifest(
        tmp_path / "runs", "R", suite, ["simple-claude"], 4, False, tmp_path,
        started_at="2026-01-01T00:00:00Z", **kw,
    )


def test_manifest_without_extra_is_unchanged(tmp_path, monkeypatch):
    plain = _manifest_args(tmp_path, monkeypatch).read_text()
    assert _manifest_args(tmp_path, monkeypatch, extra=None).read_text() == plain
    assert _manifest_args(tmp_path, monkeypatch, extra={}).read_text() == plain
    lines = plain.splitlines()
    assert lines[0] == "run_id: R" and lines[1] == 'started_at: "2026-01-01T00:00:00Z"'
    assert lines[-1].startswith("gate_auto_approve_env:")
    assert "paired_mode" not in plain


def test_manifest_extra_round_trips(tmp_path, monkeypatch):
    path = _manifest_args(tmp_path, monkeypatch, extra={
        "paired_mode": True, "tiers": ["easy", "large"], "by_tier": {"easy": 1800},
        "max_budget_usd_per_task": 5.5, "note": None,
    })
    data = read_manifest(path.parent)
    assert data["tiers"] == ["easy", "large"]
    assert json.loads(data["by_tier"]) == {"easy": 1800}
    assert float(data["max_budget_usd_per_task"]) == 5.5 and data["note"] is None
    assert data["started_at"] == "2026-01-01T00:00:00Z"


def _run_stub(monkeypatch, behaviour):
    def fake(argv, *a, **k):
        if argv and argv[0] == "docker":
            if isinstance(behaviour, Exception):
                raise behaviour
            return subprocess.CompletedProcess(argv, behaviour, b"", b"")
        raise AssertionError(f"unexpected subprocess {argv}")

    monkeypatch.setattr(subprocess, "run", fake)


def test_judge_prereqs_docker_did_not_run(monkeypatch):
    _run_stub(monkeypatch, OSError("no docker"))
    assert run_benchmark._judge_prereqs()[:3] == (
        False, "docker", "docker info did not run; use --skip-judge to defer judging")


def test_judge_prereqs_docker_failed(monkeypatch):
    _run_stub(monkeypatch, 1)
    assert run_benchmark._judge_prereqs()[:3] == (
        False, "docker", "docker info failed; use --skip-judge to defer judging")


def test_judge_prereqs_wrong_swebench_version(monkeypatch):
    _run_stub(monkeypatch, 0)
    monkeypatch.setattr(judge, "probe_swebench_version", lambda *a, **k: "0.0.1")
    ok, clause, detail, version = run_benchmark._judge_prereqs()
    assert (ok, clause, version) == (False, "swebench", "0.0.1")
    assert detail == f"swebench {judge.SWEBENCH_PINNED_VERSION} is required, found 0.0.1"


def test_judge_prereqs_success(monkeypatch):
    _run_stub(monkeypatch, 0)
    monkeypatch.setattr(judge, "probe_swebench_version", lambda *a, **k: judge.SWEBENCH_PINNED_VERSION)
    assert run_benchmark._judge_prereqs() == (True, "", "", judge.SWEBENCH_PINNED_VERSION)


def test_build_parser_resolves_spend_ledger_to_an_absolute_path():
    args = run_benchmark.build_parser().parse_args(["--run-id", "R", "--spend-ledger", "rel/ledger.jsonl"])
    assert args.spend_ledger.is_absolute()


def test_quiet_keyword_is_forwarded_to_execute(monkeypatch, tmp_path, arm_repo, auth, standin):
    seen = {}

    def spy(args, suite_path, doc, pre, *, quiet_deferred_hint=False):
        seen["quiet"] = quiet_deferred_hint
        return 0

    monkeypatch.setattr(run_benchmark, "_paired_execute", spy)
    monkeypatch.setattr(arm_sandbox, "check_arm_tree", lambda *a, **k: [])
    argv = _args(tmp_path, arm_repo, suite=standin["suite"]) + ["--skip-judge"]
    args = run_benchmark.build_parser().parse_args(argv)
    doc = json.loads(standin["suite"].read_text())
    assert run_benchmark._paired_main(args, standin["suite"], doc, quiet_deferred_hint=True) == 0
    assert seen["quiet"] is True
    assert run_benchmark._paired_main(args, standin["suite"], doc) == 0
    assert seen["quiet"] is False


def test_quiet_skip_judge_run_prints_no_deferred_line(monkeypatch, capsys, tmp_path, arm_repo, auth,
                                                      standin, paired_arm_stubs, sandbox_parent):
    _complete(monkeypatch)
    argv = _args(tmp_path, arm_repo, suite=standin["suite"]) + ["--skip-judge"]
    args = run_benchmark.build_parser().parse_args(argv)
    doc = json.loads(standin["suite"].read_text())
    assert run_benchmark._paired_main(args, standin["suite"], doc, quiet_deferred_hint=True) == 0
    assert "Judging deferred" not in capsys.readouterr().out


def _widen_check(tmp_path, monkeypatch, stored, requested):
    extra = {"max_budget_usd_per_task": 5.5, "arm_config_mode": "symmetric"}
    if stored is not None:
        extra["tiers"] = stored
    _manifest_args(tmp_path, monkeypatch, extra=extra)
    return run_benchmark._check_resume_manifest(
        tmp_path / "runs", "R", tmp_path / "suite.json", 5.5, requested)


@pytest.mark.parametrize("stored,requested", [
    (["easy"], ["easy", "medium"]),
    (["easy", "medium"], ["easy", "medium"]),
])
def test_resume_may_add_tiers(tmp_path, monkeypatch, stored, requested):
    assert _widen_check(tmp_path, monkeypatch, stored, requested) is None


@pytest.mark.parametrize("stored,requested", [
    (["easy", "medium"], ["easy"]),
    (["easy"], ["medium"]),
    (None, ["easy"]),
    ([], ["easy"]),
])
def test_resume_may_not_drop_tiers(tmp_path, monkeypatch, stored, requested):
    why = _widen_check(tmp_path, monkeypatch, stored, requested)
    assert why and "tiers differ from the stored manifest" in why
