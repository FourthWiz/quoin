"""Specific test-failure reasons and newer-finding refusals at the gate, plus
the symlinked stage folder refusal at the gate and at harvest."""
from __future__ import annotations

import os
import shutil

import pytest

import _opencode_boundary_helpers as bh
import _opencode_gate_helpers as gh
from quoin.opencode_adapter import gate, snapshot

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="symlinks are POSIX-only"),
]


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    return gh.Fixture(tmp_path, monkeypatch)


REASONS = (
    "tests-settings-changed", "tests-not-run", "tests-result-stale", "tests-timeout",
    "tests-real-tree-changed", "tests-workspace-failed",
)


@pytest.mark.parametrize("reason", REASONS)
def test_each_reason_is_a_second_failing_code_even_when_the_explanation_claims_success(fx, reason):
    fx.settings(test_command="true")
    fx.record("implement", tests_reason=reason)
    result = fx.evaluate("implement", explanation="all tests passed")
    assert result.verdict == "FAIL"
    assert "tests-failed" in result.reasons and reason in result.reasons
    tests = [c for c in result.checks if c.name == "tests"][0]
    assert reason in tests.codes and any(d for d in tests.details)


def test_a_hook_entry_without_a_result_reports_both_codes(fx):
    fx.settings(test_command="true")
    fx.record("implement", tests_reason="tests-not-run")
    result = fx.evaluate("implement")
    assert {"tests-failed", "tests-not-run"} <= set(result.reasons)


def test_settings_changed_names_the_recovery_command(fx):
    fx.settings(test_command="true")
    fx.record("implement", tests_reason="tests-settings-changed")
    result = fx.evaluate("implement")
    tests = [c for c in result.checks if c.name == "tests"][0]
    assert "tests-settings-changed" in result.reasons
    assert any("--workflow --test-command" in d for d in tests.details)


def test_unknown_reason_strings_are_dropped_not_echoed(fx):
    fx.settings(test_command="true")
    fx.record("implement", tests_reason="sk-secret-looking-reason")
    result = fx.evaluate("implement")
    assert result.reasons == ("tests-failed",)
    assert "sk-secret" not in repr(result.to_dict())


def test_a_passing_result_ignores_a_stale_reason(fx):
    fx.settings(test_command="true")
    fx.record("implement", tests={"command": "true", "exit_code": 0}, tests_reason="tests-not-run")
    assert "tests-failed" not in fx.evaluate("implement").reasons


def test_a_newer_critic_response_refuses_finding_superseded(fx):
    fx.record("plan")
    assert "finding-superseded" not in fx.evaluate("plan").reasons
    gh.write(fx.base / "stage-1" / "critic-response-2.md", gh.CRITIC_PASS)
    result = fx.evaluate("plan")
    assert "finding-superseded" in result.reasons


def test_a_newer_critic_response_refuses_for_single_phase_entries_too(fx):
    fx.record("plan", origin="phase-run", outputs_recorded=True,
              critic_responses=[str(fx.base / "stage-1" / "critic-response-1.md")])
    gh.write(fx.base / "stage-1" / "critic-response-2.md", gh.CRITIC_PASS)
    assert "finding-superseded" in fx.evaluate("plan").reasons


def test_a_newer_review_refuses_finding_superseded(fx):
    review = str(fx.base / "stage-1" / "review-1.md")
    fx.record("review", harvested=[{"path": review, "sha256": "0" * 64}])
    assert "finding-superseded" not in fx.evaluate("review").reasons
    gh.write(fx.base / "stage-1" / "review-2.md", gh.REVIEW)
    assert "finding-superseded" in fx.evaluate("review").reasons


def test_a_symlinked_stage_folder_refuses_at_the_gate_and_at_harvest(fx, tmp_path):
    fx.record("plan")
    real = tmp_path / "elsewhere"
    shutil.move(str(fx.base / "stage-1"), str(real))
    os.symlink(real, fx.base / "stage-1")
    result = fx.evaluate("plan")
    assert "path-unresolved" in result.reasons


def test_harvest_refuses_a_symlinked_stage_folder(tmp_path, monkeypatch):
    proj = bh.SnapshotProject(tmp_path, monkeypatch)
    state_root = tmp_path / "state-root"
    snap = snapshot.create(
        proj.root, bh.TASK, 1, role="reviewer", source_dir=bh.SOURCE_DIR, state_root=state_root,
        clock=lambda: 1_700_000_000.0,
    )
    try:
        before = snapshot.outbox_names(snap)
        (snap.root / snap.outbox_rel / "review-5.md").write_text(gh.REVIEW)
        stage = proj.root / ".workflow_artifacts" / bh.TASK / "stage-1"
        real = tmp_path / "elsewhere"
        shutil.move(str(stage), str(real))
        os.symlink(real, stage)
        got = snapshot.harvest(snap, proj.root, bh.TASK, 1, kind="review", source_dir=bh.SOURCE_DIR,
                               before_names=before)
        assert got.error == "harvest-target-unresolved"
        assert not list(real.glob("review-*.md"))
    finally:
        snapshot.remove(snap)


def test_extended_codes_are_distinct_from_the_original_closed_set():
    assert not set(gate.EXTENDED_REASON_CODES) & set(gate.REASON_CODES)
    assert set(REASONS) | {"finding-superseded"} == set(gate.EXTENDED_REASON_CODES)
