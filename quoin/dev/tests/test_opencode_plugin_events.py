"""The Quoin lifecycle plugin's log and its translation into runtime events.

`fixtures/plugin-events/real-plugin.jsonl` was captured from the built plugin
running inside the pinned OpenCode binary (see
test_opencode_plugin_binary_tier.py, which refreshes it).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter import plugin_events as pe

ADAPTER = Path(__file__).resolve().parents[2] / "adapters" / "opencode"
# The TypeScript build project lives under dev/ so its npm tree never ships;
# only the built file lands in the adapter's plugin directory.
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "opencode-plugin"
FIXTURE = ADAPTER / "fixtures" / "plugin-events" / "real-plugin.jsonl"
RUN_ID = "oc-20261102T120000Z-0a1b2c3d"


def _fixture_records():
    read = pe.read_log(FIXTURE)
    assert read.counters == {}
    return read.records


def test_real_plugin_fixture_translates_into_valid_events():
    records = _fixture_records()
    events = pe.translate(records, run_id=RUN_ID, clock=lambda: 1793621000.0)
    assert [e.sequence for e in events] == list(range(len(records)))
    for event in events:
        assert event.origin == "native" and event.revision == 1
        assert ev.RuntimeEvent.from_json(event.to_json()) == event
    summary = [(e.type.value, getattr(e.payload, "status", None)) for e in events]
    assert summary[:11] == [
        ("progress", "created"),
        ("progress", "asked"),
        ("progress", "once"),
        ("progress", "completed"),
        ("usage", None),
        ("usage", None),
        ("progress", "idle"),
        ("progress", "compacting"),
        ("usage", None),
        ("progress", "compacted"),
        ("progress", "idle"),
    ]
    usage = [e.payload for e in events if e.type is ev.EventType.USAGE]
    assert [(u.input_tokens, u.output_tokens, u.finish_reason) for u in usage] == [
        (10, 5, "tool-calls"), (10, 5, "stop"), (10, 5, "stop"),
    ]
    # An approval prompt is progress, correlated with its reply, and never the
    # terminal approval_required event the headless driver halts on.
    assert not any(e.type is ev.EventType.APPROVAL_REQUIRED for e in events)
    asked, replied = events[1].payload, events[2].payload
    assert (asked.kind, asked.tool, asked.summary) == ("approval", "edit", "<project>/hello.txt")
    assert asked.request_id.startswith("per_") and asked.request_id == replied.request_id
    error = next(e for e in events if e.type is ev.EventType.ERROR).payload
    assert (error.name, error.message, error.failure_kind) == ("APIError", "bad request", "config")
    # Record timestamps are kept; only observed_at comes from the clock.
    assert events[0].timestamp == records[0]["ts"]


def test_a_reread_or_redelivered_log_adds_nothing():
    records = _fixture_records()
    deduper = ev.Deduper()
    first = pe.translate(records, run_id=RUN_ID, deduper=deduper)
    assert pe.translate(records + records, run_id=RUN_ID, deduper=deduper) == []
    assert deduper.dropped_duplicates == 2 * len(records)
    # A deduper rebuilt from persisted events recognises the same records.
    assert pe.translate(records, run_id=RUN_ID, deduper=ev.Deduper.from_events(first)) == []


def test_a_finished_step_becomes_usage_and_a_reemitted_step_a_revision():
    step = {
        "v": 1, "plugin_version": "0.2.0", "run_id": RUN_ID, "instance": "0f7e423f", "seq": 0,
        "ts": "2026-11-05T12:00:00.000Z", "source": "event", "native_type": "message.part.updated",
        "native_id": "prt_step1", "session_id": "ses_a", "parent_id": None,
        "data": {"message_id": "msg_a", "reason": "stop", "cost": 0.0125,
                 "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 3, "write": 0}}},
    }
    redone = json.loads(json.dumps(step))
    redone["seq"], redone["data"]["tokens"]["output"] = 1, 7
    deduper = ev.Deduper()
    first, second = pe.translate([step, redone], run_id=RUN_ID, deduper=deduper)
    assert first.type is ev.EventType.USAGE and first.session_id == "ses_a"
    assert first.payload == ev.UsagePayload(
        input_tokens=10, output_tokens=5, reasoning_tokens=0, cache_read_tokens=3,
        cache_write_tokens=0, cost="0.0125", finish_reason="stop",
    )
    assert (second.native.id, second.revision, second.payload.output_tokens) == ("prt_step1", 2, 7)


def test_a_changed_record_under_the_same_native_id_is_a_revision():
    records = _fixture_records()
    asked = next(r for r in records if r["native_type"] == "permission.asked")
    changed = json.loads(json.dumps(asked))
    changed["data"]["patterns"] = ["<project>/other.txt"]
    deduper = ev.Deduper()
    pe.translate([asked], run_id=RUN_ID, deduper=deduper)
    (event,) = pe.translate([changed], run_id=RUN_ID, deduper=deduper, start_sequence=1)
    assert event.revision == 2 and event.native.revision == 2 and event.sequence == 1


def test_a_restarted_plugin_reusing_a_sequence_number_still_records_new_events():
    records = _fixture_records()
    compacting = next(r for r in records if r["native_id"] is None)
    deduper = ev.Deduper()
    pe.translate(records, run_id=RUN_ID, deduper=deduper)
    # A new plugin instance restarts its counter, so an id-less record can
    # reuse the exact sequence number already seen; the instance id keeps it
    # distinct instead of dropping it as a duplicate.
    restarted = dict(compacting, instance="ffffffff")
    (event,) = pe.translate([restarted], run_id=RUN_ID, deduper=deduper)
    assert event.revision == 1 and event.native.id == "seq:ffffffff:%d" % compacting["seq"]


def test_the_redactor_reaches_every_persisted_string():
    records = _fixture_records()
    secret = "hello.txt"
    events = pe.translate(records, run_id=RUN_ID, redact=lambda s: s.replace(secret, "[redacted]"))
    text = "".join(e.to_json() for e in events)
    assert secret not in text and "[redacted]" in text
    error = [r for r in records if r["native_type"] == "session.error"]
    (err,) = pe.translate(error, run_id=RUN_ID, redact=lambda s: s.replace("bad request", "[r]"))
    assert err.payload.message == "[r]"


def test_records_for_another_run_are_skipped():
    records = _fixture_records()
    other = [dict(r, run_id="oc-20261102T120000Z-ffffffff") for r in records]
    assert pe.translate(other, run_id=RUN_ID) == []


def test_bad_lines_are_counted_not_raised():
    good = FIXTURE.read_bytes().splitlines()[0]
    lines = [
        b"not json",
        b"[1, 2]",
        json.dumps(dict(json.loads(good), v=2)).encode(),
        json.dumps(dict(json.loads(good), native_type="message.updated")).encode(),
        json.dumps(dict(json.loads(good), run_id="nope")).encode(),
        json.dumps(dict(json.loads(good), seq=-1)).encode(),
        b"x" * (pe.MAX_LINE_BYTES + 1),
        b"",
        good,
    ]
    read = pe.parse_lines(lines)
    assert len(read.records) == 1
    assert read.counters == {
        "non-json": 1, "non-object": 1, "wrong-version": 1, "unknown-type": 1,
        "bad-run-id": 1, "bad-seq": 1, "oversized": 1,
    }


def test_a_missing_log_reads_empty(tmp_path):
    assert pe.read_log(tmp_path / "absent.jsonl").records == []


# ---------------------------------------------------------------------------
# the plugin source and its built artifact
# ---------------------------------------------------------------------------

SOURCE = (PLUGIN_DIR / "src" / "quoin-opencode-plugin.ts").read_text(encoding="utf-8")
PACKAGE = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
ARTIFACT = (PLUGIN_DIR / PACKAGE["quoin"]["artifact"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("text", [SOURCE, ARTIFACT], ids=["source", "artifact"])
def test_plugin_only_observes(text):
    # Exactly three hooks, none of which can answer a permission request,
    # change model parameters or headers, or register providers or tools.
    # Each takes a single parameter, so no hook ever receives the output
    # object OpenCode lets plugins mutate.
    hooks = re.findall(r'^\s+"?([a-z.]+)"?: async \((\w+)\) =>', text, re.MULTILINE)
    assert {name for name, _ in hooks} == {"event", "tool.execute.after", "experimental.session.compacting"}
    assert len(re.findall(r'^\s+"?[a-z.]+"?: async', text, re.MULTILINE)) == 3


def test_artifact_is_pinned_and_matches_the_package():
    assert PACKAGE["quoin"]["opencode"] == "1.18.32"
    assert PACKAGE["devDependencies"]["@opencode-ai/plugin"] == "1.18.32"
    assert 'const PLUGIN_VERSION = "%s";' % PACKAGE["version"] in SOURCE
    assert 'const PLUGIN_VERSION = "%s";' % PACKAGE["version"] in ARTIFACT
    assert (PLUGIN_DIR / "package-lock.json").is_file()
    assert (PLUGIN_DIR / PACKAGE["quoin"]["artifact"]).resolve() == (ADAPTER / "plugin" / "quoin-opencode-plugin.js")
    assert sorted(p.name for p in (ADAPTER / "plugin").iterdir()) == ["quoin-opencode-plugin.js"]


def _recorded_events(text):
    block = text.split("RECORDED_EVENTS", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"((?:session|permission|message)\.[a-z.]+)"', block))


def test_recorded_types_match_what_python_translates():
    hooks = {"tool.execute.after", "experimental.session.compacting"}
    assert _recorded_events(SOURCE) == _recorded_events(ARTIFACT)
    assert _recorded_events(ARTIFACT) | hooks == set(pe.PLUGIN_NATIVE_TYPES)


def test_artifact_is_the_build_of_the_source(tmp_path):
    tsc = PLUGIN_DIR / "node_modules" / ".bin" / "tsc"
    if not tsc.exists():
        pytest.skip("plugin build tools not installed (npm ci in quoin/dev/opencode-plugin)")
    subprocess.run(
        [str(tsc), "-p", str(PLUGIN_DIR / "tsconfig.json"), "--outDir", str(tmp_path)],
        check=True, capture_output=True, timeout=120,
    )
    assert (tmp_path / "quoin-opencode-plugin.js").read_text(encoding="utf-8") == ARTIFACT
