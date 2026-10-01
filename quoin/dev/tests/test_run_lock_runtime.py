"""Runtime ownership recorded in the supervisor lock, and readers of it."""
from __future__ import annotations

import json
import os

import pytest

from quoin import cli
from quoin import supervisor as sup

ISO = "2026-01-01T00:00:00Z"


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_iso_now", lambda: ISO)


def _memory(root):
    return root / ".workflow_artifacts" / "memory"


def _paths(root):
    return cli._supervisor_paths(root, "demo")


def _acquire(root, **kwargs):
    p = _paths(root)
    return cli._acquire_supervisor_lock(
        p["memory_dir"], p["lock"], p["result"], "demo", 3, None, **kwargs
    )


def _dead_pid():
    pid = 2 ** 22 + 12345
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


# --- lock payload ----------------------------------------------------------


def test_default_payload_has_no_runtime_key(project):
    assert _acquire(project) == (True, None)
    expected = (
        '{"granted": 3, "pid": %d, "started_at": "%s", "task": "demo", "writer": "cli"}\n'
        % (os.getpid(), ISO)
    )
    assert _paths(project)["lock"].read_bytes() == expected.encode()


def test_runtime_key_written_when_given(project):
    assert _acquire(project, runtime="opencode") == (True, None)
    expected = (
        '{"granted": 3, "pid": %d, "runtime": "opencode", "started_at": "%s", '
        '"task": "demo", "writer": "cli"}\n' % (os.getpid(), ISO)
    )
    assert _paths(project)["lock"].read_bytes() == expected.encode()


def test_retry_after_failed_create_keeps_runtime(monkeypatch, project):
    real = cli._create_lock_exclusive
    calls = []

    def flaky(path, payload):
        calls.append(payload)
        if len(calls) == 1:
            return False
        return real(path, payload)

    monkeypatch.setattr(cli, "_create_lock_exclusive", flaky)
    p = _paths(project)
    p["lock"].write_text(json.dumps({"pid": _dead_pid(), "writer": "cli"}) + "\n")
    assert _acquire(project, runtime="opencode") == (True, None)
    assert len(calls) == 2
    assert all(b'"runtime": "opencode"' in c for c in calls)
    assert json.loads(p["lock"].read_text())["runtime"] == "opencode"


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, "claude"),
        ({"runtime": ""}, "claude"),
        ({"runtime": 5}, "claude"),
        ({"runtime": "opencode"}, "opencode"),
        (None, "claude"),
    ],
)
def test_lock_runtime(data, expected):
    assert cli._lock_runtime(data) == expected


def _patch_supervise(monkeypatch):
    calls = []
    monkeypatch.setattr(sup, "make_launch_fn", lambda *a, **k: (lambda t: sup.LaunchResult(0)))

    def fake(*a, **k):
        calls.append((a, k))
        return sup.SuperviseResult("SUCCESS")

    monkeypatch.setattr(sup, "supervise", fake)
    return calls


def test_live_opencode_lock_refuses_plain_claude_run(monkeypatch, project, capsys):
    ppid = os.getppid()
    _paths(project)["lock"].write_text(
        json.dumps({"pid": ppid, "writer": "cli", "task": "demo", "runtime": "opencode"}) + "\n"
    )
    calls = _patch_supervise(monkeypatch)
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 3
    assert capsys.readouterr().out == (
        f"quoin run: REFUSED (supervisor lock held by pid {ppid})\n  task: demo\n"
        f"  takeover: quoin run --takeover demo --project-root {project}\n"
    )
    assert calls == []


def test_dead_opencode_lock_is_reclaimed_by_claude_run(monkeypatch, project):
    _paths(project)["lock"].write_text(
        json.dumps({"pid": _dead_pid(), "writer": "cli", "task": "demo", "runtime": "opencode"}) + "\n"
    )
    calls = _patch_supervise(monkeypatch)
    assert cli.main(["run", "demo", "--project-root", str(project)]) == 0
    assert len(calls) == 1
    assert not _paths(project)["lock"].exists()
