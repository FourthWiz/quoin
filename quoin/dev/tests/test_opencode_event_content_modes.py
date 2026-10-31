"""Content modes of the runtime event translator and their end-to-end effect."""
from __future__ import annotations

import hashlib
import json
import secrets
from decimal import Decimal

import pytest

from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter.events import EventType as ET

RUN_ID = "oc-20260101T000000Z-0123abcd"
OBSERVED = "2026-01-01T00:00:00.000Z"
REGISTERED = "sk-REGISTERED0123456789"


def redact(text):
    return text.replace(REGISTERED, "[REDACTED]")


def tr(obj, content="summary"):
    return ev.translate(
        obj, run_id=RUN_ID, attempt=1, observed_at=OBSERVED, redact=redact, content=content
    )


def dump(events):
    return json.dumps([e.to_dict() for e in events], sort_keys=True)


def _part(kind, text):
    return {"type": kind, "timestamp": 1, "sessionID": "ses_a",
            "part": {"id": "prt_1", "messageID": "msg_1", "text": text}}


def _tool(status, **state):
    return {"type": "tool_use", "timestamp": 1, "sessionID": "ses_a",
            "part": {"id": "prt_2", "messageID": "msg_1", "type": "tool", "tool": "read",
                     "state": dict(status=status, **state)}}


def _error(message):
    return {"type": "error", "timestamp": 1, "sessionID": "ses_a",
            "error": {"name": "UnknownError", "data": {"message": message}}}


def _seed():
    return "SEED-" + secrets.token_hex(8)


SITES = {
    "text": lambda s: _part("text", s),
    "reasoning": lambda s: _part("reasoning", s),
    "tool-title": lambda s: _tool("completed", title=s),
    "tool-error": lambda s: _tool("error", error=s),
    "native-error": lambda s: _error(s),
}


@pytest.mark.parametrize("site", sorted(SITES))
def test_content_site_summary_keeps_text_metadata_drops_it(site):
    seed = _seed()
    obj = SITES[site](seed)
    assert seed in dump(tr(obj, "summary"))
    out = dump(tr(obj, "metadata"))
    assert seed not in out
    assert "[metadata] length=%d sha256=" % len(seed.encode()) in out


@pytest.mark.parametrize("site", sorted(SITES))
def test_metadata_leaves_dedup_identity_unchanged(site):
    obj = SITES[site](_seed())
    summary, metadata = tr(obj, "summary")[0], tr(obj, "metadata")[0]
    assert summary.native.id == metadata.native.id
    assert summary.native.type == metadata.native.type
    key = ev.native_key(obj, attempt=1, redact=redact)
    assert key == ev.native_key(obj, attempt=1, redact=redact)
    assert ev.content_hash(obj) == ev.content_hash(obj)


def test_parse_metadata_summary_reads_redacted_length():
    text = "abc " + REGISTERED + " é"
    value = ev.metadata_summary(text, redact)
    clean = redact(text).encode("utf-8")
    assert ev.parse_metadata_summary(value) == (len(clean), hashlib.sha256(clean).hexdigest()[:12])
    assert ev.metadata_summary(None, redact) is None
    for bad in ("[metadata] length=1 sha256=ABC", "x [metadata] length=1 sha256=" + "0" * 12, 5, None):
        assert ev.parse_metadata_summary(bad) is None


def test_registered_secret_absent_in_both_modes_and_digest_is_of_redacted_text():
    obj = _part("text", "see " + REGISTERED)
    for mode in ("summary", "metadata"):
        assert REGISTERED not in dump(tr(obj, mode))
    value = tr(obj, "metadata")[0].payload.summary
    redacted = redact("see " + REGISTERED).encode()
    assert ev.parse_metadata_summary(value) == (len(redacted), hashlib.sha256(redacted).hexdigest()[:12])


REJECTED = "The user rejected permission to use this specific tool call."
DENIED = (
    "The user has specified a rule which prevents you from using this specific tool call. "
    "Here are some of the relevant rules " + json.dumps([{"permission": "bash", "pattern": "rm *", "action": "deny"}])
)


def test_permission_classification_unchanged_under_metadata():
    denied = tr(_tool("error", error=DENIED), "metadata")[0]
    assert denied.payload.permission_outcome == "denied"
    assert DENIED not in dump([denied])
    rejected = tr(_tool("error", error=REJECTED), "metadata")[0]
    assert rejected.type is ET.APPROVAL_REQUIRED


def test_error_event_keeps_kind_and_status_under_metadata():
    obj = {"type": "error", "timestamp": 1, "sessionID": "ses_a",
           "error": {"name": "APIError", "data": {"message": _seed(), "statusCode": 429,
                                                  "responseHeaders": {"retry-after": "7"}}}}
    payload = tr(obj, "metadata")[0].payload
    assert (payload.failure_kind, payload.http_status, payload.retry_after) == ("http", 429, "7")
    assert payload.name == "APIError"


def test_stderr_approval_metadata():
    seed = _seed()
    signal = ev.StderrSignal("approval", "bash", (seed, "second"), True)
    summary = ev.approval_from_stderr(signal, redact=redact)
    meta = ev.approval_from_stderr(signal, redact=redact, content="metadata")
    assert seed in summary.patterns
    assert seed not in json.dumps(meta.to_dict())
    assert [ev.parse_metadata_summary(p) is not None for p in meta.patterns] == [True, True]
    assert meta.permission == "bash"
    odd = ev.StderrSignal("approval", "sk-looks-secret " + REGISTERED, ("p",), True)
    shown = ev.approval_from_stderr(odd, redact=redact, content="metadata").permission
    assert REGISTERED not in shown and shown.startswith("h-")
    many = ev.StderrSignal("approval", "bash", tuple("p%d" % i for i in range(20)), True)
    assert ev.approval_from_stderr(many, redact=redact, content="metadata").patterns[-1] == "…[4 more]"


def test_unknown_mode_never_raises_in_translate_and_raises_in_pipeline():
    seed = _seed()
    assert seed not in dump(tr(_part("text", seed), "verbose"))
    with pytest.raises(ValueError):
        ev.EventPipeline(RUN_ID, 1, observed_clock=lambda: 1.0, content="verbose")


def test_pipeline_content_mode_reaches_translation():
    seed = _seed()
    raw = (json.dumps(_part("text", seed)) + "\n").encode()
    summary = ev.EventPipeline(RUN_ID, 1, redact=redact, observed_clock=lambda: 1.0)
    meta = ev.EventPipeline(RUN_ID, 1, redact=redact, observed_clock=lambda: 1.0, content="metadata")
    assert seed in dump(summary.feed_line(raw))
    assert seed not in dump(meta.feed_line(raw))
