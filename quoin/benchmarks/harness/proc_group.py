"""Process-group helpers for the Claude cells.

A cell that starts the CLI in its own session can signal the whole tree, so a
background child the agent left running cannot outlive the leader and write
into the clone while the patch is captured.
"""
from __future__ import annotations

import os
import signal
import time


def signal_tree(proc, sig: int, group: bool) -> None:
    """Send ``sig`` to the process group when ``group`` is set, else to the leader."""
    try:
        if group:
            os.killpg(os.getpgid(proc.pid), sig)
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


def reap_group(pgid: int) -> None:
    """SIGKILL whatever is left in the group after the leader exited."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def leader_exited_reap(proc, pgid: int, grace: float = 0.5) -> None:
    """Terminate the group left behind by an exited leader, then kill the rest."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(grace)
    reap_group(pgid)
