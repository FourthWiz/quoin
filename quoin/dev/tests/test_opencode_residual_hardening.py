"""Residual hardening cases for the phase loop that sit outside its pinned suite."""
from __future__ import annotations

from quoin.opencode_adapter import phase_loop, runstore

from _opencode_run_helpers import ScriptedDriver, outcome
from test_opencode_phase_loop import go


def test_unknown_block_reason_on_a_closed_run_is_reported_as_run_closed(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    record = drv.record(run_id)
    record["telemetry"] = {"final": True}
    record["resume_blocked"] = "not-a-known-value"
    runstore.write_record(drv.directory, record)
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert r.outcome == "INTERRUPTED" and r.run_id == run_id
    assert r.resume_blocked == "run-closed"
    assert phase_loop.exit_code(r) == 5
    assert "start" not in drv.names() and "resume" not in drv.names()
