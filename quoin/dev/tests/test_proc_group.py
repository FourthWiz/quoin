"""Process-group helpers used by the Claude cells."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from quoin.benchmarks.harness import proc_group


def _spawn(code="import time; time.sleep(60)", **kw):
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True, **kw)


def _wait_gone(pgid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc_group.group_gone(pgid):
            return True
        time.sleep(0.02)
    return False


def test_signal_tree_signals_the_whole_group():
    proc = _spawn()
    try:
        proc_group.signal_tree(proc, signal.SIGKILL, True)
        proc.wait(timeout=5)
        assert _wait_gone(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_signal_tree_without_a_group_signals_only_the_leader():
    proc = _spawn()
    try:
        proc_group.signal_tree(proc, signal.SIGKILL, False)
        assert proc.wait(timeout=5) == -signal.SIGKILL
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_signal_tree_treats_an_unsignalable_group_as_gone(monkeypatch, error):
    proc = _spawn()
    try:
        def refuse(*a, **k):
            raise error()

        monkeypatch.setattr(os, "killpg", refuse)
        proc_group.signal_tree(proc, signal.SIGTERM, True)
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_reap_group_and_group_gone_tolerate_both_errors(monkeypatch, error):
    def refuse(*a, **k):
        raise error()

    monkeypatch.setattr(os, "killpg", refuse)
    proc_group.reap_group(12345)
    assert proc_group.group_gone(12345) is True


def test_leader_exit_reap_kills_a_lingering_child_and_returns_early_when_it_exits():
    leader = _spawn(
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
    )
    leader.wait(timeout=5)
    pgid = leader.pid
    assert not proc_group.group_gone(pgid)
    start = time.monotonic()
    assert proc_group.leader_exited_reap(leader, pgid, grace=5.0) is True
    # A child that dies on SIGTERM must not make the caller wait out the grace.
    assert time.monotonic() - start < 3.0
    assert _wait_gone(pgid)


def test_leader_exit_reap_escalates_to_kill_after_the_grace(tmp_path):
    ready = tmp_path / "ready"
    code = ("import signal, subprocess, sys\n"
            "child = ('import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
            "         'open(sys.argv[1], \"w\").close(); time.sleep(60)')\n"
            "subprocess.Popen([sys.executable, '-c', child, sys.argv[1]])\n")
    leader = subprocess.Popen([sys.executable, "-c", code, str(ready)], start_new_session=True)
    leader.wait(timeout=5)
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    start = time.monotonic()
    assert proc_group.leader_exited_reap(leader, leader.pid, grace=0.3) is True
    assert time.monotonic() - start >= 0.3
    assert _wait_gone(leader.pid)


def test_leader_exit_reap_on_an_empty_group_reports_gone_without_waiting():
    leader = _spawn("pass")
    leader.wait(timeout=5)
    assert _wait_gone(leader.pid)
    start = time.monotonic()
    assert proc_group.leader_exited_reap(leader, leader.pid, grace=5.0) is True
    assert time.monotonic() - start < 1.0


def test_kill_and_reap_stops_a_running_session_and_its_children():
    proc = _spawn("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "time.sleep(60)")
    time.sleep(0.3)
    proc_group.kill_and_reap(proc, proc.pid, True)
    assert proc.poll() is not None
    assert _wait_gone(proc.pid)
