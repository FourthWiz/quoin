"""Attempt lifecycle of the OpenCode driver against the fake executable."""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

import pytest

import _opencode_driver_helpers as h
from quoin.opencode_adapter import driver, runstore
from quoin.opencode_adapter import events as ev

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture(autouse=True)
def _no_strays(tmp_path):
    yield
    h.cleanup_fakes(tmp_path)
    assert h.stray_pids(tmp_path) == []


def _run(tmp_path, scenario, drv_kw=None, **start_kw):
    prepared = h.make_prepared(tmp_path, scenario, **{k: start_kw.pop(k) for k in ("secret", "task") if k in start_kw})
    drv = h.make_driver(tmp_path, **(drv_kw or {}))
    handle, events = h.run_to_end(drv, prepared, **start_kw)
    return drv, prepared, handle, events


def _stopped(events):
    return [e for e in events if e.type is ev.EventType.STOPPED][-1].payload


# ------------------------------------------------------------ transitions


@pytest.mark.parametrize(
    "scenario, state, evidence, reason",
    [
        ("record_only", "completed", "full", None),
        ("native_error", "failed", None, "error-event"),
        ("approval_tool_error", "awaiting_approval", None, "approval-required"),
        ("approval_task_error", "awaiting_approval", None, "approval-required"),
        ("approval_stderr_notice", "awaiting_approval", None, "approval-required"),
        ("doom_loop_rejected", "awaiting_approval", None, "approval-required"),
        ("denied_tool_then_clean_finish", "completed", "full", None),
        ("doom_loop_denied", "completed", "partial", "denied-halt"),
        ("task_sync_completed", "completed", "full", None),
        ("task_background", "completed", "partial", "delegated-background"),
        ("task_promoted", "completed", "partial", "delegated-background"),
        ("task_denied_tail", "completed", "partial", "delegated-denied-tail"),
        ("task_errored_then_clean_finish", "failed", None, "delegation-failed"),
        ("agent_fallback", "failed", None, "agent-fallback"),
        ("approval_notice_then_finish", "awaiting_approval", None, "approval-required"),
    ],
)
def test_attempt_states(tmp_path, scenario, state, evidence, reason):
    drv, prepared, handle, events = _run(tmp_path, scenario)
    outcome = handle.outcome
    assert (outcome.state, outcome.evidence, outcome.reason) == (state, evidence, reason)
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == state
    assert record["attempts"][0]["state"] == state
    assert _stopped(events).state == state
    if state == "awaiting_approval":
        assert h.stray_pids(tmp_path) == []


# --------------------------------------------------------- crash boundaries


@pytest.mark.parametrize(
    "scenario, step_open, ran",
    [
        ("crash_after_start", True, True),
        ("crash_mid_stream", True, True),
        ("crash_in_open_step", True, True),
        ("crash_after_last_finish", False, True),
        ("exit_early", False, False),
        ("exit0_without_step_finish", None, None),
    ],
)
def test_crash_boundaries(tmp_path, scenario, step_open, ran):
    drv, prepared, handle, events = _run(tmp_path, scenario)
    assert handle.outcome.state == "interrupted"
    checkpoint = runstore.load_checkpoint(h.store_of(tmp_path), prepared.run_id)
    if step_open is not None:
        assert checkpoint["step_open"] is step_open
        assert checkpoint["ran_anything"] is ran


def test_hang_hits_the_deadline(tmp_path):
    drv, prepared, handle, events = _run(tmp_path, "hang", deadline_s=1)
    assert (handle.outcome.state, handle.outcome.reason) == ("interrupted", "timeout")
    assert handle.proc.poll() is not None


def test_closed_stdout_without_exit(tmp_path):
    drv, prepared, handle, events = _run(tmp_path, "stdout_closed_hang", deadline_s=30)
    assert (handle.outcome.state, handle.outcome.reason) == ("interrupted", "eof-without-exit")
    assert handle.proc.poll() is not None


def test_endless_line_is_one_diagnostic_and_the_replay_still_translates(tmp_path):
    drv, prepared, handle, events = _run(tmp_path, "endless_line")
    counters = h.load_record(tmp_path, prepared.run_id)["counters_total"]
    assert counters["oversized"] == 1
    assert handle.outcome.state == "completed"


# ------------------------------------------------------ exception safety


def test_sidecar_failure_stops_the_child_and_records_driver_error(tmp_path, monkeypatch):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    real = runstore.SidecarWriter.write
    calls = {"n": 0}

    def flaky(self, event):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk full")
        return real(self, event)

    monkeypatch.setattr(runstore.SidecarWriter, "write", flaky)
    with pytest.raises(OSError):
        list(drv.observe(handle))
    monkeypatch.undo()
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == "interrupted"
    assert record["attempts"][0]["reason"] == "driver-error"
    assert handle.proc.poll() is not None
    assert h.stray_pids(tmp_path) == []


def test_closing_the_generator_records_observer_closed(tmp_path):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    gen = drv.observe(handle)
    next(gen)
    gen.close()
    record = h.load_record(tmp_path, prepared.run_id)
    assert (record["state"], record["attempts"][0]["reason"]) == ("interrupted", "observer-closed")
    assert handle.proc.poll() is not None


def test_stdout_holding_grandchild_is_reaped_and_counted(tmp_path):
    prepared = h.make_prepared(tmp_path, "grandchild_holds_stdout")
    drv = h.make_driver(tmp_path)
    started = time.monotonic()
    handle, events = h.run_to_end(drv, prepared)
    assert time.monotonic() - started < 2 * 0.5 + 2.0 + 1 + 2
    assert handle.outcome.state == "completed" and handle.outcome.evidence == "full"
    assert h.load_record(tmp_path, prepared.run_id)["attempts"][0]["leftovers_reaped"] is True
    assert h.stray_pids(tmp_path) == []


def test_stderr_secret_straddling_the_bound_is_gone(tmp_path):
    secret = "sk-FAKESTRADDLE0123456789"
    drv, prepared, handle, events = _run(tmp_path, "secret_echo_straddle")
    record = h.load_record(tmp_path, prepared.run_id)
    assert secret not in record["stderr_tail"]
    assert secret not in prepared.artifact_paths.record.read_text()


# ------------------------------------------------------- record ownership


def test_cancel_from_another_thread_writes_one_terminal_record(tmp_path):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    seen = []
    results = []
    thread = threading.Thread(target=lambda: (time.sleep(0.5), results.append(drv.cancel(handle))))
    thread.start()
    seen = list(drv.observe(handle))
    thread.join(10)
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == "cancelled"
    assert [s["state"] for s in record["history"]].count("cancelled") == 1
    assert drv.cancel(handle) is results[0]
    sidecar = h.read_events(prepared)
    assert record["counters_total"]["lines"] == handle.pipeline.counters["lines"]
    assert record["usage_totals"] == ev.usage_totals(sidecar).to_dict()


def test_cancel_without_an_observer_finalizes_itself(tmp_path):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    time.sleep(0.3)
    result = drv.cancel(handle)
    assert result.signalled_term and handle.finalized
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == "cancelled"
    assert drv.cancel(handle) is result
    assert list(drv.observe(handle)) == []


def test_request_cancel_and_cancel_terminate_once(tmp_path):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    calls = []
    real = drv._terminate

    def counted(handle, *a, **kw):
        calls.append(1)
        return real(handle, *a, **kw)

    drv._terminate = counted
    handle = drv.start(prepared)
    handle.request_cancel()
    results = []
    thread = threading.Thread(target=lambda: results.append(drv.cancel(handle)))
    thread.start()
    list(drv.observe(handle))
    thread.join(10)
    assert len(calls) == 1
    assert handle.cancel_result is results[0]
    assert handle.outcome.state == "cancelled"


def test_record_writes_never_interleave(tmp_path, monkeypatch):
    prepared = h.make_prepared(tmp_path, "slow_finish")
    drv = h.make_driver(tmp_path)
    handle = drv.start(prepared)
    real = runstore.atomic_write_json
    active = {"n": 0, "max": 0}

    def slow(path, obj):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        real(path, obj)
        active["n"] -= 1

    monkeypatch.setattr(runstore, "atomic_write_json", slow)
    thread = threading.Thread(target=lambda: (time.sleep(0.2), drv.cancel(handle)))
    thread.start()
    list(drv.observe(handle))
    thread.join(10)
    assert active["max"] == 1


# ----------------------------------------------------------- record content


def test_hashes_revisions_and_sequences(tmp_path):
    scenario = {"attempts": [{"steps": [
        {"do": "write_file", "path": ".workflow_artifacts/demo/out.md", "content": "made\n"},
        fake_step("start"), fake_step("finish"), {"do": "exit", "code": 0}]}]}
    drv, prepared, handle, events = _run(tmp_path, scenario)
    refs = [e for e in events if e.type is ev.EventType.ARTIFACT_REFERENCE]
    assert [(e.payload.path, e.payload.change) for e in refs] == [(".workflow_artifacts/demo/out.md", "created")]
    record = h.load_record(tmp_path, prepared.run_id)
    attempt = record["attempts"][0]
    assert attempt["input_hashes_before"] and ".workflow_artifacts/demo/out.md" in attempt["input_hashes_after"]
    sidecar = h.read_events(prepared)
    assert [e.sequence for e in sidecar] == list(range(1, len(sidecar) + 1))
    assert all(e.schema_version == 1 for e in sidecar)
    assert sidecar[0].type is ev.EventType.STARTED and sidecar[-1].type is ev.EventType.STOPPED


def fake_step(kind):
    if kind == "start":
        return h.fake._step_start("prt_s1")
    return h.fake._step_finish("prt_f1", "stop")


def test_start_requires_a_prepared_run(tmp_path):
    prepared = h.make_prepared(tmp_path, "record_only")
    drv = h.make_driver(tmp_path)
    handle, _ = h.run_to_end(drv, prepared)
    with pytest.raises(driver.IllegalTransition):
        drv.start(prepared)


def test_spawn_failure_leaves_the_run_interrupted(tmp_path):
    prepared = h.make_prepared(tmp_path, "record_only")
    prepared = driver.dataclasses.replace(prepared, argv=("/nonexistent/opencode", "run"))
    drv = h.make_driver(tmp_path)
    with pytest.raises(OSError):
        drv.start(prepared)
    record = h.load_record(tmp_path, prepared.run_id)
    assert record["state"] == "interrupted"
    assert record["attempts"][0]["reason"] == "driver-error"


def test_seeded_secret_never_persisted(tmp_path):
    import hashlib
    import json

    secret = "sk-FAKEECHOSECRET0123456789"
    prepared = h.make_prepared(tmp_path, "secret_echo", secret=secret)
    assert secret not in repr(prepared)
    drv = h.make_driver(tmp_path)
    handle, events = h.run_to_end(drv, prepared)
    store = h.store_of(tmp_path)
    files = list(store.iterdir())
    assert files
    for path in files:
        assert secret not in path.read_text(), path.name
    for ev_ in events:
        assert secret not in json.dumps(ev_.to_dict(), default=str)
    record = h.load_record(tmp_path, prepared.run_id)
    assert secret not in json.dumps(record, default=str)
    assert secret not in "\n".join(map(str, prepared.argv))
    state = h.state_of(tmp_path)
    invocations = (state / "invocations.jsonl").read_text()
    assert secret not in invocations
    recorded = json.loads(invocations.splitlines()[0])
    assert secret not in " ".join(recorded["argv"]) and "PROV_KEY" in recorded["env_names"]
    # The value reached the child (only its hash was written), and nowhere else.
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert ("PROV_KEY %s" % digest) in (state / "env-hashes.txt").read_text().splitlines()
    for path in state.rglob("*"):
        if path.is_file() and path.name != "scenario.json":
            assert secret not in path.read_text(errors="replace"), path.name


def test_a_late_stderr_notice_is_read_before_the_attempt_is_classified(tmp_path):
    drv, prepared, handle, events = _run(
        tmp_path, "approval_notice_then_finish", drv_kw={"stderr_read_delay_s": 0.3}
    )
    assert handle.outcome.state == "awaiting_approval"
    assert any(e.type is ev.EventType.APPROVAL_REQUIRED for e in events)


def test_a_detached_leftover_is_reaped_at_the_end_of_a_clean_attempt(tmp_path):
    # The grandchild is detached from the child's session, so only a scan made
    # while the child is alive can find it; the fake lingers so one does.
    scenario = {"attempts": [{"steps": [
        h.fake._step_start("prt_s1"), {"do": "spawn_grandchild", "ignore_term": False, "detach": True},
        {"do": "sleep", "seconds": 0.8}, h.fake._step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}]}]}
    drv, prepared, handle, events = _run(tmp_path, scenario, drv_kw={"descendant_scan_s": 0.1})
    assert handle.outcome.state == "completed"
    attempt = h.load_record(tmp_path, prepared.run_id)["attempts"][0]
    assert attempt["leftovers_reaped"] is True and attempt["leftovers_reaped_count"] >= 1
    assert h.stray_pids(tmp_path) == []


def test_a_slow_consumer_back_pressures_through_a_bounded_queue(tmp_path):
    steps = [h.fake._step_start("prt_s0")]
    steps += [h.fake._text("prt_t%d" % i, "line %d" % i) for i in range(120)]
    steps += [h.fake._step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}]
    prepared = h.make_prepared(tmp_path, {"attempts": [{"steps": steps}]})
    drv = h.make_driver(tmp_path, queue_size=2)
    handle = drv.start(prepared)
    biggest = 0
    seen = 0
    for _event in drv.observe(handle):
        biggest = max(biggest, handle.stdout_q.qsize())
        seen += 1
        if seen < 30:
            time.sleep(0.02)
    assert biggest <= 2
    assert handle.outcome.state == "completed"
    texts = [e for e in h.read_events(prepared) if e.origin == "native"]
    assert len(texts) == 122


def test_the_line_cap_is_configurable_and_counted_once_per_line(tmp_path):
    drv, prepared, handle, events = _run(
        tmp_path, ("endless_line", {"nbytes": 200_000}), drv_kw={"max_line_bytes": 1024}
    )
    counters = h.load_record(tmp_path, prepared.run_id)["counters_total"]
    assert counters["oversized"] == 1
    assert handle.outcome.state == "completed"
