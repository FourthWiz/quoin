"""End-to-end offline tests of the paired command.

Every case calls ``run_benchmark.main()`` in-process against a stand-in suite
of three tasks, with a fake ``claude`` on PATH, a fake quoin installer, and
the judge, Docker and SWE-bench probes stubbed. The only child processes are
the fake shim and git.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quoin.benchmarks.harness import arm_sandbox, judge, runner
from quoin.benchmarks.harness.judge import JudgeResult
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.harness.session_state import is_judge_pending
from quoin.benchmarks.harness.tiered_report import read_manifest
from quoin.benchmarks.scripts import run_benchmark
from fakes.fake_arm import fake_install_quoin
from fakes.fake_claude import PLATFORM_INJECTED_ENV, make_paired_fake_claude, read_paired_log
from test_run_benchmark_paired_cli import _no_network, arm_repo, auth, sandbox_parent  # noqa: F401
from verified_standin import build_upstream, configure_env

PAIR = "simple-claude,quoin-claude"
ARMS = ("simple-claude", "quoin-claude")
REAL_CHECK_ARM_TREE = arm_sandbox.check_arm_tree
CEILING = 33.0  # 3 tasks x 2 arms x the 5.5 cap


@pytest.fixture
def world(tmp_path, monkeypatch, arm_repo, auth, sandbox_parent):  # noqa: F811
    up = build_upstream(tmp_path)
    tasks = [
        dict(up["task"], id=f"verified_easy_00{i}", source_id=f"org__repo-{i}", wall_clock_seconds=20)
        for i in (1, 2, 3)
    ]
    cache = configure_env(monkeypatch, tmp_path, up)
    prepare_mirrors(tasks, log=lambda *_: None)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "tiered": {
            "paired_run": True, "dataset_revision": "rev",
            "worst_case_target_usd": CEILING, "worst_case_ceiling_usd": CEILING,
            "tiers": {"easy": {"n": 3}},
        },
        "tasks": tasks,
    }))

    monkeypatch.setattr(arm_sandbox, "check_arm_tree", lambda *a, **k: [])
    monkeypatch.setattr(arm_sandbox, "install_quoin", fake_install_quoin)
    real_run = subprocess.run

    def fake_docker(argv, *a, **k):
        if argv and argv[0] == "docker":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_docker)
    monkeypatch.setattr(judge, "probe_swebench_version", lambda *a, **k: judge.SWEBENCH_PINNED_VERSION)
    judged = []

    def judge_stub(task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
                   task_spec=None, judge_context=None):
        judged.append((Path(task_dir).parent.name, task_id))
        return JudgeResult(task_id, "swebench_verified", "pass", "ev", 0.0, extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", judge_stub)

    bin_dir, log = make_paired_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return {
        "tmp": tmp_path, "suite": suite, "tasks": tasks, "arm_repo": arm_repo, "runs": tmp_path / "runs",
        "ledger": tmp_path / "ledger.jsonl", "bin": bin_dir, "log": log, "judged": judged,
        "sandbox_parent": sandbox_parent, "monkeypatch": monkeypatch,
    }


def _argv(w, *extra, **over):
    args = {
        "--suite": str(w["suite"]), "--run-id": "R", "--run-dir": str(w["runs"]), "--cells": PAIR,
        "--max-budget-usd-per-task": "5.5", "--spend-ledger": str(w["ledger"]),
        "--authorised-usd": "200", "--arm-root": str(w["arm_repo"]["root"]),
        "--expected-quoin-commit": w["arm_repo"]["head"],
    }
    args.update(over)
    out = []
    for key, value in args.items():
        if value is not None:
            out += [key, value]
    return out + list(extra)


def _run(w, *extra, **over):
    w["monkeypatch"].setattr(sys, "argv", ["run_benchmark.py", *_argv(w, *extra, **over)])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    return exc.value.code


def _scenario(w, scenario):
    make_paired_fake_claude(w["tmp"], scenario)


def _starts(w):
    return [e for e in read_paired_log(w["log"]) if e["event"] == "start"]


def _arm_of(start):
    return "simple-claude" if "_plain_" in start["config_dir"] else "quoin-claude"


def _no_leftovers(w):
    assert list(w["sandbox_parent"].rglob("qbench_sandbox_*")) == []
    assert list(w["sandbox_parent"].glob("qbench_run_*")) == []


def _ledger(w):
    return [json.loads(l) for l in w["ledger"].read_text().splitlines() if l.strip()]


def _summary(w):
    return json.loads((w["runs"] / "R" / "summary.json").read_text())


def test_sessions_are_sequential_task_major_and_plain_first(world):
    w = world
    assert _run(w, "--skip-judge", **{"--max-parallel": "4"}) == 0
    log = read_paired_log(w["log"])
    starts = [e for e in log if e["event"] == "start"]
    ends = {e["pid"]: e for e in log if e["event"] == "end"}
    assert [_arm_of(s) for s in starts] == list(ARMS) * 3
    for a, b in zip(starts, starts[1:]):
        assert ends[a["pid"]]["t1"] <= b["t0"]
    reservations = [(r["arm"], r["task_id"]) for r in _ledger(w) if r["kind"] == "reservation"]
    assert reservations == [(arm, t["id"]) for t in w["tasks"] for arm in ("plain", "quoin")]
    _no_leftovers(w)


def test_environment_is_the_allowlist_and_stays_in_the_sandbox(world):
    w = world
    assert _run(w, "--skip-judge") == 0
    real_claude = str(Path.home() / ".claude")
    for start in _starts(w):
        keys = set(start["env_keys"]) - PLATFORM_INJECTED_ENV
        expected = {k for k in arm_sandbox.PASSTHROUGH_KEYS if k in os.environ}
        expected |= {"CLAUDE_CODE_OAUTH_TOKEN", "HOME", "CLAUDE_CONFIG_DIR", "GIT_AUTHOR_NAME",
                     "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL",
                     "CLAUDE_CODE_TMPDIR", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                     "ANTHROPIC_SMALL_FAST_MODEL"}
        if _arm_of(start) == "quoin-claude":
            expected |= {"QUOIN_GATE_AUTO_APPROVE", "QUOIN_BENCHMARK_RUN", "QUOIN_AUTO_RESUME"}
        assert keys == expected
        parent = os.path.realpath(w["sandbox_parent"])
        assert os.path.realpath(start["home"]).startswith(parent)
        assert os.path.realpath(start["config_dir"]).startswith(parent)
        assert real_claude not in json.dumps(start)
    by_arm = {}
    for start in _starts(w):
        assert start["cli_tmp_exists"] is True
        assert len(start["cli_tmp"]) <= arm_sandbox.CLI_TMP_MAX_LENGTH
        by_arm.setdefault(_arm_of(start), set()).add(start["cli_tmp"])
    assert all(len(v) == 1 for v in by_arm.values())
    assert by_arm["simple-claude"].isdisjoint(by_arm["quoin-claude"])
    for value in by_arm["simple-claude"] | by_arm["quoin-claude"]:
        assert not os.path.exists(value)
    plain_row = w["runs"] / "R" / "simple-claude" / w["tasks"][0]["id"]
    init = next(json.loads(l) for l in (plain_row / "transcript.jsonl").read_text().splitlines()
                if '"init"' in l)
    assert set(init["skills"]) <= {"run", "debug", "code-review"}


def test_both_sandboxes_share_a_private_run_parent_and_not_the_operator_temp_dir(world):
    w = world
    real_create = arm_sandbox.create_sandbox
    seen = []

    def spy(kind, parent=None):
        box = real_create(kind, parent)
        seen.append((kind, parent, box))
        return box

    w["monkeypatch"].setattr(arm_sandbox, "create_sandbox", spy)
    assert _run(w, "--skip-judge") == 0
    assert [k for k, _, _ in seen] == ["plain", "quoin"]
    parents = {p for _, p, _ in seen}
    assert len(parents) == 1
    parent = parents.pop()
    assert parent is not None and parent != w["sandbox_parent"]
    assert parent.name.startswith("qbench_run_")
    assert not parent.exists()
    for start in _starts(w):
        assert os.path.realpath(start["home"]).startswith(os.path.realpath(parent))


def test_clean_run_leaves_only_the_expected_files(world):
    w = world
    assert _run(w, "--skip-judge") == 0
    assert {p.name for p in (w["runs"] / "R").iterdir()} == {
        "simple-claude", "quoin-claude", "summary.json", "summary.md", "run-manifest.yaml",
        "invariants-report.md",
    }
    manifest = read_manifest(w["runs"] / "R")
    assert manifest["contamination_checks"] == "static-passed; runtime-passed"
    assert _summary(w)["tiered"]["validity"]["valid"] is True
    _no_leftovers(w)


def test_seeded_plain_sandbox_refuses_before_any_session(world, capsys):
    w = world
    real_create = arm_sandbox.create_sandbox

    def seeded(kind, parent=None):
        box = real_create(kind, parent)
        if kind == "plain":
            (box.claude_dir / "skills" / "plan").mkdir(parents=True)
            (box.claude_dir / "skills" / "plan" / "SKILL.md").write_text("x")
        return box

    w["monkeypatch"].setattr(arm_sandbox, "create_sandbox", seeded)
    assert _run(w, "--skip-judge") == 2
    assert "REFUSED: plain-arm-contaminated" in capsys.readouterr().err
    assert read_paired_log(w["log"]) == []
    _no_leftovers(w)


def test_contaminated_init_on_the_plain_arm_aborts_the_run(world):
    w = world
    _scenario(w, {"runs": {"1": {"contaminated_plain": True}}})
    assert _run(w, "--skip-judge") == 1
    assert len(_starts(w)) == 1
    manifest = read_manifest(w["runs"] / "R")
    assert manifest["contamination_checks"].startswith("runtime-tripped")
    tiered = _summary(w)["tiered"]
    assert tiered["validity"]["valid"] is False
    assert tiered["overall"]["simple-claude"]["outcomes"]["not_run"] == 2
    assert tiered["overall"]["quoin-claude"]["outcomes"]["not_run"] == 3
    _no_leftovers(w)


def test_spend_ceiling_stops_on_a_pair_boundary(world, capsys):
    w = world
    _scenario(w, {"cost": {"plain": 7.0, "quoin": 7.0}})
    assert _run(w, "--skip-judge") == 3
    assert len(_starts(w)) == 4
    rows = _ledger(w)
    assert sorted(r["attempt_id"] for r in rows if r["kind"] == "reservation") == \
        sorted(r["attempt_id"] for r in rows if r["kind"] == "settlement")
    assert "spend-ceiling" in capsys.readouterr().err
    _no_leftovers(w)


def test_skip_judge_then_judge_only_then_resume_runs_nothing(world, capsys):
    w = world
    _scenario(w, {"runs": {"3": {"result_subtype": "error_max_budget_usd"}}})
    assert _run(w, "--skip-judge") == 0
    assert "Judging deferred" in capsys.readouterr().out
    assert len(_starts(w)) == 6
    rows = [w["runs"] / "R" / arm / t["id"] for arm in ARMS for t in w["tasks"]]
    assert sum(1 for r in rows if is_judge_pending(r)) == 5

    before = read_paired_log(w["log"])
    w["monkeypatch"].setattr(sys, "argv", ["run_benchmark.py", "--suite", str(w["suite"]),
                                           "--run-dir", str(w["runs"]), "--run-id", "R", "--judge-only"])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    assert exc.value.code == 0
    assert not any(is_judge_pending(r) for r in rows)
    assert len(w["judged"]) == 5
    assert read_paired_log(w["log"]) == before

    assert _run(w, "--skip-judge", "--resume") == 0
    assert read_paired_log(w["log"]) == before
    budget_row = w["runs"] / "R" / "simple-claude" / w["tasks"][1]["id"]
    assert json.loads((budget_row / "session.json").read_text())["terminal_state"] == "budget_stopped"


@pytest.mark.parametrize("variant", ["devnull", "inherit"])
def test_a_lingering_child_does_not_stall_the_session(world, variant):
    w = world
    _scenario(w, {"background": variant})
    started = time.monotonic()
    assert _run(w) == 0
    assert time.monotonic() - started < 40
    assert len(w["judged"]) == 6
    children = [e["pid"] for e in read_paired_log(w["log"]) if e["event"] == "child"]
    assert len(children) == 6
    for pid in children:
        assert _pid_gone(pid)
    for arm in ARMS:
        for t in w["tasks"]:
            row = w["runs"] / "R" / arm / t["id"]
            assert "edited by" in (row / "diff.patch").read_text()
    _no_leftovers(w)


def test_pilot_rows_in_the_ledger_do_not_count_against_the_suite_ceiling(world):
    from quoin.benchmarks.harness import paired_spend

    w = world
    for task in ("pilot_a", "pilot_b", "pilot_c"):
        attempt = paired_spend.reserve(w["ledger"], "R-pilot", task, "simple-claude", 5.5)
        paired_spend.settle(w["ledger"], attempt, "R-pilot", task, "simple-claude", 5.5, 5.0)
    assert _run(w, "--skip-judge") == 0
    assert len(_starts(w)) == 6


def test_a_skill_written_into_the_plain_sandbox_aborts_before_the_next_plain_task(world):
    w = world
    _scenario(w, {"runs": {"1": {"write_skill": "injected"}}})
    assert _run(w, "--skip-judge") == 1
    assert len(_starts(w)) == 2
    manifest = read_manifest(w["runs"] / "R")
    assert manifest["contamination_checks"].startswith("runtime-tripped")
    assert "plain config directory changed" in manifest["contamination_checks"]
    # The check fires before the second plain task and names the arm and task
    # it was about to start, plus the task whose session wrote the skill.
    label = manifest["contamination_checks"]
    assert f"simple-claude/{w['tasks'][1]['id']}" in label
    assert f"ran on {w['tasks'][0]['id']}" in label
    validity = _summary(w)["tiered"]["validity"]
    assert validity["valid"] is False
    assert any("plain config directory changed" in r for r in validity["invalid_reasons"])
    _no_leftovers(w)

    # The rebuilt sandbox on resume passes its checks, but the run stays invalid.
    _scenario(w, {})
    assert _run(w, "--skip-judge", "--resume") == 0
    assert read_manifest(w["runs"] / "R")["contamination_checks"] == "static-passed; runtime-passed"
    resumed = _summary(w)["tiered"]["validity"]
    assert resumed["valid"] is False
    assert any("plain config directory changed" in r for r in resumed["invalid_reasons"])


def _git_reset_to(w):
    root = w["arm_repo"]["root"]
    subprocess.run(["git", "-C", str(root), "reset", "-q", "--hard", w["arm_repo"]["head"]],
                   check=True, capture_output=True)


def test_arm_checkout_moving_mid_run_stops_resumably_without_voiding_the_run(world):
    w = world
    _scenario(w, {"runs": {"1": {"commit_in": str(w["arm_repo"]["root"])}}})
    assert _run(w, "--skip-judge") == 1
    starts = _starts(w)
    assert [_arm_of(s) for s in starts] == ["simple-claude"]
    manifest = read_manifest(w["runs"] / "R")
    assert "arm contamination" not in json.dumps(manifest.get("run_invalid_reasons") or [])
    assert not manifest.get("run_invalid_reasons")
    assert _summary(w)["tiered"]["validity"]["valid"] is True
    # The quoin session was refused before it spawned, so it settles at zero.
    rows = _ledger(w)
    assert len([r for r in rows if r["kind"] == "reservation"]) == 2
    settlements = [r for r in rows if r["kind"] == "settlement"]
    assert len(settlements) == 2 and settlements[-1]["actual_usd"] == 0
    assert len(_starts(w)) == 1
    _no_leftovers(w)

    # With the checkout back on the expected commit the run resumes and finishes.
    _scenario(w, {})
    _git_reset_to(w)
    assert _run(w, "--skip-judge", "--resume") == 0
    assert _summary(w)["tiered"]["validity"]["valid"] is True


def test_a_git_failure_reading_the_arm_head_mid_run_does_not_void_the_run(world):
    w = world
    calls = {"n": 0}
    from quoin.benchmarks.scripts import run_three_arm_gate as gate

    real_head = gate.worktree_head

    def flaky(root):
        calls["n"] += 1
        return None if calls["n"] > 1 else real_head(root)

    w["monkeypatch"].setattr(gate, "worktree_head", flaky)
    assert _run(w, "--skip-judge", **{"--expected-quoin-commit": None}) == 0
    assert calls["n"] == 1  # read once at start, never re-read per spawn
    manifest = read_manifest(w["runs"] / "R")
    assert not manifest.get("run_invalid_reasons")
    assert _summary(w)["tiered"]["validity"]["valid"] is True
    _no_leftovers(w)


def test_a_skill_written_by_the_last_plain_session_still_voids_the_run(world):
    w = world
    _scenario(w, {"runs": {"5": {"write_skill": "injected"}}})
    assert _run(w, "--skip-judge") == 1
    assert len(_starts(w)) == 6
    manifest = read_manifest(w["runs"] / "R")
    assert "plain config directory changed" in manifest["contamination_checks"]
    assert _summary(w)["tiered"]["validity"]["valid"] is False
    _no_leftovers(w)


def test_run_parent_cannot_be_listed_while_sessions_run(world):
    w = world
    seen = []
    real_create = arm_sandbox.create_sandbox

    def spy(kind, parent=None):
        box = real_create(kind, parent)
        seen.append(parent)
        return box

    w["monkeypatch"].setattr(arm_sandbox, "create_sandbox", spy)
    real_install = arm_sandbox.install_quoin

    def install(box, *a, **k):
        seen.append(os.stat(seen[0]).st_mode & 0o777)
        return real_install(box, *a, **k)

    w["monkeypatch"].setattr(arm_sandbox, "install_quoin", install)
    assert _run(w, "--skip-judge") == 0
    assert seen[-1] == 0o100
    _no_leftovers(w)


def test_hermetic_against_checkout_state(world):
    w = world
    stray = w["arm_repo"]["root"] / "quoin" / "dev" / "tests" / "stray_untracked.py"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("x = 1\n")
    try:
        assert REAL_CHECK_ARM_TREE(w["arm_repo"]["root"], w["arm_repo"]["head"])
        assert _run(w, "--skip-judge") == 0
    finally:
        stray.unlink()


def test_sigint_during_install_exits_cleanly(world):
    w = world

    def install(*a, **k):
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)
        return {"ok": True}

    w["monkeypatch"].setattr(arm_sandbox, "install_quoin", install)
    started = time.monotonic()
    assert _run(w, "--skip-judge") == 1
    assert time.monotonic() - started < 4
    _no_leftovers(w)
    assert read_paired_log(w["log"]) == []
    assert not w["ledger"].exists() or _ledger(w) == []


def _pid_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _pgid_gone(pgid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_sigterm_stops_cleanly_and_resume_finishes(world):
    w = world
    received = []

    def test_handler(signum, frame):
        received.append(signum)
        raise AssertionError("the paired run did not install its own SIGTERM handler")

    previous = signal.signal(signal.SIGTERM, test_handler)
    try:
        _scenario(w, {"runs": {"3": {"sleep_forever": True}}})
        failed = []

        def killer():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and len(_starts(w)) < 3:
                time.sleep(0.05)
            if len(_starts(w)) < 3:
                failed.append("the third spawn never started")
            os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=killer, daemon=True)
        thread.start()
        code = _run(w)
        thread.join(5)
        assert not failed and not received
        assert code == 1
        assert signal.getsignal(signal.SIGTERM) is test_handler

        log = read_paired_log(w["log"])
        starts = [e for e in log if e["event"] == "start"]
        assert len(starts) == 3
        assert not [e for e in log if e["event"] == "end" and e["pid"] == starts[2]["pid"]]
        assert _pgid_gone(starts[2]["pgid"])
        _no_leftovers(w)
        rows = _ledger(w)
        assert len([r for r in rows if r["kind"] == "reservation"]) == \
            len([r for r in rows if r["kind"] == "settlement"]) == 3
        assert read_manifest(w["runs"] / "R")["interrupted"] == "SIGTERM"

        _scenario(w, {})
        assert _run(w, "--resume") == 0
        assert signal.getsignal(signal.SIGTERM) is test_handler
        resumed = _starts(w)[3:]
        assert len(resumed) == 4
        assert [_arm_of(s) for s in resumed] == list(ARMS) * 2
        sup = w["runs"] / ".superseded" / "R" / "simple-claude" / w["tasks"][1]["id"]
        assert sup.is_dir()
        for arm in ARMS:
            for t in w["tasks"]:
                assert not is_judge_pending(w["runs"] / "R" / arm / t["id"])
        _no_leftovers(w)
    finally:
        signal.signal(signal.SIGTERM, previous)
