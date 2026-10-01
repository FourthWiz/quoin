"""The per-task workflow state record in the run store."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import manifest as opencode_manifest
from quoin.opencode_adapter import runstore, status

CLEANUP_SKILL = helpers.SOURCE_DIR / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"


def fixed_clock(value=1_700_000_000.0):
    return lambda: value


@pytest.fixture()
def store(tmp_path):
    return runstore.store_dir(tmp_path, create=True)


def entry(phase="plan", origin="coordinator", stage=1, **extra):
    return dict(phase=phase, origin=origin, stage=stage, **extra)


def test_round_trip_and_private_modes(store):
    state = runstore.new_workflow_state("t1", fixed_clock())
    runstore.record_phase_entry(state, entry(runs=["oc-1"]), fixed_clock())
    runstore.write_workflow_state(store, state)
    path = runstore.workflow_state_path(store, "t1")
    assert path.name == "workflow-t1.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (store.stat().st_mode & 0o777) == 0o700
    loaded = runstore.load_workflow_state(store, "t1")
    assert loaded == state
    assert loaded["kind"] == "quoin-opencode-workflow" and loaded["schema_version"] == 1
    assert not [p for p in store.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".")]


def test_absent_is_none_and_damage_is_an_error(store):
    assert runstore.load_workflow_state(store, "t1") is None
    path = runstore.workflow_state_path(store, "t1")
    path.write_text("{not json")
    with pytest.raises(runstore.RunStoreError) as exc:
        runstore.load_workflow_state(store, "t1")
    assert exc.value.code == "corrupt-record"
    path.write_text(json.dumps({"schema_version": 7}))
    with pytest.raises(runstore.RunStoreError) as exc:
        runstore.load_workflow_state(store, "t1")
    assert exc.value.code == "unsupported-schema"
    path.write_text(json.dumps({"schema_version": 1, "kind": "other", "entries": [], "settings": {}}))
    with pytest.raises(runstore.RunStoreError) as exc:
        runstore.load_workflow_state(store, "t1")
    assert exc.value.code == "corrupt-record"


def test_task_mismatch_refused(store):
    state = runstore.new_workflow_state("other", fixed_clock())
    runstore.atomic_write_json(runstore.workflow_state_path(store, "t1"), state)
    with pytest.raises(runstore.RunStoreError) as exc:
        runstore.load_workflow_state(store, "t1")
    assert exc.value.code == "state-task-mismatch"


def test_symlinked_store_component_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "proj").mkdir()
    os.symlink(real, tmp_path / "proj" / ".workflow_artifacts")
    with pytest.raises(runstore.RunStoreError) as exc:
        runstore.store_dir(tmp_path / "proj", create=True)
    assert exc.value.code == "unsafe-path"


def test_invalid_task_name_refused(store):
    with pytest.raises(runstore.RunStoreError):
        runstore.workflow_state_path(store, "../x")


def test_supersede_keeps_history(store):
    clock = fixed_clock()
    state = runstore.new_workflow_state("t1", clock)
    first = runstore.record_phase_entry(state, entry(), clock)
    other = runstore.record_phase_entry(state, entry(stage=2), clock)
    second = runstore.record_phase_entry(state, entry(origin="adopted"), clock)
    assert len(state["entries"]) == 3
    assert first["superseded"] is True and other["superseded"] is False and second["superseded"] is False
    assert runstore.current_entry(state, 1, "plan") is second
    assert runstore.current_entry(state, 2, "plan") is other
    assert runstore.current_entry(state, 1, "review") is None
    runstore.update_current_entry(state, 1, "plan", gate={"verdict": "PASS"})
    assert second["gate"] == {"verdict": "PASS"} and first["gate"] is None


def test_entry_shape_has_every_key():
    state = runstore.new_workflow_state("t1", fixed_clock())
    e = runstore.record_phase_entry(state, entry(stage="1"), fixed_clock())
    assert e["stage"] == 1
    assert set(e) == {
        "stage", "phase", "origin", "recorded_at", "superseded", "runs", "boundary", "critic_responses",
        "harvested", "envelope_path", "tests", "continuation_validation", "ledger_uuids",
        "ledger_lines_appended_during_run", "evidence", "gate",
    }


@pytest.mark.parametrize("bad", [
    entry(phase="revise"), entry(origin="human"), entry(stage=0), entry(stage="x"), entry(stage=True),
    entry(phase="discover", stage=1), entry(phase="architect", stage=2),
])
def test_invalid_entries_rejected(bad):
    state = runstore.new_workflow_state("t1", fixed_clock())
    with pytest.raises(ValueError):
        runstore.record_phase_entry(state, bad, fixed_clock())
    assert state["entries"] == []


def test_stageless_phase_accepts_none():
    state = runstore.new_workflow_state("t1", fixed_clock())
    assert runstore.record_phase_entry(state, entry(phase="discover", stage=None), fixed_clock())["stage"] is None


def test_clock_relative_timestamps():
    state = runstore.new_workflow_state("t1", fixed_clock(0))
    assert state["created_at"] == "1970-01-01T00:00:00Z"
    e = runstore.record_phase_entry(state, entry(), fixed_clock(86400))
    assert e["recorded_at"] == "1970-01-02T00:00:00Z" and state["updated_at"] == e["recorded_at"]


@pytest.mark.parametrize("value,expected", [("1", 1), (1, 1), (None, None), ("01", 1), ("12", 12)])
def test_normalize_stage_accepts(value, expected):
    assert runstore.normalize_stage(value) == expected


@pytest.mark.parametrize("value", ["0", "x", "1.0", True, False, -1, 0, "", " 1", "١", 1.0])
def test_normalize_stage_rejects(value):
    with pytest.raises(ValueError):
        runstore.normalize_stage(value)


def test_normalize_phase():
    assert runstore.normalize_phase("thorough-plan") == "thorough_plan"
    assert runstore.normalize_phase("plan") == "plan"


@pytest.mark.parametrize("run_phase,expected", [
    ("thorough-plan", "plan"), ("plan", "plan"), ("critic", "plan"), ("discover", "discover"),
    ("architect", "architect"), ("implement", "implement"), ("review", "review"),
    ("gate", None), ("checkpoint", None), ("continue_work", None), ("continue-work", None),
    ("end_of_task", None), ("end-of-task", None), ("revise", None), ("revise-fast", None),
    ("run", None), ("nope", None),
])
def test_entry_phase_for_run(run_phase, expected):
    assert runstore.entry_phase_for_run(run_phase) == expected


def test_gated_and_workflow_constants():
    assert set(runstore.GATED_PHASES) < set(runstore.WORKFLOW_PHASES)
    assert "critic" in runstore.WORKFLOW_PHASES and "critic" not in runstore.GATED_PHASES


# -- naming ---------------------------------------------------------------


def _cleanup_families():
    text = CLEANUP_SKILL.read_text(encoding="utf-8")
    block = text.split("\n## Hardcoded sentinel allow-list", 1)[1].split("\n## ", 1)[0]
    families = re.findall(r"^\d+\.\s+`([^`]+)`", block, re.MULTILINE)
    families += ["run-state-*.json", "run-notes-*.md", "run-notes-*.md.1", "run-state-*.json.*.tmp"]
    return families


def test_workflow_state_name_avoids_every_cleanup_family(store):
    import fnmatch

    families = _cleanup_families()
    assert len(families) >= 9
    name = runstore.workflow_state_path(store, "t1").name
    for glob in families:
        assert not fnmatch.fnmatch(name, glob), (name, glob)
    assert not runstore._RECORD_FILE_RE.match(name)
    assert not fnmatch.fnmatch(name, "task-*.json")


def test_workflow_state_sits_below_the_depth_one_sweeps(tmp_path, store):
    runstore.write_workflow_state(store, runstore.new_workflow_state("t1", fixed_clock()))
    path = runstore.workflow_state_path(store, "t1")
    memory = tmp_path / ".workflow_artifacts" / "memory"
    assert path.relative_to(memory).parts[:-1] == ("runtime", "opencode")
    depth_one = [p.name for p in memory.iterdir() if p.is_file()]
    assert path.name not in depth_one


def test_store_listing_and_status_ignore_the_file(tmp_path, store):
    before = status.collect(tmp_path, task="t1")
    runstore.write_workflow_state(store, runstore.new_workflow_state("t1", fixed_clock()))
    assert runstore.list_records(store) == ([], 0)
    assert status.collect(tmp_path, task="t1") == before


# -- manifest consistency --------------------------------------------------


def _runnable_ids():
    feature = opencode_manifest.load_manifest(helpers.SOURCE_DIR)
    runnable, other = set(), set()
    entries = feature["catalog_entries"]
    for e in entries:
        ok = (
            e.get("status") == "supported"
            and "command" in (e.get("assets") or ())
            and bool((e.get("opencode") or {}).get("command"))
        )
        (runnable if ok else other).add(e["id"])
    return entries, runnable, other


def test_run_phase_constants_match_the_shipped_manifest():
    entries, runnable, other = _runnable_ids()
    assert len(entries) >= 30 and len(runnable) >= 10
    mapped = {i for i in runnable if runstore.entry_phase_for_run(i) is not None}
    unmapped = set(runstore.UNMAPPED_RUN_PHASES)
    assert mapped | unmapped == runnable
    assert not (mapped & unmapped)
    assert set(runstore.RUN_PHASES_FOR) == set(runstore.GATED_PHASES)
    accepted = set().union(*runstore.RUN_PHASES_FOR.values())
    assert accepted <= runnable
    assert runstore.PLAN_PRODUCER_PHASES <= runnable
    for run_id in mapped:
        accepted_for = runstore.RUN_PHASES_FOR.get(runstore.entry_phase_for_run(run_id), frozenset())
        assert run_id in accepted_for, "%s is mapped to %r, which does not accept it" % (
            run_id, runstore.entry_phase_for_run(run_id))
    for run_id in other:
        assert run_id not in accepted | set(runstore.PLAN_PRODUCER_PHASES) | mapped | unmapped, run_id


@pytest.mark.parametrize("entries", [["not-a-dict"], [{"phase": "plan"}], [{"origin": "adopted"}], [{"origin": 3, "phase": "plan"}],
    [{"origin": "adopted", "phase": "plan", "runs": 5}],
    [{"origin": "adopted", "phase": "plan", "critic_responses": 5}],
    [{"origin": "adopted", "phase": "plan", "harvested": 5}],
    [{"origin": "adopted", "phase": "plan", "evidence": "x"}],
    [{"origin": "adopted", "phase": "plan", "stage": "1"}]])
def test_malformed_entries_are_a_corrupt_record(tmp_path, entries):
    directory = runstore.store_dir(tmp_path, create=True)
    state = runstore.new_workflow_state("t1")
    state["entries"] = entries
    runstore.write_workflow_state(directory, state)
    with pytest.raises(runstore.RunStoreError) as caught:
        runstore.load_workflow_state(directory, "t1")
    assert caught.value.code == "corrupt-record"
