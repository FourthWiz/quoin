"""Runtime event schema and translation for OpenCode headless runs.

Turns the raw lines of ``opencode run --format json`` (and the notices it
prints on stderr) into typed, bounded, redacted Quoin events. Everything here
is pure: no file, process, clock or network access. A caller supplies the
clock, the redactor and the bytes.

One invariant is enforced structurally rather than by convention: a
permission sentence never becomes a failure. A rule-based denial is progress
(the model was told no by configuration, and the run can go on or stop
short), a human rejection is an approval request, and only text that is
neither can reach an error event. Classification looks at the anchored start
of the message and never searches inside it, because a denial message embeds
user-controlled rule text that may itself contain the rejection sentence.

Facts about the runtime that this module relies on are cited in the
"Headless run events and process lifecycle" section of
``quoin/adapters/opencode/compatibility.md``; ``CITED_CAPABILITIES`` names
the rows by key.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quoin.opencode_adapter.retry import FAILURE_KINDS

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^oc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
_DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")

RUN_STATES = (
    "prepared", "running", "completed", "failed",
    "awaiting_approval", "cancelled", "interrupted",
)
TRANSLATABLE_FAILURE_KINDS = ("connect", "timeout", "http", "auth", "config")
assert set(TRANSLATABLE_FAILURE_KINDS) <= set(FAILURE_KINDS)

# Anchored message prefixes, taken from the cited runtime sources.
REJECTED_PREFIX = "The user rejected permission to use this specific tool call"
DENIED_PREFIX = (
    "The user has specified a rule which prevents you from using this specific tool call"
)
# The dismissed-question sentence is deliberately not classified: whether it can
# reach a headless stream is unverified, so such text stays a plain tool error.
QUESTION_DISMISSED_PREFIX: Optional[str] = None

_SUBAGENT_PREFIX_RE = re.compile(r"^(?:Subagent failed \(task_id: [^)]*\): )*")
# A task id longer than this is not captured at all rather than persisted in full.
_FIRST_SUBAGENT_RE = re.compile(r"^Subagent failed \(task_id: ([^)]{0,128})\): ")
# Terminal control sequences: OSC (title and hyperlink), CSI (colour, erase,
# cursor movement), escapes with intermediate bytes (character-set selection),
# two-byte escapes, and carriage returns used to redraw a line.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[@-Z\\-_]"
    r"|\r"
)
# Identifiers (session, message, part and task ids, native type names) are
# persisted verbatim only when they have this shape; anything else is replaced
# by a short digest so it stays bounded and cannot carry free text.
_SAFE_IDENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
MAX_JSON_DEPTH = 128
_MAX_COST_ADJUSTED_EXPONENT = 30
_MIN_COST_EXPONENT = -64

CITED_CAPABILITIES: Dict[str, str] = {
    "json-envelope": "every stdout line is a JSON object with type, timestamp and sessionID",
    "json-event-types": "only tool_use, step_start, step_finish, text, reasoning and error are emitted",
    "deny-vs-reject": "rejected and denied permission sentences are distinct and anchored by prefix",
    "task-failure-text": "a failed subagent is reported with a Subagent failed prefix that can nest",
    "task-background-metadata": "background delegation is decided by the output metadata flag",
    "stderr-notice-format": "the permission notice is printed on stderr with ANSI codes",
    "agent-fallback-notice": "an unusable agent name falls back silently on stdout, with a stderr notice",
    "step-finish-shape": "step_finish carries tokens, cost and a finish reason",
    "finish-reason-terminal": "a finish reason other than tool-calls or unknown ends a step",
    "native-error-shape": "native errors are named objects with statusCode and metadata",
    "retry-after": "responseHeaders can carry a retry-after value",
    "permission-ask-outside-tool": "permission sentences can arrive on the native error event",
    "halt-error-shape": "a failed stream is reported as UnknownError with a message",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_in(name: str, value: object, allowed: Sequence[object]) -> None:
    if value not in allowed:
        raise ValueError("%s must be one of %s, got %r" % (name, list(allowed), value))


def _check_opt_str(name: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError("%s must be a string or None" % name)


def _check_str(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % name)


def _check_opt_count(name: str, value: object) -> None:
    if value is not None and (not _is_int(value) or value < 0):
        raise ValueError("%s must be a non-negative integer or None" % name)


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


def _str_tuple(name: str, value: object) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ValueError("%s must be a sequence of strings" % name)


def iso_from_epoch_ms(ms: int) -> str:
    """ISO-8601 UTC with milliseconds and a ``Z`` suffix."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(ms) % 1000)


def iso_now(clock: Callable[[], float]) -> str:
    """Timestamp for the injected clock (seconds since the epoch)."""
    return iso_from_epoch_ms(int(clock() * 1000))


class EventType(str, Enum):
    STARTED = "started"
    PROGRESS = "progress"
    USAGE = "usage"
    ARTIFACT_REFERENCE = "artifact_reference"
    APPROVAL_REQUIRED = "approval_required"
    ERROR = "error"
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------


class _Payload:
    _TUPLES: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, Mapping):
                value = dict(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        if not isinstance(data, Mapping):
            raise ValueError("payload must be an object")
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        extra = set(data) - known
        if extra:
            raise ValueError("unexpected payload keys %s" % sorted(extra))
        kwargs: Dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                value = data[f.name]
                if f.name in cls._TUPLES and isinstance(value, list):
                    value = tuple(value)
                kwargs[f.name] = value
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise ValueError(str(exc))


@dataclass(frozen=True)
class NativeRef:
    type: str
    id: Optional[str]
    revision: int = 1
    content_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        _check_str("native.type", self.type)
        _check_opt_str("native.id", self.id)
        if not _is_int(self.revision) or self.revision < 1:
            raise ValueError("native.revision must be a positive integer")
        _check_opt_str("native.content_sha256", self.content_sha256)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "revision": self.revision,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeRef":
        if not isinstance(data, Mapping):
            raise ValueError("native must be an object")
        try:
            return cls(
                type=data["type"],
                id=data.get("id"),
                revision=data.get("revision", 1),
                content_sha256=data.get("content_sha256"),
            )
        except KeyError as exc:
            raise ValueError("native is missing %s" % exc)


@dataclass(frozen=True)
class StartedPayload(_Payload):
    pid: int
    pgid: int
    argv: Tuple[str, ...]
    env_names: Tuple[str, ...]
    runtime_version: str
    config_digest: str
    role: str
    effective_model: str
    attempt: int
    resume_mode: str
    _TUPLES = ("argv", "env_names")

    def __post_init__(self) -> None:
        for name in ("pid", "pgid", "attempt"):
            if not _is_int(getattr(self, name)):
                raise ValueError("%s must be an integer" % name)
        _set(self, "argv", _str_tuple("argv", self.argv))
        _set(self, "env_names", _str_tuple("env_names", self.env_names))
        for name in ("runtime_version", "config_digest", "role", "effective_model"):
            _check_str(name, getattr(self, name))
        _check_in("resume_mode", self.resume_mode, ("fresh", "session"))


@dataclass(frozen=True)
class ProgressPayload(_Payload):
    kind: str
    raw_type: str
    tool: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    permission_outcome: Optional[str] = None
    delegation: Optional[str] = None
    task_id: Optional[str] = None

    def __post_init__(self) -> None:
        _check_in("kind", self.kind, ("step_start", "text", "reasoning", "tool", "halted", "unknown"))
        _check_str("raw_type", self.raw_type)
        for name in ("tool", "status", "summary", "task_id"):
            _check_opt_str(name, getattr(self, name))
        _check_in("permission_outcome", self.permission_outcome, (None, "denied"))
        _check_in(
            "delegation", self.delegation,
            (None, "completed", "background", "failed", "denied-tail"),
        )


@dataclass(frozen=True)
class UsagePayload(_Payload):
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cost: Optional[str] = None
    finish_reason: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            _check_opt_count(name, getattr(self, name))
        if self.cost is not None and not (
            isinstance(self.cost, str) and _DECIMAL_RE.match(self.cost)
        ):
            raise ValueError("cost must be decimal text or None")
        _check_opt_str("finish_reason", self.finish_reason)


@dataclass(frozen=True)
class ArtifactReferencePayload(_Payload):
    path: str
    sha256_before: Optional[str]
    sha256_after: Optional[str]
    change: str

    def __post_init__(self) -> None:
        _check_str("path", self.path)
        parts = self.path.replace("\\", "/").split("/")
        if not self.path or self.path.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("path must be relative without a '..' segment")
        _check_opt_str("sha256_before", self.sha256_before)
        _check_opt_str("sha256_after", self.sha256_after)
        _check_in("change", self.change, ("created", "modified", "deleted"))


@dataclass(frozen=True)
class ApprovalRequiredPayload(_Payload):
    evidence_source: str
    permission: Optional[str] = None
    patterns: Tuple[str, ...] = ()
    tool: Optional[str] = None
    task_id: Optional[str] = None
    native_decision: str = "rejected"
    _TUPLES = ("patterns",)

    def __post_init__(self) -> None:
        _check_in(
            "evidence_source", self.evidence_source,
            ("tool_error", "task_error", "error_event", "stderr_notice"),
        )
        for name in ("permission", "tool", "task_id"):
            _check_opt_str(name, getattr(self, name))
        _set(self, "patterns", _str_tuple("patterns", self.patterns))
        _check_str("native_decision", self.native_decision)


@dataclass(frozen=True)
class ErrorPayload(_Payload):
    name: str
    message: str
    failure_kind: str
    http_status: Optional[int] = None
    retry_after: Optional[str] = None

    def __post_init__(self) -> None:
        _check_str("name", self.name)
        _check_str("message", self.message)
        _check_in("failure_kind", self.failure_kind, TRANSLATABLE_FAILURE_KINDS)
        _check_opt_count("http_status", self.http_status)
        _check_opt_str("retry_after", self.retry_after)


@dataclass(frozen=True)
class StoppedPayload(_Payload):
    state: str
    exit_code: Optional[int]
    signal: Optional[int]
    reason: Optional[str]
    evidence: str
    step_summary: Mapping

    def __post_init__(self) -> None:
        _check_in("state", self.state, RUN_STATES)
        if self.exit_code is not None and not _is_int(self.exit_code):
            raise ValueError("exit_code must be an integer or None")
        if self.signal is not None and not _is_int(self.signal):
            raise ValueError("signal must be an integer or None")
        _check_opt_str("reason", self.reason)
        _check_in("evidence", self.evidence, ("full", "partial", "none"))
        if not isinstance(self.step_summary, Mapping):
            raise ValueError("step_summary must be an object")
        _set(self, "step_summary", dict(self.step_summary))


PAYLOAD_TYPES = {
    EventType.STARTED: StartedPayload,
    EventType.PROGRESS: ProgressPayload,
    EventType.USAGE: UsagePayload,
    EventType.ARTIFACT_REFERENCE: ArtifactReferencePayload,
    EventType.APPROVAL_REQUIRED: ApprovalRequiredPayload,
    EventType.ERROR: ErrorPayload,
    EventType.STOPPED: StoppedPayload,
}


@dataclass(frozen=True)
class RuntimeEvent:
    schema_version: int
    run_id: str
    attempt: int
    sequence: int
    session_id: Optional[str]
    parent_id: Optional[str]
    timestamp: str
    observed_at: str
    type: EventType
    origin: str
    native: Optional[NativeRef]
    payload: Any
    revision: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if not isinstance(self.run_id, str) or not RUN_ID_RE.match(self.run_id):
            raise ValueError("run_id has the wrong shape")
        if not _is_int(self.attempt) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not _is_int(self.sequence) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        _check_opt_str("session_id", self.session_id)
        _check_opt_str("parent_id", self.parent_id)
        for name in ("timestamp", "observed_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TS_RE.match(value):
                raise ValueError("%s must be ISO-8601 UTC with milliseconds" % name)
        if not isinstance(self.type, EventType):
            raise ValueError("type must be an EventType")
        _check_in("origin", self.origin, ("native", "driver"))
        if (self.origin == "native") != (self.native is not None):
            raise ValueError("native is required exactly when origin is native")
        if not isinstance(self.payload, PAYLOAD_TYPES[self.type]):
            raise ValueError("payload does not match type %s" % self.type.value)
        if not _is_int(self.revision) or self.revision < 1:
            raise ValueError("revision must be a positive integer")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "observed_at": self.observed_at,
            "type": self.type.value,
            "origin": self.origin,
            "native": self.native.to_dict() if self.native is not None else None,
            "payload": self.payload.to_dict(),
            "revision": self.revision,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @classmethod
    def from_json(cls, text: str) -> "RuntimeEvent":
        try:
            obj = json.loads(text)
        except ValueError as exc:
            raise ValueError("not JSON: %s" % exc)
        if not isinstance(obj, dict):
            raise ValueError("event must be an object")
        if "schema_version" not in obj:
            raise ValueError("missing schema_version")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        try:
            etype = EventType(obj.get("type"))
        except ValueError:
            raise ValueError("unknown type %r" % (obj.get("type"),))
        payload = PAYLOAD_TYPES[etype].from_dict(obj.get("payload"))
        native = obj.get("native")
        try:
            return cls(
                schema_version=obj["schema_version"],
                run_id=obj.get("run_id"),
                attempt=obj.get("attempt"),
                sequence=obj.get("sequence"),
                session_id=obj.get("session_id"),
                parent_id=obj.get("parent_id"),
                timestamp=obj.get("timestamp"),
                observed_at=obj.get("observed_at"),
                type=etype,
                origin=obj.get("origin"),
                native=NativeRef.from_dict(native) if native is not None else None,
                payload=payload,
                revision=obj.get("revision", 1),
            )
        except TypeError as exc:
            raise ValueError(str(exc))


# ---------------------------------------------------------------------------
# line parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedLine:
    """Either a decoded object or a diagnostic code, never both."""

    obj: Optional[Dict[str, Any]] = None
    diagnostic: Optional[str] = None


# ``unhashable`` is counted by the pipeline, the others by ``parse_line``.
DIAGNOSTIC_CODES = (
    "oversized", "non-utf8", "non-json", "non-object", "missing-type", "empty",
    "too-deep", "unhashable",
)


def _nesting_exceeds(obj: object, limit: int) -> bool:
    """Iterative depth check, so a deep value cannot exhaust the call stack."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children: Iterable[Any] = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if depth > limit:
            return True
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return False


def parse_line(
    raw: bytes, *, max_bytes: int = 1_048_576, max_depth: int = MAX_JSON_DEPTH
) -> ParsedLine:
    """Decode one stdout line. Never raises, whatever the input.

    Nesting deeper than ``max_depth`` is rejected here, because later steps
    (canonical hashing, event serialisation) recurse over the value.
    """
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        raw = bytes(raw)
        if len(raw) > max_bytes:
            return ParsedLine(diagnostic="oversized")
        stripped = raw.strip()
        if not stripped:
            return ParsedLine(diagnostic="empty")
        try:
            text = stripped.decode("utf-8")
        except UnicodeDecodeError:
            return ParsedLine(diagnostic="non-utf8")
        try:
            obj = json.loads(text, parse_float=Decimal)
        except (ValueError, RecursionError):
            return ParsedLine(diagnostic="non-json")
        if not isinstance(obj, dict):
            return ParsedLine(diagnostic="non-object")
        if _nesting_exceeds(obj, max_depth):
            return ParsedLine(diagnostic="too-deep")
        if not isinstance(obj.get("type"), str):
            return ParsedLine(diagnostic="missing-type")
        return ParsedLine(obj=obj)
    except Exception:  # noqa: BLE001 - parse_line must never raise
        return ParsedLine(diagnostic="non-json")


def _canonical_bytes(obj: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in obj.items() if k != "timestamp"}
    # JSON escapes can decode to lone surrogates; surrogatepass hashes them
    # instead of failing, and leaves every other string's bytes unchanged.
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8", "surrogatepass")


def content_hash(obj: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


# ---------------------------------------------------------------------------
# permission classification
# ---------------------------------------------------------------------------


def classify_permission_error(text: str) -> Optional[str]:
    """Return ``"rejected"``, ``"denied"`` or ``None`` for an error message.

    Anchored prefix comparison after stripping any number of subagent
    prefixes; never a substring search.
    """
    if not isinstance(text, str):
        return None
    rest = _SUBAGENT_PREFIX_RE.sub("", text, count=1)
    if rest.startswith(REJECTED_PREFIX):
        return "rejected"
    if QUESTION_DISMISSED_PREFIX is not None and rest.startswith(QUESTION_DISMISSED_PREFIX):
        return "rejected"
    if rest.startswith(DENIED_PREFIX):
        return "denied"
    return None


def _first_task_id(text: str, redact: Callable[[str], str]) -> Optional[str]:
    match = _FIRST_SUBAGENT_RE.match(text)
    return _safe_ident(match.group(1), redact) if match else None


# ---------------------------------------------------------------------------
# bounding and redaction
# ---------------------------------------------------------------------------


def _identity(text: str) -> str:
    return text


def _scrub(text: str) -> str:
    """Replace lone surrogates so the text is always UTF-8 encodable."""
    return _SURROGATE_RE.sub("\ufffd", text)


def _bound(text: object, redact: Callable[[str], str], limit: int) -> Optional[str]:
    """Redact first, then truncate on a UTF-8 boundary."""
    if not isinstance(text, str):
        return None
    clean = _scrub(redact(_scrub(text)))
    data = clean.encode("utf-8")
    if len(data) <= limit:
        return clean
    head = data[:limit].decode("utf-8", "ignore")
    dropped = len(data) - len(head.encode("utf-8"))
    return "%s…[truncated %d bytes]" % (head, dropped)


def _safe_ident(value: object, redact: Callable[[str], str]) -> Optional[str]:
    """An identifier as-is when well formed and untouched by redaction, else a digest.

    Deterministic for a given redactor, so a dedup key computed from the raw
    line matches the id later persisted on the event.
    """
    if not isinstance(value, str):
        return None
    if _SAFE_IDENT_RE.fullmatch(value) and redact(value) == value:
        return value
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
    return "h-" + digest[:32]


# ---------------------------------------------------------------------------
# native error mapping
# ---------------------------------------------------------------------------


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _retry_after(headers: Mapping[str, Any]) -> Optional[str]:
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "retry-after" and isinstance(value, str):
            return value
    return None


def map_native_error(
    err: Mapping[str, Any],
    *,
    redact: Callable[[str], str] = _identity,
    summary_limit: int = 2048,
) -> ErrorPayload:
    """Map a native error object to an error payload. Never ``policy``."""
    err = _mapping(err)
    name = err.get("name") if isinstance(err.get("name"), str) else "UnknownError"
    data = _mapping(err.get("data"))
    message = data.get("message") if isinstance(data.get("message"), str) else err.get("message")
    if not isinstance(message, str):
        message = name
    message = _bound(message, redact, summary_limit) or ""
    status = data.get("statusCode")
    status = status if _is_int(status) and status >= 0 else None
    metadata = _mapping(data.get("metadata"))
    code = metadata.get("code") if isinstance(metadata.get("code"), str) else ""
    retryable = data.get("isRetryable") is True
    retry_after = _retry_after(_mapping(data.get("responseHeaders")))

    kind = "config"
    if status == 429 or (status is not None and 500 <= status <= 599):
        kind = "http"
    elif status in (401, 403) or name == "ProviderAuthError":
        kind = "auth"
    elif "timeout" in code.lower():
        kind = "timeout"
    elif code == "ECONNRESET" or (retryable and status is None):
        kind = "connect"
    return ErrorPayload(
        name=_bound(name, redact, 256) or "UnknownError",
        message=message,
        failure_kind=kind,
        http_status=status if kind == "http" else None,
        retry_after=_bound(retry_after, redact, 256) if kind == "http" else None,
    )


# ---------------------------------------------------------------------------
# stderr notices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StderrSignal:
    kind: str  # "approval" | "agent_fallback"
    name: str
    patterns: Tuple[str, ...] = ()
    patterns_complete: bool = False


_APPROVAL_HEAD_RE = re.compile(r"^!\s+permission requested: (?P<perm>\S+) \(")
_APPROVAL_TAIL = "); auto-rejecting"
_FALLBACK_RE = re.compile(
    r'^!\s+agent "(?P<name>[^"]*)" (?:not found|is a subagent, not a primary agent)'
    r"\. Falling back to default agent$"
)


def parse_stderr_line(line: str) -> Optional[StderrSignal]:
    """Recognise the runtime's own notices on stderr; anything else is ``None``.

    The approval signal keys on the head of the line only: a pattern that
    contains a newline splits the notice over several lines, and the
    continuation lines match nothing.
    """
    if not isinstance(line, str):
        return None
    text = _ANSI_RE.sub("", line).strip()
    head = _APPROVAL_HEAD_RE.match(text)
    if head:
        rest = text[head.end():]
        if rest.endswith(_APPROVAL_TAIL):
            inner = rest[: -len(_APPROVAL_TAIL)]
            patterns = tuple(inner.split(", ")) if inner else ()
            return StderrSignal("approval", head.group("perm"), patterns, True)
        patterns = tuple(rest.split(", ")) if rest else ()
        return StderrSignal("approval", head.group("perm"), patterns, False)
    fallback = _FALLBACK_RE.match(text)
    if fallback:
        return StderrSignal("agent_fallback", fallback.group("name"))
    return None


# ---------------------------------------------------------------------------
# translation
# ---------------------------------------------------------------------------


def _count(value: object) -> Optional[int]:
    return value if _is_int(value) and value >= 0 else None


def _decimal_text(value: object) -> Optional[str]:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, float):
            dec = Decimal(repr(value))
        else:
            return None
        if not dec.is_finite() or dec < 0:
            return None
        # Plain-notation text grows with the exponent, so a short literal such
        # as 1e100000000 would expand to a huge string; such values are not costs.
        if dec.adjusted() > _MAX_COST_ADJUSTED_EXPONENT:
            return None
        if dec.as_tuple().exponent < _MIN_COST_EXPONENT:  # type: ignore[operator]
            return None
        return format(dec, "f")
    except (InvalidOperation, ValueError):
        return None


def _iso_from_envelope(obj: Mapping[str, Any], fallback: str) -> str:
    ts = obj.get("timestamp")
    if _is_int(ts) and ts >= 0:
        try:
            return iso_from_epoch_ms(ts)
        except (OverflowError, OSError, ValueError):
            return fallback
    return fallback


def translate(
    obj: Mapping[str, Any],
    *,
    run_id: str,
    attempt: int,
    observed_at: str,
    redact: Callable[[str], str] = _identity,
    summary_limit: int = 2048,
) -> Tuple[RuntimeEvent, ...]:
    """Translate one native object into unstamped events (``sequence`` 0).

    Never raises. No ``tool_use`` input produces an error event, and every
    permission sentence is classified before any error mapping runs.
    """
    try:
        return _translate(obj, run_id, attempt, observed_at, redact, summary_limit)
    except Exception:  # noqa: BLE001 - translation must not raise
        return (_fallback_event(obj, run_id, attempt, observed_at, redact),)


def _fallback_event(obj, run_id, attempt, observed_at, redact) -> RuntimeEvent:
    """Progress ``unknown`` that keeps the part id, so a rebuilt deduper keys it the same way."""
    payload = ProgressPayload(kind="unknown", raw_type="unparseable")
    source = obj if isinstance(obj, Mapping) else {}
    try:
        part_id = _safe_ident(_mapping(source.get("part")).get("id"), redact)
        return _event(
            source, run_id, attempt, observed_at, EventType.PROGRESS, payload, part_id,
            redact=redact,
        )
    except Exception:  # noqa: BLE001 - last resort drops everything line-derived
        return _event(
            {}, run_id, attempt, observed_at, EventType.PROGRESS, payload, None,
            redact=_identity,
        )


def _event(
    obj: Mapping[str, Any],
    run_id: str,
    attempt: int,
    observed_at: str,
    etype: EventType,
    payload: Any,
    native_id: Optional[str],
    parent_id: Optional[str] = None,
    *,
    redact: Callable[[str], str],
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        attempt=attempt,
        sequence=0,
        session_id=_safe_ident(obj.get("sessionID"), redact),
        parent_id=parent_id,
        timestamp=_iso_from_envelope(obj, observed_at),
        observed_at=observed_at,
        type=etype,
        origin="native",
        native=NativeRef(
            type=_native_type(obj, redact), id=native_id, revision=1, content_sha256=None
        ),
        payload=payload,
    )


def _native_type(obj: Mapping[str, Any], redact: Callable[[str], str]) -> str:
    return _safe_ident(obj.get("type"), redact) or "unknown"


def _translate(obj, run_id, attempt, observed_at, redact, limit) -> Tuple[RuntimeEvent, ...]:
    raw_type = obj.get("type")
    part = _mapping(obj.get("part"))
    part_id = _safe_ident(part.get("id"), redact)
    message_id = _safe_ident(part.get("messageID"), redact)
    shown_type = _native_type(obj, redact)

    def make(etype: EventType, payload: Any) -> Tuple[RuntimeEvent, ...]:
        return (
            _event(
                obj, run_id, attempt, observed_at, etype, payload, part_id, message_id,
                redact=redact,
            ),
        )

    def progress(**kw: Any) -> Tuple[RuntimeEvent, ...]:
        return make(EventType.PROGRESS, ProgressPayload(raw_type=shown_type, **kw))

    if raw_type == "step_start":
        return progress(kind="step_start")

    if raw_type in ("text", "reasoning"):
        return progress(kind=raw_type, summary=_bound(part.get("text"), redact, limit))

    if raw_type == "step_finish":
        tokens = _mapping(part.get("tokens"))
        cache = _mapping(tokens.get("cache"))
        reason = part.get("reason") if isinstance(part.get("reason"), str) else None
        return make(
            EventType.USAGE,
            UsagePayload(
                input_tokens=_count(tokens.get("input")),
                output_tokens=_count(tokens.get("output")),
                reasoning_tokens=_count(tokens.get("reasoning")),
                cache_read_tokens=_count(cache.get("read")),
                cache_write_tokens=_count(cache.get("write")),
                cost=_decimal_text(part.get("cost")),
                finish_reason=_bound(reason, redact, 128),
            ),
        )

    if raw_type == "tool_use":
        return _translate_tool(part, make, redact, limit)

    if raw_type == "error":
        err = _mapping(obj.get("error"))
        data = _mapping(err.get("data"))
        # Only the message that the error mapping would report is classified,
        # and never one carrying an HTTP status: a provider response is a
        # failure to report, whatever its text says.
        message = data.get("message") if isinstance(data.get("message"), str) else err.get("message")
        outcome = None
        if isinstance(message, str) and data.get("statusCode") is None:
            outcome = classify_permission_error(message)
        if outcome == "rejected":
            return make(
                EventType.APPROVAL_REQUIRED,
                ApprovalRequiredPayload(evidence_source="error_event", permission=None),
            )
        if outcome == "denied":
            return progress(kind="halted", permission_outcome="denied")
        return make(
            EventType.ERROR, map_native_error(err, redact=redact, summary_limit=limit)
        )

    return progress(kind="unknown")


def _translate_tool(part, make, redact, limit) -> Tuple[RuntimeEvent, ...]:
    raw_tool = part.get("tool")
    is_task = raw_tool == "task"
    tool = _bound(raw_tool, redact, 256)
    state = _mapping(part.get("state"))
    status = state.get("status")

    def progress(**kw: Any):
        return make(EventType.PROGRESS, ProgressPayload(raw_type="tool_use", **kw))

    if status == "completed":
        if is_task:
            background = _mapping(state.get("metadata")).get("background") is True
            return progress(
                kind="tool", tool=tool, status="completed",
                delegation="background" if background else "completed",
            )
        return progress(
            kind="tool", tool=tool, status="completed",
            summary=_bound(state.get("title"), redact, limit),
        )

    if status == "error":
        text = state.get("error")
        outcome = classify_permission_error(text) if isinstance(text, str) else None
        summary = _bound(text, redact, limit)
        if outcome == "rejected":
            return make(
                EventType.APPROVAL_REQUIRED,
                ApprovalRequiredPayload(
                    evidence_source="task_error" if is_task else "tool_error",
                    tool=tool,
                    task_id=_first_task_id(text, redact) if is_task else None,
                ),
            )
        if is_task:
            if outcome == "denied":
                return progress(
                    kind="tool", tool=tool, status="error", summary=summary,
                    delegation="denied-tail", permission_outcome="denied",
                    task_id=_first_task_id(text, redact),
                )
            return progress(
                kind="tool", tool=tool, status="error", summary=summary,
                delegation="failed",
                task_id=_first_task_id(text, redact) if isinstance(text, str) else None,
            )
        return progress(
            kind="tool", tool=tool, status="error", summary=summary,
            permission_outcome="denied" if outcome == "denied" else None,
        )

    return progress(kind="tool", tool=tool, status=_bound(status, redact, 256))


# ---------------------------------------------------------------------------
# pipeline pieces
# ---------------------------------------------------------------------------

Key = Tuple[str, str]


def native_key(
    obj: Mapping[str, Any],
    *,
    attempt: int,
    redact: Callable[[str], str] = _identity,
    digest: Optional[str] = None,
) -> Key:
    """Dedup key: ``(type, part id)`` when a part id exists, else a content hash.

    A line without a part id is keyed per attempt: an identical native error
    on a later attempt of the same session is a new failure, not a replay.
    Type and id go through the same shaping as the persisted event, so
    ``Deduper.from_events`` rebuilds exactly these keys.
    """
    raw_type = _native_type(obj, redact)
    part_id = _safe_ident(_mapping(obj.get("part")).get("id"), redact)
    if part_id is not None:
        return (raw_type, part_id)
    if digest is None:
        digest = content_hash(obj)
    return (raw_type, "sha256:%s:%d" % (digest, attempt))


def _driver_key(event: RuntimeEvent) -> Key:
    if event.type is EventType.ARTIFACT_REFERENCE:
        return (
            "driver:artifact_reference",
            "%d:%s" % (event.attempt, event.payload.path),
        )
    return ("driver:" + event.type.value, str(event.attempt))


def _payload_hash(payload: Any) -> str:
    text = json.dumps(payload.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Deduper:
    """Exact repeats are dropped; changed content becomes a new revision.

    One entry is kept per distinct key for the deduper's lifetime, which is
    one run. ``dropped_duplicates`` and ``revisions`` count what this instance
    saw: a deduper rebuilt with ``from_events`` starts them at zero, so a
    caller reporting run-wide totals must carry them across attempts.
    """

    def __init__(self) -> None:
        self._seen: Dict[Key, Tuple[int, str]] = {}
        self.dropped_duplicates = 0
        self.revisions = 0

    def admit(self, key: Key, content_hash_: str) -> Tuple[str, int]:
        seen = self._seen.get(key)
        if seen is None:
            self._seen[key] = (1, content_hash_)
            return ("new", 1)
        revision, old_hash = seen
        if old_hash == content_hash_:
            self.dropped_duplicates += 1
            return ("duplicate", revision)
        self._seen[key] = (revision + 1, content_hash_)
        self.revisions += 1
        return ("revision", revision + 1)

    @classmethod
    def from_events(cls, events: Iterable[RuntimeEvent]) -> "Deduper":
        deduper = cls()
        for event in events:
            if event.native is not None:
                native = event.native
                if native.id is not None:
                    ident = native.id
                else:
                    ident = "sha256:%s:%d" % (native.content_sha256, event.attempt)
                key: Key = (native.type, ident)
                digest = native.content_sha256 or ""
                revision = native.revision
            else:
                key = _driver_key(event)
                digest = _payload_hash(event.payload)
                revision = event.revision
            previous = deduper._seen.get(key)
            if previous is None or revision >= previous[0]:
                deduper._seen[key] = (revision, digest)
        return deduper


class EventPipeline:
    """parse, translate, dedup and stamp; no I/O.

    ``max_line_bytes`` classifies a line the caller has already read; it does
    not bound the read itself. A reader must use a bounded
    ``readline(max_line_bytes + 1)`` and discard up to the next newline when
    the limit is hit, otherwise one endless line is buffered in full.
    ``feed_line`` never raises for any input bytes.
    """

    def __init__(
        self,
        run_id: str,
        attempt: int,
        *,
        redact: Callable[[str], str] = _identity,
        observed_clock: Callable[[], float],
        start_sequence: int = 1,
        deduper: Optional[Deduper] = None,
        summary_limit: int = 2048,
        max_line_bytes: int = 1_048_576,
    ) -> None:
        # Validated here so a bad argument fails at construction instead of on
        # the first stdout line, inside the reader loop.
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise ValueError("run_id has the wrong shape")
        if not _is_int(attempt) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not _is_int(start_sequence) or start_sequence < 0:
            raise ValueError("start_sequence must be a non-negative integer")
        self.run_id = run_id
        self.attempt = attempt
        self._redact = redact
        self._clock = observed_clock
        self._next_sequence = start_sequence
        self._deduper = deduper if deduper is not None else Deduper()
        self._limit = summary_limit
        self._max_line = max_line_bytes
        self.counters: Dict[str, int] = {
            "lines": 0,
            "dropped_duplicates": 0,
            "revisions": 0,
        }
        for code in DIAGNOSTIC_CODES:
            self.counters[code] = 0

    def _stamp(self, event: RuntimeEvent) -> RuntimeEvent:
        stamped = replace(event, sequence=self._next_sequence)
        self._next_sequence += 1
        return stamped

    def _sync_counters(self) -> None:
        self.counters["dropped_duplicates"] = self._deduper.dropped_duplicates
        self.counters["revisions"] = self._deduper.revisions

    def feed_line(self, raw: bytes) -> List[RuntimeEvent]:
        self.counters["lines"] += 1
        parsed = parse_line(raw, max_bytes=self._max_line)
        if parsed.obj is None:
            self.counters[parsed.diagnostic or "non-json"] += 1
            return []
        obj = parsed.obj
        try:
            digest = content_hash(obj)
            key = native_key(obj, attempt=self.attempt, redact=self._redact, digest=digest)
        except Exception:  # noqa: BLE001 - one bad line must not stop the reader
            self.counters["unhashable"] += 1
            return []
        verdict, revision = self._deduper.admit(key, digest)
        self._sync_counters()
        if verdict == "duplicate":
            return []
        observed = iso_now(self._clock)
        out: List[RuntimeEvent] = []
        for event in translate(
            obj, run_id=self.run_id, attempt=self.attempt, observed_at=observed,
            redact=self._redact, summary_limit=self._limit,
        ):
            native = replace(event.native, revision=revision, content_sha256=digest)
            out.append(self._stamp(replace(event, native=native, revision=revision)))
        return out

    def driver_event(self, etype: EventType, payload: Any) -> Optional[RuntimeEvent]:
        """Build a driver-origin event; ``None`` for an exact repeat."""
        observed = iso_now(self._clock)
        event = RuntimeEvent(
            schema_version=SCHEMA_VERSION,
            run_id=self.run_id,
            attempt=self.attempt,
            sequence=0,
            session_id=None,
            parent_id=None,
            timestamp=observed,
            observed_at=observed,
            type=etype,
            origin="driver",
            native=None,
            payload=payload,
        )
        verdict, revision = self._deduper.admit(_driver_key(event), _payload_hash(payload))
        self._sync_counters()
        if verdict == "duplicate":
            return None
        return self._stamp(replace(event, revision=revision))


@dataclass(frozen=True)
class StepSummary:
    starts: int
    finishes: int
    open_step: bool
    last_finish_reason: Optional[str]
    last_finish_terminal: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "starts": self.starts,
            "finishes": self.finishes,
            "open_step": self.open_step,
            "last_finish_reason": self.last_finish_reason,
            "last_finish_terminal": self.last_finish_terminal,
        }


def _identity_of(event: RuntimeEvent) -> Tuple[str, Any]:
    if event.native is not None and event.native.id is not None:
        return ("id", event.native.id)
    return ("seq", event.sequence)


def summarize_steps(events: Iterable[RuntimeEvent]) -> StepSummary:
    """Report step boundaries seen in stream order; the state decision is not made here."""
    starts = set()
    finishes: Dict[Tuple[str, Any], RuntimeEvent] = {}
    first_seen: Dict[Tuple[str, Any], int] = {}
    for index, event in enumerate(events):
        if event.type is EventType.PROGRESS and event.payload.kind == "step_start":
            starts.add(_identity_of(event))
        elif event.type is EventType.USAGE:
            ident = _identity_of(event)
            first_seen.setdefault(ident, index)
            previous = finishes.get(ident)
            if previous is None or event.revision >= previous.revision:
                finishes[ident] = event
    # The last step is the one that appeared last, not the one revised last.
    last_reason: Optional[str] = None
    if finishes:
        last_ident = max(first_seen, key=first_seen.__getitem__)
        last_reason = finishes[last_ident].payload.finish_reason
    terminal = None
    if last_reason is not None:
        terminal = last_reason not in ("tool-calls", "unknown")
    return StepSummary(
        starts=len(starts),
        finishes=len(finishes),
        open_step=len(starts) > len(finishes),
        last_finish_reason=last_reason,
        last_finish_terminal=terminal,
    )


def usage_totals(events: Iterable[RuntimeEvent]) -> UsagePayload:
    """Sum the last revision per part; any unknown value makes that field unknown.

    Empty input gives every field ``None`` (unknown) rather than zero. The five
    token fields are kept apart; a caller converting to ``retry.Usage`` must
    decide how they add up to its single token count.
    """
    latest: Dict[Tuple[str, Any], RuntimeEvent] = {}
    for event in events:
        if event.type is not EventType.USAGE:
            continue
        ident = _identity_of(event)
        previous = latest.get(ident)
        if previous is None or event.revision >= previous.revision:
            latest[ident] = event
    if not latest:
        return UsagePayload()
    chosen = list(latest.values())
    sums: Dict[str, Any] = {}
    for name in (
        "input_tokens", "output_tokens", "reasoning_tokens",
        "cache_read_tokens", "cache_write_tokens",
    ):
        values = [getattr(e.payload, name) for e in chosen]
        sums[name] = None if any(v is None for v in values) else sum(values)
    costs = [e.payload.cost for e in chosen]
    cost = None
    if all(c is not None for c in costs):
        cost = format(sum((Decimal(c) for c in costs), Decimal(0)), "f")
    last = chosen[-1].payload.finish_reason
    return UsagePayload(cost=cost, finish_reason=last, **sums)
