"""Driver types, the run-state table, the pure classifier and failure mapping."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import driver as d
from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter import launch_env, retry, runstore
from quoin.opencode_adapter.events import EventType as ET

RUN_ID = "oc-20260101T000000Z-0123abcd"
TS = "2026-01-01T00:00:00.000Z"
_seq = [0]


def _event(etype=ET.PROGRESS, *, kind="text", delegation=None, permission_outcome=None,
           finish=None, origin="native", payload=None, native_id=None):
    _seq[0] += 1
    n = _seq[0]
    if payload is None:
        if etype is ET.USAGE:
            payload = ev.UsagePayload(input_tokens=1, output_tokens=1, finish_reason=finish)
        else:
            payload = ev.ProgressPayload(kind=kind, raw_type=kind, delegation=delegation,
                                         permission_outcome=permission_outcome)
    native = ev.NativeRef(type="part", id=native_id or "p%d" % n) if origin == "native" else None
    return ev.RuntimeEvent(
        schema_version=1, run_id=RUN_ID, attempt=1, sequence=n, session_id="s1", parent_id=None,
        timestamp=TS, observed_at=TS, type=etype, origin=origin, native=native, payload=payload,
    )


def _clean_steps():
    return [
        _event(kind="step_start", native_id="s1"),
        _event(ET.USAGE, finish="stop", native_id="f1"),
    ]


def _facts(events=None, seed=None, **kw):
    facts = d.AttemptFacts.seed(seed) if seed is not None else d.AttemptFacts()
    for e in (_clean_steps() if events is None else events):
        facts.update(e)
    facts.exit_code = kw.pop("exit_code", 0)
    for key, value in kw.items():
        setattr(facts, key, value)
    return facts


def _error(kind="http", status=429, retry_after="5"):
    return ev.ErrorPayload(name="APIError", message="boom", failure_kind=kind,
                           http_status=status, retry_after=retry_after)


def _approval():
    return _event(ET.APPROVAL_REQUIRED, payload=ev.ApprovalRequiredPayload(evidence_source="tool_error"))


def _native_error(payload=None):
    return _event(ET.ERROR, payload=payload or _error())


def _seed(downgrades=(), halt=False):
    return runstore.RunFacts(True, frozenset(downgrades), halt, False)


# ------------------------------------------------------------- constants


def test_constants():
    assert d.REFUSAL_CATEGORIES == (
        "missing-binary", "unsupported-version", "invalid-configuration", "unqualified-gateway",
        "policy-denial", "missing-optional-integration", "workflow-validation",
    )
    assert d.WRITABLE_TOOLS == ("write", "edit", "patch", "multiedit", "bash")
    assert d.RESUME_MESSAGE
    assert d.STEP_SETTLING_VERIFIED is True and d.NON_GIT_DISCOVERY_VERIFIED is True


def test_launch_env_categories_are_driver_categories():
    source = (Path(launch_env.__file__)).read_text(encoding="utf-8")
    used = set(re.findall(r'"((?:missing|unsupported|invalid|unqualified|policy|workflow)[a-z-]+)"', source))
    assert used and used <= set(d.REFUSAL_CATEGORIES)


# ------------------------------------------------------------ transitions

ALLOWED = [
    ("prepared", "running"), ("prepared", "prepared"),
    ("running", "completed"), ("running", "failed"), ("running", "awaiting_approval"),
    ("running", "cancelled"), ("running", "interrupted"),
    ("interrupted", "running"), ("failed", "running"),
]


def _record(state):
    return {"state": state, "history": [], "updated_at": TS}


@pytest.mark.parametrize("old,new", ALLOWED)
def test_allowed_transitions(old, new):
    record = d.transition(_record(old), new, "why", "T1")
    assert record["state"] == new and record["updated_at"] == "T1"
    assert record["history"][-1] == {"state": new, "at": "T1", "reason": "why"}


@pytest.mark.parametrize("old", ["completed", "awaiting_approval", "cancelled"])
@pytest.mark.parametrize("new", ["running", "prepared", "failed"])
def test_terminal_states_are_sticky(old, new):
    with pytest.raises(d.IllegalTransition):
        d.transition(_record(old), new, None, "T")


@pytest.mark.parametrize("old,new", [
    ("interrupted", "prepared"), ("failed", "prepared"), ("prepared", "completed"),
    ("running", "running"), ("running", "prepared"), ("interrupted", "completed"), ("failed", "cancelled"),
])
def test_illegal_transitions_leave_record_untouched(old, new):
    record = _record(old)
    before = copy.deepcopy(record)
    with pytest.raises(d.IllegalTransition):
        d.transition(record, new, None, "T")
    assert record == before


def test_unknown_states_are_illegal():
    with pytest.raises(d.IllegalTransition):
        d.transition(_record("weird"), "running", None, "T")
    with pytest.raises(d.IllegalTransition):
        d.transition(_record("running"), "weird", None, "T")


# ------------------------------------------------------------- classifier


def _c(**kw):
    return d.classify(_facts(**kw))


def test_clean_run_is_completed_full():
    out = _c()
    assert (out.state, out.evidence, out.reason, out.failure) == ("completed", "full", None, None)


def test_operator_cancel_beats_approval():
    out = _c(cancelled=True, approval=True)
    assert out.state == "cancelled"


def test_rejection_plus_driver_kill_is_awaiting_approval():
    out = _c(events=_clean_steps() + [_approval()], exit_signal=15, exit_code=None)
    assert out.state == "awaiting_approval" and out.failure is None


def test_native_error_plus_approval_notice_is_awaiting_approval():
    out = _c(events=_clean_steps() + [_native_error(), _approval()], exit_code=1)
    assert out.state == "awaiting_approval"


def test_native_error_plus_crash_is_failed_with_failure():
    out = _c(events=[_event(kind="step_start", native_id="a")] + [_native_error()], exit_signal=9, exit_code=None)
    assert out.state == "failed" and out.reason == "error-event"
    assert out.failure == retry.Failure("http", status=429, retry_after="5", mutating_tool_in_flight=True)


def test_deadline_kill_is_interrupted_timeout():
    out = _c(timeout=True, exit_signal=15, exit_code=None)
    assert (out.state, out.reason) == ("interrupted", "timeout")


def test_denied_halt_with_exit_one_is_completed_partial():
    events = _clean_steps() + [_event(kind="halted", permission_outcome="denied")]
    for code in (0, 1):
        out = d.classify(_facts(events=events, exit_code=code))
        assert (out.state, out.evidence, out.reason) == ("completed", "partial", "denied-halt")


def test_denied_halt_with_sigkill_is_interrupted():
    events = _clean_steps() + [_event(kind="halted", permission_outcome="denied")]
    out = d.classify(_facts(events=events, exit_signal=9, exit_code=None))
    assert (out.state, out.reason) == ("interrupted", "signal")


def test_denied_tool_then_clean_finish_is_full():
    events = [_event(kind="tool", permission_outcome="denied")] + _clean_steps()
    assert d.classify(_facts(events=events)).evidence == "full"


def test_background_task_is_partial():
    out = d.classify(_facts(events=_clean_steps() + [_event(kind="tool", delegation="background")]))
    assert (out.state, out.evidence) == ("completed", "partial")


def test_denied_tail_is_partial():
    out = d.classify(_facts(events=_clean_steps() + [_event(kind="tool", delegation="denied-tail")]))
    assert (out.state, out.evidence, out.reason) == ("completed", "partial", "delegated-denied-tail")


@pytest.mark.parametrize("seed", [_seed(["background"]), _seed(["denied-tail"]), _seed(["failed"]), _seed(halt=True)])
def test_earlier_attempt_downgrade_survives_a_clean_continuation(seed):
    out = d.classify(_facts(seed=seed))
    assert (out.state, out.evidence, out.reason) == ("completed", "partial", "earlier-attempt-downgrade")


def test_seeded_stop_evidence_is_ignored():
    seed = runstore.RunFacts(True, frozenset(), False, True)  # earlier attempt left a step open
    out = d.classify(_facts(seed=seed))
    assert (out.state, out.evidence) == ("completed", "full")


def test_errored_task_then_clean_finish_is_failed_delegation():
    events = [_event(kind="tool", delegation="failed")] + _clean_steps()
    out = d.classify(_facts(events=events))
    assert (out.state, out.reason) == ("failed", "delegation-failed")
    assert out.failure == retry.Failure("config")


def test_agent_fallback_is_failed_config():
    out = _c(agent_fallback=True)
    assert (out.state, out.reason, out.failure) == ("failed", "agent-fallback", retry.Failure("config"))


@pytest.mark.parametrize("flag,reason", [
    ("driver_error", "driver-error"), ("orphan", "driver-lost"), ("eof_without_exit", "eof-without-exit"),
])
def test_hard_interrupts(flag, reason):
    out = _c(exit_code=None, **{flag: True})
    assert (out.state, out.reason) == ("interrupted", reason)


def test_session_lost_blocks_resume_with_config_failure():
    out = _c(events=[], exit_code=1, session_lost=True)
    assert (out.state, out.reason, out.resume_blocked) == ("interrupted", "session-lost", "session-lost")
    assert out.failure == retry.Failure("config")


def test_external_signal_is_interrupted():
    out = _c(exit_signal=9, exit_code=None)
    assert (out.state, out.reason, out.signal) == ("interrupted", "signal", 9)


def test_soft_interrupts():
    assert _c(exit_code=2).reason == "nonzero-exit"
    assert _c(events=[]).reason == "no-stop-evidence"
    assert d.classify(_facts(events=[_event(kind="step_start", native_id="x")])).reason == "no-stop-evidence"
    tool_calls = [_event(kind="step_start", native_id="x"), _event(ET.USAGE, finish="tool-calls", native_id="y")]
    assert d.classify(_facts(events=tool_calls)).state == "interrupted"


def test_new_native_events_and_exit_are_carried():
    out = d.classify(_facts(exit_code=0))
    assert out.new_native_events == 2 and out.exit_code == 0 and out.signal is None


# ---------------------------------------------------------- failure_for


def test_failure_for_table():
    facts = d.AttemptFacts(native_error=_error("http", 503, "Wed, 21 Oct 2026 07:28:00 GMT"))
    failed = d.AttemptOutcome("failed", reason="error-event")
    assert d.failure_for(failed, facts, True) == retry.Failure(
        "http", status=503, retry_after="Wed, 21 Oct 2026 07:28:00 GMT", mutating_tool_in_flight=True)
    assert d.failure_for(failed, facts, False).mutating_tool_in_flight is False
    auth = d.AttemptFacts(native_error=_error("auth", None, None))
    assert d.failure_for(failed, auth, False) == retry.Failure("auth")
    for reason in ("agent-fallback", "delegation-failed"):
        assert d.failure_for(d.AttemptOutcome("failed", reason=reason), facts, True) == retry.Failure("config")
    assert d.failure_for(d.AttemptOutcome("interrupted", reason="session-lost"), facts, False) == retry.Failure("config")
    for state in ("completed", "cancelled", "awaiting_approval", "interrupted"):
        assert d.failure_for(d.AttemptOutcome(state, reason="x"), facts, False) is None


def test_failure_for_never_produces_policy():
    for kind in ev.TRANSLATABLE_FAILURE_KINDS:
        status = 500 if kind == "http" else None
        facts = d.AttemptFacts(native_error=_error(kind, status, None))
        got = d.failure_for(d.AttemptOutcome("failed", reason="error-event"), facts, False)
        assert got.kind != "policy"


# ----------------------------------------------------------------- types


def test_handoff_from_checkpoint():
    cp = runstore.new_checkpoint(
        RUN_ID, 2, last_sequence=7, sidecar_offset=10, native_session_id="ses_1",
        repo_revisions=[{"repo": ".", "head": "abc", "dirty": False}], step_open=True,
        ran_anything=True, state_changing_part_ids=[],
    )
    h = d.Handoff.from_checkpoint(cp)
    assert (h.run_id, h.last_sequence, h.native_session_id, h.step_open, h.ran_anything) == (RUN_ID, 7, "ses_1", True, True)
    assert h.repo_revisions == ({"repo": ".", "head": "abc", "dirty": False},)
    for bad in ({}, {"run_id": "x", "last_sequence": 1}, {"run_id": RUN_ID, "last_sequence": -1},
                {"run_id": RUN_ID, "last_sequence": True}, "nope"):
        with pytest.raises(ValueError):
            d.Handoff.from_checkpoint(bad)
    for session in ("--agent", "-x", "a b", "ses\n1", "", "s" * 200, 5):
        with pytest.raises(ValueError):
            d.Handoff.from_checkpoint(dict(cp, native_session_id=session))


def test_prepared_run_hides_secrets_and_exceptions_carry_fields(tmp_path):
    env = launch_env.LaunchEnv({"KEY": "sk-secret-value-000"})
    req = d.RunRequest(tmp_path, "t", None, "plan", "work")
    run = d.PreparedRun(
        request=req, run_id=RUN_ID, binary=Path("/b"), runtime_version="1", role="r", effective_model="m",
        config_digest="d", native_sha256="s", config_path=Path("/c"), cwd=tmp_path, argv=("a",),
        env_names=("KEY",), policy=None, retry=None, artifact_paths=None, launch_env=env,
    )
    assert "sk-secret-value-000" not in repr(run)
    assert run == d.PreparedRun(**{**run.__dict__, "launch_env": None})
    err = d.PrepareRefused("policy-denial", "code", "msg", RUN_ID)
    assert (err.category, err.code, err.message, err.run_id, str(err)) == ("policy-denial", "code", "msg", RUN_ID, "msg")
    blocked = d.ResumeBlocked("effect-uncertain", RUN_ID)
    assert (blocked.reason, blocked.run_id) == ("effect-uncertain", RUN_ID)


@pytest.mark.parametrize("module", ["proctree", "runstore", "launch_env", "driver"])
def test_new_modules_do_not_import_the_cli_or_the_supervisor(module):
    import ast

    path = Path(d.__file__).with_name(module + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imported.add(base)
            imported.update(base + "." + alias.name if base and not base.endswith(".") else base + alias.name
                            for alias in node.names)
    for name in imported:
        leaf = name.lstrip(".")
        assert not (leaf in ("cli", "supervisor") or leaf.endswith((".cli", ".supervisor"))
                    or leaf.startswith(("quoin.cli", "quoin.supervisor"))), (module, name)
