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


# ---- sweep ------------------------------------------------------------------

DAY = 86400.0


def _world(tmp_path, other=True):
    project = tmp_path / "proj"
    (project / ".workflow_artifacts").mkdir(parents=True)
    root = tmp_path / "prof"
    root.mkdir(mode=0o700)
    dirs = profile_dirs.ProfileDirs.at(root)
    for sub in ("data", "state", "cache", "tmp"):
        (root / sub).mkdir(mode=0o700)
    profile_dirs.record_project(dirs, project)
    peer = None
    if other:
        peer = tmp_path / "peer"
        (peer / ".workflow_artifacts").mkdir(parents=True)
        _store(peer)
        profile_dirs.record_project(dirs, peer)
    return project, peer, dirs


def _policy(wide=30, project=None, **kinds):
    keys = ("runs_days", "cache_days", "logs_days", "crash_days", "exports_days")
    w = {k: kinds.get(k, wide) for k in keys}
    p = dict(w)
    if project is not None:
        p["runs_days"] = project
    return rt.RetentionPolicy(w, p)


def _key(path):
    return paths.project_key(path)


def _dir_aged(path, age):
    path.mkdir(parents=True, exist_ok=True)
    os.utime(str(path), (NOW - age, NOW - age))


def _run_sweep(dirs, project, policy, table=None, current_run=None):
    return rt.sweep(dirs=dirs, project_root=project, policy=policy, now=NOW, uid=UID,
                    profile="work", table=table, current_run=current_run)


def test_project_scope_applies_to_current_project_only(tmp_path):
    project, peer, dirs = _world(tmp_path)
    store = _store(project)
    _write_run(store, RUN_A, _record("completed"), age=2 * DAY)
    _touch(dirs.tests / _key(peer) / "t1" / "pin.json", age=2 * DAY)
    _touch(dirs.snapshots / _key(peer) / "s1" / "f", age=2 * DAY)
    _touch(dirs.workflow / _key(peer) / "t1" / "w", age=2 * DAY)
    _touch(dirs.data / "opencode" / "log" / "a.log", age=2 * DAY)
    for d in (dirs.tests / _key(peer) / "t1", dirs.snapshots / _key(peer) / "s1", dirs.workflow / _key(peer) / "t1"):
        os.utime(str(d), (NOW - 2 * DAY, NOW - 2 * DAY))
    out = _run_sweep(dirs, project, _policy(wide=30, project=1))
    assert out["status"] == "done"
    assert not (store / (RUN_A + ".run.json")).exists()
    assert (dirs.tests / _key(peer) / "t1").exists()
    assert (dirs.snapshots / _key(peer) / "s1").exists()
    assert (dirs.workflow / _key(peer) / "t1").exists()
    assert (dirs.data / "opencode" / "log" / "a.log").exists()


def test_unfinished_task_keeps_evidence_until_finalized(tmp_path):
    project, peer, dirs = _world(tmp_path, other=False)
    store = _store(project)
    key = _key(project)
    _write_run(store, RUN_A, _record("completed"), age=40 * DAY)
    _touch(store / "workflow-t1.json", age=40 * DAY, text=json.dumps({"run": RUN_A}))
    (project / ".workflow_artifacts" / "t1").mkdir()
    _touch(dirs.tests / key / "t1" / "pin", age=40 * DAY)
    _dir_aged(dirs.tests / key / "t1", 40 * DAY)
    _dir_aged(dirs.tests / key, 40 * DAY)
    _run_sweep(dirs, project, _policy())
    assert (dirs.tests / key / "t1").exists()
    assert (store / (RUN_A + ".run.json")).exists()
    # finalized move
    (project / ".workflow_artifacts" / "t1").rmdir()
    _dir_aged(project / ".workflow_artifacts" / "finalized" / "t1", 1 * DAY)
    _run_sweep(dirs, project, _policy())
    assert not (dirs.tests / key / "t1").exists()
    assert not (store / (RUN_A + ".run.json")).exists()


def _attempt_rec(state="running", **attempt):
    base = {"pid": None, "child_start": None, "driver_pid": None, "driver_start": None}
    base.update(attempt)
    return _record(state, attempts=[base] if attempt else [])


def test_run_shapes_in_the_sweep(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    store = _store(project)
    ids = {n: "oc-20260101T0000%02dZ-%08x" % (n, n) for n in range(1, 7)}
    _write_run(store, ids[1], _record("prepared", refusal={"code": "x"}), age=40 * DAY)
    _write_run(store, ids[2], _attempt_rec(driver_pid=10, driver_start="100"), age=40 * DAY)
    _write_run(store, ids[3], _attempt_rec(driver_pid=20, driver_start="100"), age=40 * DAY)
    _write_run(store, ids[4], _attempt_rec(), age=40 * DAY)
    _write_run(store, ids[5], _attempt_rec(), age=600)
    table = _table(_proc(20, "100"))
    _run_sweep(dirs, project, _policy(), table=table)
    exists = {n: (store / (i + ".run.json")).exists() for n, i in ids.items() if n < 6}
    assert exists == {1: False, 2: False, 3: True, 4: False, 5: True}
    # no table: old undecidable removed, fresh kept
    _write_run(store, ids[6], _attempt_rec(driver_pid=1, driver_start="1"), age=40 * DAY)
    _run_sweep(dirs, project, _policy(), table=None)
    assert not (store / (ids[6] + ".run.json")).exists()
    assert (store / (ids[5] + ".run.json")).exists()


def test_other_profile_and_unattributable_runs_kept(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    store = _store(project)
    _write_run(store, RUN_A, _record("completed", profile="personal"), age=40 * DAY)
    _touch(store / (RUN_B + ".jsonl"), age=40 * DAY, text="not empty")
    _touch(store / (RUN_C + ".jsonl"), age=40 * DAY, text="")
    _run_sweep(dirs, project, _policy())
    assert (store / (RUN_A + ".run.json")).exists()
    assert (store / (RUN_B + ".jsonl")).exists()
    assert not (store / (RUN_C + ".jsonl")).exists()


def test_empty_orphan_fresh_is_kept(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    store = _store(project)
    _touch(store / (RUN_C + ".jsonl"), age=60, text="")
    _run_sweep(dirs, project, _policy())
    assert (store / (RUN_C + ".jsonl")).exists()


def test_profile_in_use_skips_cache_and_tmp_but_not_logs(tmp_path):
    project, peer, dirs = _world(tmp_path)
    _write_run(_store(peer), RUN_A, _attempt_rec(), age=60)
    old = 40 * DAY
    _touch(dirs.cache / "opencode" / "bin" / "tool", age=old)
    _dir_aged(dirs.cache / "opencode" / "bin", old)
    _touch(dirs.tmp / "x", age=old)
    _dir_aged(dirs.tmp, old)
    _touch(dirs.data / "opencode" / "log" / "a.log", age=old)
    out = _run_sweep(dirs, project, _policy())
    assert (dirs.cache / "opencode" / "bin").exists()
    assert (dirs.tmp / "x").exists()
    assert not (dirs.data / "opencode" / "log" / "a.log").exists()
    assert out["removed"] == {"logs": 1}
    # past the stale window the undecidable record no longer protects the profile
    _write_run(_store(peer), RUN_A, _attempt_rec(), age=40 * DAY)
    _run_sweep(dirs, project, _policy())
    assert not (dirs.cache / "opencode" / "bin").exists()
    assert not (dirs.tmp / "x").exists()


def test_manifest_siblings_are_one_unit(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    base = dirs.cache / "opencode"
    old = 40 * DAY
    _touch(base / "package.json", age=1 * DAY)
    _touch(base / "node_modules" / "m" / "f", age=old)
    for d in (base / "node_modules" / "m", base / "node_modules"):
        _dir_aged(d, old)
    _touch(base / "bin" / "tool", age=old)
    _dir_aged(base / "bin", old)
    _dir_aged(base, old)
    _run_sweep(dirs, project, _policy())
    assert (base / "package.json").exists() and (base / "node_modules").exists()
    assert not (base / "bin").exists()
    os.utime(str(base / "package.json"), (NOW - old, NOW - old))
    _run_sweep(dirs, project, _policy())
    assert not (base / "package.json").exists() and not (base / "node_modules").exists()


def test_fresh_package_with_old_files_is_kept_whole(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    pkg = dirs.cache / "opencode" / "pkgdir"
    _touch(pkg / "a.js", age=400 * DAY)
    _touch(pkg / "sub" / "b.js", age=400 * DAY)
    _dir_aged(pkg / "sub", 400 * DAY)
    os.utime(str(pkg), (NOW, NOW))
    _run_sweep(dirs, project, _policy())
    assert (pkg / "a.js").exists() and (pkg / "sub" / "b.js").exists()


def test_cache_tmp_logs_crash_exports_records_by_own_days(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    log = dirs.data / "opencode" / "log"
    age = 10 * DAY
    _touch(log / "heap-1.heapsnapshot", age=age)
    _touch(log / "app.log", age=age)
    _touch(dirs.exports / "e1" / "f", age=age)
    _dir_aged(dirs.exports / "e1", age)
    _touch(dirs.records / ("start-%s.json" % ("a" * 12)), age=age)
    _touch(dirs.cache / "other" / "f", age=age)
    _dir_aged(dirs.cache / "other", age)
    _touch(dirs.tmp / "sub" / "t", age=age)
    _dir_aged(dirs.tmp / "sub", age)
    _dir_aged(dirs.tmp, age)
    policy = _policy(wide=30, crash_days=5)
    _run_sweep(dirs, project, policy)
    assert not (log / "heap-1.heapsnapshot").exists()
    assert (log / "app.log").exists()
    assert (dirs.exports / "e1").exists()
    assert (dirs.cache / "other").exists()
    assert (dirs.tmp / "sub" / "t").exists()
    _run_sweep(dirs, project, _policy(wide=5))
    assert not (log / "app.log").exists()
    assert not (dirs.exports / "e1").exists()
    assert not (dirs.cache / "other").exists()
    assert not (dirs.tmp / "sub" / "t").exists()
    assert not (dirs.tmp / "sub").exists()
    assert dirs.tmp.exists()
    assert not list(dirs.records.iterdir())


def test_default_policy_skips_and_database_is_never_swept(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    db = dirs.data / "opencode" / "opencode.db"
    for suffix in ("", "-wal", "-shm"):
        _touch(db.with_name(db.name + suffix), age=400 * DAY)
    _touch(dirs.data / "opencode" / "auth.json", age=400 * DAY)
    empty = rt.RetentionPolicy({k: None for k in ("runs_days", "cache_days", "logs_days", "crash_days", "exports_days")},
                               {k: None for k in ("runs_days", "cache_days", "logs_days", "crash_days", "exports_days")})
    assert _run_sweep(dirs, project, empty)["status"] == "skipped"
    _run_sweep(dirs, project, _policy(wide=1))
    for suffix in ("", "-wal", "-shm"):
        assert db.with_name(db.name + suffix).exists()
    assert (dirs.data / "opencode" / "auth.json").exists()


def test_invalid_days_skip_the_class(tmp_path):
    project, _peer, dirs = _world(tmp_path, other=False)
    _touch(dirs.exports / "e1", age=400 * DAY)
    bad = _policy(exports_days=0)
    _run_sweep(dirs, project, bad)
    assert (dirs.exports / "e1").exists()


def test_live_lock_keeps_snapshots_and_missing_store_keeps_units(tmp_path):
    project, peer, dirs = _world(tmp_path)
    _store(project)
    key = _key(project)
    _touch(dirs.snapshots / key / "s1" / "f", age=40 * DAY)
    _dir_aged(dirs.snapshots / key / "s1", 40 * DAY)
    memory = project / ".workflow_artifacts" / "memory"
    _touch(memory / "run-supervisor-t9.pid", text=json.dumps({"pid": os.getpid()}))
    _run_sweep(dirs, project, _policy())
    assert (dirs.snapshots / key / "s1").exists()
    (memory / "run-supervisor-t9.pid").unlink()
    gone = tmp_path / "gone"
    (gone / ".workflow_artifacts").mkdir(parents=True)
    profile_dirs.record_project(dirs, gone)
    _touch(dirs.tests / _key(gone) / "t1" / "pin", age=40 * DAY)
    _dir_aged(dirs.tests / _key(gone) / "t1", 40 * DAY)
    _run_sweep(dirs, project, _policy())
    assert not (dirs.snapshots / key / "s1").exists()
    assert (dirs.tests / _key(gone) / "t1").exists()


def test_sweep_never_raises(tmp_path, monkeypatch):
    project, _peer, dirs = _world(tmp_path, other=False)

    def boom(**_):
        raise RuntimeError("x")

    monkeypatch.setattr(rt, "plan_sweep", boom)
    assert _run_sweep(dirs, project, _policy())["status"] == "error"


def test_driver_sweeps_once_per_profile_and_not_on_resume(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quoin.opencode_adapter import driver as drv

    calls = []
    monkeypatch.setattr(rt, "sweep", lambda **kw: calls.append(kw) or {"status": "done"})
    monkeypatch.setattr(rt.RetentionPolicy, "from_effective", classmethod(lambda cls, e: "policy"))
    d = drv.OpenCodeDriver.__new__(drv.OpenCodeDriver)
    d._swept = set()
    d._clock = lambda: NOW
    d._proc = SimpleNamespace(snapshot=lambda: None)
    ev = SimpleNamespace(profile="work", effective=None)
    assert d._retention_sweep(ev, "dirs", tmp_path, "r1", None) == {"status": "done"}
    assert d._retention_sweep(ev, "dirs", tmp_path, "r2", None) == {"status": "skipped", "reason": "already-swept"}
    other = SimpleNamespace(profile="other", effective=None)
    assert d._retention_sweep(other, "dirs", tmp_path, "r3", "oc-x")["reason"] == "resume"
    assert len(calls) == 1 and calls[0]["current_run"] == "r1"
    monkeypatch.setattr(rt, "sweep", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    third = SimpleNamespace(profile="third", effective=None)
    assert d._retention_sweep(third, "dirs", tmp_path, "r4", None)["status"] == "error"


def test_start_sweep_is_best_effort(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quoin import cli

    seen = []
    monkeypatch.setattr(rt, "sweep", lambda **kw: seen.append(kw))
    launch = SimpleNamespace(retention="p", profile_root=tmp_path, profile="work")
    cli._opencode_start_sweep(launch, tmp_path)
    assert len(seen) == 1
    monkeypatch.setattr(rt, "sweep", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    cli._opencode_start_sweep(launch, tmp_path)
    cli._opencode_start_sweep(SimpleNamespace(retention=None, profile_root=None, profile="w"), tmp_path)
