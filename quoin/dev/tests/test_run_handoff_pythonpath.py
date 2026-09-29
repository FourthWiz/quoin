"""Tests for `_strip_handoff_pythonpath` and its wiring into `quoin run`
(IVG-281 T-07).

An auto-resume hand-off prepends its recorded `pythonpath` to `PYTHONPATH`
so the relaunched `quoin` CLI can import itself; `quoin run` must strip
that prepend before launching `claude`, so the `claude` subprocess (which
inherits `os.environ`) sees the user's own `PYTHONPATH`, not the
hand-off's. Reuses the fake-launch-fn pattern from
`test_supervisor_lock_and_halt_on_abort.py` — the fake launch_fn snapshots
`os.environ` at call time.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from quoin import cli
from quoin import supervisor as sup


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sup._RealClock, "sleep", staticmethod(lambda seconds: None))


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _memory_dir(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "memory"


def _fake_launch_fn(project_root: Path, task: str):
    """Writes the done sentinel on its first call and snapshots the
    environment `claude` would have inherited at launch time."""
    seen = {}

    def fn(t):
        seen["environ"] = dict(os.environ)
        (_memory_dir(project_root) / f"autonomous-done-{task}.md").write_text("done\n")

    fn.seen = seen
    return fn


def _run(monkeypatch, project, task="demo"):
    fn = _fake_launch_fn(project, task)
    monkeypatch.setattr(sup, "make_launch_fn", lambda project_root, permission_mode=None: fn)
    code = cli.main(["run", "--autonomous", task, "--project-root", str(project)])
    return code, fn.seen["environ"]


def test_marker_present_strips_recorded_entry_keeps_user_entries(monkeypatch, project):
    monkeypatch.setenv("QUOIN_HANDOFF_PYTHONPATH", "/rec")
    monkeypatch.setenv("PYTHONPATH", "/rec" + os.pathsep + "/user")
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert env.get("PYTHONPATH") == "/user"
    assert "QUOIN_HANDOFF_PYTHONPATH" not in env


def test_marker_present_only_entry_removes_pythonpath_entirely(monkeypatch, project):
    monkeypatch.setenv("QUOIN_HANDOFF_PYTHONPATH", "/rec")
    monkeypatch.setenv("PYTHONPATH", "/rec")
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert "PYTHONPATH" not in env
    assert "QUOIN_HANDOFF_PYTHONPATH" not in env


def test_no_marker_pythonpath_untouched(monkeypatch, project):
    monkeypatch.delenv("QUOIN_HANDOFF_PYTHONPATH", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/user/only")
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert env.get("PYTHONPATH") == "/user/only"


def test_marker_not_present_in_pythonpath_leaves_it_untouched_but_pops_marker(monkeypatch, project):
    monkeypatch.setenv("QUOIN_HANDOFF_PYTHONPATH", "/rec")
    monkeypatch.setenv("PYTHONPATH", "/unrelated/only")
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert env.get("PYTHONPATH") == "/unrelated/only"
    assert "QUOIN_HANDOFF_PYTHONPATH" not in env


def test_duplicate_entries_only_first_removed(monkeypatch, project):
    monkeypatch.setenv("QUOIN_HANDOFF_PYTHONPATH", "/rec")
    monkeypatch.setenv("PYTHONPATH", "/rec" + os.pathsep + "/user" + os.pathsep + "/rec")
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert env.get("PYTHONPATH") == "/user" + os.pathsep + "/rec"


def test_no_pythonpath_at_all_no_marker_no_crash(monkeypatch, project):
    monkeypatch.delenv("QUOIN_HANDOFF_PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    code, env = _run(monkeypatch, project)
    assert code == 0
    assert "PYTHONPATH" not in env
