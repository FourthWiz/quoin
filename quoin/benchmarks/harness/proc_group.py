"""Process-group helpers for the Claude cells.

A cell that starts the CLI in its own session can signal the whole tree, so a
background child the agent left running cannot outlive the leader and write
into the clone while the patch is captured.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

# On macOS, signalling a group that holds only zombies fails with EPERM rather
# than ESRCH; both mean there is nothing left to signal.
_GONE = (ProcessLookupError, PermissionError)


def signal_tree(proc, sig: int, group: bool) -> None:
    """Send ``sig`` to the process group when ``group`` is set, else to the leader."""
    try:
        if group:
            os.killpg(os.getpgid(proc.pid), sig)
        else:
            proc.send_signal(sig)
    except _GONE:
        pass


def reap_group(pgid: int) -> None:
    """SIGKILL whatever is left in the group after the leader exited."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except _GONE:
        pass


def group_gone(pgid: int) -> bool:
    """True when no process is left in the group."""
    try:
        os.killpg(pgid, 0)
    except _GONE:
        return True
    return False


def leader_exited_reap(proc, pgid: int, grace: float = 0.5) -> bool:
    """Terminate the group left behind by an exited leader, then kill the rest.

    Returns True once the group is known to be empty or has been sent SIGKILL,
    so the caller need not signal that pgid again after it may have been reused.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except _GONE:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if group_gone(pgid):
            return True
        time.sleep(0.02)
    reap_group(pgid)
    return True


def kill_and_reap(proc, pgid, group: bool, wait: float = 10.0) -> None:
    """Stop a spawned session and anything it left running."""
    if proc.poll() is None:
        signal_tree(proc, signal.SIGKILL, group)
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
    if group and pgid is not None:
        reap_group(pgid)
