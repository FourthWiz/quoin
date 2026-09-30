"""Schema, translation, classification and pipeline tests for runtime events."""
from __future__ import annotations

import json
import random
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter.events import EventType as ET

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPAT = REPO_ROOT / "quoin" / "adapters" / "opencode" / "compatibility.md"
RUN_ID = "oc-20260101T000000Z-0123abcd"
OBSERVED = "2026-01-01T00:00:00.000Z"
SECRET = "sk-TESTSECRET0123456789"

REJECTED = "The user rejected permission to use this specific tool call."
CORRECTED = REJECTED[:-1] + " with the following feedback: use another path"
DENIED_RULES = [{"permission": "bash", "pattern": "rm *", "action": "deny"}]
DENIED = (
    "The user has specified a rule which prevents you from using this specific tool call. "
    "Here are some of the relevant rules " + json.dumps(DENIED_RULES)
)
DENIED_ADVERSARIAL = (
    "The user has specified a rule which prevents you from using this specific tool call. "
    "Here are some of the relevant rules "
    + json.dumps([{"permission": "bash", "pattern": REJECTED, "action": "deny"}])
)


def redact(text):
    return text.replace(SECRET, "[REDACTED]")


def tr(obj, **kw):
    return ev.translate(
        obj, run_id=RUN_ID, attempt=1, observed_at=OBSERVED, redact=redact, **kw
    )


def tool_use(tool, status, **state):
    return {
        "type": "tool_use", "timestamp": 1000, "sessionID": "ses_a",
        "part": {"id": "prt_1", "messageID": "msg_1", "type": "tool", "tool": tool,
                 "state": dict(status=status, **state)},
    }


def error_event(message, where="data", name="UnknownError"):
    err = {"name": name}
    if where == "data":
        err["data"] = {"message": message}
    else:
        err["message"] = message
    return {"type": "error", "timestamp": 1, "sessionID": "ses_a", "error": err}


def step_finish(pid="prt_f1", reason="stop", cost=Decimal("0.5"), tokens=None):
    tokens = tokens or {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 1, "write": 2}}
    return {"type": "step_finish", "timestamp": 1, "sessionID": "ses_a",
            "part": {"id": pid, "messageID": "msg_1", "reason": reason,
                     "tokens": tokens, "cost": cost}}


def step_start(pid="prt_s1"):
    return {"type": "step_start", "timestamp": 1, "sessionID": "ses_a",
            "part": {"id": pid, "messageID": "msg_1"}}


def line(obj):
    class _Enc(json.JSONEncoder):
        def default(self, o):
            return float(o) if isinstance(o, Decimal) else super().default(o)
    return (json.dumps(obj, cls=_Enc) + "\n").encode()


def pipeline(**kw):
    return ev.EventPipeline(RUN_ID, 1, redact=redact, observed_clock=lambda: 1.0, **kw)


# ---------------------------------------------------------------------------
# schema core
# ---------------------------------------------------------------------------

def _all_payloads():
    return {
        ET.STARTED: ev.StartedPayload(1, 1, ("opencode", "run"), ("PATH",), "1.18.32", "abc", "writer", "m", 1, "fresh"),
        ET.PROGRESS: ev.ProgressPayload("tool", "tool_use", tool="bash", status="completed", summary="s"),
        ET.USAGE: ev.UsagePayload(1, 2, 3, 4, 5, "0.25", "stop"),
        ET.ARTIFACT_REFERENCE: ev.ArtifactReferencePayload("a/b.md", None, "x", "created"),
        ET.APPROVAL_REQUIRED: ev.ApprovalRequiredPayload("stderr_notice", "bash", ("rm *",), "bash", None),
        ET.ERROR: ev.ErrorPayload("APIError", "boom", "http", 503, "5"),
        ET.STOPPED: ev.StoppedPayload("completed", 0, None, None, "full", {"starts": 1}),
    }


def _event(etype, payload, origin="driver", native=None):
    return ev.RuntimeEvent(1, RUN_ID, 1, 3, "ses_a", None, OBSERVED, OBSERVED, etype, origin, native, payload)


@pytest.mark.parametrize("etype", list(ET))
def test_round_trip_every_payload(etype):
    payload = _all_payloads()[etype]
    native = ev.NativeRef("x", "prt", 2, "ab") if etype is ET.PROGRESS else None
    event = _event(etype, payload, "native" if native else "driver", native)
    text = event.to_json()
    assert "\n" not in text and '"schema_version":1' in text
    assert ev.RuntimeEvent.from_json(text) == event
    assert json.loads(text)["type"] == etype.value


def test_from_json_rejections():
    good = json.loads(_event(ET.PROGRESS, _all_payloads()[ET.PROGRESS]).to_json())
    for mutate, msg in (
        (lambda d: d.pop("schema_version"), "schema_version"),
        (lambda d: d.update(schema_version=2), "unsupported schema_version"),
        (lambda d: d.update(type="bogus"), "unknown type"),
        (lambda d: d.update(type="usage"), "payload"),
    ):
        d = json.loads(json.dumps(good))
        mutate(d)
        with pytest.raises(ValueError, match=msg):
            ev.RuntimeEvent.from_json(json.dumps(d))
    d = dict(good, future_key=1)
    assert ev.RuntimeEvent.from_json(json.dumps(d))


@pytest.mark.parametrize("build", [
    lambda: ev.StartedPayload(1, 1, (), (), "v", "d", "r", "m", 1, "bogus"),
    lambda: ev.ProgressPayload("bogus", "x"),
    lambda: ev.ProgressPayload("tool", "x", permission_outcome="approved"),
    lambda: ev.ProgressPayload("tool", "x", delegation="weird"),
    lambda: ev.UsagePayload(input_tokens=-1),
    lambda: ev.UsagePayload(cost="1e5"),
    lambda: ev.ArtifactReferencePayload("../x", None, None, "created"),
    lambda: ev.ArtifactReferencePayload("/abs", None, None, "created"),
    lambda: ev.ArtifactReferencePayload("a", None, None, "moved"),
    lambda: ev.ApprovalRequiredPayload("nowhere"),
    lambda: ev.ErrorPayload("n", "m", "policy"),
    lambda: ev.StoppedPayload("running-ish", 0, None, None, "full", {}),
    lambda: ev.StoppedPayload("completed", 0, None, None, "some", {}),
    lambda: _event(ET.USAGE, _all_payloads()[ET.PROGRESS]),
    lambda: _event(ET.PROGRESS, _all_payloads()[ET.PROGRESS], origin="native"),
    lambda: _event(ET.PROGRESS, _all_payloads()[ET.PROGRESS], origin="driver", native=ev.NativeRef("t", None)),
    lambda: ev.RuntimeEvent(1, "bad", 1, 0, None, None, OBSERVED, OBSERVED, ET.PROGRESS, "driver", None, _all_payloads()[ET.PROGRESS]),
    lambda: ev.RuntimeEvent(1, RUN_ID, 1, -1, None, None, OBSERVED, OBSERVED, ET.PROGRESS, "driver", None, _all_payloads()[ET.PROGRESS]),
    lambda: ev.RuntimeEvent(1, RUN_ID, 1, 0, None, None, "yesterday", OBSERVED, ET.PROGRESS, "driver", None, _all_payloads()[ET.PROGRESS]),
])
def test_validation_branches(build):
    with pytest.raises(ValueError):
        build()


def test_policy_never_translatable():
    assert "policy" not in ev.TRANSLATABLE_FAILURE_KINDS
    with pytest.raises(ValueError):
        ev.ErrorPayload("n", "m", failure_kind="policy")


def test_iso_helpers():
    assert ev.iso_from_epoch_ms(0) == "1970-01-01T00:00:00.000Z"
    assert ev.iso_from_epoch_ms(1_700_000_000_123).endswith(".123Z")
    assert ev.iso_now(lambda: 1.5) == "1970-01-01T00:00:01.500Z"


def test_import_has_no_cli_dependency():
    import subprocess
    code = "import sys; import quoin.opencode_adapter.events; sys.exit(1 if 'quoin.cli' in sys.modules else 0)"
    env = {"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": ""}
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0


# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------

def test_parse_line_codes():
    p = ev.parse_line
    assert p(b'{"type":"text"}\n').obj == {"type": "text"}
    assert p(b"   \n").diagnostic == "empty"
    assert p(b"").diagnostic == "empty"
    assert p(b"x" * 20, max_bytes=10).diagnostic == "oversized"
    assert p(b"\xff\xfe{").diagnostic == "non-utf8"
    assert p(b"not json").diagnostic == "non-json"
    assert p(b"[1,2]").diagnostic == "non-object"
    assert p(b'{"a":1}').diagnostic == "missing-type"
    assert p(b'{"type":5}').diagnostic == "missing-type"
    assert p(b"[" * 100000).diagnostic == "non-json"


def test_parse_line_keeps_decimal_cost():
    obj = ev.parse_line(b'{"type":"x","cost":0.000123456789012345678}').obj
    assert obj["cost"] == Decimal("0.000123456789012345678")


def test_parse_line_never_raises_on_fuzz():
    rng = random.Random(7)
    for _ in range(500):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60)))
        result = ev.parse_line(raw)
        assert (result.obj is None) != (result.diagnostic is None)
    assert ev.parse_line(None).diagnostic is not None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (REJECTED, "rejected"),
    (CORRECTED, "rejected"),
    ("Subagent failed (task_id: ses_c): " + REJECTED, "rejected"),
    ("Subagent failed (task_id: ses_c): Subagent failed (task_id: ses_d): " + REJECTED, "rejected"),
    (DENIED, "denied"),
    (DENIED_ADVERSARIAL, "denied"),
    ("Subagent failed (task_id: ses_c): " + DENIED, "denied"),
    ("Tool execution failed", None),
    ("note: " + REJECTED, None),
    ("this specific tool call", None),
    ("", None),
])
def test_classifier(text, expected):
    assert ev.classify_permission_error(text) == expected


def test_classifier_non_string():
    assert ev.classify_permission_error(None) is None  # type: ignore[arg-type]
    assert ev.classify_permission_error(5) is None  # type: ignore[arg-type]


def test_question_dismissed_is_not_classified():
    assert ev.QUESTION_DISMISSED_PREFIX is None
    assert ev.classify_permission_error("The user dismissed this question") is None


# ---------------------------------------------------------------------------
# translate
# ---------------------------------------------------------------------------

def types(events):
    return [e.type for e in events]


def test_tool_rejected_becomes_approval():
    for text in (REJECTED, CORRECTED):
        (e,) = tr(tool_use("bash", "error", error=text))
        assert e.type is ET.APPROVAL_REQUIRED
        assert e.payload.evidence_source == "tool_error" and e.payload.tool == "bash"


def test_task_rejection_nested():
    nested = "Subagent failed (task_id: ses_a1): Subagent failed (task_id: ses_b2): " + REJECTED
    (e,) = tr(tool_use("task", "error", error=nested))
    assert e.type is ET.APPROVAL_REQUIRED
    assert e.payload.evidence_source == "task_error"
    assert e.payload.task_id == "ses_a1"


def test_task_delegation_progress():
    (e,) = tr(tool_use("task", "completed", metadata={"background": True}))
    assert e.payload.delegation == "background"
    (e,) = tr(tool_use("task", "completed", metadata={}))
    assert e.payload.delegation == "completed"
    part = tool_use("task", "completed", metadata={})
    part["part"]["state"]["input"] = {"run_in_background": True}
    assert tr(part)[0].payload.delegation == "completed"
    (e,) = tr(tool_use("task", "error", error="Subagent failed (task_id: ses_c): Tool execution failed"))
    assert e.payload.delegation == "failed" and e.type is ET.PROGRESS
    for text in ("Task failed", "Task cancelled"):
        assert tr(tool_use("task", "error", error=text))[0].payload.delegation == "failed"
    (e,) = tr(tool_use("task", "error", error="Subagent failed (task_id: ses_c): " + DENIED))
    assert e.payload.delegation == "denied-tail" and e.payload.permission_outcome == "denied"


def test_error_events_with_permission_sentences():
    for where in ("data", "message"):
        (e,) = tr(error_event(REJECTED, where))
        assert e.type is ET.APPROVAL_REQUIRED and e.payload.evidence_source == "error_event"
        assert e.payload.permission is None
        (e,) = tr(error_event(DENIED, where))
        assert e.type is ET.PROGRESS
        assert e.payload.kind == "halted" and e.payload.permission_outcome == "denied"
    (e,) = tr(error_event("something else"))
    assert e.type is ET.ERROR and e.payload.failure_kind == "config"


def test_error_event_with_non_string_message_does_not_raise():
    events = tr({"type": "error", "error": {"name": "X", "data": {"message": 5}, "message": ["a"]}})
    assert types(events) == [ET.ERROR]
    assert tr({"type": "error", "error": "weird"})[0].type is ET.ERROR
    assert tr({"type": "tool_use", "part": "nope"})  # no exception


def test_translate_never_raises_on_garbage():
    for obj in ({}, {"type": 5}, {"type": "text", "part": {"text": 5}}, {"type": "step_finish"}):
        assert tr(obj)


def test_usage_keeps_exact_cost_and_none_stays_none():
    parsed = ev.parse_line(b'{"type":"step_finish","part":{"id":"p","tokens":{"input":3},"cost":0.000123456789012345678,"reason":"stop"}}').obj
    (e,) = tr(parsed)
    assert e.payload.cost == "0.000123456789012345678"
    assert e.payload.input_tokens == 3 and e.payload.output_tokens is None
    assert e.payload.finish_reason == "stop"
    assert tr(step_finish(cost="x"))[0].payload.cost is None


def test_text_progress_is_bounded_and_redacted():
    obj = {"type": "text", "sessionID": "s", "part": {"id": "p", "text": "a" * 100 + SECRET + "é" * 3000}}
    (e,) = tr(obj, summary_limit=120)
    dumped = e.to_json()
    assert SECRET not in dumped
    assert "…[truncated" in e.payload.summary
    assert len(e.payload.summary.encode()) < 200


def test_secret_straddling_truncation_boundary_never_leaks():
    for pad in range(100, 140):
        obj = {"type": "text", "sessionID": "s", "part": {"id": "p", "text": "a" * pad + SECRET}}
        (e,) = tr(obj, summary_limit=120)
        assert SECRET not in e.to_json()
        assert SECRET[:8] not in e.payload.summary.replace("[REDACTED]", "")


def test_secret_absent_in_tool_title_error_and_stderr():
    (e,) = tr(tool_use("bash", "completed", title="ran " + SECRET))
    assert SECRET not in e.to_json()
    (e,) = tr(tool_use("bash", "error", error="failed " + SECRET))
    assert SECRET not in e.to_json()
    (e,) = tr(error_event("bad " + SECRET))
    assert SECRET not in e.to_json()
    sig = ev.parse_stderr_line("! permission requested: bash (echo); auto-rejecting")
    assert SECRET not in json.dumps(sig.__dict__, default=list)


def test_tool_input_and_output_never_persisted():
    obj = tool_use("bash", "completed", title="t", input={"command": SECRET}, output=SECRET)
    assert SECRET not in tr(obj)[0].to_json()


def test_unknown_type_is_progress_unknown():
    (e,) = tr({"type": "session_compacted", "sessionID": "s"})
    assert e.type is ET.PROGRESS and e.payload.kind == "unknown" and e.payload.raw_type == "session_compacted"


def test_ids_from_envelope():
    (e,) = tr(tool_use("bash", "completed", title="x"))
    assert e.session_id == "ses_a" and e.parent_id == "msg_1" and e.native.id == "prt_1"
    assert e.sequence == 0 and e.timestamp == "1970-01-01T00:00:01.000Z"


# ---------------------------------------------------------------------------
# native error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err,kind,status", [
    ({"name": "APIError", "data": {"message": "m", "statusCode": 429, "isRetryable": True}}, "http", 429),
    ({"name": "APIError", "data": {"message": "m", "statusCode": 503, "isRetryable": True}}, "http", 503),
    ({"name": "APIError", "data": {"message": "m", "statusCode": 401, "isRetryable": False}}, "auth", None),
    ({"name": "ProviderAuthError", "data": {"providerID": "fixture-provider", "message": "m"}}, "auth", None),
    ({"name": "APIError", "data": {"message": "m", "isRetryable": True, "metadata": {"code": "ECONNRESET"}}}, "connect", None),
    ({"name": "APIError", "data": {"message": "m", "isRetryable": True, "metadata": {"code": "HeaderTimeoutError"}}}, "timeout", None),
    ({"name": "APIError", "data": {"message": "m", "statusCode": 400, "isRetryable": False}}, "config", None),
    ({"name": "UnknownError", "data": {"message": "m"}}, "config", None),
    ({}, "config", None),
])
def test_map_native_error(err, kind, status):
    payload = ev.map_native_error(err)
    assert payload.failure_kind == kind and payload.http_status == status


def test_retry_after_from_headers():
    err = {"name": "APIError", "data": {"message": "m", "statusCode": 429, "isRetryable": True,
                                         "responseHeaders": {"Retry-After": "7"}}}
    assert ev.map_native_error(err).retry_after == "7"


# ---------------------------------------------------------------------------
# stderr
# ---------------------------------------------------------------------------

ANSI_NOTICE = "\x1b[93m\x1b[1m! \x1b[0mpermission requested: bash (rm -rf x, ls (a)); auto-rejecting"


def test_stderr_approval_with_ansi():
    sig = ev.parse_stderr_line(ANSI_NOTICE)
    assert sig.kind == "approval" and sig.name == "bash" and sig.patterns_complete
    assert sig.patterns == ("rm -rf x", "ls (a)")


def test_stderr_multiline_pattern_yields_one_signal():
    lines = ["! permission requested: bash (echo one", "two); auto-rejecting"]
    signals = [ev.parse_stderr_line(l) for l in lines]
    real = [s for s in signals if s]
    assert len(real) == 1 and not real[0].patterns_complete and real[0].name == "bash"


def test_stderr_agent_fallback_forms():
    for text in ('! agent "x" not found. Falling back to default agent',
                 '\x1b[93m\x1b[1m! \x1b[0m agent "y" is a subagent, not a primary agent. Falling back to default agent'):
        sig = ev.parse_stderr_line(text)
        assert sig.kind == "agent_fallback"
    assert ev.parse_stderr_line('! agent "x" not found. Falling back to default agent').name == "x"


def test_stderr_unrelated_and_denied_text():
    assert ev.parse_stderr_line("compiling...") is None
    assert ev.parse_stderr_line(DENIED) is None
    assert ev.parse_stderr_line("\x1b[91m" + DENIED + "\x1b[0m") is None
    assert ev.parse_stderr_line(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pipeline pieces
# ---------------------------------------------------------------------------

def stream():
    return [line(step_start()), line(step_finish("prt_f1", "tool-calls")),
            line(step_start("prt_s2")), line(step_finish("prt_f2", "stop"))]


def run_all(lines, **kw):
    pipe = pipeline(**kw)
    out = []
    for raw in lines:
        out.extend(pipe.feed_line(raw))
    return pipe, out


def test_duplicate_and_replayed_streams_yield_each_event_once():
    base = stream()
    _, once = run_all(base)
    _, dup = run_all([x for l in base for x in (l, l)])
    pipe, replay = run_all(base + base)
    assert len(once) == len(dup) == len(replay) == 4
    assert pipe.counters["dropped_duplicates"] == 4
    assert [e.sequence for e in replay] == [1, 2, 3, 4]


def test_changed_content_is_a_revision_and_counted_once_in_totals():
    first = step_finish("prt_f1", "stop", tokens={"input": 10, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}})
    second = step_finish("prt_f1", "stop", tokens={"input": 20, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}})
    pipe, out = run_all([line(first), line(second)])
    assert [e.native.revision for e in out] == [1, 2]
    assert pipe.counters["revisions"] == 1
    totals = ev.usage_totals(out)
    assert totals.input_tokens == 20 and totals.output_tokens == 2


def test_usage_totals_unknown_makes_field_none():
    a = tr(step_finish("prt_a"))
    b = tr({"type": "step_finish", "part": {"id": "prt_b", "tokens": {"input": 1}, "cost": Decimal("1")}})
    out = []
    for i, e in enumerate(a + b):
        out.append(ev.replace(e, sequence=i + 1))
    totals = ev.usage_totals(out)
    assert totals.input_tokens == 11 and totals.output_tokens is None
    assert totals.cost == "1.5"
    assert ev.usage_totals([]) == ev.UsagePayload()


def test_rebuild_from_first_half_emits_only_second_half():
    base = stream()
    _, first = run_all(base[:2])
    deduper = ev.Deduper.from_events(first)
    pipe, second = run_all(base, deduper=deduper, start_sequence=first[-1].sequence + 1)
    assert len(second) == 2
    assert [e.sequence for e in second] == [3, 4]


def test_driver_events_dedup_per_attempt_and_path():
    pipe = pipeline()
    a = pipe.driver_event(ET.ARTIFACT_REFERENCE, ev.ArtifactReferencePayload("a.md", None, "1", "created"))
    b = pipe.driver_event(ET.ARTIFACT_REFERENCE, ev.ArtifactReferencePayload("b.md", None, "2", "created"))
    assert a.revision == 1 and b.revision == 1
    assert pipe.driver_event(ET.ARTIFACT_REFERENCE, ev.ArtifactReferencePayload("a.md", None, "1", "created")) is None
    c = pipe.driver_event(ET.ARTIFACT_REFERENCE, ev.ArtifactReferencePayload("a.md", None, "9", "modified"))
    assert c.revision == 2
    started = ev.StartedPayload(1, 1, ("o",), (), "v", "d", "r", "m", 1, "fresh")
    assert pipe.driver_event(ET.STARTED, started).origin == "driver"
    assert pipe.driver_event(ET.STARTED, started) is None
    assert [e.sequence for e in (a, b, c)] == [1, 2, 3]


def test_diagnostics_are_counted():
    pipe, out = run_all([b"", b"nope", b"[1]", b'{"a":1}', b"\xff", b"z" * 50])
    pipe2 = pipeline(max_line_bytes=10)
    pipe2.feed_line(b"z" * 50)
    assert out == []
    c = pipe.counters
    assert (c["empty"], c["non-json"], c["non-object"], c["missing-type"], c["non-utf8"]) == (1, 2, 1, 1, 1)
    assert pipe2.counters["oversized"] == 1


def test_summarize_steps():
    _, out = run_all(stream())
    s = ev.summarize_steps(out)
    assert (s.starts, s.finishes, s.open_step, s.last_finish_reason, s.last_finish_terminal) == (2, 2, False, "stop", True)
    _, out = run_all(stream()[:3])
    s = ev.summarize_steps(out)
    assert s.open_step and s.last_finish_terminal is False
    assert ev.summarize_steps([]).last_finish_terminal is None


def _verified_keys():
    text = COMPAT.read_text(encoding="utf-8")
    body = text.split("## Headless run events and process lifecycle", 1)[1]
    keys = {}
    for row in body.splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split(" | ")]
        if len(cells) == 4:
            m = re.match(r"`key: ([a-z0-9-]+)`", cells[3])
            if m:
                keys[m.group(1)] = cells[1]
    return keys


def test_cited_capabilities_are_verified_rows():
    keys = _verified_keys()
    for key in ev.CITED_CAPABILITIES:
        assert keys.get(key) == "verified", key


# ---------------------------------------------------------------------------
# permission sentences are never a failure and a denial is never an approval
# ---------------------------------------------------------------------------

FIXTURES_DIR = REPO_ROOT / "quoin" / "adapters" / "opencode" / "fixtures" / "runtime-events"

_MATRIX_TEXTS = {
    "rejected": REJECTED,
    "corrected": CORRECTED,
    "denied": DENIED,
    "denied-with-rejected-in-rules": DENIED_ADVERSARIAL,
    "nested-task-denied": "Subagent failed (task_id: ses_a): Subagent failed (task_id: ses_b): " + DENIED,
    "nested-task-rejected": "Subagent failed (task_id: ses_a): Subagent failed (task_id: ses_b): " + REJECTED,
    "generic": "Tool execution failed",
}


def _expected_outcome(text):
    """Independent restatement of the rule: strip prefixes, compare the start."""
    rest = text
    while rest.startswith("Subagent failed (task_id: "):
        rest = rest.split("): ", 1)[1]
    if rest.startswith("The user rejected permission to use this specific tool call"):
        return "rejected"
    if rest.startswith("The user has specified a rule which prevents you from using this specific tool call"):
        return "denied"
    return None


def _fixture_events(name):
    pipe = pipeline()
    out = []
    for raw in (FIXTURES_DIR / name).read_bytes().split(b"\n"):
        out.extend(pipe.feed_line(raw))
    return out


class TestDenyIsNeverFailureOrApproval:
    def test_denied_tool_then_finish(self):
        out = _fixture_events("tool-denied-then-finish.jsonl")
        denied = [e for e in out if e.type is ET.PROGRESS and e.payload.permission_outcome == "denied"]
        assert len(denied) == 1
        assert not [e for e in out if e.type in (ET.APPROVAL_REQUIRED, ET.ERROR)]

    def test_adversarial_ruleset_stays_denied(self):
        out = _fixture_events("tool-denied-adversarial.jsonl")
        assert [e.payload.permission_outcome for e in out if e.type is ET.PROGRESS
                and e.payload.permission_outcome] == ["denied"]
        assert not [e for e in out if e.type in (ET.APPROVAL_REQUIRED, ET.ERROR)]

    def test_denied_tail_of_a_task(self):
        out = _fixture_events("task-errored-denied-tail.jsonl")
        assert [e.payload.delegation for e in out if e.type is ET.PROGRESS and e.payload.delegation] == ["denied-tail"]
        assert not [e for e in out if e.type in (ET.APPROVAL_REQUIRED, ET.ERROR)]

    @pytest.mark.parametrize("tool", ["bash", "edit", "question", "plan_enter", "plan_exit", "task"])
    @pytest.mark.parametrize("label", list(_MATRIX_TEXTS))
    def test_tool_use_matrix(self, tool, label):
        text = _MATRIX_TEXTS[label]
        for status in ("error", "completed"):
            state = {"error": text} if status == "error" else {"title": "ok", "metadata": {}}
            out = tr(tool_use(tool, status, **state))
            assert set(types(out)) <= {ET.PROGRESS, ET.APPROVAL_REQUIRED}
            wants_approval = status == "error" and _expected_outcome(text) == "rejected"
            assert (ET.APPROVAL_REQUIRED in types(out)) == wants_approval

    @pytest.mark.parametrize("name", ["UnknownError", "APIError"])
    @pytest.mark.parametrize("where", ["data", "message"])
    @pytest.mark.parametrize("label", list(_MATRIX_TEXTS))
    def test_error_event_matrix(self, name, where, label):
        text = _MATRIX_TEXTS[label]
        out = tr(error_event(text, where, name))
        assert len(out) == 1
        outcome = _expected_outcome(text)
        if outcome == "denied":
            assert out[0].type is ET.PROGRESS
            assert out[0].payload.kind == "halted" and out[0].payload.permission_outcome == "denied"
        elif outcome == "rejected":
            assert out[0].type is ET.APPROVAL_REQUIRED
            assert out[0].payload.evidence_source == "error_event"
        else:
            assert out[0].type is ET.ERROR

    def test_doom_loop_fixtures(self):
        rejected = _fixture_events("doom-loop-rejected.jsonl")
        assert [e.payload.evidence_source for e in rejected if e.type is ET.APPROVAL_REQUIRED] == ["error_event"]
        assert not [e for e in rejected if e.type is ET.ERROR]
        denied = _fixture_events("doom-loop-denied.jsonl")
        halted = [e for e in denied if e.type is ET.PROGRESS and e.payload.kind == "halted"]
        assert len(halted) == 1 and halted[0].payload.permission_outcome == "denied"
        assert not [e for e in denied if e.type in (ET.ERROR, ET.APPROVAL_REQUIRED)]

    def test_policy_is_not_a_failure_kind(self):
        assert "policy" not in ev.TRANSLATABLE_FAILURE_KINDS
        with pytest.raises(ValueError):
            ev.ErrorPayload("n", "m", failure_kind="policy")

    def test_stderr_denial_sentence_is_no_signal(self):
        for text in (DENIED, "\x1b[93m! \x1b[0m" + DENIED, DENIED_ADVERSARIAL):
            assert ev.parse_stderr_line(text) is None

    def test_native_error_mapping_never_policy(self):
        errors = [json.loads(l)["error"] for f in sorted(FIXTURES_DIR.glob("native-error-*.jsonl"))
                  for l in f.read_text().splitlines() if '"type": "error"' in l]
        assert len(errors) == 5
        errors += [{}, {"name": 5}, {"data": 5}, {"name": "X", "data": {"statusCode": "429"}},
                   {"name": "APIError", "data": {"statusCode": 418, "isRetryable": True}}]
        for err in errors:
            assert ev.map_native_error(err).failure_kind != "policy"

    def test_no_permission_sentence_produces_an_error_payload(self):
        for text in _MATRIX_TEXTS.values():
            if _expected_outcome(text) is None:
                continue
            candidates = [error_event(text, where, name) for where in ("data", "message")
                          for name in ("UnknownError", "APIError")]
            candidates += [tool_use(tool, "error", error=text) for tool in ("bash", "task", "question")]
            for obj in candidates:
                assert ET.ERROR not in types(tr(obj))


# ---------------------------------------------------------------------------
# hostile and unusual input reaching the pipeline
# ---------------------------------------------------------------------------

SURROGATE_LINES = [
    b'{"type":"text","sessionID":"ses_a","part":{"id":"prt_t","text":"hi \\ud800 there"}}',
    b'{"type":"error","sessionID":"ses_a","error":{"name":"UnknownError","data":{"message":"boom \\udfff"}}}',
    b'{"type":"text","sessionID":"ses_\\ud800","part":{"id":"prt_u","text":"x"}}',
    b'{"type":"tool_use","sessionID":"ses_a","part":{"id":"prt_\\ud800","tool":"b\\ud800sh",'
    b'"state":{"status":"error","error":"fail \\ud800"}}}',
    b'{"type":"t\\ud800","sessionID":"ses_a"}',
    b'{"\\ud800":1,"type":"step_start","sessionID":"ses_a"}',
]


def _deep_line(depth):
    return b'{"type":"text","a":' + b"[" * depth + b"]" * depth + b"}"


def _assert_encodable(events):
    for e in events:
        e.to_json().encode("utf-8")
        assert ev.RuntimeEvent.from_json(e.to_json()) == e


def test_feed_line_survives_surrogates_and_deep_nesting():
    pipe = pipeline()
    for raw in SURROGATE_LINES:
        out = pipe.feed_line(raw)
        assert out, raw
        _assert_encodable(out)
    for depth in (ev.MAX_JSON_DEPTH + 5, 5000, 100_000):
        assert pipe.feed_line(_deep_line(depth)) == []
    assert pipe.counters["too-deep"] + pipe.counters["non-json"] == 3
    assert pipe.feed_line(_deep_line(ev.MAX_JSON_DEPTH - 3))


def test_surrogate_error_is_still_an_error_and_denial_still_denied():
    (e,) = pipeline().feed_line(SURROGATE_LINES[1])
    assert e.type is ET.ERROR and "�" in e.payload.message
    rules = json.dumps([{"permission": "bash", "pattern": "x\ud800", "action": "deny"}])
    text = DENIED.split("Here are")[0] + "Here are some of the relevant rules " + rules
    raw = json.dumps(tool_use("bash", "error", error=text)).encode("utf-8", "surrogatepass")
    (e,) = pipeline().feed_line(raw)
    assert e.type is ET.PROGRESS and e.payload.permission_outcome == "denied"


def test_parse_line_depth_bound():
    assert ev.parse_line(_deep_line(ev.MAX_JSON_DEPTH + 1)).diagnostic in ("too-deep", "non-json")
    assert ev.parse_line(_deep_line(3), max_depth=4).obj is not None
    assert ev.parse_line(_deep_line(3), max_depth=3).diagnostic == "too-deep"


def test_feed_line_never_raises_on_fuzz():
    rng = random.Random(11)
    pieces = [b'{"type":"text"', b',"part":{"id":"p","text":"', b"\\ud800", b"\\udc00", b'"}',
              b"}", b"[", b"]", b'"x"', b",", b":", b"1e999999", b"\xff", b"\\u0000", b'{"a":']
    pipe = pipeline()
    for _ in range(800):
        raw = b"".join(rng.choice(pieces) for _ in range(rng.randrange(0, 12)))
        _assert_encodable(pipe.feed_line(raw))
    for raw in SURROGATE_LINES:
        mutated = raw.replace(b'"prt_', b'"q')
        _assert_encodable(pipe.feed_line(mutated))


def test_hashing_failure_is_counted_not_raised(monkeypatch):
    pipe = pipeline()

    def boom(obj):
        raise RecursionError("too deep")

    monkeypatch.setattr(ev, "content_hash", boom)
    assert pipe.feed_line(line(step_start())) == []
    assert pipe.counters["unhashable"] == 1


def test_identical_error_on_a_later_attempt_is_not_dropped():
    err = {"type": "error", "timestamp": 1, "sessionID": "ses_a",
           "error": {"name": "APIError", "data": {"message": "Rate limit", "statusCode": 429}}}
    first = pipeline()
    out1 = first.feed_line(line(err))
    assert types(out1) == [ET.ERROR]
    later = dict(err, timestamp=99)
    second = ev.EventPipeline(RUN_ID, 2, redact=redact, observed_clock=lambda: 2.0,
                              deduper=ev.Deduper.from_events(out1), start_sequence=2)
    out2 = second.feed_line(line(later))
    assert types(out2) == [ET.ERROR] and out2[0].attempt == 2 and out2[0].sequence == 2
    assert second.counters["dropped_duplicates"] == 0
    same = pipeline(deduper=ev.Deduper.from_events(out1), start_sequence=2)
    assert same.feed_line(line(later)) == []
    again = ev.EventPipeline(RUN_ID, 2, redact=redact, observed_clock=lambda: 2.0,
                             deduper=ev.Deduper.from_events(out1 + out2), start_sequence=3)
    assert again.feed_line(line(err)) == []


@pytest.mark.parametrize("literal", [
    b"1e100000000", b"1e999999999", b"1e-100000000", b"0E-100000", b"1e31",
    b"0." + b"1" * 100,
])
def test_extreme_cost_becomes_unknown(literal):
    raw = b'{"type":"step_finish","part":{"id":"p","reason":"stop","cost":' + literal + b"}}"
    (e,) = pipeline().feed_line(raw)
    assert e.payload.cost is None
    assert len(e.to_json()) < 1000


def test_ordinary_costs_are_kept():
    for value, text in ((Decimal("0.000123"), "0.000123"), (Decimal("12345"), "12345"), (0, "0")):
        assert tr(step_finish(cost=value))[0].payload.cost == text


def test_id_tool_and_task_fields_are_bounded_and_redacted():
    huge_tool = "t" * 200_000
    huge_task = "Subagent failed (task_id: " + "k" * 300_000 + "): " + REJECTED
    objs = [
        {"type": "tool_use", "sessionID": "ses_" + SECRET, "part": {
            "id": "prt_" + SECRET, "messageID": "m" * 5000, "tool": huge_tool + SECRET,
            "state": {"status": "s" * 50_000 + SECRET}}},
        {"type": "x" * 200_000, "sessionID": "ses_a", "part": {"id": "p" * 200}},
        tool_use("task", "error", error=huge_task),
        tool_use("task", "error", error="Subagent failed (task_id: ses_" + SECRET + "): boom"),
    ]
    for obj in objs:
        (e,) = tr(obj)
        dumped = e.to_json()
        assert SECRET not in dumped
        assert len(dumped.encode()) < 4000, len(dumped)
    (e,) = tr(objs[2])
    assert e.type is ET.APPROVAL_REQUIRED and e.payload.task_id is None
    (e,) = tr(objs[0])
    assert e.native.id.startswith("h-") and e.session_id.startswith("h-")
    assert e.payload.tool.endswith("bytes]")


def test_shaped_ids_survive_and_rebuild_matches_live_keys():
    pipe = pipeline()
    secret_part = {"type": "text", "sessionID": "ses_a", "part": {"id": "prt_" + SECRET, "text": "a"}}
    out = pipe.feed_line(line(secret_part)) + pipe.feed_line(line(step_start()))
    assert out[1].native.id == "prt_s1"
    rebuilt = pipeline(deduper=ev.Deduper.from_events(out), start_sequence=3)
    assert rebuilt.feed_line(line(secret_part)) == []
    assert rebuilt.feed_line(line(step_start())) == []


def test_translate_fallback_keeps_part_id(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(ev, "_translate", broken)
    pipe = pipeline()
    (e,) = pipe.feed_line(line(step_start("prt_z")))
    assert e.payload.raw_type == "unparseable" and e.native.id == "prt_z"
    rebuilt = pipeline(deduper=ev.Deduper.from_events([e]), start_sequence=2)
    assert rebuilt.feed_line(line(step_start("prt_z"))) == []


def test_pipeline_rejects_bad_arguments_at_construction():
    for kwargs in ({"run_id": "bad"}, {"attempt": 0}, {"attempt": "1"}):
        args = dict(run_id=RUN_ID, attempt=1)
        args.update(kwargs)
        with pytest.raises(ValueError):
            ev.EventPipeline(args["run_id"], args["attempt"], observed_clock=lambda: 1.0)
    with pytest.raises(ValueError):
        ev.EventPipeline(RUN_ID, 1, observed_clock=lambda: 1.0, start_sequence=-1)


def test_provider_error_text_does_not_steer_classification():
    err = {"type": "error", "sessionID": "ses_a", "error": {
        "name": "APIError", "data": {"message": DENIED, "statusCode": 503, "isRetryable": True}}}
    (e,) = tr(err)
    assert e.type is ET.ERROR and e.payload.failure_kind == "http"
    both = {"type": "error", "sessionID": "ses_a", "error": {
        "name": "UnknownError", "message": REJECTED, "data": {"message": "provider exploded"}}}
    assert types(tr(both)) == [ET.ERROR]


@pytest.mark.parametrize("prefix", [
    "\x1b[2K\r", "\x1b]0;title\x07", "\x1b]8;;http://x\x1b\\", "\x1b(B", "\x1b[1A\x1b[2K",
])
def test_stderr_notice_behind_other_control_sequences(prefix):
    sig = ev.parse_stderr_line(prefix + ANSI_NOTICE)
    assert sig is not None and sig.kind == "approval" and sig.patterns_complete


def test_summarize_steps_uses_latest_step_not_latest_revision():
    a1 = step_finish("prt_a", "stop")
    b = step_finish("prt_b", "tool-calls")
    a2 = step_finish("prt_a", "stop", tokens={"input": 99})
    _, out = run_all([line(step_start("prt_s1")), line(a1), line(step_start("prt_s2")),
                      line(b), line(a2)])
    s = ev.summarize_steps(out)
    assert s.last_finish_reason == "tool-calls" and s.last_finish_terminal is False
