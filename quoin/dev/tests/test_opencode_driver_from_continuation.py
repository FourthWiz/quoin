"""`Handoff.from_continuation` returns a native resume token only when the
record, the run store and the checkpoint all agree."""
from __future__ import annotations

import copy
import shutil

import pytest

import _opencode_handoff_helpers as hh
from quoin.opencode_adapter import driver, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    fx = hh.Project(tmp_path, monkeypatch)
    fx.seed_gate("architect", None, "PASS")
    run_id = fx.seed_run("implement", "1", "interrupted", checkpoint=True, session="ses_one")
    return fx, run_id, fx.build()


def token(fx, record):
    return driver.Handoff.from_continuation(record, fx.directory())


def edit_run(fx, run_id, **changes):
    record = runstore.load_record(fx.directory(), run_id)
    for key, value in changes.items():
        record[key] = value
    runstore.write_record(fx.directory(), record)


def test_agreeing_interrupted_run_gives_the_checkpoint_token(seeded):
    fx, run_id, record = seeded
    expected = driver.Handoff.from_checkpoint(runstore.load_checkpoint(fx.directory(), run_id))
    assert token(fx, record) == expected


def test_thorough_plan_run_maps_to_the_plan_phase(tmp_path, monkeypatch):
    fx = hh.Project(tmp_path, monkeypatch, passed=False)
    fx.seed_gate("architect", None, "PASS")
    fx.seed_run("thorough_plan", "1", "interrupted", checkpoint=True)
    record = fx.build()
    assert record["phase"]["current"] == "plan"
    assert token(fx, record) is not None


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(origin_runtime="claude"),
    lambda r: r.pop("native"),
    lambda r: r["native"].update(run_id="oc-20200101T000000Z-deadbeef"),
    lambda r: r["native"].update(run_id="not-a-run-id"),
    lambda r: r.update(task="other"),
    lambda r: r["phase"].update(current="review"),
    lambda r: r["phase"].update(stage=2),
    lambda r: r["native"].update(session_id="ses_two"),
], ids=["origin", "no-native", "unknown-run", "bad-run-id", "task", "phase", "stage", "session"])
def test_record_mismatches_start_fresh(seeded, mutate):
    fx, _, record = seeded
    changed = copy.deepcopy(record)
    mutate(changed)
    assert token(fx, changed) is None


@pytest.mark.parametrize("state", ["completed", "failed", "running"])
def test_run_state_must_be_interrupted(seeded, state):
    fx, run_id, record = seeded
    edit_run(fx, run_id, state=state)
    assert token(fx, record) is None


@pytest.mark.parametrize("blocked", ["session-lost", "sidecar-behind-checkpoint"])
def test_resume_blocked_runs_start_fresh(seeded, blocked):
    fx, run_id, record = seeded
    edit_run(fx, run_id, resume_blocked=blocked)
    assert token(fx, record) is None


def test_driver_lost_attempt_starts_fresh(seeded):
    fx, run_id, record = seeded
    edit_run(fx, run_id, attempts=[{"attempt": 1, "driver_lost": True}])
    assert token(fx, record) is None


def test_run_task_mismatch_starts_fresh(seeded):
    fx, run_id, record = seeded
    edit_run(fx, run_id, task="other")
    assert token(fx, record) is None


def test_run_stage_and_phase_must_match(seeded):
    fx, run_id, record = seeded
    stored = runstore.load_record(fx.directory(), run_id)
    edit_run(fx, run_id, request=dict(stored["request"], stage="2"))
    assert token(fx, record) is None
    edit_run(fx, run_id, request=dict(stored["request"], phase="review"))
    assert token(fx, record) is None


def test_missing_or_foreign_checkpoint_starts_fresh(seeded):
    fx, run_id, record = seeded
    checkpoint = runstore.load_checkpoint(fx.directory(), run_id)
    runstore.atomic_write_json(
        runstore.run_paths(fx.directory(), run_id).checkpoint, dict(checkpoint, run_id="oc-20200101T000000Z-deadbeef"),
    )
    assert token(fx, record) is None
    runstore.run_paths(fx.directory(), run_id).checkpoint.unlink()
    assert token(fx, record) is None


def test_corrupt_run_record_starts_fresh(seeded):
    fx, run_id, record = seeded
    runstore.run_paths(fx.directory(), run_id).record.write_text("{broken")
    assert token(fx, record) is None
