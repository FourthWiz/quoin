"""Retention evidence, purge scope and deletion-engine robustness."""
from __future__ import annotations

import json
import os

import pytest

from quoin import cli
from quoin.opencode_adapter import paths, profile_dirs
from quoin.opencode_adapter import retention as rt

import test_opencode_purge as purge_helpers
import test_opencode_retention_sweep as sweep_helpers

UID = os.getuid()
NOW = purge_helpers.NOW
DAY = purge_helpers.DAY
needs_non_root = pytest.mark.skipif(UID == 0, reason="permission bits do not bind root")


@pytest.fixture(autouse=True)
def _forgeable_arrival_time(monkeypatch):
    monkeypatch.setattr(rt, "_moved_time", lambda info: info.st_mtime)


def _run_id(i):
    return "oc-20260101T0000%02dZ-%08x" % (i, i)


# ---- sweep: only the current project's truncation guards the cache ------------------

def test_a_peer_projects_truncated_evidence_does_not_pin_the_cache(tmp_path, monkeypatch):
    project, peer, dirs = sweep_helpers._world(tmp_path)
    sweep_helpers._store(project)
    old = 40 * DAY
    sweep_helpers._touch(dirs.cache / "opencode" / "bin" / "tool", age=old)
    sweep_helpers._dir_aged(dirs.cache / "opencode" / "bin", old)
    sweep_helpers._touch(dirs.tests / sweep_helpers._key(peer) / "t1" / "pin.json", age=old)
    real_load = rt.ProjectEvidence.load.__func__
    peer_real = os.path.realpath(str(peer))

    def peer_truncating_load(cls, root, **kwargs):
        evidence = real_load(cls, root, **kwargs)
        if os.path.realpath(str(root)) == peer_real and not kwargs.get("attribute"):
            kwargs["budget"].truncated = True
        return evidence

    monkeypatch.setattr(rt.ProjectEvidence, "load", classmethod(peer_truncating_load))
    plan = rt.plan_sweep(dirs=dirs, project_root=project, policy=sweep_helpers._policy(),
                         now=NOW, uid=UID, profile="work")
    reasons = {k.display: k.reason for k in plan.keep}
    assert reasons.get("cache") != "profile-in-use"
    assert "cache/opencode/bin" in {i.display for i in plan.remove}


# ---- purge: lock ownership by task, unchecked stores, unbounded evidence ---------------

def test_a_live_lock_blocks_a_run_of_its_task_without_a_text_reference(tmp_path):
    adapter, dirs, project, store = purge_helpers._world(tmp_path)
    record = purge_helpers._record("completed")
    record["task"] = "t9"
    purge_helpers._write_run(store, purge_helpers.RUN_A, record)
    purge_helpers._touch(project / ".workflow_artifacts" / "memory" / "run-supervisor-t9.pid",
                         text=json.dumps({"pid": os.getpid()}))
    with pytest.raises(rt.PurgeRefused) as refused:
        purge_helpers._plan(adapter)
    assert refused.value.code == "purge-run-active"
    assert [(b.run_id_or_task, b.reason) for b in refused.value.blocking] == [("t9", "live-lock")]


@needs_non_root
def test_an_unreadable_store_refuses_and_is_named(tmp_path, monkeypatch, capsys):
    adapter, dirs, project, store = purge_helpers._cli_world(tmp_path, monkeypatch)
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir(exist_ok=True)
    memory.chmod(0)
    try:
        code, out, _ = purge_helpers._run_cli(capsys, "--yes", "--json")
    finally:
        memory.chmod(0o700)
    body = json.loads(out)
    assert code == 2 and body["code"] == "purge-run-active"
    assert body["blocking"][0]["reason"] == "store-unchecked"
    assert body["blocking"][0]["store"].startswith(("~", "/"))


def test_a_store_over_the_walk_budget_is_visited_not_refused(tmp_path, monkeypatch):
    adapter, dirs, project, store = purge_helpers._world(tmp_path)
    for i in range(30):
        purge_helpers._write_run(store, _run_id(i), purge_helpers._record("completed"))
    probe = rt.Budget()
    rt.ProjectEvidence.load(project, uid=UID, attribute=True, table=None, budget=probe, now=NOW)
    assert not probe.truncated  # the default bound is generous
    monkeypatch.setattr(rt.Budget.__init__, "__defaults__", (10,))
    small = rt.Budget()
    rt.ProjectEvidence.load(project, uid=UID, attribute=True, table=None, budget=small, now=NOW)
    assert small.truncated  # with this bound the old purge would have skipped the store
    plan = purge_helpers._plan(adapter)
    assert not plan.stores_not_visited
    assert {_run_id(i) for i in range(30)} <= purge_helpers._removed(plan)


def test_a_store_whose_run_listing_fails_makes_the_purge_partial(tmp_path, monkeypatch, capsys):
    adapter, dirs, project, store = purge_helpers._cli_world(tmp_path, monkeypatch)

    def failing(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(rt, "plan_store_runs", failing)
    code, out, _ = purge_helpers._run_cli(capsys, "--yes", "--json")
    body = json.loads(out)
    assert code == 1 and body["outcome"] == "partial"
    assert [r["reason"] for r in body["stores_not_visited"]] == ["unreadable"]
    plan = rt.Plan("work", "purge", (), (), (), ({"project": "p", "reason": "unreadable"},))
    assert rt.purge_incomplete(plan)
    assert not rt.purge_incomplete(rt.Plan("work", "purge", (), (), (), ({"project": "p", "reason": "missing"},)))


# ---- deletion engine --------------------------------------------------------------------

def _foreign_at_execution(monkeypatch, name):
    """Make `name` look foreign-owned, but only while the executor runs, so the plan
    still selects the unit."""
    real_lstat = os.lstat
    active = {"on": False}

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if active["on"] and path == name:
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)
        return info

    real_execute = rt.execute

    def execute(plan, **kwargs):
        active["on"] = True
        try:
            return real_execute(plan, **kwargs)
        finally:
            active["on"] = False

    monkeypatch.setattr(rt.os, "lstat", lstat)
    monkeypatch.setattr(rt, "execute", execute)


def test_a_foreign_file_deep_in_a_swept_unit_leaves_only_itself(tmp_path, monkeypatch):
    project, _peer, dirs = sweep_helpers._world(tmp_path, other=False)
    old = 40 * DAY
    unit = dirs.cache / "opencode" / "pkg"
    sweep_helpers._touch(unit / "a" / "b" / "foreign-file", age=old)
    sweep_helpers._touch(unit / "a" / "sibling", age=old)
    sweep_helpers._touch(unit / "top", age=old)
    for d in (unit / "a" / "b", unit / "a", unit):
        sweep_helpers._dir_aged(d, old)
    _foreign_at_execution(monkeypatch, "foreign-file")
    out = sweep_helpers._run_sweep(dirs, project, sweep_helpers._policy())
    assert out["status"] == "done"
    assert out["removed"].get("cache") == 1 and out["left"] == 1 and out["failed"] == 0
    assert (unit / "a" / "b" / "foreign-file").exists()
    assert not (unit / "a" / "sibling").exists() and not (unit / "top").exists()


def test_the_unit_itself_still_fails_when_foreign(tmp_path, monkeypatch):
    victim = tmp_path / "root" / "unit"
    victim.mkdir(parents=True)
    item = rt.Item("cache", "unit", "unit", ("unit",), "dir", 0, 0, 0.0, (), str(tmp_path / "root"))
    _foreign_at_execution(monkeypatch, "unit")
    result = rt.execute(rt.Plan("p", "sweep", (item,)), uid=UID)
    assert result.failed == (("unit", "foreign-owner"),) and not result.left


def test_purge_leaves_a_foreign_file_and_reports_it(tmp_path, monkeypatch, capsys):
    adapter, dirs, project, store = purge_helpers._cli_world(tmp_path, monkeypatch)
    purge_helpers._touch(dirs.data / "sub" / "deep" / "foreign-file")
    purge_helpers._touch(dirs.data / "sub" / "deep" / "other")
    purge_helpers._touch(dirs.data / "sub" / "sibling")
    _foreign_at_execution(monkeypatch, "foreign-file")
    code, out, _ = purge_helpers._run_cli(capsys, "--yes", "--json")
    body = json.loads(out)
    assert code == 1 and body["outcome"] == "partial"
    assert {"path": "PROFILE/data/sub/deep/foreign-file", "reason": "foreign-owner"} in body["keep"]
    assert (dirs.data / "sub" / "deep" / "foreign-file").exists()
    assert not (dirs.data / "sub" / "deep" / "other").exists()
    assert not (dirs.data / "sub" / "sibling").exists()


def test_a_clean_purge_still_reports_purged(tmp_path, monkeypatch, capsys):
    purge_helpers._cli_world(tmp_path, monkeypatch)
    code, out, _ = purge_helpers._run_cli(capsys, "--yes", "--json")
    assert code == 0 and json.loads(out)["outcome"] == "purged"


def test_a_tree_deeper_than_the_cap_is_removed_down_to_it(tmp_path, monkeypatch, capsys):
    adapter, dirs, project, store = purge_helpers._cli_world(tmp_path, monkeypatch)
    purge_helpers._touch(dirs.data / "a" / "top")
    purge_helpers._touch(dirs.data / "a" / "b" / "c" / "d" / "e" / "bottom")
    monkeypatch.setattr(rt, "MAX_WALK_DEPTH", 4)
    code, out, _ = purge_helpers._run_cli(capsys, "--yes", "--json")
    body = json.loads(out)
    assert code == 1 and body["outcome"] == "partial"
    assert any(k["reason"] == "walk-truncated" and k["path"].startswith("PROFILE/data/a/b/c/d")
               for k in body["keep"])
    assert not (dirs.data / "a" / "top").exists()
    assert (dirs.data / "a" / "b" / "c" / "d" / "e" / "bottom").exists()


@needs_non_root
def test_an_unreadable_directory_gets_a_chmod_hint(tmp_path, monkeypatch, capsys):
    adapter, dirs, project, store = purge_helpers._cli_world(tmp_path, monkeypatch)
    locked = dirs.root / "extra"
    locked.mkdir(mode=0o700)
    locked.chmod(0)
    try:
        code, out, _ = purge_helpers._run_cli(capsys, "--yes")
        _, js, _ = purge_helpers._run_cli(capsys, "--dry-run", "--json")
    finally:
        locked.chmod(0o700)
    assert code == 1
    assert "run: chmod -R u+rwx PROFILE/extra" in out
    keep = [k for k in json.loads(js)["keep"] if k["path"] == "PROFILE/extra"]
    assert keep == [{"path": "PROFILE/extra", "reason": "unreadable", "hint": "chmod-u-rwx"}]
