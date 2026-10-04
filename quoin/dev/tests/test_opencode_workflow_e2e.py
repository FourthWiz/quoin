"""The headless coordinator end to end: the real driver, the fake executable,
the real gates, in a generated git project with a two-stage task.

Commands proved here: quoin-discover, quoin-architect, quoin-plan, quoin-critic,
quoin-implement, quoin-review, and the whole-task form `quoin run --workflow`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _opencode_gate_helpers as gh
from _opencode_helpers import SOURCE_DIR
import _opencode_workflow_e2e_helpers as eh
from quoin import cli
from quoin.opencode_adapter import gate, paths, runstore

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

MARKER = " (non-interactive run)"
SRC = str(Path(__file__).resolve().parents[3] / "src")
BOOT = (
    "import sys; sys.path.insert(0, %r); from quoin import cli; "
    "raise SystemExit(cli.main(['opencode', 'test-run', '--task', 'demo', '--stage', '1']))" % SRC
)
SECRET = "sk-seeded-secret-value-1234567890"
FLOW = ("--from-discover", "--no-pause")


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario=None):
        proj = eh.WorkflowProject(tmp_path, monkeypatch, scenario)
        holder.append(proj)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def artifact_dir(proj, stage):
    from quoin.core.scripts import path_resolve
    return path_resolve.task_path(eh.TASK, stage, project_root=proj.root)


def all_files(proj):
    base = proj.root / ".workflow_artifacts"
    return sorted(p for p in base.rglob("*") if p.is_file())


VALIDATOR = Path(__file__).resolve().parents[2] / "core" / "scripts" / "validate_artifact.py"


def validate(path):
    res = subprocess.run([sys.executable, str(VALIDATOR), str(path)], capture_output=True, text=True)
    return res.returncode, res.stdout + res.stderr


def test_full_run_produces_every_artifact_and_gate(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW)
    assert code == 0, json.dumps(summary)
    assert summary["outcome"] == "COMPLETED", summary
    assert summary["workflow_validated"] is True
    state = proj.state()
    phases = [(p["phase"], p["stage"]) for p in summary["phases"]]
    assert phases == [("discover", None), ("architect", None)] + [
        (phase, stage) for stage in (1, 2) for phase in ("plan", "implement", "review")]
    for item in summary["phases"]:
        assert item["gate"] == "PASS", item
        gate_file = proj.root / item["gate_artifact"]
        assert gate_file.is_file()
        assert gate_file.parent == artifact_dir(proj, item["stage"]), item
        text = gate_file.read_text(encoding="utf-8")
        assert 'verdict: "PASS"' in text and "evaluator: deterministic" in text
    for stage in (1, 2):
        sdir = artifact_dir(proj, stage)
        for name in ("current-plan.md", "critic-response-1.md", "review-1.md"):
            assert (sdir / name).is_file(), (stage, name)
    assert (artifact_dir(proj, None) / "architecture.md").is_file()
    for path in all_files(proj):
        if path.suffix == ".md" and path.name != "cost-ledger.md" and "memory" not in path.parts:
            code, text = validate(path)
            assert code == 0, (path, text)
    # every phase entry carries evidence with per-repo source state
    live = [e for e in state["entries"] if not e["superseded"]]
    assert len(live) == 8
    for entry in state["entries"]:
        repos = entry["evidence"]["repos"]
        assert repos and all(r["head"] and "source_dirty" in r for r in repos), entry["phase"]
        assert entry["evidence"]["task_hashes"]
    for run_id in {rid for e in state["entries"] for rid in e["runs"]}:
        record = runstore.load_record(runstore.store_dir(proj.root), run_id)
        for attempt in record["attempts"]:
            assert attempt["input_hashes_before"] is not None
            assert attempt["input_hashes_after"] is not None
            assert attempt["repo_revisions_before"]
            assert all(r["head"] and "dirty" in r for r in attempt["repo_revisions_before"]), attempt


def test_ledger_has_one_row_per_run_and_sessions_differ(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW)
    assert code == 0, summary
    ledger = (proj.root / ".workflow_artifacts" / eh.TASK / "cost-ledger.md").read_text(encoding="utf-8")
    run_ids = [rid for p in summary["phases"] for rid in p["run_ids"]]
    assert len(run_ids) == len(set(run_ids))
    for run_id in run_ids:
        assert ledger.count(run_id) == 1, run_id
    assert not (proj.root / ".workflow_artifacts" / "finalized").exists()
    # one native session per run, and none shared between roles
    sessions = [i["session_id"] for i in proj.runs()]
    assert len(sessions) == len(set(sessions)) == len(run_ids)
    assert all("--session" not in i["argv"] for i in proj.runs())


def test_every_coordinator_argument_ends_with_the_marker_and_context_is_named(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW)
    assert code == 0, summary
    args = proj.arguments()
    assert all(a.endswith(MARKER) for a in args), args
    commands = proj.commands()
    assert commands[:2] == ["quoin-discover", "quoin-architect"]
    plans = [a for a, c in zip(args, commands) if c == "quoin-plan"]
    first, second = plans[0], plans[1]
    assert "(context:" not in first
    assert "critic-response-1.md" in second and second.endswith(MARKER)
    assert "quoin-end-of-task" not in commands
    assert not (proj.root / ".workflow_artifacts" / "finalized").exists()


def test_pause_after_plan_gate_then_after_review_gate(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, "--from-discover")
    assert summary["outcome"] == "PAUSED_AT_GATE", summary
    last = summary["phases"][-1]
    assert (last["phase"], last["stage"]) == ("plan", 1)
    code, summary = proj.run_workflow(capsys, "--continue")
    assert summary["outcome"] == "PAUSED_AT_GATE", summary
    last = summary["phases"][-1]
    assert (last["phase"], last["stage"]) == ("review", 1)


def test_review_refusal_then_rerun_from_implement(make, capsys):
    rejected = gh.REVIEW.replace("APPROVED", "CHANGES_REQUESTED")
    scenario = eh.workflow_scenario(review_text=[rejected])
    proj = make(scenario)
    code, summary = proj.run_workflow(capsys, *FLOW, "--through", "review")
    assert code == 7, summary
    assert "review-not-approved" in summary["reasons"]
    sdir = ".workflow_artifacts/demo/stage-1"
    # the fixed implementer and an approving reviewer for the rerun
    scenario["commands"]["quoin-implement@1"] = {"attempts": [eh._step(eh._write("src/stage1.py", "VALUE = 11\n"))]}
    scenario["commands"]["quoin-review@1"] = {"attempts": [eh._step(
        eh._try(sdir + "/review-2.md", gh.REVIEW))]}
    proj.set_scenario(scenario)
    before = len(proj.runs())
    code, summary = proj.run_workflow(capsys, "--continue", "--rerun-from", "implement", "--through", "review")
    assert code == 0, summary
    new_runs = list(zip(proj.commands()[before:], proj.arguments()[before:]))
    implement_args = [a for c, a in new_runs if c == "quoin-implement"]
    assert implement_args and "review-1.md" in implement_args[0]
    assert implement_args[0].endswith(MARKER)
    assert implement_args[0].index("review-1.md") < implement_args[0].index(MARKER)
    assert [c for c, _ in new_runs][-1] == "quoin-review"


def test_headless_ask_stops_with_adopt_advice(make, capsys):
    from _opencode_driver_helpers import fake
    approval = fake._fixture_scenario("tool-rejected.jsonl")()["attempts"]
    scenario = eh.workflow_scenario(extra={"quoin-implement@1": approval})
    proj = make(scenario)
    code, summary = proj.run_workflow(capsys, *FLOW)
    err = proj.last_err
    assert code == 4, summary
    assert summary["outcome"] == "AWAITING_APPROVAL"
    assert not list((proj.root / ".workflow_artifacts" / eh.TASK / "stage-1").glob("gate-implement-*"))
    assert "opencode adopt" in err and "implement" in err, err


def test_agent_run_of_tests_uses_the_drivers_state_root(make, capsys, monkeypatch):
    steps = lambda stage: [  # noqa: E731
        eh._write("src/stage%d.py" % stage, "VALUE = %d\n" % stage),
        {"do": "run_cmd", "argv": [sys.executable, "-c", BOOT]},
    ] if stage == 1 else [eh._write("src/stage2.py", "VALUE = 2\n")]
    proj = make(eh.workflow_scenario(implement_steps=steps))
    code, summary = proj.run_workflow(capsys, *FLOW, "--test-command", "sh -c true", "--through", "implement")
    assert code == 0, summary
    drv = proj.last_driver
    assert drv.state_root != paths.state_dir(os.environ, Path.home())
    ran = [line for line in proj.effects() if line.startswith("ran ")]
    assert ran and ran[0].startswith("ran 0 ") and "PASSED" in ran[0], ran
    assert "tests-settings-changed" not in ran[0]
    entry = [e for e in proj.state()["entries"] if e["phase"] == "implement" and e["stage"] == 1][-1]
    assert entry["tests"]["exit_code"] == 0
    assert entry["gate"]["verdict"] == "PASS"
    assert "tests-settings-changed" not in entry["gate"]["reasons"]


def test_changed_pin_refuses_before_spawn_and_single_phase_gate_reports_it(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW, "--test-command", "sh -c true", "--through", "implement")
    assert code == 0, summary
    drv = proj.last_driver
    pins = list(Path(drv.state_root).rglob("settings.pin"))
    assert len(pins) == 1
    pins[0].write_text("0" * 64 + "\n", encoding="utf-8")
    before = len(proj.runs())
    code, summary = proj.run_workflow(capsys, "--continue", "--rerun-from", "implement", "--through", "implement")
    assert code != 0
    assert "tests-settings-changed" in json.dumps(summary)
    assert len(proj.runs()) == before
    # a single-phase run in this state, then the gate CLI
    code = cli.main(["run", eh.TASK, "--runtime", "opencode", "--project-root", str(proj.root),
                     "--profile", "work", "--phase", "implement", "--stage", "1"])
    capsys.readouterr()
    code = cli.main(["opencode", "gate", "--task", eh.TASK, "--stage", "1", "--phase", "implement",
                     "--project-root", str(proj.root)])
    out = capsys.readouterr().out
    assert "tests-settings-changed" in out


def test_a_seeded_secret_never_reaches_stored_files(make, capsys, monkeypatch):
    monkeypatch.setenv("QUOIN_CORP_GW_API_KEY", SECRET)
    monkeypatch.setenv("QUOIN_CORP_GW_B_API_KEY", SECRET)
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW)
    out = capsys.readouterr()
    assert code == 0
    assert SECRET not in json.dumps(summary) and SECRET not in out.err
    roots = [proj.root / ".workflow_artifacts", Path(proj.last_driver.state_root)]
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes(), path


def test_continuing_into_stage_two_tolerates_the_rewritten_plan(make, capsys):
    proj = make()
    code, summary = proj.run_workflow(capsys, *FLOW, "--through", "implement")
    assert code == 0, summary
    code, summary = proj.run_workflow(capsys, "--continue", *FLOW)
    assert code == 0, summary
    assert "continuation-state-mismatch" not in json.dumps(summary)
    plan1 = [e for e in proj.state()["entries"] if e["phase"] == "plan" and e["stage"] == 1 and not e["superseded"]]
    assert plan1
    res = gate.evaluate(proj.root, eh.TASK, 1, "plan", source_dir=SOURCE_DIR)
    assert res.verdict in ("PASS", "FAIL")

