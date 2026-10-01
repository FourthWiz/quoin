"""The phase loop, driven by a scripted driver, a fake clock and a fake sleep."""
from __future__ import annotations

import inspect
import json
import signal

import pytest

from quoin.opencode_adapter import driver, launch_env, phase_loop, retry, runstore

from _opencode_run_helpers import (
    TASK, Boom, FakeClock, RecToken, ScriptedDriver, outcome,
)

COMPLETED, FAILED, INTERRUPTED = "completed", "failed", "interrupted"


def request_for(tmp_path, **kw):
    kw.setdefault("stage", None)
    kw.setdefault("phase", "plan")
    kw.setdefault("profile", "work")
    return driver.RunRequest(project_root=tmp_path, task=TASK, **kw)


def go(tmp_path, script, *, max_relaunch=3, new_run=False, token=None, backoff=None,
       request=None, drv=None, **drv_kw):
    drv = drv or ScriptedDriver(tmp_path, script, **drv_kw)
    token = token or RecToken(drv.clock)
    calls = []

    def default_backoff(n):
        calls.append(n)
        return float(n * 10)

    result = phase_loop.run_phase(
        drv, request or request_for(tmp_path), max_relaunch=max_relaunch, cancel=token,
        new_run=new_run, backoff_fn=backoff or default_backoff, monotonic=drv.clock,
    )
    return result, drv, token, calls


def http429(**kw):
    return retry.Failure("http", status=429, **kw)


# --- terminal mapping -----------------------------------------------------


def test_completed_full(tmp_path):
    r, drv, *_ = go(tmp_path, [outcome()])
    assert (r.outcome, r.attempts, r.evidence) == ("COMPLETED", 1, "full")
    assert r.run_state == "completed" and r.sidecar and r.artifact_coverage == "full"
    assert drv.names() == ["reconcile_task", "prepare", "start", "observe"]


def test_completed_partial_is_unverified(tmp_path):
    r, *_ = go(tmp_path, [outcome(evidence="partial", reason="denied-halt")])
    assert r.outcome == "COMPLETED_UNVERIFIED" and r.reason == "denied-halt"


def test_awaiting_approval_and_cancelled(tmp_path):
    assert go(tmp_path, [outcome("awaiting_approval", reason="approval-required")])[0].outcome == "AWAITING_APPROVAL"
    other = tmp_path / "b"
    other.mkdir()
    assert go(other, [outcome("cancelled", reason="cancelled")])[0].outcome == "CANCELLED"


@pytest.mark.parametrize("reason", driver.RESUME_BLOCK_REASONS)
def test_interrupted_with_resume_block_stops(tmp_path, reason):
    r, drv, *_ = go(tmp_path, [outcome(INTERRUPTED, reason=reason, resume_blocked=reason)])
    assert (r.outcome, r.resume_blocked, r.attempts) == ("INTERRUPTED", reason, 1)
    assert drv.names().count("start") == 1 and "resume" not in drv.names()


@pytest.mark.parametrize("reason", driver.RESUME_BLOCK_REASONS)
def test_resume_raising_resume_blocked(tmp_path, reason):
    script = [outcome(INTERRUPTED, reason="signal"), Boom(driver.ResumeBlocked(reason), "resume")]
    r, drv, *_ = go(tmp_path, script)
    assert (r.outcome, r.resume_blocked) == ("INTERRUPTED", reason)
    assert drv.names().count("start") == 1 and drv.names().count("resume") == 1
    assert drv.names().count("observe") == 1


# --- budgets ----------------------------------------------------------------


def test_interrupted_budget(tmp_path):
    script = [outcome(INTERRUPTED, reason="signal")] * 5
    r, drv, token, backoffs = go(tmp_path, script, max_relaunch=2)
    assert (r.outcome, r.reason, r.attempts) == ("INTERRUPTED", "relaunch cap", 3)
    assert backoffs == [1, 2] and token.delays == [10.0, 20.0]
    assert drv.policy.decisions == []
    assert drv.names().count("start") == 1 and drv.names().count("resume") == 2


def test_max_relaunch_zero_means_single_attempt(tmp_path):
    r, drv, *_ = go(tmp_path, [outcome(INTERRUPTED, reason="signal")] * 2, max_relaunch=0)
    assert (r.reason, r.attempts) == ("relaunch cap", 1)


def test_failed_budget_exhausted(tmp_path):
    script = [outcome(FAILED, reason="error-event", failure=http429())] * 5
    r, drv, token, _ = go(tmp_path, script, limits={"max_transient_retries": 2})
    assert (r.outcome, r.reason, r.attempts) == ("ABORTED", "relaunch cap", 3)
    assert token.delays == [0.5, 1.0]
    assert drv.names().count("resume") == 2


def test_retry_after_sets_the_delay(tmp_path):
    script = [outcome(FAILED, failure=http429(retry_after="7")), outcome()]
    r, drv, token, _ = go(tmp_path, script, limits={"max_transient_retries": 2})
    assert r.outcome == "COMPLETED" and token.delays == [7.0]


def test_non_transient_failure_stops(tmp_path):
    r, drv, *_ = go(tmp_path, [outcome(FAILED, failure=retry.Failure("auth"))] * 2,
                    limits={"max_transient_retries": 5})
    assert (r.outcome, r.reason, r.attempts) == ("FAILED", "not-transient", 1)


def test_missing_failure_is_treated_as_config(tmp_path):
    r, *_ = go(tmp_path, [outcome(FAILED)], limits={"max_transient_retries": 5})
    assert (r.outcome, r.reason) == ("FAILED", "not-transient")


def test_mutating_tool_in_flight_is_effect_uncertain(tmp_path):
    f = http429(mutating_tool_in_flight=True)
    r, *_ = go(tmp_path, [outcome(FAILED, failure=f)] * 2, limits={"max_transient_retries": 5})
    assert (r.outcome, r.reason) == ("FAILED", "effect-uncertain")


def test_time_budget_uses_first_attempt_origin(tmp_path):
    script = [outcome(FAILED, failure=http429())] * 3
    r, drv, *_ = go(
        tmp_path, script, attempt_seconds=50.0,
        limits={"max_transient_retries": 9, "max_run_seconds": 60},
    )
    assert (r.outcome, r.reason) == ("ABORTED", "run budget")
    assert drv.policy.starts == 1
    assert all(started_at == 0.0 for _, started_at, _ in drv.policy.decisions)
    assert [d[0] for d in drv.policy.decisions] == [1, 2]


def test_budgets_are_never_mixed(tmp_path):
    script = [
        outcome(INTERRUPTED, reason="signal"), outcome(FAILED, failure=http429()),
        outcome(INTERRUPTED, reason="signal"), outcome(FAILED, failure=http429()),
        outcome(),
    ]
    r, drv, token, backoffs = go(tmp_path, script, max_relaunch=2,
                                 limits={"max_transient_retries": 2})
    assert r.outcome == "COMPLETED" and r.attempts == 5
    assert backoffs == [1, 2]
    assert [d[0] for d in drv.policy.decisions] == [1, 2]


# --- progress guard ---------------------------------------------------------


def test_two_attempts_without_progress_abort(tmp_path):
    script = [outcome(INTERRUPTED, reason="signal", new_native_events=0)] * 5
    r, drv, *_ = go(tmp_path, script, max_relaunch=5, events_per_attempt=5)
    assert (r.outcome, r.reason, r.attempts) == ("ABORTED", "no forward progress", 2)


def test_progress_resets_the_streak(tmp_path):
    script = [
        outcome(INTERRUPTED, reason="signal", new_native_events=0),
        outcome(INTERRUPTED, reason="signal", new_native_events=2),
        outcome(INTERRUPTED, reason="signal", new_native_events=0),
        outcome(),
    ]
    r, *_ = go(tmp_path, script, max_relaunch=5)
    assert r.outcome == "COMPLETED" and r.attempts == 4


# --- cancel -------------------------------------------------------------------


def test_cancel_before_first_attempt(tmp_path):
    token = RecToken()
    token.cancel(signal.SIGTERM)
    r, drv, *_ = go(tmp_path, [outcome()], token=token)
    assert r.outcome == "CANCELLED" and r.signum == signal.SIGTERM
    assert drv.calls == []


def test_cancel_during_prepare_never_starts(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    token = RecToken(drv.clock)
    drv.on_prepare = lambda d, n: token.cancel()
    r, drv, *_ = go(tmp_path, [], drv=drv, token=token)
    assert r.outcome == "CANCELLED" and "start" not in drv.names()


def test_cancel_while_observing_reaches_the_handle(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome("cancelled", reason="cancelled")])
    token = RecToken(drv.clock)
    drv.on_observe = lambda d, h: token.cancel(signal.SIGINT)
    r, drv, *_ = go(tmp_path, [], drv=drv, token=token)
    assert r.outcome == "CANCELLED" and drv.handles[0].cancel_requests == 1
    assert token.events == ["attach", "detach"]


def test_cancel_during_backoff_wait(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome(INTERRUPTED, reason="signal")] * 3)
    token = RecToken(drv.clock)
    token._on_sleep = lambda s: token.cancel(signal.SIGTERM)
    r, drv, *_ = go(tmp_path, [], drv=drv, token=token)
    assert r.outcome == "CANCELLED" and r.attempts == 1
    assert "resume" not in drv.names()
    assert r.run_state == "interrupted"


def test_cancel_during_retry_after_wait(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome(FAILED, failure=http429(retry_after="7"))] * 3,
                         limits={"max_transient_retries": 5})
    token = RecToken(drv.clock)
    token._on_sleep = lambda s: token.cancel()
    r, drv, *_ = go(tmp_path, [], drv=drv, token=token)
    assert r.outcome == "CANCELLED" and "resume" not in drv.names()
    assert r.run_state == "failed" and token.delays == [7.0]


# --- CancelToken --------------------------------------------------------------


def test_cancel_token_attach_after_set_cancels_the_handle():
    token = phase_loop.CancelToken()
    token.cancel()
    seen = []
    token.attach(type("H", (), {"request_cancel": lambda self: seen.append(1)})())
    assert seen == [1]


def test_cancel_from_inside_sleep_ends_the_wait_after_one_slice():
    clock = FakeClock()
    sleeps = []
    holder = {}

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)
        holder["t"].cancel(15)

    holder["t"] = phase_loop.CancelToken(sleep=sleep, monotonic=clock)
    assert holder["t"].wait(30) is True
    assert sleeps == [0.2]


def test_wait_slices_are_bounded():
    clock = FakeClock()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)

    token = phase_loop.CancelToken(sleep=sleep, monotonic=clock)
    assert token.wait(1.0) is False
    assert max(sleeps) <= 0.2 and abs(sum(sleeps) - 1.0) < 1e-9


def test_cancel_token_stays_lock_free():
    source = inspect.getsource(phase_loop)
    assert "import threading" not in source and "from threading" not in source
    body = inspect.getsource(phase_loop.CancelToken)
    for word in ("Event", "Condition", "Lock", "threading"):
        assert word not in body


def test_first_signal_number_is_kept():
    token = phase_loop.CancelToken()
    token.cancel(15)
    token.cancel(2)
    assert token.signum == 15 and token.is_set()


# --- refusals -------------------------------------------------------------------


def test_prepare_refused_on_first_prepare(tmp_path):
    drv = ScriptedDriver(tmp_path, [])
    drv.prepare_errors[0] = driver.PrepareRefused("policy-denial", "global-plugin", "no plugins")
    r, *_ = go(tmp_path, [], drv=drv)
    assert r.outcome == "REFUSED"
    assert r.refusal == {"category": "policy-denial", "code": "global-plugin", "message": "no plugins"}


def test_prepare_refused_on_resume_prepare(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome(INTERRUPTED, reason="signal")])
    drv.prepare_errors[1] = driver.PrepareRefused("invalid-configuration", "changed", "config changed")
    r, *_ = go(tmp_path, [], drv=drv)
    assert r.outcome == "REFUSED" and r.refusal["code"] == "changed" and r.run_id


@pytest.mark.parametrize("where", ["start", "resume"])
def test_launch_refused_is_redacted(tmp_path, where):
    secret = "s3cr3t-value-0123456789"
    redactor = launch_env.Redactor()
    redactor.add(secret)
    token = "sk-" + "aB3" * 10
    message = "bad %s and %s" % (secret, token)
    exc = launch_env.LaunchRefused("invalid-configuration", "compiled-changed", message)
    script = [Boom(exc, "start")] if where == "start" else [
        outcome(INTERRUPTED, reason="signal"), Boom(exc, "resume")]
    r, *_ = go(tmp_path, script, redactor=redactor)
    assert r.outcome == "REFUSED"
    text = r.refusal["message"]
    assert secret not in text and token not in text


# --- run pick ------------------------------------------------------------------


def test_matching_interrupted_run_is_resumed(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert r.run_id == run_id and r.outcome == "COMPLETED"
    assert drv.calls[1] == ("prepare", run_id) and drv.names()[2] == "resume"


@pytest.mark.parametrize("override", [{"phase": "review"}, {"stage": "2"}, {"profile": "other"}])
def test_mismatched_request_starts_a_new_run(tmp_path, override):
    drv = ScriptedDriver(tmp_path, [outcome()])
    old = drv.seed("interrupted", **override)
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert r.run_id != old and drv.calls[1] == ("prepare", None)


@pytest.mark.parametrize("state", ["failed", "completed", "cancelled", "awaiting_approval"])
def test_other_states_start_a_new_run(tmp_path, state):
    drv = ScriptedDriver(tmp_path, [outcome()])
    old = drv.seed(state)
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert r.run_id != old and drv.calls[1] == ("prepare", None)


def test_new_run_flag_forces_a_new_run(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    old = drv.seed("interrupted")
    r, drv, *_ = go(tmp_path, [], drv=drv, new_run=True)
    assert r.run_id != old and drv.calls[1] == ("prepare", None)


def test_phase_name_dash_and_underscore_match(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    old = drv.seed("interrupted", phase="fast-plan")
    r, *_ = go(tmp_path, [], drv=drv, request=request_for(tmp_path, phase="fast_plan"))
    assert r.run_id == old


_STAGED = {"attempt": 1, "pid": None, "pgid": None, "driver_pid": 77, "driver_start": None, "state": "staged"}
_FULL = {"attempt": 1, "pid": 4242, "pgid": 4242, "driver_pid": 77, "driver_start": "x", "state": "running"}
SHAPES = {"no-attempts": [], "staged": [_STAGED], "full": [_FULL]}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_running_run_refuses_without_new_run(tmp_path, shape):
    drv = ScriptedDriver(tmp_path, [outcome()])
    drv.seed("running", attempts=SHAPES[shape])
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert r.outcome == "REFUSED" and r.refusal["code"] == "run-in-progress"
    assert r.refusal["category"] == "workflow-validation"
    message = r.refusal["message"]
    assert "--new-run" in message
    if shape == "full":
        assert "pid 4242" in message and "-g 4242" in message and "kill -TERM -4242" in message
    else:
        assert "no opencode child was recorded" in message and "kill" not in message
    assert "prepare" not in drv.names()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_running_run_is_superseded_with_new_run(tmp_path, shape):
    drv = ScriptedDriver(tmp_path, [outcome()])
    stale = drv.seed("running", attempts=SHAPES[shape])
    path = runstore.run_paths(drv.directory, stale).record
    before = path.read_bytes()
    r, drv, *_ = go(tmp_path, [], drv=drv, new_run=True)
    assert r.outcome == "COMPLETED" and r.run_id != stale
    assert drv.calls[1] == ("prepare", None)
    last = SHAPES[shape][-1] if SHAPES[shape] else {}
    assert r.superseded_run == {"run_id": stale, "pid": last.get("pid"), "pgid": last.get("pgid")}
    assert path.read_bytes() == before


# --- checkpoint ----------------------------------------------------------------


def test_missing_checkpoint_gives_a_minimal_handoff(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    go(tmp_path, [], drv=drv)
    h = drv.handoffs[0]
    assert (h.run_id, h.last_sequence, h.native_session_id, h.step_open) == (run_id, 0, None, False)


def test_corrupt_checkpoint_is_interrupted(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    runstore.write_checkpoint(drv.directory, {"schema_version": 1, "run_id": run_id})
    r, drv, *_ = go(tmp_path, [], drv=drv)
    assert (r.outcome, r.reason) == ("INTERRUPTED", "checkpoint-invalid")
    assert "resume" not in drv.names()


def test_valid_checkpoint_reaches_resume(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    runstore.write_checkpoint(drv.directory, runstore.new_checkpoint(
        run_id, 1, last_sequence=9, sidecar_offset=0, native_session_id="ses_1",
        repo_revisions=[], step_open=False, ran_anything=True, state_changing_part_ids=[]))
    go(tmp_path, [], drv=drv)
    assert drv.handoffs[0].last_sequence == 9 and drv.handoffs[0].native_session_id == "ses_1"


# --- run budget ----------------------------------------------------------------


def test_deadline_is_the_run_budget_then_the_remainder(tmp_path):
    script = [outcome(INTERRUPTED, reason="signal"), outcome(INTERRUPTED, reason="signal"),
              outcome()]
    r, drv, *_ = go(
        tmp_path, script, max_relaunch=5, attempt_seconds=60.0,
        backoff=lambda n: 0.0, limits={"max_run_seconds": 100},
        request=request_for(tmp_path, timeout_s=1800),
    )
    assert drv.deadlines == [100.0, 40.0]
    assert (r.outcome, r.reason) == ("INTERRUPTED", "run-budget")
    assert drv.names().count("start") + drv.names().count("resume") == 2


def test_request_timeout_caps_the_deadline(tmp_path):
    r, drv, *_ = go(tmp_path, [outcome()], limits={"max_run_seconds": 100},
                    request=request_for(tmp_path, timeout_s=30))
    assert drv.deadlines == [30]


def test_timeout_after_the_budget_is_run_budget(tmp_path):
    script = [outcome(INTERRUPTED, reason="timeout")]
    r, *_ = go(tmp_path, script, attempt_seconds=100.0, limits={"max_run_seconds": 100})
    assert (r.outcome, r.reason) == ("INTERRUPTED", "run-budget")


def test_no_limit_uses_the_default_timeout(tmp_path):
    r, drv, *_ = go(tmp_path, [outcome()])
    assert drv.deadlines == [driver.DEFAULT_TIMEOUT_S]


# --- sentinels -------------------------------------------------------------------


def test_done_and_halt_sentinels_do_not_stop_the_phase(tmp_path):
    memory = tmp_path / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True)
    done = memory / "autonomous-done-demo.md"
    halt = memory / "autonomous-halt-demo.md"
    done.write_text("x")
    halt.write_text("y")
    r, drv, *_ = go(tmp_path, [outcome()])
    assert r.outcome == "COMPLETED" and drv.names().count("start") == 1
    assert done.read_text() == "x" and halt.read_text() == "y"
    assert sorted(p.name for p in memory.iterdir() if p.is_file()) == [done.name, halt.name]


# --- artifact coverage -----------------------------------------------------------


def test_artifact_coverage_kinds():
    assert phase_loop.artifact_coverage(None) == "unknown"
    assert phase_loop.artifact_coverage({"input_hashes": {"a": "1"}, "attempts": []}) == "full"
    assert phase_loop.artifact_coverage({"input_hashes": {"<truncated>": True}}) == "partial"
    record = {"input_hashes": {}, "attempts": [{"input_hashes_before": {"<truncated>": True}}]}
    assert phase_loop.artifact_coverage(record) == "partial"


def test_result_reports_partial_coverage(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    run_id = drv.seed("interrupted")
    record = drv.record(run_id)
    record["input_hashes"] = {"<truncated>": True}
    runstore.write_record(drv.directory, record)
    r, *_ = go(tmp_path, [], drv=drv)
    assert r.run_id == run_id and r.artifact_coverage == "partial"


# --- unexpected exceptions -------------------------------------------------------


def test_exception_in_observe_becomes_error_with_the_run(tmp_path):
    script = [Boom(RuntimeError("secret-ish text"), "observe")]
    r, drv, token, _ = go(tmp_path, script)
    assert r.outcome == "ERROR" and r.reason == "driver-error: RuntimeError"
    assert r.run_id and r.sidecar and r.attempts == 1 and r.run_state == "interrupted"
    assert token.events == ["attach", "detach"]
    assert "secret-ish" not in repr(r)


@pytest.mark.parametrize("where", ["start", "resume"])
def test_exception_in_start_or_resume_keeps_the_run_id(tmp_path, where):
    script = [Boom(OSError("secret-ish text"), "start")] if where == "start" else [
        outcome(INTERRUPTED, reason="signal"), Boom(OSError("secret-ish text"), "resume")]
    r, *_ = go(tmp_path, script)
    assert r.outcome == "ERROR" and r.run_id and "secret-ish" not in repr(r)


def test_exception_from_reconcile_propagates(tmp_path):
    drv = ScriptedDriver(tmp_path, [])
    drv.reconcile_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        go(tmp_path, [], drv=drv)


def test_exception_from_first_prepare_propagates(tmp_path):
    drv = ScriptedDriver(tmp_path, [])
    drv.prepare_errors[0] = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        go(tmp_path, [], drv=drv)


def test_keyboard_interrupt_propagates_after_detach(tmp_path):
    drv = ScriptedDriver(tmp_path, [Boom(KeyboardInterrupt(), "observe")])
    token = RecToken(drv.clock)
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, [], drv=drv, token=token)
    assert token.events == ["attach", "detach"]


# --- summary helpers -------------------------------------------------------------


def test_exit_codes():
    codes = {"COMPLETED": 0, "COMPLETED_UNVERIFIED": 6, "AWAITING_APPROVAL": 4, "INTERRUPTED": 5,
             "REFUSED": 3, "FAILED": 2, "ABORTED": 2, "ERROR": 2}
    for name, code in codes.items():
        assert phase_loop.exit_code(phase_loop.PhaseResult(outcome=name)) == code
    assert phase_loop.exit_code(phase_loop.PhaseResult("CANCELLED", signum=signal.SIGINT)) == 130
    assert phase_loop.exit_code(phase_loop.PhaseResult("CANCELLED", signum=signal.SIGTERM)) == 143
    assert phase_loop.exit_code(phase_loop.PhaseResult("CANCELLED")) == 143


def test_resume_hint_text():
    ident = {"task": "demo", "stage": None, "phase": "plan", "profile": "work"}
    interrupted = phase_loop.PhaseResult("INTERRUPTED")
    assert phase_loop.resume_hint(interrupted, ident, "/p q") == (
        "quoin run --runtime opencode --profile work --phase plan demo --project-root '/p q'"
    )
    staged = dict(ident, stage="2")
    assert "--stage 2 demo" in phase_loop.resume_hint(interrupted, staged, "/p")
    blocked = phase_loop.PhaseResult("INTERRUPTED", resume_blocked="session-lost")
    assert phase_loop.resume_hint(blocked, ident, "/p").endswith(" --new-run")
    assert phase_loop.resume_hint(phase_loop.PhaseResult("COMPLETED"), ident, "/p") is None
    assert phase_loop.resume_hint(phase_loop.PhaseResult("REFUSED"), ident, "/p") is None


def test_summary_key_set():
    ident = {"task": "demo", "stage": None, "phase": "plan", "profile": "work"}
    s = phase_loop.summary(phase_loop.PhaseResult("COMPLETED"), ident, None)
    assert sorted(s) == sorted([
        "runtime", "task", "stage", "phase", "profile", "outcome", "exit_code", "run_state",
        "evidence", "reason", "resume_blocked", "run_id", "sidecar", "attempts",
        "artifact_coverage", "refusal", "resume_hint", "superseded_run", "workflow_validated",
    ])
    assert s["workflow_validated"] is False and s["runtime"] == "opencode"
    json.dumps(s)
