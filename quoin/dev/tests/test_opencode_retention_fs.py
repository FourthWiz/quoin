from __future__ import annotations

import os
import signal
import threading

import pytest

from quoin.opencode_adapter import retention as rt

UID = os.getuid()


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _plan(root, items):
    return rt.Plan("p", "purge", tuple(items))


def _entries(root, parent=(), uid=UID, **kw):
    return rt.plan_entries(str(root), parent, cls="other", uid=uid, budget=kw.pop("budget", rt.Budget()), **kw)


def test_symlink_in_root_is_unlinked_not_followed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "keep.txt", "data")
    target_file = _write(tmp_path / "target.txt", "t")
    os.symlink(str(outside), str(root / "dirlink"))
    os.symlink(str(target_file), str(root / "filelink"))
    items, kept = _entries(root)
    assert {i.unit for i in items} == {"dirlink", "filelink"}
    assert all(i.kind == "file" for i in items)
    result = rt.execute(_plan(root, items), uid=UID)
    assert not result.failed
    assert list(root.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "data"
    assert target_file.read_text() == "t"


def test_root_symlink_is_unsafe(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(str(real), str(link))
    with pytest.raises(rt.RootUnsafe):
        _entries(link)


def test_component_swapped_for_symlink_between_plan_and_execute(tmp_path):
    root = tmp_path / "root"
    _write(root / "tests" / "KEY" / "task" / "f.txt")
    outside = tmp_path / "outside"
    _write(outside / "KEY" / "task" / "f.txt", "precious")
    items, _ = _entries(root, ("tests", "KEY"))
    assert [i.unit for i in items] == ["task"]
    plan = _plan(root, items)
    import shutil
    shutil.rmtree(str(root / "tests"))
    os.symlink(str(outside), str(root / "tests"))
    result = rt.execute(plan, uid=UID)
    assert result.failed == (("tests/KEY/task", "changed-since-plan"),)
    assert (outside / "KEY" / "task" / "f.txt").read_text() == "precious"


def test_foreign_owner_keeps_every_top_level_entry(tmp_path):
    root = tmp_path / "root"
    _write(root / "a" / "f")
    _write(root / "b")
    items, kept = _entries(root, uid=UID)
    # a root owned by "someone else" is unsafe; simulate foreign entries under an owned root
    fd = rt._open_root(str(root), UID)
    try:
        out = []
        for name in ("a", "b"):
            item, why = rt.unit_item(fd, str(root), (), name, cls="other", uid=UID + 1, budget=rt.Budget())
            out.append((item, why))
    finally:
        os.close(fd)
    assert all(item is None and why == rt.Kept(name, "foreign-owner")
               for (item, why), name in zip(out, ("a", "b")))
    result = rt.execute(_plan(root, items), uid=UID + 1)
    assert result.removed == ()
    assert (root / "a" / "f").exists() and (root / "b").exists()


def test_read_only_tree_is_removed(tmp_path):
    root = tmp_path / "root"
    _write(root / "snap" / "sub" / "f")
    os.chmod(str(root / "snap" / "sub" / "f"), 0o400)
    os.chmod(str(root / "snap" / "sub"), 0o500)
    os.chmod(str(root / "snap"), 0o500)
    items, _ = _entries(root)
    result = rt.execute(_plan(root, items), uid=UID)
    assert not result.failed
    assert not (root / "snap").exists()


def test_exclude_keeps_file_and_parents(tmp_path):
    root = tmp_path / "root"
    _write(root / "data" / "opencode" / "auth.json", "secret")
    _write(root / "data" / "opencode" / "opencode.db", "db")
    _write(root / "data" / "other" / "f")
    items, _ = _entries(root, excludes={"data": ["opencode/auth.json"]})
    assert items[0].files == 2
    result = rt.execute(_plan(root, items), uid=UID)
    assert not result.failed
    assert (root / "data" / "opencode" / "auth.json").read_text() == "secret"
    assert not (root / "data" / "opencode" / "opencode.db").exists()
    assert not (root / "data" / "other").exists()


def test_fifo_removed_without_blocking(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(str(root / "pipe"))
    done = []

    def run():
        items, _ = _entries(root)
        done.append(rt.execute(_plan(root, items), uid=UID))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert done and not done[0].failed
    assert not (root / "pipe").exists()


def test_truncation_keeps_unvisited_units(tmp_path):
    root = tmp_path / "root"
    for name in ("a", "b", "c", "d"):
        _write(root / name)
    budget = rt.Budget(2)
    items, kept = _entries(root, budget=budget)
    assert len(items) < 4
    assert any(k.reason == "walk-truncated" for k in kept)
    assert budget.truncated


def test_parity_between_plan_and_execute(tmp_path):
    root = tmp_path / "root"
    _write(root / "a" / "x")
    _write(root / "a" / "y")
    _write(root / "b")
    items, _ = _entries(root)
    plan = _plan(root, items)
    result = rt.execute(plan, uid=UID)
    assert {(i.cls, i.display) for i in result.removed} == {(i.cls, i.display) for i in plan.remove}
    assert sum(i.files for i in result.removed) == 3
    assert list(root.iterdir()) == []


def test_store_runs_use_strict_sidecar_names(tmp_path):
    project = tmp_path / "proj"
    store = project / ".workflow_artifacts" / "memory" / "runtime" / "opencode"
    run = "oc-20260101T000000Z-abcdef01"
    for suffix in (".jsonl", ".run.json", ".checkpoint.json"):
        _write(store / (run + suffix))
    _write(store / "workflow-task.json")
    _write(store / "notes.txt")
    items, kept = rt.plan_store_runs(project, uid=UID, budget=rt.Budget())
    assert [(i.kind, i.unit, i.files) for i in items] == [("run", run, 3)]
    result = rt.execute(_plan(store, items), uid=UID)
    assert not result.failed
    assert sorted(p.name for p in store.iterdir()) == ["notes.txt", "workflow-task.json"]


def test_executor_rechecks_owner_at_removal(tmp_path, monkeypatch):
    victim = tmp_path / "root" / "unit"
    victim.parent.mkdir(parents=True)
    victim.write_text("x")
    uid = os.getuid()
    item = rt.Item("cache", "unit", "unit", ("unit",), "file", 1, 1, 0.0, (), str(tmp_path / "root"))
    real_open_root = rt._open_root

    def trusting_root(path, _uid):
        return real_open_root(path, uid)

    monkeypatch.setattr(rt, "_open_root", trusting_root)
    result = rt.execute(rt.Plan("p", "sweep", (item,)), uid=uid + 1)
    assert result.failed and result.failed[0][1] == "foreign-owner"
    assert victim.exists()
