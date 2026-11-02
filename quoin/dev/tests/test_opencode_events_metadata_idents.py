"""Native identifiers under the metadata content mode: no seeded text survives."""
from __future__ import annotations

from quoin.opencode_adapter import events as ev

RUN_ID = "oc-20260101T000000Z-0123abcd"
OBSERVED = "2026-01-01T00:00:00.000Z"
SEED = "sk-SEEDEDVALUE0123456789"


def redact(text):
    return text.replace(SEED, "[REDACTED]")


def tr(obj, content):
    return ev.translate(
        obj, run_id=RUN_ID, attempt=1, observed_at=OBSERVED, redact=redact, content=content
    )


def _dump(events):
    return repr([e.payload for e in events])


def test_native_error_name_and_retry_after_carry_no_seed_under_metadata():
    err = {
        "name": "Bad " + SEED + " name",
        "data": {
            "message": "m", "statusCode": 429,
            "responseHeaders": {"Retry-After": "soon " + SEED},
        },
    }
    meta = ev.map_native_error(err, redact=redact, content="metadata")
    assert SEED not in repr(meta) and "REDACTED" not in (meta.name or "")
    assert meta.name.startswith("h-") and meta.retry_after.startswith("h-")
    summary = ev.map_native_error(err, redact=redact, content="summary")
    assert SEED not in repr(summary)
    assert "[REDACTED]" in summary.name and "[REDACTED]" in summary.retry_after


def test_plain_native_error_name_stays_readable_under_metadata():
    payload = ev.map_native_error(
        {"name": "APIError", "data": {"statusCode": 503, "responseHeaders": {"retry-after": "7"}}},
        redact=redact, content="metadata",
    )
    assert payload.name == "APIError" and payload.retry_after == "7"


def test_finish_reason_and_unknown_tool_status_are_idents_under_metadata():
    finish = {
        "type": "step_finish", "timestamp": 1, "sessionID": "ses_a",
        "part": {"id": "prt_f", "messageID": "msg_1", "reason": "odd " + SEED,
                 "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                 "cost": 0},
    }
    tool = {
        "type": "tool_use", "timestamp": 1, "sessionID": "ses_a",
        "part": {"id": "prt_1", "messageID": "msg_1", "type": "tool", "tool": "bash",
                 "state": {"status": "weird " + SEED}},
    }
    for obj in (finish, tool):
        meta = tr(obj, "metadata")
        assert SEED not in _dump(meta)
        assert "h-" in _dump(meta)
        summary = tr(obj, "summary")
        assert SEED not in _dump(summary) and "[REDACTED]" in _dump(summary)
    assert "bash" in _dump(tr(tool, "metadata"))
