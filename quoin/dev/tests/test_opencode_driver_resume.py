"""Resume, orphan reconciliation and continuation replay of the OpenCode driver.

Continuation replay: a resumed run subscribes to live events only (the
`continuation-no-replay` compatibility row is verified with a condition:
compaction pruning can republish earlier completed parts when a configuration
layer enables it). The replay tests below force that case and check that it
can only cause a false failure, never a false success.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

import _opencode_driver_helpers as h
from quoin.opencode_adapter import driver, proctree, runstore
from quoin.opencode_adapter import events as ev

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture(autouse=True)
def _no_strays(tmp_path):
    yield
    h.cleanup_fakes(tmp_path)
    assert h.stray_pids(tmp_path) == []


# ------------------------------------------------------------- helpers


def _directory(prepared):
    return prepared.artifact_paths.sidecar.parent


def _first_attempt(tmp_path, scenario, **drv_kw):
    prepared = h.make_prepared(tmp_path, scenario)
    drv = h.make_driver(tmp_path, **drv_kw)
    handle, events = h.run_to_end(drv, prepared)
    return drv, prepared, handle, events


def _stage(prepared):
    """What `prepare` does in resume mode: stage the next attempt."""
    directory = _directory(prepared)
    record = runstore.load_record(directory, prepared.run_id)
    number = max((a["attempt"] for a in record["attempts"] if a["state"] != "staged"), default=0) + 1
    staged = runstore.new_attempt(
        number, pid=None, pgid=None, child_start=None, driver_pid=os.getpid(), driver_start=None,
        resume_mode="fresh", input_hashes_before=prepared.input_hashes,
    )
    staged["state"] = "staged"
    record["attempts"].append(staged)
    runstore.write_record(directory, record)


def _handoff(prepared):
    return driver.Handoff.from_checkpoint(runstore.load_checkpoint(_directory(prepared), prepared.run_id))


def _resume(drv, prepared):
    _stage(prepared)
    handle = drv.resume(_handoff(prepared), prepared)
    return handle, list(drv.observe(handle))


def _invocations(tmp_path):
    path = h.state_of(tmp_path) / "invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def _runs(tmp_path):
    return [i for i in _invocations(tmp_path) if i.get("attempt")]


def _effects(tmp_path):
    path = h.state_of(tmp_path) / "effects.log"
    return path.read_text().splitlines() if path.exists() else []


def _no_duplicate_parts(prepared):
    keys = [
        (e.native.type, e.native.id, e.native.revision)
        for e in h.read_events(prepared) if e.native is not None and e.native.id
    ]
    assert len(keys) == len(set(keys))


def _sequences_continue(prepared):
    seqs = [e.sequence for e in h.read_events(prepared)]
    assert seqs == list(range(1, len(seqs) + 1))


def _dead_identity():
    proc = subprocess.Popen(["sleep", "30"])
    start = proctree.start_time(proc.pid)
    proc.kill()
    proc.wait()
    return proc.pid, start


def _forge_driver(prepared, pid, start):
    directory = _directory(prepared)
    record = runstore.load_record(directory, prepared.run_id)
    record["attempts"][-1]["driver_pid"] = pid
    record["attempts"][-1]["driver_start"] = start
    runstore.write_record(directory, record)
    return record


def _wait(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _blocked(drv, prepared, reason="effect-uncertain"):
    _stage(prepared)
    with pytest.raises(driver.ResumeBlocked) as info:
        drv.resume(_handoff(prepared), prepared)
    assert info.value.reason == reason
    record = runstore.load_record(_directory(prepared), prepared.run_id)
    assert record["state"] == "interrupted" and record["resume_blocked"] == reason
    return record


# --------------------------------------------------------------- AC end to end


@pytest.mark.parametrize("flag", ["real", True, False])
def test_session_continuation_end_to_end(tmp_path, monkeypatch, flag):
    if flag != "real":
        monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", flag)
    settled = driver.STEP_SETTLING_VERIFIED
    drv, prepared, handle, _events = _first_attempt(tmp_path, "session_continuation")
    assert h.load_record(tmp_path, prepared.run_id)["state"] == "interrupted"
    if not settled:
        _stage(prepared)
        with pytest.raises(driver.ResumeBlocked) as info:
            drv.resume(_handoff(prepared), prepared)
        assert info.value.reason == "effect-uncertain"
        assert len(_runs(tmp_path)) == 1 and len(_effects(tmp_path)) == 1
        return
    handle2, _ = _resume(drv, prepared)
    assert handle2.outcome.state == "completed" and handle2.outcome.evidence == "full"
    runs = _runs(tmp_path)
    assert len(runs) == 2
    second = runs[1]["argv"]
    assert "--agent" in second and "--command" not in second
    assert "--session=" + runs[0]["session_id"] in second
    assert len(_effects(tmp_path)) == 1
    _no_duplicate_parts(prepared)
    _sequences_continue(prepared)
    totals = h.load_record(tmp_path, prepared.run_id)["usage_totals"]
    assert totals["input_tokens"] == 20
    assert totals == ev.usage_totals(h.read_events(prepared)).to_dict()


def test_a_replayed_part_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, _h, _e = _first_attempt(tmp_path, "session_continuation_replay")
    handle2, _ = _resume(drv, prepared)
    assert handle2.outcome.state == "completed"
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["counters_total"]["dropped_duplicates"] >= 3
    _no_duplicate_parts(prepared)
    _sequences_continue(prepared)
    assert record["usage_totals"]["input_tokens"] == 20


def test_a_replayed_error_without_an_id_fails_the_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, first, _e = _first_attempt(tmp_path, "transient_error_then_continue")
    assert first.outcome.state == "failed"
    handle2, events = _resume(drv, prepared)
    errors = [e for e in events if e.type is ev.EventType.ERROR and e.attempt == 2]
    assert errors
    assert handle2.outcome.state == "failed"


def test_nothing_ran_starts_a_fresh_attempt(tmp_path):
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "exit_early")
    handle2, _ = _resume(drv, prepared)
    runs = _runs(tmp_path)
    assert len(runs) == 2 and runs[1]["argv"] == runs[0]["argv"]
    assert "--session" not in runs[1]["argv"] and "--command" in runs[1]["argv"]
    assert runs[0]["session_id"] != runs[1]["session_id"]
    _sequences_continue(prepared)
    _no_duplicate_parts(prepared)
    assert handle2.attempt == 2


# ---------------------------------------------------------- blocked resumes


def test_an_open_step_blocks_the_resume(tmp_path):
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "crash_in_open_step")
    _blocked(drv, prepared)
    assert len(_runs(tmp_path)) == 1


def test_a_checkpoint_that_lags_the_sidecar_still_blocks(tmp_path):
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "crash_after_start")
    directory = _directory(prepared)
    checkpoint = runstore.load_checkpoint(directory, prepared.run_id)
    checkpoint["step_open"] = False
    runstore.write_checkpoint(directory, checkpoint)
    _blocked(drv, prepared)
    assert len(_runs(tmp_path)) == 1


def test_a_sidecar_behind_the_checkpoint_blocks(tmp_path):
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "session_continuation")
    directory = _directory(prepared)
    checkpoint = runstore.load_checkpoint(directory, prepared.run_id)
    checkpoint["last_sequence"] += 50
    runstore.write_checkpoint(directory, checkpoint)
    _blocked(drv, prepared, "sidecar-behind-checkpoint")
    assert len(_runs(tmp_path)) == 1


def test_a_handoff_for_another_run_is_refused(tmp_path):
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "session_continuation")
    handoff = _handoff(prepared)
    other = driver.Handoff(**dict(handoff.__dict__, run_id=handoff.run_id[:-1] + ("0" if handoff.run_id[-1] != "0" else "1")))
    with pytest.raises(ValueError):
        drv.resume(other, prepared)


def test_a_driver_lost_after_work_blocks_the_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    prepared = h.make_prepared(tmp_path, "step_closed_then_hang")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    gen = drv.observe(handle)
    for event in gen:
        if event.type is ev.EventType.USAGE:
            break
    record = _forge_driver(prepared, *_dead_identity())
    assert drv.reconcile_run(record) is True
    assert record["state"] == "interrupted" and record["attempts"][-1]["driver_lost"] is True
    _blocked(drv, prepared)
    assert len(_runs(tmp_path)) == 1 and len(_effects(tmp_path)) == 1
    gen.close()


def test_a_driver_lost_before_any_line_blocks_the_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    prepared = h.make_prepared(tmp_path, "effect_before_first_line")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    _wait(lambda: _effects(tmp_path))
    record = _forge_driver(prepared, *_dead_identity())
    assert drv.reconcile_run(record) is True
    assert not [e for e in h.read_events(prepared) if e.origin == "native"]
    _blocked(drv, prepared)
    assert len(_runs(tmp_path)) == 1 and len(_effects(tmp_path)) == 1
    drv.cancel(handle)


# ------------------------------------------------- run-wide downgrade facts


@pytest.mark.parametrize("scenario", ["task_background_then_boundary_crash", "task_denied_tail_then_boundary_crash"])
def test_an_earlier_downgrade_survives_a_clean_second_attempt(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, first, _e = _first_attempt(tmp_path, scenario)
    assert first.outcome.state == "interrupted"
    handle2, _ = _resume(drv, prepared)
    assert (handle2.outcome.state, handle2.outcome.evidence) == ("completed", "partial")
    assert handle2.outcome.reason == "earlier-attempt-downgrade"


# ------------------------------------------------------------- session loss


def test_an_unknown_session_ends_session_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "session_continuation")
    shutil.rmtree(h.state_of(tmp_path) / "sessions")
    (h.state_of(tmp_path) / "sessions").mkdir()
    handle2, _ = _resume(drv, prepared)
    assert (handle2.outcome.state, handle2.outcome.reason) == ("interrupted", "session-lost")
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["resume_blocked"] == "session-lost"
    _stage(prepared)
    with pytest.raises(driver.ResumeBlocked):
        drv.resume(_handoff(prepared), prepared)


# ------------------------------------------------------------------ orphans


def test_a_dead_driver_gets_its_group_killed_and_the_run_marked(tmp_path):
    prepared = h.make_prepared(tmp_path, "grandchild")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    _wait(lambda: (h.state_of(tmp_path) / "grandchildren.txt").exists())
    time.sleep(0.5)
    directory = _directory(prepared)
    record = _forge_driver(prepared, *_dead_identity())
    # The pure check never signals anything.
    table = proctree.snapshot()
    assert runstore.orphan_state(record, table) == "driver-lost"
    assert handle.proc.poll() is None and h.stray_pids(tmp_path)
    reconciled = drv.reconcile_task(h.TASK)
    assert reconciled["state"] == "interrupted"
    assert reconciled["history"][-1]["reason"] == "driver-lost"
    assert reconciled["attempts"][-1]["driver_lost"] is True
    assert runstore.load_record(directory, prepared.run_id)["state"] == "interrupted"
    _wait(lambda: handle.proc.poll() is not None)
    assert h.stray_pids(tmp_path) == []
    drv.cancel(handle)


def test_a_live_driver_leaves_the_run_alone(tmp_path):
    prepared = h.make_prepared(tmp_path, "hang")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    time.sleep(0.3)
    record = _forge_driver(prepared, os.getpid(), proctree.start_time(os.getpid()))
    assert drv.reconcile_run(record) is False
    assert runstore.load_record(_directory(prepared), prepared.run_id)["state"] == "running"
    assert handle.proc.poll() is None
    drv.cancel(handle)


# ---------------------------------------------------------------- torn tails


def test_a_torn_tail_is_truncated_and_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "session_continuation")
    sidecar = prepared.artifact_paths.sidecar
    with open(sidecar, "ab") as fh:
        fh.write(b'{"schema_version": 1, "ty')
    handle2, _ = _resume(drv, prepared)
    assert handle2.outcome.state == "completed"
    assert h.load_record(tmp_path, prepared.run_id)["counters_total"]["torn-tail"] == 1
    assert runstore.read_sidecar(sidecar).torn_tail is False
    _sequences_continue(prepared)


def test_corruption_in_the_middle_blocks_with_a_store_error(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "STEP_SETTLING_VERIFIED", True)
    drv, prepared, _h1, _e = _first_attempt(tmp_path, "session_continuation")
    sidecar = prepared.artifact_paths.sidecar
    lines = sidecar.read_bytes().splitlines(keepends=True)
    lines[1] = b"not json\n"
    sidecar.write_bytes(b"".join(lines))
    _stage(prepared)
    with pytest.raises(runstore.RunStoreError):
        drv.resume(_handoff(prepared), prepared)
    assert len(_runs(tmp_path)) == 1
