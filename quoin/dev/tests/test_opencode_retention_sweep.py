from __future__ import annotations

import json
import os

import pytest

from quoin.opencode_adapter import boundaries, handoff, jsonio, paths, profile_dirs, runstore
from quoin.opencode_adapter import retention as rt
from quoin.opencode_adapter.proctree import ProcInfo

UID = os.getuid()
NOW = 1_800_000_000.0
RUN_A = "oc-20260101T000000Z-aaaaaaaa"
RUN_B = "oc-20260101T000001Z-bbbbbbbb"
RUN_C = "oc-20260101T000002Z-cccccccc"


def _store(root):
    return runstore.store_dir(root, create=True)


def _touch(path, age=0.0, text="{}"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(str(path), (NOW - age, NOW - age))
    return path


def _load(root, attribute=False, table=None, budget=None):
    return rt.ProjectEvidence.load(root, uid=UID, attribute=attribute, table=table,
                                   budget=budget or rt.Budget(), now=NOW)


def _record(state="running", profile="work", attempts=None, refusal=None, **extra):
    rec = {"state": state, "request": {"profile": profile}, "attempts": attempts or [], "refusal": refusal}
    rec.update(extra)
    return rec


def _write_run(directory, run_id, record, age=0.0, sidecar_age=None, checkpoint=False):
    _touch(directory / (run_id + ".run.json"), age, json.dumps(record))
    _touch(directory / (run_id + ".jsonl"), age if sidecar_age is None else sidecar_age, "x")
    if checkpoint:
        _touch(directory / (run_id + ".checkpoint.json"), age)


def _table(*procs):
    return {p.pid: p for p in procs}


def _proc(pid, start="100", state="S", pgid=None):
    return ProcInfo(pid=pid, ppid=1, pgid=pgid or pid, state=state, start=start)


# ---- references -----------------------------------------------------------

def test_references_from_three_sources(tmp_path):
    d = _store(tmp_path)
    _touch(d / "workflow-t1.json", text="{\"a\": \"%s\", \"old\": \"%s\"}" % (RUN_A, RUN_C))
    _touch(d / "task-t2.json", text=RUN_B)
    cdir = handoff.continuation_dir(tmp_path, create=True)
    _touch(cdir / "t3.json", text="run %s" % RUN_C)
    ev = _load(tmp_path)
    assert ev.readable
    assert ev.referenced[RUN_A] == ("t1", "workflow-state")
    assert ev.referenced[RUN_B] == ("t2", "pointer")
    assert RUN_C in ev.referenced
    assert ev.unfinished_tasks == {"t1", "t2", "t3"}


def test_continuation_without_state_protects_run(tmp_path):
    _store(tmp_path)
    cdir = handoff.continuation_dir(tmp_path, create=True)
    _touch(cdir / "solo.json", text=RUN_A)
    ev = _load(tmp_path)
    assert ev.referenced[RUN_A] == ("solo", "continuation")
    assert "solo" in ev.unfinished_tasks


def test_unreadable_workflow_state_marks_unreadable(tmp_path):
    d = _store(tmp_path)
    p = _touch(d / "workflow-t1.json", text=RUN_A)
    os.chmod(str(p), 0)
    try:
        ev = _load(tmp_path)
    finally:
        os.chmod(str(p), 0o600)
    assert not ev.readable and ev.reason == "unreadable"


def test_symlinked_store_component_is_unsafe(tmp_path):
    d = _store(tmp_path)
    moved = tmp_path / "moved"
    os.rename(str(d), str(moved))
    os.symlink(str(moved), str(d))
    ev = _load(tmp_path)
    assert not ev.readable and ev.reason == "unsafe"


def test_missing_store(tmp_path):
    ev = _load(tmp_path)
    assert not ev.readable and ev.reason == "missing"


def test_symlinked_continuation_dir_is_unsafe(tmp_path):
    _store(tmp_path)
    cdir = handoff.continuation_dir(tmp_path, create=True)
    real = tmp_path / "real"
    os.rename(str(cdir), str(real))
    os.symlink(str(real), str(cdir))
    ev = _load(tmp_path)
    assert not ev.readable and ev.reason == "unsafe"


# ---- finished tasks ---------------------------------------------------------

def test_finished_only_with_finalized_move(tmp_path):
    d = _store(tmp_path)
    _touch(d / "workflow-done.json", age=1000, text=RUN_A)
    art = tmp_path / ".workflow_artifacts"
    (art / "finalized" / "done").mkdir(parents=True)
    os.utime(str(art / "finalized" / "done"), (NOW, NOW))
    assert "done" not in _load(tmp_path).unfinished_tasks


def test_task_folder_still_present_is_unfinished(tmp_path):
    d = _store(tmp_path)
    _touch(d / "workflow-done.json", age=1000, text=RUN_A)
    art = tmp_path / ".workflow_artifacts"
    (art / "finalized" / "done").mkdir(parents=True)
    (art / "done").mkdir()
    assert "done" in _load(tmp_path).unfinished_tasks


def test_no_finalized_move_is_unfinished(tmp_path):
    d = _store(tmp_path)
    _touch(d / "workflow-open.json", text=RUN_A)
    assert "open" in _load(tmp_path).unfinished_tasks


def test_finalized_name_reused_by_newer_pointer_is_unfinished(tmp_path):
    d = _store(tmp_path)
    art = tmp_path / ".workflow_artifacts"
    (art / "finalized" / "again").mkdir(parents=True)
    os.utime(str(art / "finalized" / "again"), (NOW - 5000, NOW - 5000))
    _touch(d / "task-again.json", age=10, text=RUN_A)
    assert "again" in _load(tmp_path).unfinished_tasks


# ---- locks ------------------------------------------------------------------

def test_live_lock_detected(tmp_path):
    _store(tmp_path)
    mem = tmp_path / ".workflow_artifacts" / "memory"
    _touch(mem / "run-supervisor-locked.pid", text=json.dumps({"pid": os.getpid()}))
    assert _load(tmp_path).live_lock_tasks == {"locked"}
    assert boundaries._live_locks(str(tmp_path)) == {"locked"}


def test_unscannable_memory_dir_marks_unreadable(tmp_path):
    _store(tmp_path)
    mem = tmp_path / ".workflow_artifacts" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    os.chmod(str(mem), 0)
    try:
        ev = _load(tmp_path)
        assert not ev.readable and ev.reason == "unreadable"
        assert boundaries._live_locks(str(tmp_path)) == frozenset()
    finally:
        os.chmod(str(mem), 0o700)


# ---- index ------------------------------------------------------------------

def _dirs(tmp_path, projects):
    root = tmp_path / "prof"
    root.mkdir(mode=0o700)
    dirs = profile_dirs.ProfileDirs.__new__(profile_dirs.ProfileDirs)
    return root, dirs


def test_read_index_refuses_non_string_entry(tmp_path, monkeypatch):
    idx = tmp_path / "projects.json"
    idx.write_text(json.dumps({"index_format": profile_dirs.INDEX_FORMAT, "projects": ["/a", 3]}))

    class D:
        index = idx

    with pytest.raises(profile_dirs.ProfileDirUnsafe):
        profile_dirs.read_index(D())


def test_project_keys_and_unattributable(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    idx = tmp_path / "projects.json"
    idx.write_text(json.dumps({"index_format": profile_dirs.INDEX_FORMAT,
                               "projects": [os.path.realpath(str(proj))]}))

    class D:
        index = idx

    keys = rt.project_keys(D())
    assert paths.project_key(proj) in keys
    assert "0123456789abcdef" not in keys


def test_read_index_absent_is_empty(tmp_path):
    class D:
        index = tmp_path / "none.json"

    assert profile_dirs.read_index(D()) == []


# ---- run_activity -----------------------------------------------------------

def _attempt(**kw):
    base = {"pid": None, "child_start": None, "driver_pid": None, "driver_start": None}
    base.update(kw)
    return base


def test_refused_prepared_is_terminal():
    assert rt.run_activity(_record("prepared", refusal={"code": "x"}), {}, NOW, NOW) is None


def test_prepared_window():
    assert rt.run_activity(_record("prepared"), {}, NOW, NOW - 60) == "prepare-in-progress"
    assert rt.run_activity(_record("prepared"), {}, NOW, NOW - 3600) is None


def test_terminal_states():
    assert rt.run_activity(_record("succeeded"), {}, NOW, NOW) is None


def test_live_driver():
    rec = _record(attempts=[_attempt(driver_pid=10, driver_start="100")])
    assert rt.run_activity(rec, _table(_proc(10, "100")), NOW, NOW - 99999) == "live-driver"


def test_dead_driver_pid_reused_is_terminal():
    rec = _record(attempts=[_attempt(driver_pid=10, driver_start="100", pid=11, child_start="100")])
    table = _table(_proc(10, "999"), _proc(11, "888"))
    assert rt.run_activity(rec, table, NOW, NOW) is None


def test_live_child_with_dead_driver():
    rec = _record(attempts=[_attempt(driver_pid=10, driver_start="100", pid=11, child_start="100")])
    table = _table(_proc(11, "100"))
    assert rt.run_activity(rec, table, NOW, NOW - 99999) == "live-child"


UNKNOWN_SHAPES = {
    "no-attempt": (_record(attempts=[]), _table(_proc(1))),
    "staged": (_record(attempts=[_attempt(driver_pid=10)]), _table(_proc(1))),
    "driver-start-unset": (_record(attempts=[_attempt(driver_pid=10, driver_start=None)]), _table(_proc(1))),
    "no-table": (_record(attempts=[_attempt(driver_pid=10, driver_start="100")]), None),
}


@pytest.mark.parametrize("shape", sorted(UNKNOWN_SHAPES))
def test_liveness_unknown_inside_and_past_window(shape):
    rec, table = UNKNOWN_SHAPES[shape]
    window = rt.stale_window(rec)
    assert rt.run_activity(rec, table, NOW, NOW - 60) == "liveness-unknown"
    assert rt.run_activity(rec, table, NOW, NOW - window - 1) is None


def test_window_honours_record_max_run_seconds():
    rec = _record(limits={"max_run_seconds": 10800})
    assert rt.run_activity(rec, None, NOW, NOW - 7200) == "liveness-unknown"
    assert rt.run_activity(_record(), None, NOW, NOW - 7200) is None


def test_future_mtime_reads_age_zero():
    assert rt.run_activity(_record("prepared"), {}, NOW, NOW + 500) == "prepare-in-progress"


def test_sidecar_keeps_old_record_active(tmp_path):
    d = _store(tmp_path)
    _write_run(d, RUN_A, _record(), age=99999, sidecar_age=5)
    ev = _load(tmp_path, attribute=True, table=None)
    assert ev.runs[RUN_A].active
    (tmp_path / "b").mkdir()
    d2 = _store(tmp_path / "b")
    _write_run(d2, RUN_A, _record(), age=99999)
    assert not _load(tmp_path / "b", attribute=True, table=None).runs[RUN_A].active


# ---- attribution --------------------------------------------------------------

def test_attribution_profile_and_orphans(tmp_path):
    d = _store(tmp_path)
    _write_run(d, RUN_A, _record("succeeded", profile="work"))
    _touch(d / (RUN_B + ".run.json"), text="not json")
    _touch(d / (RUN_B + ".jsonl"), text="x")
    _touch(d / (RUN_C + ".jsonl"), text="")
    ev = _load(tmp_path, attribute=True, table={})
    assert ev.runs[RUN_A] == rt.RunFacts("work", False, False)
    assert ev.runs[RUN_B].profile is None
    assert ev.runs[RUN_C].empty_orphan


def test_attribute_false_reads_no_records(tmp_path, monkeypatch):
    d = _store(tmp_path)
    _write_run(d, RUN_A, _record())
    calls = []
    real = rt._read_record
    monkeypatch.setattr(rt, "_read_record", lambda p: calls.append(p) or real(p))
    ev = _load(tmp_path, attribute=False)
    assert not calls and ev.runs == {}
    _load(tmp_path, attribute=True, table={})
    assert len(calls) == 1
