"""Cost rows and run telemetry for OpenCode phase runs.

One run id yields at most one cost-ledger row and one telemetry block in the
run record. The policies every part of this module follows:

* A value that is not known is JSON null with a sibling reason in records, and
  an omitted key plus ``src=unresolved`` in the ledger. It is never written as
  0 and never estimated.
* A dollar amount is written only when the compiled configuration prices the
  effective model and the stream reported a cost that is not 0 while tokens
  were used. A model without prices reports 0 for every step, so a reported 0
  cannot tell a free model from an unpriced one.
* ``src=opencode_stream`` marks a row whose dollar cost is known. It lives in
  this adapter and in its README, never in the portable core.
* Usage is the latest revision of each native part id in the run's own
  sidecar, which covers the run's own session only; child sessions are not
  visible in the parent stream and their usage is reported as unavailable.
* The ledger is append-only. A row is appended once per run id, guarded by a
  scan for the run id; nothing here removes or rewrites a ledger line, and a
  ledger whose earlier bytes changed during the run is reported as a boundary
  violation.
* Records are replaced atomically; telemetry is written in one record write
  after the ledger append, so a crash between the two loses only the closed
  marking and the telemetry, never the row.

This module knows nothing about workflow entries. ``run_hooks`` composes it
with the evidence layer for single-phase runs, and a coordinator can call
``record_run`` and ``prelaunch_row`` directly.
"""
from __future__ import annotations

import calendar
import hashlib
import importlib.util
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import events as ev
from . import gate, runstore

SRC_TAG = "opencode_stream"
SCOPE = "parent-session-only"
MAX_LEDGER_BYTES = 16 * 1024 * 1024
LEDGER_NAME = "cost-ledger.md"
USD_PLACES = 12
SMALLEST_USD = "0.000000000001"
TELEMETRY_SCHEMA = 1
MAX_APPENDED_LISTED = 50
MAX_SESSION_IDS = 16
MAX_PART_IDS = 256

# Run phases (hyphens normalised to underscores) and the ledger phase each is
# recorded under. `run` is the coordinator phase; it is mapped now so the table
# already covers it once the coordinator command is supported.
LEDGER_PHASES: Dict[str, str] = {
    "discover": "discover",
    "architect": "architect",
    "plan": "plan",
    "thorough_plan": "thorough-plan",
    "critic": "critic",
    "implement": "implement",
    "review": "review",
    "gate": "gate",
    "end_of_task": "end-of-task",
    "checkpoint": "checkpoint",
    "continue_work": "ad-hoc",
    "run": "run-orchestrator",
}

PROVENANCE_CITATIONS = (
    "step-finish-shape", "child-events-filtered", "parent-cost-scope",
    "cost-unpriced-zero", "effective-variant-visibility", "continuation-no-replay",
)

_ATTRIBUTION_RE = re.compile(r"^(usd=[0-9.]+;)?(tok=[0-9]+;)?src=[a-z_]+$")
_UNSAFE_RE = re.compile("[|\x00-\x1f\x7f-\x9f  ]")
_SPACE_RE = re.compile(r"\s+")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def ledger_phase(run_phase: Any) -> Optional[str]:
    """The ledger phase word for a run phase, or None for an unmapped phase."""
    return LEDGER_PHASES.get(runstore.normalize_phase(run_phase))


def sanitize(text: Any, limit: int = 200) -> str:
    """Text that is safe inside one ledger column."""
    cleaned = _UNSAFE_RE.sub("_", str(text))
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()[:limit].strip()
    return cleaned or "unknown"


# ---------------------------------------------------------------------------
# run state
# ---------------------------------------------------------------------------


def is_open(record: Mapping[str, Any], resume_blocked: Optional[str] = None) -> bool:
    """Whether the run can still continue.

    A record with a successor named is never open. Prepared and running runs
    are open; an interrupted run is open unless a block reason is known. The
    reason is the record's own, or the caller's when the record has none yet
    (the phase loop reports a checkpoint-invalid block only in its result)."""
    if record.get("superseded_by"):
        return False
    state = record.get("state")
    if state in ("prepared", "running"):
        return True
    if state == "interrupted":
        return not (record.get("resume_blocked") or resume_blocked)
    return False


def _real_attempts(record: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [
        a for a in record.get("attempts") or []
        if isinstance(a, Mapping) and a.get("state") != "staged"
    ]


def spawned(record: Mapping[str, Any]) -> bool:
    """Whether at least one attempt recorded a child process id."""
    return any(a.get("pid") is not None for a in record.get("attempts") or [] if isinstance(a, Mapping))


def model_priced(document: Any, model_ref: str) -> bool:
    """True only when the compiled document prices the model: a ``cost`` block
    whose ``input`` and ``output`` are non-negative finite numbers."""
    try:
        native, model_id = str(model_ref).split("/", 1)
        cost = document["provider"][native]["models"][model_id]["cost"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if not isinstance(cost, Mapping):
        return False
    for name in ("input", "output"):
        value = cost.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or value < 0:
            return False
    return True


# ---------------------------------------------------------------------------
# usage, price and attribution
# ---------------------------------------------------------------------------

_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
)


def usage_from_events(events: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """Run usage from the sidecar's usage events: the latest revision of each
    native part id. Every field that cannot be known is None with a reason."""
    out: Dict[str, Any] = {name: None for name in _TOKEN_FIELDS}
    out.update(cost=None, tokens=None, reasons={})
    if events is None:
        out["reasons"] = {name: "sidecar-unreadable" for name in (*_TOKEN_FIELDS, "cost", "tokens")}
        return out
    items = events if isinstance(events, (list, tuple)) else list(events)
    usage_events = [e for e in items if e.type is ev.EventType.USAGE]
    totals = ev.usage_totals(usage_events)
    reasons: Dict[str, str] = {}
    for name in _TOKEN_FIELDS:
        out[name] = getattr(totals, name)
    out["cost"] = totals.cost
    parts = (totals.input_tokens, totals.output_tokens, totals.reasoning_tokens)
    out["tokens"] = None if any(p is None for p in parts) else sum(parts)
    why = "no-usage-events" if not usage_events else "field-unreported"
    for name in (*_TOKEN_FIELDS, "cost", "tokens"):
        if out[name] is None:
            reasons[name] = why
    out["reasons"] = reasons
    return out


def _usd_text(cost: Decimal) -> str:
    if cost != cost.quantize(Decimal(1).scaleb(-USD_PLACES)):
        rounded = cost.quantize(Decimal(1).scaleb(-USD_PLACES), rounding=ROUND_HALF_EVEN)
        if cost > 0 and rounded == 0:
            return SMALLEST_USD
        cost = rounded
    text = format(cost, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def priced_cost(usage: Mapping[str, Any], priced: bool) -> Tuple[Optional[str], Optional[str]]:
    """`(usd_text, None)` when the dollar cost is known, else `(None, reason)`."""
    if not priced:
        return None, "cost-unpriced-model"
    raw = usage.get("cost")
    if raw is None:
        return None, "cost-unreported"
    try:
        cost = Decimal(str(raw))
    except InvalidOperation:
        return None, "cost-unreported"
    if not cost.is_finite() or cost < 0:
        return None, "cost-unreported"
    tokens = usage.get("tokens")
    if cost > 0 or tokens == 0:
        return _usd_text(cost), None
    return None, "cost-unpriced-model"


def attribution(usd: Optional[str], tokens: Optional[int]) -> str:
    """The ledger's attribution column: dollars only when known."""
    if usd is not None:
        text = "usd=%s;" % usd + ("tok=%d;" % tokens if tokens is not None else "") + "src=" + SRC_TAG
    elif tokens is not None:
        text = "tok=%d;src=unresolved" % tokens
    else:
        text = "src=unresolved"
    return text if _ATTRIBUTION_RE.match(text) else "src=unresolved"


def note(run_phase: Any, ended_as: str, attempts: int) -> str:
    command = runstore.normalize_phase(run_phase).replace("_", "-")
    outcome = str(ended_as).lower().replace("-", "_")
    return sanitize(
        "runtime=opencode command=quoin-%s outcome=%s attempts=%d scope=%s"
        % (command, outcome, attempts, SCOPE),
        limit=300,
    )


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def _parse_time(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    try:
        return calendar.timegm(time.strptime(value, _TIME_FORMAT))
    except ValueError:
        return None


def _seconds(start: Any, end: Any) -> Tuple[Optional[int], Optional[str]]:
    a, b = _parse_time(start), _parse_time(end)
    if a is None:
        return None, "attempt-not-started"
    if b is None:
        return None, "attempt-not-ended"
    return max(0, b - a), None


def _native_block(events: Optional[Sequence[Any]]) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "session_ids": [], "step_finish_part_ids": [], "part_ids_truncated": False,
        "usage_events": 0, "revised_parts": 0, "event_count": 0,
        "first_sequence": None, "last_sequence": None,
    }
    if events is None:
        return block
    sessions = set()
    part_ids: List[str] = []
    seen = set()
    revised = set()
    usage_events = 0
    for event in events:
        if event.session_id:
            sessions.add(event.session_id)
        if event.type is ev.EventType.USAGE:
            usage_events += 1
            native = event.native
            ident = native.id if native is not None and native.id is not None else None
            if ident is not None:
                if ident not in seen:
                    seen.add(ident)
                    part_ids.append(ident)
                if max(event.revision, native.revision) > 1:
                    revised.add(ident)
    ordered = sorted(sessions)
    block.update(
        session_ids=ordered[:MAX_SESSION_IDS],
        step_finish_part_ids=part_ids[:MAX_PART_IDS],
        part_ids_truncated=len(part_ids) > MAX_PART_IDS or len(ordered) > MAX_SESSION_IDS,
        usage_events=usage_events,
        revised_parts=len(revised),
        event_count=len(events),
        first_sequence=min((e.sequence for e in events), default=None),
        last_sequence=max((e.sequence for e in events), default=None),
    )
    return block


def build_telemetry(
    record: Mapping[str, Any], events: Optional[Sequence[Any]], *, ended_as: str,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """The run's telemetry. Nothing here copies event text, stderr or argv."""
    prepared = record.get("prepared") if isinstance(record.get("prepared"), Mapping) else {}
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    attempts = _real_attempts(record)
    items = (events if isinstance(events, (list, tuple)) else list(events)) if events is not None else None

    def recorded(key: str) -> Any:
        return prepared.get(key)

    provider = recorded("provider")
    effective_model = recorded("effective_model")
    requested = request.get("effort") if request.get("effort") is not None else recorded("effort")
    configured = recorded("configured_effort")
    if "provider" not in prepared:
        configured_reason: Optional[str] = "not-recorded"
    else:
        configured_reason = None if configured is not None else "not-configured"
    elapsed_items = []
    for attempt in attempts:
        seconds, reason = _seconds(attempt.get("started_at"), attempt.get("ended_at"))
        elapsed_items.append({"attempt": attempt.get("attempt"), "seconds": seconds, "reason": reason})
    known = [i["seconds"] for i in elapsed_items]
    total = None if not known or any(s is None for s in known) else sum(known)
    wall = None
    if attempts:
        wall, _why = _seconds(attempts[0].get("started_at"), attempts[-1].get("ended_at"))
    failed_retried = sum(1 for a in attempts[:-1] if a.get("state") == "failed")
    relaunched = sum(1 for a in attempts[:-1] if a.get("state") == "interrupted")

    usage = usage_from_events(items)
    priced = bool(recorded("model_priced"))
    usd, usd_reason = priced_cost(usage, priced)
    return {
        "schema": TELEMETRY_SCHEMA,
        "final": True,
        "ended_as": str(ended_as).lower().replace("-", "_"),
        "recorded_at": time.strftime(_TIME_FORMAT, time.gmtime(clock())),
        "provider": {
            "id": provider,
            "native_id": recorded("native_provider"),
            "reason": None if provider is not None else "not-recorded",
        },
        "model": {
            "effective": effective_model,
            "reason": None if effective_model is not None else "not-recorded",
        },
        "effort": {
            "requested": requested,
            "requested_reason": None if requested is not None else "not-requested",
            "configured": configured,
            "configured_reason": configured_reason,
            "variant": recorded("variant"),
            "effective": "unknown",
            "effective_reason": "not-visible-in-stream",
        },
        "elapsed": {"attempts": elapsed_items, "total_seconds": total, "wall_seconds": wall},
        "retries": {
            "attempts": len(attempts),
            "failed_attempts_retried": failed_retried,
            "interrupted_relaunches": relaunched,
        },
        "native": _native_block(items),
        "usage": usage,
        "cost": {"usd": usd, "usd_reason": usd_reason, "priced": priced},
        "usage_provenance": {
            "source": "step_finish",
            "session_scope": "run-session-only",
            "dedup": "part-id-latest-revision",
            "child_usage": None,
            "child_usage_reason": "child-events-filtered",
            "parent_cost_includes_children": False,
            "citations": list(PROVENANCE_CITATIONS),
        },
        "unavailable": ["child-session-usage", "effective-effort"],
    }


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def ledger_path(project_root: Any, task: str) -> Path:
    """The task's cost ledger, always at the task root (also for staged tasks)."""
    return Path(project_root) / ".workflow_artifacts" / runstore.check_task_name(task) / LEDGER_NAME


def task_folder_present(project_root: Any, task: str) -> bool:
    try:
        info = os.lstat(str(Path(project_root) / ".workflow_artifacts" / runstore.check_task_name(task)))
    except (OSError, runstore.RunStoreError):
        return False
    return stat.S_ISDIR(info.st_mode)


def _now_text(clock: Callable[[], float]) -> str:
    return time.strftime(_TIME_FORMAT, time.gmtime(clock()))


def _read_ledger(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """`(bytes, None)`, `(None, "absent")`, or `(None, reason)`; never raises."""
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "ledger-unreadable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "ledger-unsafe"
    if info.st_size > MAX_LEDGER_BYTES:
        return None, "ledger-too-large"
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None, "ledger-unreadable"
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            return None, "ledger-unsafe"
    except OSError:
        os.close(fd)
        return None, "ledger-unreadable"
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_LEDGER_BYTES + 1)
    except OSError:
        return None, "ledger-unreadable"
    if len(data) > MAX_LEDGER_BYTES:
        return None, "ledger-too-large"
    return data, None


def ledger_mark(project_root: Any, task: str, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """The ledger's size and digest before a run. Never raises."""
    taken = _now_text(clock)
    try:
        if not task_folder_present(project_root, task):
            return {"exists": False, "size": 0, "sha256": None, "taken_at": taken}
        data, reason = _read_ledger(ledger_path(project_root, task))
    except Exception:  # noqa: BLE001 - a mark is advisory
        return {"exists": None, "reason": "ledger-unreadable", "taken_at": taken}
    if reason == "absent":
        return {"exists": False, "size": 0, "sha256": None, "taken_at": taken}
    if data is None:
        return {"exists": None, "reason": reason, "taken_at": taken}
    return {"exists": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "taken_at": taken}


@dataclass
class Scan:
    has_row: Optional[bool] = False
    prefix: str = "unavailable"
    prefix_reason: Optional[str] = None
    appended: List[Dict[str, str]] = field(default_factory=list)
    appended_total: int = 0


def _fields(raw: bytes) -> Optional[Tuple[str, str]]:
    text = raw.decode("utf-8", "replace").strip()
    if not text or text.startswith("#"):
        return None
    parts = [p.strip() for p in text.split("|")]
    return parts[0], parts[2] if len(parts) > 2 else ""


def scan(path: Any, mark: Optional[Mapping[str, Any]], run_id: str, own_uuids: Iterable[str] = ()) -> Scan:
    """Compare the ledger with its mark and look for the run's own row."""
    result = Scan()
    data, reason = _read_ledger(Path(path))
    own = set(own_uuids) | {run_id}
    usable = isinstance(mark, Mapping) and mark.get("exists") in (True, False)
    if reason not in (None, "absent"):
        result.has_row = None if reason == "ledger-too-large" else False
        if usable and reason in ("ledger-unsafe", "ledger-too-large"):
            # A usable mark and a ledger that is now a link, a special file or
            # past the cap: the file was replaced, which is a rewrite.
            result.prefix, result.prefix_reason = "changed", reason + "-after-run"
        else:
            result.prefix, result.prefix_reason = "unavailable", reason
        return result
    if data is None:
        result.has_row = False
        if not usable:
            result.prefix, result.prefix_reason = "unavailable", "no-mark"
        elif mark.get("exists") is True:
            result.prefix, result.prefix_reason = "changed", "ledger-vanished"
        else:
            result.prefix = "unchanged"
        return result
    start = 0
    if not usable:
        result.prefix, result.prefix_reason = "unavailable", "no-mark"
    elif mark.get("exists") is False:
        result.prefix = "unchanged"
    else:
        size = mark.get("size")
        if not isinstance(size, int) or len(data) < size:
            result.prefix, result.prefix_reason = "changed", "ledger-shrank"
        elif hashlib.sha256(data[:size]).hexdigest() != mark.get("sha256"):
            result.prefix, result.prefix_reason = "changed", "prefix-differs"
            start = size
        else:
            result.prefix = "unchanged"
            start = size
    if result.prefix_reason == "ledger-shrank":
        start = len(data)
    lines = data.split(b"\n")
    offset = 0
    for raw in lines:
        parsed = _fields(raw)
        line_start = offset
        offset += len(raw) + 1
        if parsed is None:
            continue
        uuid, phase = parsed
        if uuid == run_id:
            result.has_row = True
        if line_start >= start and uuid not in own and result.prefix != "unavailable":
            result.appended_total += 1
            if len(result.appended) < MAX_APPENDED_LISTED:
                result.appended.append({"uuid": sanitize(uuid, 120), "phase": sanitize(phase, 60)})
    return result


def append_row(project_root: Any, task: str, line: str) -> str:
    """Append one row; `written`, or `ledger-unsafe` for a symlink or a
    non-regular file. A missing ledger is created with the standard header."""
    path = ledger_path(project_root, task)
    flags_nofollow = getattr(os, "O_NOFOLLOW", 0)
    for _ in range(2):
        try:
            info = os.lstat(str(path))
        except FileNotFoundError:
            info = None
        if info is None:
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags_nofollow, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(("# Cost Ledger — %s\n%s\n" % (task, line)).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            return "written"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return "ledger-unsafe"
        try:
            fd = os.open(
                str(path), os.O_RDWR | os.O_APPEND | flags_nofollow | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return "ledger-unsafe"
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return "ledger-unsafe"
            prefix = ""
            if opened.st_size > 0 and os.pread(fd, 1, opened.st_size - 1) != b"\n":
                prefix = "\n"
            os.write(fd, (prefix + line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return "written"
    return "ledger-unsafe"


def _ledger_date(record: Mapping[str, Any], clock: Callable[[], float]) -> str:
    attempts = _real_attempts(record)
    stamp = _parse_time(attempts[-1].get("ended_at")) if attempts else None
    return time.strftime("%Y-%m-%d", time.gmtime(stamp if stamp is not None else clock()))


_CORE_MODULES: Dict[str, Any] = {}


def load_cost_event(source_dir: Any):
    """The portable cost-event module, loaded by file path and registered under
    a private name (its dataclass needs its module to be importable while the
    class is built). `GateRefused("source-unavailable")` when it cannot load."""
    path = Path(source_dir) / "core" / "scripts" / "cost_event.py"
    key = str(path.resolve())
    module = _CORE_MODULES.get(key)
    if module is not None:
        return module
    name = "_quoin_cost_event_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise gate.GateRefused("source-unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError):
        sys.modules.pop(name, None)
        raise gate.GateRefused("source-unavailable") from None
    finally:
        sys.dont_write_bytecode = old
    _CORE_MODULES[key] = module
    return module


def _build_row(
    source_dir: Any, *, run_id: str, run_phase: Any, model: Any, date: str,
    note_text: str, attribution_text: str,
) -> Tuple[Optional[str], Optional[str]]:
    phase = ledger_phase(run_phase)
    if phase is None:
        return None, "phase-unmapped"
    core = load_cost_event(source_dir)
    event = core.CostEvent(
        uuid=run_id, date=date, phase=phase, model_or_effort=sanitize(model), category="task",
        note=note_text, fallback_fires=0, attribution=attribution_text,
    )
    line = core.format_row(event)
    parsed = core.parse_row(line)
    if (
        parsed is None or parsed.uuid != run_id or parsed.phase != phase
        or parsed.attribution != attribution_text or "\n" in line
    ):
        return None, "row-roundtrip-failed"
    return line, None


def format_line(
    source_dir: Any, record: Mapping[str, Any], telemetry: Mapping[str, Any], ended_as: str,
    clock: Callable[[], float] = time.time,
) -> Tuple[Optional[str], Optional[str]]:
    """`(row_text, None)` or `(None, reason)`. The row is built with the core
    cost-event formatter and re-parsed before it may be written."""
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    usd = (telemetry.get("cost") or {}).get("usd")
    tokens = (telemetry.get("usage") or {}).get("tokens")
    attempts = (telemetry.get("retries") or {}).get("attempts")
    if not isinstance(attempts, int):
        attempts = len(_real_attempts(record))
    return _build_row(
        source_dir, run_id=record["run_id"], run_phase=request.get("phase"),
        model=(telemetry.get("model") or {}).get("effective") or "unknown",
        date=_ledger_date(record, clock),
        note_text=note(request.get("phase"), ended_as, attempts),
        attribution_text=attribution(usd, tokens),
    )


# ---------------------------------------------------------------------------
# recording a run
# ---------------------------------------------------------------------------


@dataclass
class CostOutcome:
    run_id: str
    row: str = "skipped"
    row_reason: Optional[str] = None
    prefix: str = "unavailable"
    appended: List[Dict[str, str]] = field(default_factory=list)
    violation: bool = False
    telemetry: Optional[Dict[str, Any]] = None
    closed: bool = False


def _outcome_from_stored(run_id: str, telemetry: Mapping[str, Any]) -> CostOutcome:
    ledger = telemetry.get("ledger") if isinstance(telemetry.get("ledger"), Mapping) else {}
    prefix = str(ledger.get("prefix") or "unavailable")
    return CostOutcome(
        run_id=run_id, row=str(ledger.get("row") or "skipped"), row_reason=ledger.get("row_reason"),
        prefix=prefix, appended=list(ledger.get("ledger_lines_appended_during_run") or []),
        violation=prefix == "changed", telemetry=dict(telemetry), closed=True,
    )


def _closed_hint(successor: str) -> str:
    return "this run was closed and costed when run %s took its place; there is nothing to resume" % successor


def record_run(
    project_root: Any, run_id: str, *, source_dir: Any, mark: Optional[Mapping[str, Any]] = None,
    ended_as: str, superseded_by: Optional[str] = None, resume_blocked: Optional[str] = None,
    own_uuids: Iterable[str] = (), clock: Callable[[], float] = time.time,
) -> CostOutcome:
    """Cost a run that can no longer continue: final telemetry in the record
    and one ledger row. The caller holds the task lock. Never raises."""
    try:
        return _record_run(
            project_root, run_id, source_dir=source_dir, mark=mark, ended_as=ended_as,
            superseded_by=superseded_by, resume_blocked=resume_blocked,
            own_uuids=tuple(own_uuids), clock=clock,
        )
    except Exception as exc:  # noqa: BLE001 - costing must never break the caller
        return CostOutcome(run_id=run_id, row="skipped", row_reason="error-" + type(exc).__name__)


def _record_run(
    project_root, run_id, *, source_dir, mark, ended_as, superseded_by, resume_blocked, own_uuids, clock,
) -> CostOutcome:
    directory = runstore.inspect_store(project_root)
    record = runstore.load_record(directory, run_id) if directory is not None else None
    if record is None:
        return CostOutcome(run_id=run_id, row="skipped", row_reason="record-missing")
    stored = record.get("telemetry")
    if isinstance(stored, Mapping) and stored.get("final") is True:
        return _outcome_from_stored(run_id, stored)
    closing = ended_as == "superseded"
    if closing and (not superseded_by or superseded_by == run_id):
        return CostOutcome(run_id=run_id, row="skipped", row_reason="superseded-without-successor")
    if not closing and is_open(record, resume_blocked):
        return CostOutcome(run_id=run_id, row="skipped", row_reason="run-open")

    task = record.get("task")
    try:
        sidecar_path = runstore.run_paths(directory, run_id).sidecar
        try:
            too_big = os.stat(str(sidecar_path)).st_size > runstore.DEFAULT_MAX_FILE_BYTES
        except OSError:
            too_big = False
        events: Optional[Sequence[Any]] = None if too_big else runstore.read_sidecar(sidecar_path).events
    except Exception:  # noqa: BLE001
        events = None
    telemetry = build_telemetry(record, events, ended_as=ended_as, clock=clock)
    effective_mark = record.get("ledger_mark") if record.get("ledger_mark") is not None else mark
    ledger: Dict[str, Any] = {
        "mark": effective_mark, "row": "skipped", "row_reason": None, "prefix": "unavailable",
        "prefix_reason": None, "ledger_lines_appended_during_run": [], "appended_total": 0,
        "uuid": run_id,
    }
    violation = False
    if not spawned(record):
        ledger["row_reason"] = "never-spawned"
        ledger["prefix_reason"] = "never-spawned"
    elif not task_folder_present(project_root, task):
        ledger["row_reason"] = "task-folder-missing"
        ledger["prefix_reason"] = "task-folder-missing"
    else:
        result = scan(ledger_path(project_root, task), effective_mark, run_id, own_uuids)
        ledger.update(
            prefix=result.prefix, prefix_reason=result.prefix_reason,
            ledger_lines_appended_during_run=result.appended, appended_total=result.appended_total,
        )
        violation = result.prefix == "changed"
        if result.has_row is None:
            ledger["row_reason"] = "ledger-too-large"
        elif result.has_row:
            ledger["row"] = "present"
        elif source_dir is None:
            ledger["row_reason"] = "source-unavailable"
        else:
            try:
                line, reason = format_line(source_dir, record, telemetry, ended_as, clock)
                if line is None:
                    ledger["row_reason"] = reason
                else:
                    status = append_row(project_root, task, line)
                    if status == "written":
                        ledger["row"] = "written"
                    else:
                        ledger["row_reason"] = status
            except gate.GateRefused:
                ledger["row_reason"] = "source-unavailable"
            except OSError as exc:
                ledger["row_reason"] = "ledger-write-" + type(exc).__name__
    telemetry["ledger"] = ledger
    if closing:
        record["superseded_by"] = superseded_by
        if not record.get("resume_blocked"):
            record["resume_blocked"] = "superseded"
        record["resume_hint"] = _closed_hint(str(superseded_by))
    elif resume_blocked and not record.get("resume_blocked"):
        record["resume_blocked"] = resume_blocked
    record["telemetry"] = telemetry
    runstore.write_record(directory, record)
    return CostOutcome(
        run_id=run_id, row=ledger["row"], row_reason=ledger["row_reason"], prefix=ledger["prefix"],
        appended=list(ledger["ledger_lines_appended_during_run"]), violation=violation,
        telemetry=telemetry, closed=True,
    )


def store_mark(project_root: Any, run_id: str, mark: Optional[Mapping[str, Any]]) -> None:
    """Keep the first ledger mark a run is seen with. Never raises."""
    try:
        directory = runstore.inspect_store(project_root)
        record = runstore.load_record(directory, run_id) if directory is not None else None
        if record is None or record.get("ledger_mark") is not None or mark is None:
            return
        record["ledger_mark"] = dict(mark)
        runstore.write_record(directory, record)
    except Exception:  # noqa: BLE001
        return


def prelaunch_row(
    project_root: Any, prepared: Any, *, source_dir: Any, mark: Optional[Mapping[str, Any]],
    clock: Callable[[], float] = time.time,
) -> None:
    """Write the end-of-task row before the child starts: the task folder may be
    archived by the run itself, after which no ledger can be appended to."""
    request = prepared.request
    if runstore.normalize_phase(request.phase) != "end_of_task":
        return
    run_id, task = prepared.run_id, request.task
    outcome: Dict[str, Any] = {"row": "skipped", "row_reason": None}
    try:
        store_mark(project_root, run_id, mark)
        if not task_folder_present(project_root, task):
            outcome["row_reason"] = "task-folder-missing"
        elif source_dir is None:
            outcome["row_reason"] = "source-unavailable"
        else:
            found = scan(ledger_path(project_root, task), mark, run_id)
            if found.has_row is None:
                outcome["row_reason"] = "ledger-too-large"
            elif found.has_row:
                outcome["row"] = "present"
            else:
                line, reason = _build_row(
                    source_dir, run_id=run_id, run_phase=request.phase, model=prepared.effective_model,
                    date=time.strftime("%Y-%m-%d", time.gmtime(clock())),
                    note_text=note(request.phase, "launched", 1), attribution_text=attribution(None, None),
                )
                if line is None:
                    outcome["row_reason"] = reason
                else:
                    status = append_row(project_root, task, line)
                    if status == "written":
                        outcome["row"] = "written"
                    else:
                        outcome["row_reason"] = status
    except Exception as exc:  # noqa: BLE001 - the launch goes on without a row
        outcome["row_reason"] = "error-" + type(exc).__name__
    try:
        directory = runstore.inspect_store(project_root)
        record = runstore.load_record(directory, run_id) if directory is not None else None
        if record is not None:
            record["prelaunch_row"] = outcome
            runstore.write_record(directory, record)
    except Exception:  # noqa: BLE001
        return
