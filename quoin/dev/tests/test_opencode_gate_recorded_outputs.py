"""Gate and continuation advice for entries whose run recorded its own outputs."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import _opencode_gate_helpers as gh
import _opencode_handoff_helpers as hh
from quoin.opencode_adapter import gate, handoff, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REVIEW_REL = ".workflow_artifacts/t1/stage-1/review-1.md"


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    return gh.Fixture(tmp_path, monkeypatch)


def codes(items):
    return [(status, code) for status, code, _detail in items]


def critic(fx, entry, origin="phase-run"):
    return gate.critic_status(entry, origin, fx.base / "stage-1", {}, project_root=fx.root)


def test_recorded_outputs_without_a_response_fail_even_with_pass_files_on_disk(fx):
    assert (fx.base / "stage-1" / "critic-response-1.md").exists()
    entry = {"origin": "phase-run", "critic_responses": [], "outputs_recorded": True}
    assert codes(critic(fx, entry)) == [("FAIL", "critic-missing")]


def test_the_same_entry_without_the_flag_keeps_the_disk_fallback(fx):
    entry = {"origin": "phase-run", "critic_responses": []}
    assert codes(critic(fx, entry)) == [("PASS", "")]


def test_adopted_entries_keep_the_fallback(fx):
    entry = {"origin": "adopted", "critic_responses": []}
    assert codes(critic(fx, entry, origin="adopted")) == [("PASS", "")]


def test_a_recorded_response_is_still_used(fx):
    entry = {"origin": "phase-run", "outputs_recorded": True,
             "critic_responses": [".workflow_artifacts/t1/stage-1/critic-response-1.md"]}
    assert codes(critic(fx, entry)) == [("PASS", "")]


def artifacts(fx, entry):
    return gate.expected_artifacts(fx.root, fx.task, 1, "review", entry, fx.base / "stage-1")


def test_review_with_the_flag_and_nothing_harvested_is_missing(fx):
    paths, missing = artifacts(fx, {"origin": "phase-run", "outputs_recorded": True, "harvested": []})
    assert paths == [] and missing == ["review-N.md"]


def test_review_with_the_flag_uses_the_harvested_path(fx):
    gh.write(fx.base / "stage-1" / "review-2.md", gh.REVIEW)
    entry = {"origin": "phase-run", "outputs_recorded": True,
             "harvested": [{"path": ".workflow_artifacts/t1/stage-1/review-2.md", "sha256": "0" * 64}]}
    paths, missing = artifacts(fx, entry)
    assert [p.name for p in paths] == ["review-2.md"] and missing == []


def test_review_without_the_flag_falls_back_to_the_disk(fx):
    paths, missing = artifacts(fx, {"origin": "phase-run", "harvested": []})
    assert [p.name for p in paths] == ["review-1.md"] and missing == []


def test_adopted_review_keeps_the_fallback(fx):
    paths, _ = artifacts(fx, {"origin": "adopted"})
    assert [p.name for p in paths] == ["review-1.md"]


# -- advice text -------------------------------------------------------------


def _advice(project):
    directory = runstore.inspect_store(project.root)
    state = None if directory is None else runstore.load_workflow_state(directory, project.task)
    record = project.build()
    return handoff.next_step(project.root, record, state, handoff.run_facts(directory, project.task), project.source)


def _text(advice):
    return " ".join([advice.get("hint") or "", *advice.get("steps", []), *advice.get("candidates", [])])


def test_run_completed_hint_text(tmp_path, monkeypatch):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    project.seed_gate("architect", None, "PASS")
    project.seed_run("critic", "1", "completed")
    advice = _advice(project)
    assert advice["status"] == "run-completed"
    hint = advice["hint"]
    assert "after that run" in hint and "no run writes one yet" not in hint
    assert "telemetry" in hint and "quoin run" not in _text(advice)
    assert not re.search(r"\b(stage|task|decision)[- ]?\d|\b[DTRFQS]-\d+\b", hint)


def test_gate_failed_hint_names_a_new_run_and_the_adopted_plan_recovery(tmp_path, monkeypatch):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    project.seed_gate("architect", None, "PASS")
    project.seed_gate("plan", 1, "FAIL", reasons=("critic-missing",), origin="phase-run")
    advice = _advice(project)
    assert advice["status"] == "gate-failed"
    hint = advice["hint"]
    assert "new run of the phase records a replacement entry" in hint
    assert "adopt the plan again" in hint and "quoin run" not in hint
    assert "belongs to the coordinator" not in hint


def test_the_core_continuation_doc_states_that_runs_record_entries():
    doc = (Path(__file__).resolve().parents[2] / "core" / "workflow" / "continuation-handoff.md").read_text()
    assert "A phase run records a workflow entry for its phase when it ends" in doc
    assert "left to a later coordinator" not in doc
