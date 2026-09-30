"""Cancellation and descendant reaping of the OpenCode driver (POSIX only)."""
from __future__ import annotations

import os
import threading
import time

import pytest

import _opencode_driver_helpers as h
from quoin.opencode_adapter import proctree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

GRACE = 3.0
KILL_GRACE = 2.0


@pytest.fixture(autouse=True)
def _no_strays(tmp_path):
    yield
    # Every test ends with a process check; the stop file releases helpers
    # that only end on it.
    h.cleanup_fakes(tmp_path)
    assert h.stray_pids(tmp_path) == []


def _start(tmp_path, scenario, **drv_kw):
    prepared = h.make_prepared(tmp_path, scenario)
    drv = h.make_driver(tmp_path, grace_s=GRACE, kill_grace_s=KILL_GRACE, **drv_kw)
    handle = drv.start(prepared)
    return drv, prepared, handle


def _wait(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _grandchildren(tmp_path):
    path = h.state_of(tmp_path) / "grandchildren.txt"
    if not path.exists():
        return []
    return [int(x) for x in path.read_text().split() if x.strip().isdigit()]


def _ready(tmp_path):
    pids = _grandchildren(tmp_path)
    ok = bool(pids) and all((h.state_of(tmp_path) / ("grandchild-%d.ready" % p)).exists() for p in pids)
    return pids if ok else None


def _alive_pids(tmp_path):
    return h.stray_pids(tmp_path)


def _cancel_with_observer(drv, handle, delay=0.0):
    """Iterate observe() on a thread and cancel from this one."""
    events = []
    thread = threading.Thread(target=lambda: events.extend(drv.observe(handle)))
    thread.start()
    time.sleep(delay)
    result = drv.cancel(handle)
    thread.join(20)
    assert not thread.is_alive()
    return result, events


def test_detached_grandchild_is_reaped(tmp_path):
    drv, prepared, handle = _start(tmp_path, "grandchild_detached")
    (gpid,) = _wait(lambda: _ready(tmp_path))
    result, _ = _cancel_with_observer(drv, handle, 0.4)
    assert result.descendants_found >= 1
    assert result.descendants_remaining == 0
    assert result.escalated_kill is False and result.duration_s < GRACE
    assert handle.proc.poll() is not None
    assert gpid not in _alive_pids(tmp_path)
    assert h.load_record(tmp_path, prepared.run_id)["state"] == "cancelled"


def test_hang_obeys_term(tmp_path):
    drv, prepared, handle = _start(tmp_path, "hang")
    time.sleep(0.3)
    result, _ = _cancel_with_observer(drv, handle)
    assert result.signalled_term and result.group_empty
    assert result.escalated_kill is False and result.duration_s < GRACE


def test_grandchild_ignoring_term_is_killed(tmp_path):
    drv, prepared, handle = _start(tmp_path, "grandchild_ignore_term")
    _wait(lambda: _ready(tmp_path))
    result, _ = _cancel_with_observer(drv, handle, 0.3)
    assert result.signalled_term and result.escalated_kill
    assert result.descendants_remaining == 0
    assert handle.proc.poll() is not None


def test_child_ignoring_term_is_killed_within_the_grace_windows(tmp_path):
    drv, prepared, handle = _start(tmp_path, "ignore_term")
    time.sleep(0.5)
    started = time.monotonic()
    result, _ = _cancel_with_observer(drv, handle)
    assert result.escalated_kill
    assert time.monotonic() - started < GRACE + KILL_GRACE + 1


def test_reparented_grandchild_tracked_earlier_is_still_reaped(tmp_path):
    drv, prepared, handle = _start(
        tmp_path, ("grandchild_reparented", {"linger_s": 1.0}), descendant_scan_s=0.1
    )
    events = []
    thread = threading.Thread(target=lambda: events.extend(drv.observe(handle)))
    thread.start()
    (gpid,) = _wait(lambda: _ready(tmp_path))
    _wait(lambda: any(i.pid == gpid for i in list(handle.tracked.values())))
    _wait(lambda: _reparented(gpid))
    result = drv.cancel(handle)
    thread.join(20)
    assert result.escalated_kill is False
    assert gpid not in _alive_pids(tmp_path)


def _reparented(pid):
    table = proctree.snapshot()
    info = table.get(pid) if table else None
    # Re-parented: the intermediate that started it is gone from the table.
    return info is not None and (info.ppid <= 1 or info.ppid not in table)


def test_untrusted_process_listing_falls_back_to_the_group(tmp_path, monkeypatch):
    drv, prepared, handle = _start(tmp_path, "grandchild")
    _wait(lambda: _ready(tmp_path))
    monkeypatch.setattr(proctree, "snapshot", lambda **kw: None)
    result, _ = _cancel_with_observer(drv, handle, 0.2)
    monkeypatch.undo()
    assert result.descendants_remaining is None
    assert result.group_empty
    assert handle.proc.poll() is not None
    assert _alive_pids(tmp_path) == []


def test_cancel_is_idempotent_and_safe_from_two_threads(tmp_path):
    drv, prepared, handle = _start(tmp_path, "grandchild_detached")
    _wait(lambda: _ready(tmp_path))
    results = []
    threads = [threading.Thread(target=lambda: results.append(drv.cancel(handle))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 2 and results[0] is results[1]
    assert drv.cancel(handle) is results[0]
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == "cancelled"
    assert [s["state"] for s in record["history"]].count("cancelled") == 1


def test_a_detached_leftover_is_reaped_and_counted_at_the_end_of_a_clean_attempt(tmp_path):
    prepared = h.make_prepared(tmp_path, "grandchild_detached_finish")
    drv = h.make_driver(tmp_path, descendant_scan_s=0.1)
    handle, _events = h.run_to_end(drv, prepared)
    assert handle.outcome.state == "completed"
    attempt = h.load_record(tmp_path, prepared.run_id)["attempts"][0]
    assert attempt["leftovers_reaped"] is True and attempt["leftovers_reaped_count"] >= 1
