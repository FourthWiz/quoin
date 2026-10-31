from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from quoin.opencode_adapter import launch_codes, paths, profile_dirs, runstore
from quoin.opencode_adapter import retention as rt
from quoin.opencode_adapter.proctree import ProcInfo

UID = os.getuid()
NOW = 1_800_000_000.0
DAY = 86400.0
RUN_A = "oc-20260101T000000Z-aaaaaaaa"
RUN_B = "oc-20260101T000001Z-bbbbbbbb"
RUN_C = "oc-20260101T000002Z-cccccccc"


def _touch(path, age=0.0, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(str(path), (NOW - age, NOW - age))
    return path


def _record(state="completed", profile="work", attempts=None, refusal=None):
    return {"state": state, "request": {"profile": profile}, "attempts": attempts or [], "refusal": refusal}


def _write_run(directory, run_id, record, age=0.0):
    _touch(directory / (run_id + ".run.json"), age, json.dumps(record))
    _touch(directory / (run_id + ".jsonl"), age, "x")


def _proc(pid, start="100"):
    return ProcInfo(pid=pid, ppid=1, pgid=pid, state="S", start=start)


def _attempt(**kw):
    base = {"pid": None, "child_start": None, "driver_pid": None, "driver_start": None}
    base.update(kw)
    return base


def _world(tmp_path):
    adapter = tmp_path / "adapter"
    adapter.mkdir(mode=0o700)
    root = adapter / "work"
    root.mkdir(mode=0o700)
    dirs = profile_dirs.ProfileDirs.at(root)
    for sub in ("data", "state", "cache", "tmp"):
        (root / sub).mkdir(mode=0o700)
    project = tmp_path / "proj"
    (project / ".workflow_artifacts").mkdir(parents=True)
    store = runstore.store_dir(project, create=True)
    profile_dirs.record_project(dirs, project)
    _touch(root / "profile.json", text="{}")
    _touch(dirs.data / "opencode" / "auth.json", text="secret")
    _touch(dirs.data / "opencode" / "mcp-auth.json", text="secret")
    _touch(dirs.data / "opencode" / "opencode.db", text="db")
    _touch(dirs.state / "npmrc", text="token")
    _touch(dirs.state / "other", text="o")
    _touch(dirs.cache / "opencode" / "pkg" / "f")
    return adapter, dirs, project, store


def _plan(adapter, extra=None, table=None, env=None, profile="work"):
    return rt.plan_purge(adapter_root=adapter, profile=profile, extra_project=extra,
                         env=env or {}, uid=UID, table=table, now=NOW)


def _removed(plan):
    return {i.display for i in plan.remove}


def test_refusals(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter, env={paths.ENV_STATE_DIR: "/x"})
    assert e.value.code == "purge-inside-launch"
    for bad in ("tests", "Bad Name", ""):
        with pytest.raises(rt.PurgeRefused) as e:
            _plan(adapter, profile=bad)
        assert e.value.code == "purge-profile-invalid"
    link = adapter / "linked"
    link.symlink_to(dirs.root)
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter, profile="linked")
    assert e.value.code == "purge-profile-unsafe"
    dirs.index.write_text("not json")
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter)
    assert e.value.code == "purge-profile-unsafe"


def test_absent_root_is_an_empty_plan(tmp_path):
    plan = _plan(tmp_path, profile="ghost")
    assert plan.remove == () and plan.keep == ()


def test_kept_set_is_exact_and_everything_else_goes(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record())
    _touch(dirs.tests / "k" / "t" / "pin")
    _touch(dirs.exports / "e")
    plan = _plan(adapter)
    kept = {(k.display, k.reason) for k in plan.keep}
    assert kept >= {
        ("profile.json", "profile-stamp"), ("projects.json", "project-index"),
        ("projects.json.lock", "project-index"),
        ("data/opencode/auth.json", "credential"), ("data/opencode/mcp-auth.json", "credential"),
        ("state/npmrc", "credential"),
    }
    assert _removed(plan) >= {"data", "state", "cache", "tests", "exports", RUN_A}
    assert "profile.json" not in _removed(plan)


def test_data_without_exclude_would_remove_credentials(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    plan = _plan(adapter)
    data = next(i for i in plan.remove if i.display == "data")
    assert "opencode/auth.json" in data.exclude and "opencode/mcp-auth.json" in data.exclude
    state = next(i for i in plan.remove if i.display == "state")
    assert state.exclude == ("npmrc",)


def test_referenced_run_is_planned_and_named(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record())
    _touch(store / "workflow-t1.json", text=json.dumps({"run": RUN_A}))
    plan = _plan(adapter)
    assert RUN_A in _removed(plan)
    assert plan.referenced_runs[0]["run_id"] == RUN_A
    assert plan.referenced_runs[0]["task"] == "t1"
    assert plan.referenced_runs[0]["referenced_by"] == "workflow-state"


def test_other_profile_and_orphans(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record(profile="personal"))
    _touch(store / (RUN_B + ".jsonl"), text="")
    plan = _plan(adapter)
    assert RUN_A not in _removed(plan) and RUN_B not in _removed(plan)
    assert (RUN_B, "unattributable") in {(k.display, k.reason) for k in plan.keep}


def test_extra_project_store_is_visited(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    extra = tmp_path / "extra"
    (extra / ".workflow_artifacts").mkdir(parents=True)
    _write_run(runstore.store_dir(extra, create=True), RUN_C, _record())
    assert RUN_C in _removed(_plan(adapter, extra=extra))
    assert RUN_C not in _removed(_plan(adapter))


def test_unreadable_store_is_not_visited(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record())
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(exist_ok=True)
    memory.chmod(0)
    try:
        plan = _plan(adapter)
    finally:
        memory.chmod(0o700)
    assert RUN_A not in _removed(plan)
    assert {"project": os.path.realpath(str(project)), "reason": "unreadable"} in plan.stores_not_visited


def test_running_driver_dead_or_live(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    rec = _record("running", attempts=[_attempt(driver_pid=10, driver_start="100")])
    _write_run(store, RUN_A, rec, age=40 * DAY)
    assert RUN_A in _removed(_plan(adapter, table={1: _proc(1)}))
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter, table={10: _proc(10)})
    assert e.value.code == "purge-run-active"
    assert e.value.blocking[0].reason == "live-driver"


@pytest.mark.parametrize("attempts", [[], [_attempt(driver_pid=10, driver_start=None)]])
def test_undecidable_running_blocks_inside_window_only(tmp_path, attempts):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record("running", attempts=attempts), age=600)
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter, table={1: _proc(1)})
    blocker = e.value.blocking[0]
    assert (blocker.run_id_or_task, blocker.reason) == (RUN_A, "liveness-unknown")
    _write_run(store, RUN_A, _record("running", attempts=attempts), age=40 * DAY)
    assert RUN_A in _removed(_plan(adapter, table={1: _proc(1)}))


def test_live_lock_refusal_names_pid_file(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record())
    _touch(store / "workflow-t9.json", text=json.dumps({"run": RUN_A}))
    _touch(project / ".workflow_artifacts" / "memory" / "run-supervisor-t9.pid", text=json.dumps({"pid": os.getpid()}))
    with pytest.raises(rt.PurgeRefused) as e:
        _plan(adapter)
    blocker = e.value.blocking[0]
    assert blocker.reason == "live-lock" and blocker.run_id_or_task == "t9"
    assert blocker.pid_file.endswith("run-supervisor-t9.pid")


def test_other_profiles_lock_does_not_block(tmp_path):
    adapter, dirs, project, store = _world(tmp_path)
    _write_run(store, RUN_A, _record(profile="personal"))
    _touch(store / "workflow-t9.json", text=json.dumps({"run": RUN_A}))
    _touch(project / ".workflow_artifacts" / "memory" / "run-supervisor-t9.pid", text=json.dumps({"pid": os.getpid()}))
    assert _plan(adapter).remove


def test_purge_refused_codes_are_registered():
    root = Path(rt.__file__).parent
    codes = set()
    for name in ("retention.py", "../cli.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "PurgeRefused"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                codes.add(node.args[0].value)
    assert codes <= set(launch_codes.RETENTION_CODES)
    assert set(launch_codes.RETENTION_CODES) <= set(launch_codes.REGISTRY)
    assert set(launch_codes.RETENTION_CODES) == set(rt.PURGE_MESSAGES)
