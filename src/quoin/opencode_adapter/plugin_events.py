"""Translate the Quoin OpenCode plugin's event log into runtime events.

The plugin (built into ``quoin/adapters/opencode/plugin/``) runs inside OpenCode
and appends one JSON record per lifecycle event, and one per finished model
step with its token usage and cost, to a log file. It only
observes; this module owns the meaning of those records. Reading is tolerant
(a bad line is counted and skipped, never raised), and translation goes
through the same ``Deduper`` the headless pipeline uses, so a record repeated
by a re-read log or a redelivered notification is dropped, and a changed
record under the same native id becomes a new revision. A finished step is
keyed by its part id, so a step OpenCode re-emits with new numbers becomes a
revision of the same usage event rather than a second one.

A record looks like::

    {"v": 1, "plugin_version": "0.2.0", "run_id": "oc-...", "instance": "1a2b3c4d", "seq": 0,
     "ts": "2026-11-02T12:00:00.000Z", "source": "event" | "hook",
     "native_type": "session.idle", "native_id": "evt_..." | null,
     "session_id": "ses_..." | null, "parent_id": "ses_..." | null,
     "data": {...}}
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from quoin.opencode_adapter import events as ev

RECORD_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
_SESSION_STATUS = {
    "session.created": "created",
    "session.idle": "idle",
    "experimental.session.compacting": "compacting",
    "session.compacted": "compacted",
}
PLUGIN_NATIVE_TYPES = tuple(_SESSION_STATUS) + (
    "message.part.updated",
    "session.error",
    "permission.asked",
    "permission.replied",
    "tool.execute.after",
)
_MAX_PATTERNS = 20
_INSTANCE_RE = re.compile(r"^[0-9a-f]{8}$")
Redactor = Callable[[str], str]


@dataclass
class LogRead:
    records: List[Dict[str, Any]] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)

    def count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1


def _valid_record(obj: Mapping[str, Any]) -> Optional[str]:
    """The reason a decoded record is unusable, or None when it is usable."""
    if obj.get("v") != RECORD_VERSION:
        return "wrong-version"
    if obj.get("native_type") not in PLUGIN_NATIVE_TYPES:
        return "unknown-type"
    if not isinstance(obj.get("run_id"), str) or not ev.RUN_ID_RE.match(obj["run_id"]):
        return "bad-run-id"
    seq = obj.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return "bad-seq"
    if not isinstance(obj.get("data"), Mapping):
        return "bad-data"
    instance = obj.get("instance")
    if instance is not None and not (isinstance(instance, str) and _INSTANCE_RE.match(instance)):
        return "bad-instance"
    return None


def parse_lines(lines: Iterable[bytes]) -> LogRead:
    """Decode log lines; every rejected line is counted by reason."""
    out = LogRead()
    for raw in lines:
        if not raw.strip():
            continue
        if len(raw) > MAX_LINE_BYTES:
            out.count("oversized")
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            out.count("non-json")
            continue
        if not isinstance(obj, dict):
            out.count("non-object")
            continue
        reason = _valid_record(obj)
        if reason is not None:
            out.count(reason)
            continue
        out.records.append(obj)
    return out


def read_log(path) -> LogRead:
    """Read a plugin log; a missing file reads as empty."""
    try:
        with open(path, "rb") as fh:
            return parse_lines(fh)
    except FileNotFoundError:
        return LogRead()


def _payload(record: Mapping[str, Any], redact: Redactor) -> Optional[Tuple[ev.EventType, Any]]:
    def ident(value: object) -> Optional[str]:
        return ev._safe_ident(value, redact)

    native_type = record["native_type"]
    data = record["data"]
    if native_type in _SESSION_STATUS:
        return ev.EventType.PROGRESS, ev.ProgressPayload(
            kind="session", raw_type=native_type, status=_SESSION_STATUS[native_type]
        )
    if native_type == "session.error":
        return ev.EventType.ERROR, ev.map_native_error(ev._mapping(data.get("error")), redact=redact)
    if native_type == "permission.asked":
        # A prompt the person may still approve: progress, never the terminal
        # approval_required event the headless driver treats as a halt.
        patterns = data.get("patterns") if isinstance(data.get("patterns"), list) else []
        shown = ", ".join(p for p in patterns[:_MAX_PATTERNS] if isinstance(p, str))
        return ev.EventType.PROGRESS, ev.ProgressPayload(
            kind="approval",
            raw_type=native_type,
            status="asked",
            tool=ident(data.get("permission")),
            summary=ev._bound(shown, redact, 1024) if shown else None,
            request_id=ident(data.get("permission_id")),
        )
    if native_type == "permission.replied":
        return ev.EventType.PROGRESS, ev.ProgressPayload(
            kind="approval",
            raw_type=native_type,
            status=ident(data.get("reply")) or "unknown",
            request_id=ident(data.get("permission_id")),
        )
    if native_type == "message.part.updated":
        # Only finished steps are logged; the numbers map like a headless step_finish.
        tokens = ev._mapping(data.get("tokens"))
        cache = ev._mapping(tokens.get("cache"))
        return ev.EventType.USAGE, ev.UsagePayload(
            input_tokens=ev._count(tokens.get("input")),
            output_tokens=ev._count(tokens.get("output")),
            reasoning_tokens=ev._count(tokens.get("reasoning")),
            cache_read_tokens=ev._count(cache.get("read")),
            cache_write_tokens=ev._count(cache.get("write")),
            cost=ev._decimal_text(data.get("cost")),
            finish_reason=ident(data.get("reason")),
        )
    if native_type == "tool.execute.after":
        return ev.EventType.PROGRESS, ev.ProgressPayload(
            kind="tool", raw_type=native_type, tool=ident(data.get("tool")), status="completed"
        )
    return None


def record_key(record: Mapping[str, Any], redact: Redactor = ev._identity) -> ev.Key:
    """``(native type, native id)``; without an id, the plugin instance and its sequence."""
    native_id = ev._safe_ident(record.get("native_id"), redact)
    if native_id is not None:
        return (record["native_type"], native_id)
    instance = record.get("instance")
    if isinstance(instance, str):
        return (record["native_type"], "seq:%s:%d" % (instance, record["seq"]))
    return (record["native_type"], "seq:%d" % record["seq"])


def translate(
    records: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    attempt: int = 1,
    start_sequence: int = 0,
    deduper: Optional[ev.Deduper] = None,
    clock: Callable[[], float] = time.time,
    redact: Redactor = ev._identity,
) -> List[ev.RuntimeEvent]:
    """Runtime events for the records that belong to ``run_id``, deduplicated.

    Records for another run are skipped; exact repeats are dropped and a
    changed record under the same key comes back as the next revision.
    ``redact`` is applied to every persisted string, as in the headless
    pipeline.
    """
    deduper = deduper if deduper is not None else ev.Deduper()
    observed = ev.iso_now(clock)
    out: List[ev.RuntimeEvent] = []
    sequence = start_sequence
    for record in records:
        if record.get("run_id") != run_id:
            continue
        built = _payload(record, redact)
        if built is None:
            continue
        event_type, payload = built
        digest = ev._payload_hash(payload)
        key = record_key(record, redact)
        verdict, revision = deduper.admit(key, digest)
        if verdict == "duplicate":
            continue
        ts = record.get("ts")
        timestamp = ts if isinstance(ts, str) and ev._TS_RE.match(ts) else observed
        out.append(ev.RuntimeEvent(
            schema_version=ev.SCHEMA_VERSION,
            run_id=run_id,
            attempt=attempt,
            sequence=sequence,
            session_id=ev._safe_ident(record.get("session_id"), redact),
            parent_id=ev._safe_ident(record.get("parent_id"), redact),
            timestamp=timestamp,
            observed_at=observed,
            type=event_type,
            origin="native",
            native=ev.NativeRef(type=key[0], id=key[1], revision=revision, content_sha256=digest),
            payload=payload,
            revision=revision,
        ))
        sequence += 1
    return out
