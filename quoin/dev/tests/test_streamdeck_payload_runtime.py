"""The deployed command lines, run the way a hook or the plugin would run them."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, stage_runtime  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

ZENV = {"ZELLIJ_SESSION_NAME": "sess", "ZELLIJ_PANE_ID": "3"}
EVENT = json.dumps({"session_id": "sid-1", "hook_event_name": "UserPromptSubmit", "cwd": "/work"}).encode()


def system_python():
    """/usr/bin/python3 only when the developer tools are installed (never the installer stub)."""
    try:
        if subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=10).returncode != 0:
            return None, "xcode-select reports no developer tools; /usr/bin/python3 would be the installer stub"
        out = subprocess.run(["/usr/bin/python3", "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "cannot run /usr/bin/python3: %s" % type(exc).__name__
    if out.returncode != 0:
        return None, "/usr/bin/python3 failed to run"
    major, minor = (int(x) for x in out.stdout.strip().split("."))
    if (major, minor) < (3, 9):
        return None, "/usr/bin/python3 is %d.%d, below 3.9" % (major, minor)
    return "/usr/bin/python3", ""


SYSTEM_PYTHON, SYSTEM_SKIP = system_python()
INTERPRETERS = [
    pytest.param(sys.executable, id="venv"),
    pytest.param(SYSTEM_PYTHON, id="system",
                 marks=pytest.mark.skipif(SYSTEM_PYTHON is None, reason=SYSTEM_SKIP)),
]


@pytest.fixture
def staged(tmp_path):
    return stage_runtime(tmp_path)


@pytest.mark.parametrize("python", INTERPRETERS)
def test_monitor_and_snapshot_command_lines(staged, python):
    cmd = staged.command("monitor.py", "claude", python=python)
    assert cmd[1:3] == ["-E", "-s"] and "current" in cmd[3]
    rc, out, err = staged.run(cmd, EVENT, ZENV)
    assert (rc, out, err) == (0, b"", b"")
    records = list((staged.state_dir / "agents").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["session"] == "sess" and record["status"] == "working"

    rc, out, err = staged.run(staged.command("dashboard.py", "snapshot", python=python))
    assert rc == 0, err
    rows = json.loads(out)
    assert isinstance(rows, list)


def test_sleeping_zellij_does_not_hold_up_the_hook(staged):
    rc, _, _ = staged.run(staged.command("monitor.py", "claude"), EVENT, ZENV)
    assert rc == 0
    marker = staged.fakes / "zellij.started"
    zellij = staged.fakes / "zellij"
    zellij.write_text('#!/bin/sh\ntouch "%s"\nsleep 5\n' % marker)
    zellij.chmod(0o755)
    snap = subprocess.Popen(
        staged.command("dashboard.py", "snapshot"), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(staged.cwd),
        env=staged.env, start_new_session=True)
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline and snap.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), "snapshot never reached the sleeping zellij"
        started = time.monotonic()
        rc, out, err = staged.run(staged.command("monitor.py", "claude"), EVENT, ZENV)
        elapsed = time.monotonic() - started
        assert (rc, out, err) == (0, b"", b"")
        assert elapsed < 2.5
        record = json.loads(next((staged.state_dir / "agents").glob("*.json")).read_text())
        assert record["event"] == "UserPromptSubmit"
    finally:
        try:
            os.killpg(snap.pid, signal.SIGKILL)
        except OSError:
            pass
        snap.wait()
