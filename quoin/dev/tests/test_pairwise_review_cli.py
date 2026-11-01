"""Tests for the pairwise review command line. A fake reviewer is injected; nothing is spawned."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.harness import pairwise_review as pr
from quoin.benchmarks.scripts import pairwise_review as cli
from quoin.benchmarks.scripts import spend_ledger

SCRIPT = Path(cli.__file__).resolve()
REPO = SCRIPT.parents[3]
GOOD = {
    "correctness_risk_a": 2, "correctness_risk_b": 3, "minimality_a": 4, "minimality_b": 3,
    "test_quality_a": 5, "test_quality_b": 1, "preference": "B", "rationale": "ok",
}
TASKS = ("t1", "t2", "t3")


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch):
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def guarded_run(argv, *a, **k):
        # The --help smoke test runs python itself; it can never reach a reviewer.
        if list(argv)[:1] == [sys.executable] and "--help" in argv:
            monkeypatch.setattr(pr.subprocess, "Popen", real_popen)
            return real_run(argv, *a, **k)
        raise AssertionError("a real subprocess call was attempted")

    def boom(*a, **k):
        raise AssertionError("a real subprocess call was attempted")

    monkeypatch.setattr(pr.subprocess, "run", guarded_run)
    monkeypatch.setattr(pr.subprocess, "Popen", boom)


class Fake:
    def __init__(self):
        self.calls = 0

    def review(self, prompt, *, model, max_budget_usd, timeout_s):
        self.calls += 1
        self.last = {"model": model, "cap": max_budget_usd}
        return pr.ReviewReply("```json\n" + json.dumps(GOOD) + "\n```", 0.25, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs"
    run = run_dir / "R"
    suite = {
        "tiered": {
            "pairwise_review_cap_usd": 2, "pairwise_review_total_cap_usd": 80,
            "worst_case_ceiling_usd": 3170,
        },
        "tasks": [{"id": t, "tier": "easy", "description": f"Issue {t}"} for t in TASKS],
    }
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    for t in TASKS:
        for cell, n in (("simple-claude", 1), ("quoin-claude", 3)):
            d = run / cell / t
            d.mkdir(parents=True)
            (d / "session.json").write_text('{"schema_version": 1}')
            body = "".join(f"+l{i}\n" for i in range(n))
            (d / "diff.patch").write_text(
                f"diff --git a/src/{t}.py b/src/{t}.py\n--- a/src/{t}.py\n+++ b/src/{t}.py\n@@ -0,0 +1,{n} @@\n{body}"
            )
    ledger = tmp_path / "ledger.jsonl"

    def manifest(**over):
        fields = {
            "run_id": "R", "suite_path": str(suite_path), "simple_claude_model": "opus-x",
            "effort": "high", "authorised_usd": "3170.0", "spend_ledger": str(ledger),
        }
        fields.update(over)
        (run / "run-manifest.yaml").write_text(
            "".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None)
        )

    manifest()
    fake = Fake()
    monkeypatch.setattr(cli, "make_reviewer", lambda effort, run_id: fake)
    return {"run_dir": run_dir, "run": run, "ledger": ledger, "fake": fake,
            "manifest": manifest, "suite": suite, "suite_path": suite_path, "tmp": tmp_path}


def args(env, *extra):
    return ["--run-id", "R", "--run-dir", str(env["run_dir"]), *extra]


def seed_arm_spend(env, total):
    spend_ledger.append(env["ledger"], {
        "ts": "x", "attempt_id": "R#quoin-claude#t1#u", "kind": "settlement", "invocation": "paired",
        "gate_id": "g", "arm": "quoin", "cap_usd": 35.0, "actual_usd": total, "run_id": "R",
        "new_ceiling_usd": None, "note": "",
    })


def test_defaults_come_from_the_manifest_and_suite(env, capsys):
    assert cli.main(args(env, "--yes")) == 0
    assert env["fake"].calls == 3
    assert env["fake"].last == {"model": "opus-x", "cap": 2.0}
    out = capsys.readouterr().out
    assert "authorised ceiling: $3170.00 (from manifest)" in out
    state = json.loads((env["run"] / pr.STATE_FILE).read_text())
    assert state["effort"] == "high" and state["caps"]["total_usd"] == 80.0
    assert env["ledger"].exists()


def test_missing_cap_is_refused(env, capsys):
    del env["suite"]["tiered"]["pairwise_review_cap_usd"]
    env["suite_path"].write_text(json.dumps(env["suite"]))
    assert cli.main(args(env, "--yes")) == 2
    assert "cap-set" in capsys.readouterr().err and env["fake"].calls == 0
    assert cli.main(args(env, "--yes", "--max-review-usd", "1.5")) == 0


def test_effort_fallback_and_refusal(env, capsys):
    env["manifest"](effort=None)
    assert cli.main(args(env, "--yes")) == 2
    assert "effort-set" in capsys.readouterr().err and env["fake"].calls == 0
    assert cli.main(args(env, "--yes", "--effort", "low")) == 0 and env["fake"].calls == 3


def test_partial_runs_are_refused_unless_allowed(env, capsys):
    import shutil

    shutil.rmtree(env["run"] / "quoin-claude" / "t3")
    assert cli.main(args(env, "--yes")) == 2
    assert "run-unfinished" in capsys.readouterr().err and env["fake"].calls == 0
    assert cli.main(args(env, "--yes", "--allow-partial")) == 0
    assert env["fake"].calls == 2


def test_dry_run_writes_nothing_and_builds_no_reviewer(env, monkeypatch, capsys):
    def boom(*a):
        raise AssertionError("no reviewer in a dry run")

    monkeypatch.setattr(cli, "make_reviewer", boom)
    assert cli.main(args(env, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "quoin is" in out and "WORST CASE review spend: $6.00" in out
    assert not env["ledger"].exists() and not (env["run"] / pr.STATE_FILE).exists()


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_declined_prompt_exits_2_with_no_calls(env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert cli.main(args(env)) == 2 and env["fake"].calls == 0
    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert cli.main(args(env)) == 0 and env["fake"].calls == 3


def test_no_terminal_without_yes_is_refused(env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(args(env)) == 2 and env["fake"].calls == 0


def test_higher_cap_run_reviews_under_the_manifest_ceiling(env):
    env["manifest"](authorised_usd="4000.0")
    seed_arm_spend(env, 3200.0)
    assert cli.main(args(env, "--yes")) == 0
    state = json.loads((env["run"] / pr.STATE_FILE).read_text())
    assert [v["status"] for v in state["tasks"].values()] == ["reviewed"] * 3


def test_flag_ceiling_overrides_the_manifest(env):
    env["manifest"](authorised_usd="4000.0")
    seed_arm_spend(env, 3200.0)
    assert cli.main(args(env, "--yes", "--authorised-usd", "3170")) == 3
    assert env["fake"].calls == 0


def test_suite_ceiling_is_used_when_the_manifest_has_none(env, capsys):
    env["manifest"](authorised_usd=None)
    assert cli.main(args(env, "--yes")) == 0
    assert "(from suite)" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["abc", "-5", "nan", "0"])
def test_bad_manifest_ceiling_is_refused(env, capsys, bad):
    env["manifest"](authorised_usd=bad)
    assert cli.main(args(env, "--yes")) == 2
    assert "authorised-set" in capsys.readouterr().err and env["fake"].calls == 0


def test_ledger_default_is_the_manifest_ledger(env):
    assert cli.main(args(env, "--yes")) == 0
    assert env["ledger"].exists()
    other = env["tmp"] / "other.jsonl"
    env["fake"].calls = 0
    assert cli.main(args(env, "--yes", "--spend-ledger", str(other))) == 0
    assert not other.exists() or env["fake"].calls == 0


def test_unusable_auth_is_refused_before_any_spend(env, monkeypatch, capsys):
    def no_auth(effort, run_id):
        raise ValueError("no auth variable")

    monkeypatch.setattr(cli, "make_reviewer", no_auth)
    assert cli.main(args(env, "--yes")) == 2
    assert "auth" in capsys.readouterr().err and not env["ledger"].exists()


def test_help_runs_as_a_plain_script():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0 and "--run-id" in proc.stdout


def test_run_refreshes_the_summary(env, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_refresh_summary", lambda *a: calls.append(a))
    assert cli.main(args(env, "--yes")) == 0 and len(calls) == 1


def test_summary_files_gain_the_review_after_a_run(tmp_path, monkeypatch):
    from aggregate_fixtures import build_tiered_run, write_manifest, suite_sha

    monkeypatch.setattr(cli, "make_reviewer", lambda effort, run_id: Fake())
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run = run_dir / run_id
    for cell, n in (("simple-claude", 1), ("quoin-claude", 3)):
        (run / cell / "e1" / "diff.patch").write_text(
            f"diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -0,0 +1,{n} @@\n" + "+x\n" * n
        )
    for cell in ("simple-claude", "quoin-claude"):
        for task in ("e2", "m1", "m2", "l1"):
            d = run / cell / task
            if d.is_dir():
                (d / "session.json").write_text('{"schema_version": 1}')
        (run / cell / "e1" / "session.json").write_text('{"schema_version": 1}')
    suite = json.loads(Path(suite_path).read_text())
    suite["tasks"] = [t for t in suite["tasks"] if t["id"] == "e1"]
    Path(suite_path).write_text(json.dumps(suite))
    write_manifest(run, run_id=run_id, suite_path=suite_path, suite_sha=suite_sha(suite_path),
                   simple_claude_model="opus-x", effort="high", spend_ledger=tmp_path / "l.jsonl")
    rc = cli.main(["--run-id", run_id, "--run-dir", str(run_dir), "--yes",
                   "--max-review-usd", "2", "--max-review-total-usd", "10", "--authorised-usd", "100"])
    assert rc == 0
    summary = json.loads((run / "summary.json").read_text())
    assert summary["pairwise_review"]["counts"]["quoin_preferred"] + \
        summary["pairwise_review"]["counts"]["plain_preferred"] == 1
    assert "## Blind pairwise review (secondary measure)" in (run / "summary.md").read_text()
