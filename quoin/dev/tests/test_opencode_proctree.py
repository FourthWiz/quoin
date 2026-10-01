"""Process table parsing, identities and descendant walks."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quoin.opencode_adapter import proctree as pt
from quoin.opencode_adapter.proctree import Identity, ProcInfo

PS_TEXT = """\
    1     0     1 Ss   Mon Sep 29 10:00:00 2026
  100     1   100 S    Tue Sep 30 09:15:01 2026
  101   100   100 Z+   Tue Sep 30 09:15:02 2026
  102   100   200 S    Tue Sep 30 09:15:03 2026
"""


def _table(*rows):
    return {r.pid: r for r in rows}


def test_parse_ps_rows_and_zombie():
    table = pt.parse_ps(PS_TEXT)
    assert set(table) == {1, 100, 101, 102}
    assert table[100] == ProcInfo(100, 1, 100, "S", "Tue Sep 30 09:15:01 2026")
    assert table[101].zombie and not table[100].zombie


@pytest.mark.parametrize("text", ["", "   \n", "abc 1 1 S Mon Sep 29 10:00:00 2026\n", "1 2 3\n", "1 2 3 S\n"])
def test_parse_ps_untrusted_is_none(text):
    assert pt.parse_ps(text) is None


def test_ps_path_used_off_linux(monkeypatch):
    monkeypatch.setattr(pt.sys, "platform", "darwin")
    calls = []

    def ok(argv, env, timeout):
        calls.append(env)
        return 0, PS_TEXT

    assert set(pt.snapshot(runner=ok)) == {1, 100, 101, 102}
    assert calls[0]["TZ"] == "UTC" and calls[0]["LC_ALL"] == "C"
    assert pt.snapshot(runner=lambda a, e, t: (1, PS_TEXT)) is None
    assert pt.snapshot(runner=lambda a, e, t: (0, "")) is None

    def boom(a, e, t):
        raise OSError("no ps")

    assert pt.snapshot(runner=boom) is None


def test_ps_start_text_is_stable_across_caller_timezones(monkeypatch):
    monkeypatch.setattr(pt.sys, "platform", "darwin")
    if not os.path.exists("/bin/ps"):
        pytest.skip("no ps")
    seen = []
    for zone in ("Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", zone)
        table = pt.snapshot()
        assert table is not None
        seen.append(table[os.getpid()].start)
    assert seen[0] == seen[1]


def _write_proc(root: Path, pid: int, comm: str, state: str, ppid: int, pgrp: int, start: int):
    d = root / str(pid)
    d.mkdir(parents=True)
    tail = [state, str(ppid), str(pgrp)] + ["0"] * 16 + [str(start), "0", "0"]
    (d / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(tail)))


def test_proc_tree_parsing(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    _write_proc(root, 10, "node", "S", 1, 10, 555)
    _write_proc(root, 11, "a b) c", "Z", 10, 10, 556)
    _write_proc(root, 12, "(x)", "R", 10, 12, 557)
    (root / "self").mkdir()
    (root / "cpuinfo").write_text("x")
    table = pt.snapshot(proc_root=root)
    assert table[10] == ProcInfo(10, 1, 10, "S", "555")
    assert table[11].zombie and table[11].ppid == 10 and table[11].start == "556"
    assert table[12].pgid == 12
    assert pt.start_time(12, proc_root=root) == "557"
    assert pt.start_time(999, proc_root=root) is None


def test_proc_malformed_and_empty_are_none(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    assert pt.snapshot(proc_root=root) is None
    (root / "5").mkdir()
    (root / "5" / "stat").write_text("5 no-paren S 1 1\n")
    assert pt.snapshot(proc_root=root) is None
    (root / "5" / "stat").write_text("5 (x) S 1\n")
    assert pt.snapshot(proc_root=root) is None


def test_alive_rules():
    t = _table(ProcInfo(5, 1, 5, "S", "s1"), ProcInfo(6, 1, 5, "Z", "s2"))
    assert pt.alive(Identity(5, "s1"), t)
    assert not pt.alive(Identity(5, "other"), t)
    assert not pt.alive(Identity(6, "s2"), t)
    assert not pt.alive(Identity(7, "s"), t)
    assert not pt.alive(Identity(5, "s1"), None)


def test_descendants_group_and_cycles():
    t = _table(
        ProcInfo(1, 0, 1, "S", "a"),
        ProcInfo(2, 1, 2, "S", "b"),
        ProcInfo(3, 2, 3, "Z", "c"),
        ProcInfo(4, 3, 4, "S", "d"),   # under a zombie: still reached
        ProcInfo(5, 2, 2, "S", "e"),
        ProcInfo(8, 9, 8, "S", "x"),   # ppid cycle 8 <-> 9
        ProcInfo(9, 8, 8, "S", "y"),
    )
    assert [p.pid for p in pt.descendants(1, t)] == [2, 4, 5] or [p.pid for p in pt.descendants(1, t)] == [2, 5, 4]
    assert {p.pid for p in pt.descendants(8, t)} == {9}
    assert pt.descendants(1, None) == []
    assert [p.pid for p in pt.group_members(2, t)] == [2, 5]
    only_zombie = _table(ProcInfo(3, 1, 30, "Z", "c"))
    assert pt.group_members(30, only_zombie) == []


@pytest.mark.skipif(not pt.SUPPORTED, reason="posix only")
def test_live_zombie_child_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        start = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            table = pt.snapshot()
            info = table and table.get(child.pid)
            if info is not None:
                start = info.start
                if info.zombie:
                    break
            time.sleep(0.05)
        assert start is not None
        assert not pt.alive(Identity(child.pid, start), pt.snapshot())
    finally:
        child.wait()
    assert not pt.alive(Identity(child.pid, start), pt.snapshot())


@pytest.mark.skipif(not pt.SUPPORTED, reason="posix only")
def test_live_child_and_detached_grandchild(tmp_path):
    ready = tmp_path / "ready"
    code = (
        "import subprocess,sys,time,os\n"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)\n"
        "open(%r,'w').write(str(g.pid))\n"
        "time.sleep(30)\n" % str(ready)
    )
    child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    grand = None
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() or not ready.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        grand = int(ready.read_text())
        table = pt.snapshot()
        kids = {p.pid: p for p in pt.descendants(child.pid, table)}
        assert grand in kids
        assert kids[grand].pgid != table[child.pid].pgid
        ident = Identity(grand, kids[grand].start)
        assert pt.alive(ident, table)
        assert not pt.alive(Identity(grand, kids[grand].start + "x"), table)
        assert pt.start_time(grand) == kids[grand].start
    finally:
        for pid in (grand, child.pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        child.wait()
    time.sleep(0.2)
    assert not pt.alive(Identity(child.pid, "whatever"), pt.snapshot())


def test_module_imports_only_stdlib():
    import ast

    tree = ast.parse(Path(pt.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no package-relative imports"
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"__future__", "os", "subprocess", "sys", "dataclasses", "pathlib", "typing"}
