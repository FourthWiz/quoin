#!/usr/bin/env python3
"""auto_resume.py — run-continuation gate for interrupted autonomous `/run` spans.

Owns every decision about whether an interrupted `quoin run --autonomous`
span should continue: in-session (via the `Stop` hook, when the owning
session is still open) or across sessions (via a detached `quoin run
--autonomous` hand-off, when the owner is provably gone). One helper owns
every sentinel filename and every knob so hooks and `/run` stay thin and
token-clean.

Always exits 0 (fail-open); empty stdout means "do nothing". Stdlib only,
Python 3.8-safe (`from __future__ import annotations`, no runtime PEP-604
union, no walrus in comprehensions, no `str.removeprefix`).

Subcommands
-----------
- ``stop``    — reads the Stop-hook JSON payload from stdin; continues the
  owning session or hands off, subject to the shared gate.
- ``start``   — called from `SessionStart` (`startup`/`resume` only); hands
  off to a detached supervisor when the previous owner is provably gone.
- ``arm``     — called by `/run` at autonomous entry and Resume Step 0;
  writes the per-session consent record the Stop hook requires.
- ``handoff`` — spawns a detached `quoin run --autonomous` supervisor,
  subject to the same gate and budget as ``stop``/``start``.
- ``pause``   — the in-session "stop the run" verb: writes a halt sentinel
  and removes the arm.
- ``status``  — prints the counter, lock, arm and owner-liveness state.
- ``cli-check`` — read-only: prints the interpreter resolver's verdict
  (install record vs legacy PATH lookup) as one JSON line.

State files (all under ``.workflow_artifacts/memory/``, outside the task
folder so they survive `/end_of_task`'s archival move):
``auto-resume-{task}.json`` (continuation counter), ``run-supervisor-
{task}.pid``/``.result``/``.log`` (supervisor lock and outcome),
``run-continue-arm-{session_id}.txt`` (per-session consent),
``session-ended-{session_id}.txt`` (positive "owner gone" evidence).
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Sibling import: quoin/core/scripts/run_state.py (same directory) for
# append_note(), the escape-free sanitizer, and the task-name validator.
# ---------------------------------------------------------------------------
_RUN_STATE_PATH = Path(__file__).resolve().parent / "run_state.py"
_RS_SPEC = importlib.util.spec_from_file_location(
    "_quoin_auto_resume_run_state", _RUN_STATE_PATH
)
assert _RS_SPEC is not None
_run_state = importlib.util.module_from_spec(_RS_SPEC)
assert _RS_SPEC.loader is not None
sys.modules["_quoin_auto_resume_run_state"] = _run_state
_RS_SPEC.loader.exec_module(_run_state)

append_note = _run_state.append_note

# ---------------------------------------------------------------------------
# Sentinel filename templates. Mirrors ``quoin.supervisor``'s templates for
# the four it already owns (marker/progress-dir/done/halt) and
# ``COMPLETION_GLOB_TEMPLATE`` byte-for-byte — a parity test pins the
# equality since this module cannot import ``src/quoin`` (it must stand
# alone under a bare system Python 3.8 deploy).
# ---------------------------------------------------------------------------
MARKER_TEMPLATE = "autonomous-run-{task}.marker"
PROGRESS_DIR_TEMPLATE = "autonomous-progress-{task}"
COMPLETION_GLOB_TEMPLATE = "autonomous-progress-{task}/*.done"
DONE_TEMPLATE = "autonomous-done-{task}.md"
HALT_TEMPLATE = "autonomous-halt-{task}.md"
NEEDS_DECISION_TEMPLATE = "needs-decision-{task}.md"
RECORD_TEMPLATE = "run-state-{task}.json"
COUNTER_TEMPLATE = "auto-resume-{task}.json"
LOCK_TEMPLATE = "run-supervisor-{task}.pid"
RESULT_TEMPLATE = "run-supervisor-{task}.result"
LOG_TEMPLATE = "run-supervisor-{task}.log"
ARM_TEMPLATE = "run-continue-arm-{sid}.txt"
ENDED_TEMPLATE = "session-ended-{sid}.txt"
CONSENT_TEMPLATE = "run-continue-consent-{sid}.txt"
COMPACT_TEMPLATE = "compact-happened-{sid}.txt"
ERROR_LOG_TEMPLATE = "auto-resume-errors.log"

COUNTER_SCHEMA = 1
_ENDED_MARKER_PRUNE_DAYS = 7
_TREE_ENTRY_CAP = 2000

_SID_RE = re.compile(r"^[A-Za-z0-9-]+$")
_STDIN_CAP_BYTES = 65536


# ---------------------------------------------------------------------------
# Test seams (patched by unit tests; never called with side-effecting state
# baked in at import time).
# ---------------------------------------------------------------------------


def _now() -> float:
    import time

    return time.time()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _which(name: str):
    return shutil.which(name)


def _popen(argv, **kwargs):
    return subprocess.Popen(argv, **kwargs)


def _home() -> Path:
    raw = os.environ.get("HOME")
    if raw:
        return Path(raw)
    return Path.home()


def _env(name: str, default=None):
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# Knobs — read once per call, invalid values fall back to the default.
# ---------------------------------------------------------------------------


def _clamp_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _auto_resume_enabled() -> bool:
    return _env("QUOIN_AUTO_RESUME", "") != "0"


def _max_attempts() -> int:
    return _clamp_int("QUOIN_AUTO_RESUME_MAX", 10, 1, 100)


def _idle_secs() -> int:
    return _clamp_int("QUOIN_AUTO_RESUME_IDLE_SECS", 900, 60, 10 ** 9)


def _handoff_at() -> int:
    return _clamp_int("QUOIN_AUTO_RESUME_HANDOFF_AT", 6, 1, 7)


def _stale_days() -> int:
    return _run_state._stale_days_default()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _valid_sid(sid) -> bool:
    return isinstance(sid, str) and bool(sid) and bool(_SID_RE.match(sid)) and ".." not in sid


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _memory_dir(project_root) -> Path:
    return Path(project_root) / ".workflow_artifacts" / "memory"


def _atomic_write_text(memory_dir: Path, prefix: str, path: Path, content: str) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(memory_dir), prefix=prefix, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Marker / record readers
# ---------------------------------------------------------------------------


def _iter_markers(memory_dir: Path):
    try:
        return sorted(memory_dir.glob(MARKER_TEMPLATE.format(task="*")))
    except OSError:
        return []


def _task_from_marker_path(path: Path) -> str:
    name = path.name
    prefix, suffix = "autonomous-run-", ".marker"
    return name[len(prefix): -len(suffix)]


def _load_kv_text(path: Path) -> dict:
    """Parses `field: value` text files (markers, arm records)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    data = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data


def _load_marker(path: Path):
    """Markers are `field: value` text (see run/SKILL.md), not JSON."""
    data = _load_kv_text(path)
    if "timestamp" not in data:
        return None
    return data


def _record_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / RECORD_TEMPLATE.format(task=task)


def _load_record(memory_dir: Path, task: str):
    return _run_state._load_record(_record_path(memory_dir, task))


def _sentinel_exists(memory_dir: Path, template: str, task: str) -> bool:
    return (memory_dir / template.format(task=task)).exists()


def _count_done(memory_dir: Path, task: str) -> int:
    progress_dir = memory_dir / PROGRESS_DIR_TEMPLATE.format(task=task)
    if not progress_dir.is_dir():
        return 0
    try:
        return len(list(progress_dir.glob("*.done")))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Continuation counter
# ---------------------------------------------------------------------------


def _counter_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / COUNTER_TEMPLATE.format(task=task)


def _default_counter(task: str, marker_timestamp) -> dict:
    return {
        "schema": COUNTER_SCHEMA,
        "task": task,
        "marker_timestamp": marker_timestamp,
        "attempts": 0,
        "consecutive_no_progress": 0,
        "last_done_count": 0,
        "last_phase": None,
        "chain_blocks": 0,
        "last_session_id": "",
        "in_flight": False,
        "last_reason": "",
        "updated_at": _iso_now(),
    }


def _load_counter(memory_dir: Path, task: str):
    data = _load_json(_counter_path(memory_dir, task))
    if data is None:
        return None
    schema = data.get("schema")
    if not isinstance(schema, int) or schema > COUNTER_SCHEMA:
        return None
    return data


def _get_or_reset_counter(memory_dir: Path, task: str, marker_timestamp):
    """Load the durable counter, initializing a fresh one only when none
    exists yet. `marker_timestamp` is carried for a first-write audit trail
    only — a marker rewrite (which happens on every autonomous entry, see
    run SKILL.md) must never reset the budget on its own. The only path
    that resets `attempts` is `arm`, and only on a consumed consent stamp
    (see `_cmd_arm`)."""
    counter = _load_counter(memory_dir, task)
    if counter is None:
        return _default_counter(task, marker_timestamp)
    return counter


def _write_counter(memory_dir: Path, task: str, counter: dict) -> None:
    counter["updated_at"] = _iso_now()
    content = json.dumps(counter, sort_keys=True) + "\n"
    _atomic_write_text(memory_dir, f"{COUNTER_TEMPLATE.format(task=task)}.", _counter_path(memory_dir, task), content)


# ---------------------------------------------------------------------------
# Supervisor lock / result (auto_resume.py's side: `handoff` pre-writes the
# lock; `cli.py --halt-on-abort` (T-04) owns removing it and writing the
# result under normal operation).
# ---------------------------------------------------------------------------


def _lock_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / LOCK_TEMPLATE.format(task=task)


def _result_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / RESULT_TEMPLATE.format(task=task)


def _supervisor_lock_live(memory_dir: Path, task: str) -> bool:
    data = _load_json(_lock_path(memory_dir, task))
    if data is None:
        return False
    try:
        pid = int(data.get("pid", -1))
    except (TypeError, ValueError):
        return False
    return pid > 0 and _pid_alive(pid)


def _write_lock(
    memory_dir: Path, task: str, pid: int, granted: int, writer: str, token: "str | None" = None
) -> None:
    data = {"pid": pid, "started_at": _iso_now(), "granted": granted, "writer": writer}
    if token:
        data["token"] = token
    content = json.dumps(data, sort_keys=True) + "\n"
    _atomic_write_text(memory_dir, f"{LOCK_TEMPLATE.format(task=task)}.", _lock_path(memory_dir, task), content)


def _create_lock_exclusive(lock_path: Path, payload: bytes) -> bool:
    """Creates `lock_path` atomically and already fully populated with
    `payload`, so no reader can ever observe a lock file that exists but
    is still empty. The previous O_CREAT|O_EXCL-then-`os.write` split left
    exactly that window: a lock created but not yet written parsed as None,
    which a racing reader treated as "not live" and unlinked out from under
    the winner.

    `payload` is written in full to a private tempfile in the same
    directory first, then published under `lock_path` with `os.link` —
    `os.link` fails with `FileExistsError` if `lock_path` already exists,
    so at most one caller can ever win, and the file it publishes is
    always the fully-written one (never a half-written one)."""
    tmp_path = lock_path.parent / f".{lock_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(str(tmp_path), str(lock_path))
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass


_STALE_UNPARSEABLE_LOCK_SECS = 5.0


def _lock_is_stale(lock_path: Path) -> bool:
    """A lock is reclaimable only when its owner is provably gone: a
    parseable lock naming a dead pid, or a lock that still fails to parse
    well past the atomic creator's own write latency. A lock that fails to
    parse but is still fresh is never treated as stale on emptiness alone
    — that was the regression this closes (round-1's O_CREAT|O_EXCL-then-
    write split could leave a lock in exactly that state, and a racing
    reader unlinked it out from under an in-flight winner). With the
    atomic creator above, a lock only fails to parse now if it predates
    this fix or was corrupted externally, so treating an old-enough
    unparseable lock as stale keeps that recovery path without reopening
    the race."""
    data = _load_json(lock_path)
    if data is not None:
        try:
            pid = int(data.get("pid", -1))
        except (TypeError, ValueError):
            return True
        return not (pid > 0 and _pid_alive(pid))
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age > _STALE_UNPARSEABLE_LOCK_SECS


def _claim_lock_for_removal(lock_path: Path):
    """Atomically takes exclusive ownership of `lock_path` for removal and
    returns the JSON content it held (or `None` if it had none, or if
    another racer already claimed/removed it first).

    `os.rename` within the same directory is atomic on POSIX: at most one
    caller's rename against the same source name can ever succeed. A
    second caller's rename fails with `FileNotFoundError` because the
    first caller already moved it away, so the two can never both charge
    the same crashed supervisor's grant or both believe they cleared the
    path for a fresh create."""
    claim_path = lock_path.parent / f"{lock_path.name}.stale-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        os.rename(str(lock_path), str(claim_path))
    except OSError:
        return None
    try:
        data = _load_json(claim_path)
    finally:
        try:
            claim_path.unlink()
        except OSError:
            pass
    return data


def settle_supervisor(memory_dir: Path, task: str, counter: dict) -> dict:
    """Consume a finished supervisor's `.result`, or charge a crashed
    supervisor's full grant when its lock names a dead pid (D-19).

    The dead-lock charge-and-remove is claimed with `_claim_lock_for_removal`
    before it is charged, not read-then-unlinked as two separate steps: two
    processes racing to settle the same dead-pid lock could otherwise both
    read it before either removed it and both charge its grant. Only the
    caller whose atomic rename actually wins reads back real content and
    charges anything; the loser's rename fails and it charges nothing."""
    result_path = _result_path(memory_dir, task)
    result = _load_json(result_path)
    if result is not None:
        try:
            relaunches = int(result.get("relaunches", 0))
        except (TypeError, ValueError):
            relaunches = 0
        counter["attempts"] = counter.get("attempts", 0) + relaunches
        try:
            result_path.unlink()
        except OSError:
            pass
        return counter
    lock_path = _lock_path(memory_dir, task)
    lock = _load_json(lock_path)
    if lock is not None:
        try:
            pid = int(lock.get("pid", -1))
        except (TypeError, ValueError):
            pid = -1
        if pid > 0 and not _pid_alive(pid):
            claimed = _claim_lock_for_removal(lock_path)
            if claimed is not None:
                try:
                    claimed_pid = int(claimed.get("pid", -1))
                except (TypeError, ValueError):
                    claimed_pid = -1
                if claimed_pid == pid:
                    try:
                        granted = int(claimed.get("granted", 0) or 0)
                    except (TypeError, ValueError):
                        granted = 0
                    counter["attempts"] = counter.get("attempts", 0) + granted
    return counter


# ---------------------------------------------------------------------------
# Owner liveness (D-16) — UUID glob, never slug derivation.
# ---------------------------------------------------------------------------


def _owner_transcript_matches(sid: str):
    projects_dir = _home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    try:
        return glob.glob(str(projects_dir / "*" / f"{sid}.jsonl"))
    except OSError:
        return []


def owner_state(sid, memory_dir: "Path | None" = None) -> str:
    """Return ``gone`` / ``live`` / ``unknown`` for session ``sid``. `gone`
    requires positive evidence (an ended marker, or a transcript idle at
    least `QUOIN_AUTO_RESUME_IDLE_SECS`); both `live` and `unknown` refuse a
    hand-off (fail-safe against double-driving the checkout, D-16)."""
    if not _valid_sid(sid):
        return "unknown"
    if memory_dir is not None and (memory_dir / ENDED_TEMPLATE.format(sid=sid)).exists():
        return "gone"
    matches = _owner_transcript_matches(sid)
    max_mtime = None
    if len(matches) == 1:
        try:
            max_mtime = os.stat(matches[0]).st_mtime
        except OSError:
            max_mtime = None
        session_dir = Path(matches[0]).parent / sid
        if session_dir.is_dir():
            count = 0
            capped = False
            try:
                for root, _dirs, files in os.walk(str(session_dir)):
                    for fname in files:
                        count += 1
                        if count > _TREE_ENTRY_CAP:
                            capped = True
                            break
                        try:
                            mtime = os.stat(os.path.join(root, fname)).st_mtime
                        except OSError:
                            continue
                        if max_mtime is None or mtime > max_mtime:
                            max_mtime = mtime
                    if capped:
                        break
            except OSError:
                pass
            if capped:
                return "unknown"
    if max_mtime is None:
        return "unknown"
    age = _now() - max_mtime
    if age >= _idle_secs():
        return "gone"
    return "live"


def _prune_ended_markers(memory_dir: Path) -> None:
    """Prunes both `session-ended-*` markers and stale `run-continue-
    consent-*` stamps — a consent stamp a session never spends (e.g. the
    session crashed before its next `arm`) would otherwise accumulate
    forever."""
    try:
        cutoff = _now() - _ENDED_MARKER_PRUNE_DAYS * 86400
        for template in (ENDED_TEMPLATE, CONSENT_TEMPLATE):
            for path in memory_dir.glob(template.format(sid="*")):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Halt / notice writers
# ---------------------------------------------------------------------------


def _write_halt(memory_dir: Path, task: str, record, reason: str) -> None:
    record = record or {}
    phase = record.get("phase", "") or ""
    resume_hint = record.get("resume_command") or f"/run --resume {task}"
    content = (
        f"task: {task}\n"
        f"phase: {phase}\n"
        f"reason: {reason}\n"
        f"timestamp: {_iso_now()}\n"
        f"resume_hint: {resume_hint}\n"
    )
    path = memory_dir / HALT_TEMPLATE.format(task=task)
    _atomic_write_text(memory_dir, f"{HALT_TEMPLATE.format(task=task)}.", path, content)


def _notice_line(task: str, record, reason: str, attempt: int, cap: int, via: str) -> str:
    phase = (record or {}).get("phase", "") or ""
    return f"[quoin-auto-resume] task={task} phase={phase} reason={reason} attempt={attempt}/{cap} via={via}"


def _reason_text(task: str, resume_command: str) -> str:
    return (
        f"[quoin-auto-resume] The interrupted run for {task} must continue now. "
        "Invoke the run skill with the arguments of this exact command and do "
        f"not ask the user or stop until it reaches a hard stop: {resume_command}"
    )


# ---------------------------------------------------------------------------
# The shared gate (architecture "The gate" section) — order pinned.
# ---------------------------------------------------------------------------


def _select_candidates(memory_dir: Path, mode: str, session_id):
    """Ordered (task, marker, record) candidates, newest `updated_at` first."""
    out = []
    for marker_path in _iter_markers(memory_dir):
        task = _task_from_marker_path(marker_path)
        if not _run_state._valid_task(task):
            continue
        marker = _load_marker(marker_path)
        if marker is None:
            continue
        record = _load_record(memory_dir, task)
        if record is None:
            continue
        rec_sid = record.get("session_id", "") or ""
        if mode == "stop":
            if rec_sid != session_id:
                continue
            if not (memory_dir / ARM_TEMPLATE.format(sid=rec_sid)).exists():
                continue
        elif mode == "start":
            if rec_sid == session_id:
                continue
        out.append((task, marker, record))
    out.sort(key=lambda item: item[2].get("updated_at", "") or "", reverse=True)
    return out


def _evaluate_gate(memory_dir: Path, mode: str, candidates):
    """Runs the shared gate order over `candidates`. Returns a dict:
    ``{"action": "none"}``, ``{"action": "halt", "task", "reason"}`` (halt
    already written), or ``{"action": "candidate", "task", "record",
    "counter", "progressed", "done_now", "cur_phase"}`` (counter NOT yet
    persisted — the caller finishes the mutation and writes it)."""
    for task, marker, record in candidates:
        if record.get("active") is not True:
            continue
        updated_at = _parse_iso(record.get("updated_at"))
        if updated_at is None:
            continue
        age_days = (datetime.now(tz=timezone.utc) - updated_at).total_seconds() / 86400.0
        if age_days > _stale_days():
            continue
        if _sentinel_exists(memory_dir, DONE_TEMPLATE, task):
            continue
        if _sentinel_exists(memory_dir, HALT_TEMPLATE, task):
            continue
        if _sentinel_exists(memory_dir, NEEDS_DECISION_TEMPLATE, task):
            continue
        counter = _get_or_reset_counter(memory_dir, task, marker.get("timestamp"))
        counter = settle_supervisor(memory_dir, task, counter)
        if _supervisor_lock_live(memory_dir, task):
            continue
        if mode == "start":
            if owner_state(record.get("session_id", ""), memory_dir) != "gone":
                continue
        if counter.get("attempts", 0) >= _max_attempts():
            _write_halt(memory_dir, task, record, "auto-resume cap")
            _write_counter(memory_dir, task, counter)
            return {"action": "halt", "task": task, "reason": "auto-resume cap"}
        done_now = _count_done(memory_dir, task)
        last_phase = counter.get("last_phase")
        cur_phase = [record.get("phase"), record.get("phase_index")]
        progressed = done_now > (counter.get("last_done_count") or 0) or last_phase != cur_phase
        if not progressed:
            if counter.get("consecutive_no_progress", 0) + 1 >= 2:
                counter["consecutive_no_progress"] = counter.get("consecutive_no_progress", 0) + 1
                _write_halt(memory_dir, task, record, "no forward progress")
                _write_counter(memory_dir, task, counter)
                return {"action": "halt", "task": task, "reason": "no forward progress"}
        return {
            "action": "candidate",
            "task": task,
            "record": record,
            "counter": counter,
            "progressed": progressed,
            "done_now": done_now,
            "cur_phase": cur_phase,
        }
    return {"action": "none"}


# ---------------------------------------------------------------------------
# handoff (shared by `stop`'s stop-cap branch, `start`, and the standalone
# `handoff` subcommand).
# ---------------------------------------------------------------------------


def _do_handoff(
    memory_dir: Path,
    project_root: Path,
    task: str,
    reason: str,
    counter: dict,
    record,
    halt_on_cap: bool = True,
    probe_caller: str = "handoff",
):
    """Attempt a detached `quoin run --autonomous` hand-off. Returns one of
    ``HANDOFF|<pid>|<n>/<cap>``, ``NO_CLI|``, ``STALE_CLI|<kind>|<message>``,
    ``LOCKED|<pid>``, ``OWNER_LIVE|<sid>``, ``DENIED|<reason>``.

    `halt_on_cap` gates only the cap-exhausted branch: the `handoff`
    subcommand and a startup hand-off have no cheaper fallback, so they
    write the cap halt themselves. The Stop path's stop-cap branch passes
    False — it has an in-session fallback one unit cheaper than a hand-off,
    so a denied hand-off there must fall through, not end the run."""
    if not _auto_resume_enabled():
        return "DENIED|opt-out"
    record = record or {}
    if reason == "startup":
        sid = record.get("session_id", "") or ""
        state = owner_state(sid, memory_dir)
        if state == "live":
            return f"OWNER_LIVE|{sid}"
        if state != "gone":
            return "DENIED|owner-unknown"
    if _supervisor_lock_live(memory_dir, task):
        lock = _load_json(_lock_path(memory_dir, task)) or {}
        return f"LOCKED|{lock.get('pid', '')}"
    counter = settle_supervisor(memory_dir, task, counter)
    cap = _max_attempts()
    attempts = counter.get("attempts", 0)
    # A hand-off charges its own unit plus at least one grant launch, so it
    # is only safe when both fit under the cap (attempts + 2 <= cap); the
    # Stop path's in-session fallback (attempts + 1 <= cap) covers the gap.
    if attempts + 2 > cap:
        if halt_on_cap:
            _write_halt(memory_dir, task, record, "auto-resume cap")
            _write_counter(memory_dir, task, counter)
        return "DENIED|cap"
    done_now = _count_done(memory_dir, task)
    last_phase = counter.get("last_phase")
    cur_phase = [record.get("phase"), record.get("phase_index")]
    progressed = done_now > (counter.get("last_done_count") or 0) or last_phase != cur_phase
    if not progressed:
        if counter.get("consecutive_no_progress", 0) + 1 >= 2:
            counter["consecutive_no_progress"] = counter.get("consecutive_no_progress", 0) + 1
            _write_halt(memory_dir, task, record, "no forward progress")
            _write_counter(memory_dir, task, counter)
            return "DENIED|no-progress"
    res = resolve_cli(project_root, probe_caller)
    if res["status"] == "missing":
        return "NO_CLI|"
    if res["status"] == "stale":
        phase = record.get("phase", "") if record else ""
        append_note(
            memory_dir, task,
            f"[quoin-auto-resume] task={task} phase={phase} reason={reason} "
            f"cli=stale kind={res['kind']}: {res['message']}",
        )
        return f"STALE_CLI|{res['kind']}|{res['message']}"
    # The hand-off's own charge (below, attempts += 1) plus this grant must
    # together stay within cap: grant = cap - attempts_before - 1 (D-01).
    remaining = max(cap - attempts - 1, 1)

    # The lock is reserved under our own pid with O_CREAT|O_EXCL *before*
    # Popen, so two concurrent hand-offs can never both spawn a supervisor —
    # only one process can win the exclusive create. A lock left behind by a
    # dead process is settled and unlinked, then the create is retried once.
    token = uuid.uuid4().hex
    lock_path = _lock_path(memory_dir, task)
    reservation = json.dumps(
        {"pid": os.getpid(), "started_at": _iso_now(), "granted": remaining, "writer": "handoff", "token": token},
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "NO_CLI|"
    if not _create_lock_exclusive(lock_path, reservation):
        # Losing the create only proves *something* is there now — not that
        # it is stale. `_lock_is_stale` (not a bare liveness check) is what
        # decides reclaim: a lock that is live, or unparseable-but-fresh
        # (could be another creator's in-flight winner), is refused rather
        # than clobbered.
        if not _lock_is_stale(lock_path):
            lock = _load_json(lock_path) or {}
            return f"LOCKED|{lock.get('pid', '')}"
        counter = settle_supervisor(memory_dir, task, counter)
        _claim_lock_for_removal(lock_path)
        if not _create_lock_exclusive(lock_path, reservation):
            lock = _load_json(lock_path) or {}
            return f"LOCKED|{lock.get('pid', '')}"

    log_path = memory_dir / LOG_TEMPLATE.format(task=task)
    try:
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return "DENIED|log"
    try:
        log_fh = os.fdopen(log_fd, "ab")
    except OSError:
        try:
            os.close(log_fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
        return "DENIED|log"
    argv = res["argv"] + [
        "run", "--autonomous", task,
        "--project-root", str(project_root),
        "--halt-on-abort", "--max-relaunch", str(remaining),
    ]
    # D-06: the child adopts this lock (rather than racing to create its own)
    # by presenting the same token back via `QUOIN_SUPERVISOR_LOCK_TOKEN`.
    child_env = dict(os.environ)
    child_env.update(res["env_extra"])
    child_env["QUOIN_SUPERVISOR_LOCK_TOKEN"] = token
    try:
        proc = _popen(
            argv,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=child_env,
        )
    except Exception:  # noqa: BLE001 — any spawn failure (not just OSError,
        # e.g. ValueError/SubprocessError from a malformed argv or a
        # startupinfo/session-group failure) must still release the
        # reservation; otherwise it sits under this process's own pid until
        # a later settle charges it as a crashed supervisor.
        try:
            lock_path.unlink()
        except OSError:
            pass
        return "DENIED|spawn"
    finally:
        try:
            log_fh.close()
        except OSError:
            pass
    # Now that the child is real, atomically replace the reservation with
    # its actual pid (same other fields).
    _write_lock(memory_dir, task, proc.pid, remaining, "handoff", token=token)
    attempts += 1
    counter["attempts"] = attempts
    counter["consecutive_no_progress"] = 0 if progressed else counter.get("consecutive_no_progress", 0) + 1
    counter["last_done_count"] = done_now
    counter["last_phase"] = cur_phase
    counter["in_flight"] = True
    counter["last_reason"] = reason
    counter["last_session_id"] = record.get("session_id", "") or ""
    _write_counter(memory_dir, task, counter)
    notice = _notice_line(task, record, reason, attempts, cap, f"supervisor pid={proc.pid}")
    append_note(memory_dir, task, notice)
    return f"HANDOFF|{proc.pid}|{attempts}/{cap}"


# ---------------------------------------------------------------------------
# Shared interpreter resolver (IVG-281): prefers the installer's own record
# of what interpreter and package tree it deployed from, falling back to a
# legacy PATH lookup when no record exists. Used by every hand-off caller
# (Stop, SessionStart, the `handoff` subcommand) and by `cli-check`, so a
# venv/pipx/uv-tool install relaunches the same interpreter it was deployed
# from instead of guessing via `PATH`.
# ---------------------------------------------------------------------------

RUNTIME_RECORD_FILENAME = "quoin-runtime.json"
RUNTIME_RECORD_SCHEMA = 1
_RECORD_MAX_BYTES = 16384
_PROBE_SNIPPET = "import sys, quoin, quoin.cli; sys.stdout.write('\nQUOIN_VERSION=' + quoin.__version__ + '\n')"
_VERSION_TOKEN_RE = re.compile(r"^QUOIN_VERSION=(\S+)\s*$", re.M)
_REMEDY = "re-run 'quoin install' (same scope) and check 'quoin doctor'"
# Start/stop callers act on a live knob, so the remedy names it. Handoff and
# cli-check ignore the knob (fixed 8 s budget), so naming it would mislead.
_TIMEOUT_REMEDY_KNOB = (
    "the machine was slow; retry '/run --resume <task>' or raise "
    "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS (reinstalling will not help)"
)
_TIMEOUT_REMEDY_FIXED = (
    "the machine was slow; retry '/run --resume <task>' and check for a slow or "
    "cloud-synced filesystem (reinstalling will not help)"
)

_CLI_MEMO: dict = {}


def _reset_cli_memo() -> None:
    _CLI_MEMO.clear()


def _deploy_root() -> Optional[Path]:
    p = Path(os.path.abspath(__file__))
    if p.parent.name == "scripts" and p.parents[1].name == "core":
        return p.parents[2]
    return None


def _runtime_record_path() -> Optional[Path]:
    root = _deploy_root()
    if root is None:
        return None
    return root / RUNTIME_RECORD_FILENAME


def _probe_budget_ms(caller: str) -> int:
    if caller == "start":
        # T-01 sizing: baseline + default + 1s kill slack must stay under
        # the 5s SessionStart stanza budget (measured against the worst
        # case, not a healthy probe).
        return _clamp_int("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", 1500, 250, 3000)
    if caller == "stop":
        return _clamp_int("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", 3000, 250, 7000)
    return 8000  # handoff / cli-check: fixed budget, knob ignored


def _probe_retries(caller: str) -> int:
    return 1 if caller == "handoff" else 0


def _remedy(kind: str, caller: str) -> str:
    if kind == "probe-timeout":
        if caller in ("start", "stop"):
            return _TIMEOUT_REMEDY_KNOB
        return _TIMEOUT_REMEDY_FIXED
    return _REMEDY


_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x1f]")
_MULTISPACE_RE = re.compile(r" {2,}")


def _sanitize_msg(s: str) -> str:
    s = _CONTROL_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    return s[:300]


def _probe(argv0: str, env: dict, cwd: str, timeout_s: float) -> dict:
    """Runs the version probe in its own process group, with output
    captured to temp files rather than pipes — a grandchild holding a pipe
    open past the timeout would otherwise defeat the bound. Never raises;
    every failure mode maps to a status string the caller inspects."""
    try:
        out_fd, out_name = tempfile.mkstemp(prefix=".quoin-probe-out.")
        err_fd, err_name = tempfile.mkstemp(prefix=".quoin-probe-err.")
    except OSError:
        return {"status": "oserror-tmp", "rc": None, "stdout": "", "stderr_tail": ""}
    out_path = Path(out_name)
    err_path = Path(err_name)
    try:
        with os.fdopen(out_fd, "wb") as out_fh, os.fdopen(err_fd, "wb") as err_fh:
            try:
                proc = subprocess.Popen(
                    [argv0, "-c", _PROBE_SNIPPET],
                    env=env,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=err_fh,
                    start_new_session=True,
                )
            except OSError:
                return {"status": "oserror", "rc": None, "stdout": "", "stderr_tail": ""}
            try:
                rc = proc.wait(timeout=timeout_s)
                status = "ok" if rc == 0 else "exit"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                rc = None
                status = "timeout"
        stdout_text = out_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        for p in (out_path, err_path):
            try:
                p.unlink()
            except OSError:
                pass
    stripped_err = stderr_text.strip()
    stderr_tail = stripped_err.splitlines()[-1] if stripped_err else ""
    return {"status": status, "rc": rc, "stdout": stdout_text, "stderr_tail": stderr_tail}


def _load_runtime_record(path: Path):
    """Returns ``(record_dict, None)`` on a valid record or ``(None, kind)``
    on any validation failure. Never raises — permission errors and other
    ``OSError`` subclasses on the read map to ``record-invalid``, same as
    a garbled or oversized file."""
    try:
        if path.stat().st_size > _RECORD_MAX_BYTES:
            return None, "record-invalid"
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "record-invalid"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "record-invalid"
    if not isinstance(data, dict):
        return None, "record-invalid"
    schema = data.get("schema")
    python = data.get("python")
    version = data.get("version")
    pythonpath = data.get("pythonpath")
    source_version = data.get("source_version")
    if not isinstance(schema, int) or schema != RUNTIME_RECORD_SCHEMA:
        return None, "record-invalid"
    if not isinstance(python, str) or not python or not os.path.isabs(python):
        return None, "record-invalid"
    if not isinstance(version, str) or not version:
        return None, "record-invalid"
    if pythonpath is not None and (not isinstance(pythonpath, str) or not os.path.isabs(pythonpath)):
        return None, "record-invalid"
    if source_version is not None and not isinstance(source_version, str):
        return None, "record-invalid"
    return data, None


def _resolve_cli_uncached(project_root, caller: str) -> dict:
    rp = _runtime_record_path()
    base = {
        "source": None, "status": None, "argv": None, "env_extra": {}, "kind": None,
        "message": None, "record_path": str(rp) if rp is not None else None,
        "record": None, "probed_version": None, "pythonpath": None,
    }
    if rp is None or not os.path.exists(str(rp)):
        binpath = _which("quoin")
        if not binpath:
            fallback = _home() / ".local" / "bin" / "quoin"
            if fallback.exists() and os.access(str(fallback), os.X_OK):
                binpath = str(fallback)
        if binpath:
            base.update(source="legacy", status="usable", argv=[binpath])
        else:
            base.update(source="legacy", status="missing")
        return base

    record, err_kind = _load_runtime_record(rp)
    if err_kind:
        base.update(
            source="record", status="stale", kind=err_kind,
            message=_sanitize_msg(
                "the install record is missing or invalid; " + _remedy(err_kind, caller)
            ),
        )
        return base
    base["record"] = record
    python = record["python"]
    version = record["version"]
    pythonpath = record.get("pythonpath")
    source_version = record.get("source_version")

    if source_version is not None and source_version != version:
        base.update(
            source="record", status="stale", kind="version-mismatch",
            message=_sanitize_msg(
                f"deployed files are at version {source_version} but the recorded CLI is "
                f"{version}; " + _remedy("version-mismatch", caller)
            ),
        )
        return base
    if not os.path.exists(python):
        base.update(
            source="record", status="stale", kind="interpreter-missing",
            message=_sanitize_msg(
                f"recorded interpreter {python} no longer exists; "
                + _remedy("interpreter-missing", caller)
            ),
        )
        return base
    if not os.access(python, os.X_OK):
        base.update(
            source="record", status="stale", kind="interpreter-not-executable",
            message=_sanitize_msg(
                f"recorded interpreter {python} is not executable; "
                + _remedy("interpreter-not-executable", caller)
            ),
        )
        return base

    env = dict(os.environ)
    env_extra: dict = {}
    if pythonpath:
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = pythonpath + (os.pathsep + old if old else "")
        env_extra = {"PYTHONPATH": env["PYTHONPATH"], "QUOIN_HANDOFF_PYTHONPATH": pythonpath}
    base["pythonpath"] = pythonpath

    budget_ms = _probe_budget_ms(caller)
    timeout_s = budget_ms / 1000.0
    tries = 1 + _probe_retries(caller)
    result = {"status": "timeout", "rc": None, "stdout": "", "stderr_tail": ""}
    for _ in range(tries):
        result = _probe(python, env, str(project_root), timeout_s)
        if result["status"] != "timeout":
            break

    status = result["status"]
    if status == "timeout":
        base.update(
            source="record", status="stale", kind="probe-timeout",
            message=_sanitize_msg("the version probe timed out; " + _remedy("probe-timeout", caller)),
        )
        return base
    if status == "oserror-tmp":
        base.update(
            source="record", status="stale", kind="resolver-error",
            message=_sanitize_msg(
                "auto-resume could not check the installed quoin (could not create a "
                "temp file for the probe); " + _REMEDY
            ),
        )
        return base
    if status == "oserror":
        base.update(
            source="record", status="stale", kind="interpreter-not-executable",
            message=_sanitize_msg(
                f"recorded interpreter {python} could not be started; "
                + _remedy("interpreter-not-executable", caller)
            ),
        )
        return base
    if status == "exit":
        tail = result.get("stderr_tail") or "no error output"
        base.update(
            source="record", status="stale", kind="import-failed",
            message=_sanitize_msg(
                f"the version probe exited with an error ({tail}); "
                + _remedy("import-failed", caller)
            ),
        )
        return base

    # status == "ok"
    token_match = None
    for token_match in _VERSION_TOKEN_RE.finditer(result["stdout"]):
        pass
    if token_match is None:
        base.update(
            source="record", status="stale", kind="import-failed",
            message=_sanitize_msg(
                "the version probe produced no version token; "
                + _remedy("import-failed", caller)
            ),
        )
        return base
    probed_version = token_match.group(1)
    base["probed_version"] = probed_version
    if probed_version != version:
        base.update(
            source="record", status="stale", kind="version-mismatch",
            message=_sanitize_msg(
                f"the probed CLI reports {probed_version} but the record says {version}; "
                + _remedy("version-mismatch", caller)
            ),
        )
        return base

    base.update(source="record", status="usable", argv=[python, "-m", "quoin"], env_extra=env_extra)
    return base


def resolve_cli(project_root, caller: str) -> dict:
    """Finds the CLI to hand off to. Never raises: any unexpected exception
    inside the uncached resolver becomes a stale ``resolver-error`` result
    instead of escaping to the caller, since an escape here would silently
    drop the Stop-hook nudge and skip writing a requested halt. That error
    result is not memoized — a transient fault may clear on the next call."""
    key = (str(Path(project_root)), caller)
    if key in _CLI_MEMO:
        return _CLI_MEMO[key]
    try:
        result = _resolve_cli_uncached(project_root, caller)
    except Exception as exc:  # noqa: BLE001 — the resolver must be total (D-20)
        rp = _runtime_record_path()
        return {
            "source": "record", "status": "stale", "argv": None, "env_extra": {},
            "kind": "resolver-error",
            "message": _sanitize_msg(
                f"auto-resume could not check the installed quoin "
                f"({type(exc).__name__}: {exc}); " + _REMEDY
            ),
            "record_path": str(rp) if rp is not None else None,
            "record": None, "probed_version": None, "pythonpath": None,
        }
    _CLI_MEMO[key] = result
    return result


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_stop(args) -> int:
    try:
        raw = sys.stdin.buffer.read(_STDIN_CAP_BYTES + 1)
    except Exception:
        return 0
    if len(raw) > _STDIN_CAP_BYTES:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if not _auto_resume_enabled():
        return 0
    session_id = payload.get("session_id")
    if not _valid_sid(session_id):
        return 0
    # D-23: background_tasks is checked before any counter mutation.
    if payload.get("background_tasks"):
        return 0
    memory_dir = _memory_dir(args.project_root)
    if not _iter_markers(memory_dir):
        return 0
    candidates = _select_candidates(memory_dir, "stop", session_id)
    if not candidates:
        return 0
    result = _evaluate_gate(memory_dir, "stop", candidates)
    if result["action"] != "candidate":
        return 0

    task = result["task"]
    record = result["record"]
    counter = result["counter"]

    if payload.get("stop_hook_active"):
        counter["chain_blocks"] = counter.get("chain_blocks", 0) + 1
    else:
        counter["chain_blocks"] = 1

    cap = _max_attempts()
    stale_msg = None
    if counter["chain_blocks"] >= _handoff_at():
        handoff_result = _do_handoff(
            memory_dir, Path(args.project_root), task, "stop-cap", counter, record,
            halt_on_cap=False, probe_caller="stop",
        )
        if handoff_result.startswith("HANDOFF|"):
            return 0
        if handoff_result.startswith("DENIED|no-progress"):
            # I-13: a halt was already written — the Stop path stays terminal.
            return 0
        if handoff_result.startswith("LOCKED|"):
            # A supervisor won the lock between the gate check and this
            # hand-off attempt (e.g. a concurrent `start`/`handoff`) — it is
            # already driving the task, so this session must not also keep
            # blocking on it in-session.
            return 0
        if handoff_result.startswith("STALE_CLI|"):
            stale_msg = handoff_result.split("|", 2)[2]
        # DENIED|cap (no halt written — halt_on_cap=False) / NO_CLI| /
        # OWNER_LIVE| / DENIED|opt-out / STALE_CLI|: fall through to a plain
        # in-session block, still bounded by the attempts+1 <= cap check
        # `_evaluate_gate` already applied before returning this candidate.

    attempts = counter.get("attempts", 0) + 1
    counter["attempts"] = attempts
    counter["consecutive_no_progress"] = 0 if result["progressed"] else counter.get("consecutive_no_progress", 0) + 1
    counter["last_done_count"] = result["done_now"]
    counter["last_phase"] = result["cur_phase"]
    counter["in_flight"] = True
    counter["last_session_id"] = session_id
    counter["last_reason"] = "compact" if (memory_dir / COMPACT_TEMPLATE.format(sid=session_id)).exists() else "stop"
    _write_counter(memory_dir, task, counter)

    notice = _notice_line(task, record, counter["last_reason"], attempts, cap, "in-session")
    append_note(memory_dir, task, notice)
    resume_command = record.get("resume_command") or f"/run --resume {task}"
    system_message = notice if stale_msg is None else notice + " | auto-resume hand-off skipped: " + stale_msg
    print(json.dumps({
        "decision": "block",
        "reason": _reason_text(task, resume_command),
        "systemMessage": system_message,
    }))
    return 0


def _cmd_start(args) -> int:
    if args.source not in ("startup", "resume"):
        return 0
    if not _auto_resume_enabled():
        return 0
    memory_dir = _memory_dir(args.project_root)
    if not _iter_markers(memory_dir):
        _prune_ended_markers(memory_dir)
        return 0
    candidates = _select_candidates(memory_dir, "start", args.session_id)
    _prune_ended_markers(memory_dir)
    if not candidates:
        return 0
    result = _evaluate_gate(memory_dir, "start", candidates)
    if result["action"] != "candidate":
        return 0

    task = result["task"]
    record = result["record"]
    counter = result["counter"]
    handoff_result = _do_handoff(
        memory_dir, Path(args.project_root), task, "startup", counter, record, probe_caller="start"
    )

    if handoff_result.startswith("HANDOFF|"):
        pid = handoff_result.split("|")[1]
        cap = _max_attempts()
        notice = _notice_line(task, record, "startup", counter.get("attempts", 0), cap, f"supervisor pid={pid}")
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": notice}
        }))
        return 0
    if handoff_result.startswith("NO_CLI|"):
        resume_command = record.get("resume_command") or f"/run --resume {task}"
        advisory = (
            f"[quoin-auto-resume] task={task} reason=startup: the quoin CLI was not "
            f"found on PATH; resume manually: {resume_command}"
        )
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": advisory}
        }))
        return 0
    if handoff_result.startswith("STALE_CLI|"):
        message = handoff_result.split("|", 2)[2]
        resume_command = record.get("resume_command") or f"/run --resume {task}"
        advisory = (
            f"[quoin-auto-resume] task={task} reason=startup: auto-resume hand-off "
            f"skipped: {message}; resume manually: {resume_command}"
        )
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": advisory}
        }))
        return 0
    return 0


def _cmd_arm(args) -> int:
    """Records per-session consent to continue this run and, only on a
    consumed consent stamp with no live supervisor, resets the budget.

    A consent stamp (`run-continue-consent-{sid}.txt`) is written only by a
    typed `/run` prompt (see userpromptsubmit.sh) — never by a marker
    rewrite, a supervisor child's own re-entry, or a model self-resume — so
    those paths keep whatever budget the span already has. The stamp is
    consumed (unlinked) on every `arm` call whether or not it is honored —
    even a no-op call against a task with no active marker — and is
    ignored if it predates this session's previous arm (a stale stamp
    from an earlier span). Consumption happens BEFORE the marker check
    below: a plain `/run` against an inactive task must still clear a
    leftover stamp, or a later human arm in the same session could wrongly
    honor it."""
    if not _auto_resume_enabled():
        return 0
    if not _run_state._valid_task(args.task) or not _valid_sid(args.session_id):
        return 0
    memory_dir = _memory_dir(args.project_root)

    arm_path = memory_dir / ARM_TEMPLATE.format(sid=args.session_id)
    prev_armed_at = None
    if arm_path.exists():
        prev_armed_at = _parse_iso(_load_kv_text(arm_path).get("armed_at"))

    consent_path = memory_dir / CONSENT_TEMPLATE.format(sid=args.session_id)
    consent = False
    if consent_path.exists():
        try:
            consent_mtime = consent_path.stat().st_mtime
        except OSError:
            consent_mtime = None
        if consent_mtime is not None:
            consent = prev_armed_at is None or consent_mtime >= prev_armed_at.timestamp()
        try:
            consent_path.unlink()
        except OSError:
            pass

    marker_path = memory_dir / MARKER_TEMPLATE.format(task=args.task)
    if not marker_path.exists():
        return 0
    marker = _load_marker(marker_path)
    marker_ts = marker.get("timestamp") if marker else None

    try:
        _atomic_write_text(
            memory_dir,
            f"{ARM_TEMPLATE.format(sid=args.session_id)}.",
            arm_path,
            f"task: {args.task}\narmed_at: {_iso_now()}\n",
        )
    except OSError:
        return 0

    try:
        (memory_dir / ENDED_TEMPLATE.format(sid=args.session_id)).unlink()
    except OSError:
        pass

    counter = _load_counter(memory_dir, args.task) or _default_counter(args.task, marker_ts)
    counter = settle_supervisor(memory_dir, args.task, counter)
    live = _supervisor_lock_live(memory_dir, args.task)
    if consent and not live:
        counter["attempts"] = 0
        counter["consecutive_no_progress"] = 0
        counter["chain_blocks"] = 0
        counter["last_done_count"] = 0
        counter["last_phase"] = None
    counter["in_flight"] = False
    counter["marker_timestamp"] = marker_ts
    _write_counter(memory_dir, args.task, counter)
    return 0


def _cmd_handoff(args) -> int:
    if not _run_state._valid_task(args.task):
        print("DENIED|invalid-task")
        return 0
    memory_dir = _memory_dir(args.project_root)
    marker_path = memory_dir / MARKER_TEMPLATE.format(task=args.task)
    marker = _load_marker(marker_path) if marker_path.exists() else None
    marker_ts = marker.get("timestamp") if marker else None
    record = _load_record(memory_dir, args.task)
    counter = _get_or_reset_counter(memory_dir, args.task, marker_ts)
    result = _do_handoff(
        memory_dir, Path(args.project_root), args.task, args.reason, counter, record,
        probe_caller="handoff",
    )
    print(result)
    # A `LOCKED|` refusal means a live supervisor is already driving this
    # task — that is success from the caller's point of view, not a failure
    # to halt on. Halting here would stop a supervised child's own turn
    # (e.g. on context exhaustion) even though the run is progressing fine
    # under the supervisor that holds the lock.
    if not result.startswith("HANDOFF|") and not result.startswith("LOCKED|") and args.on_fail_halt:
        if not _sentinel_exists(memory_dir, HALT_TEMPLATE, args.task):
            halt_reason = args.on_fail_halt
            if result.startswith("STALE_CLI|"):
                halt_reason = f"{args.on_fail_halt}: {result.split('|', 2)[2]}"
            _write_halt(memory_dir, args.task, record, halt_reason)
    return 0


def _cmd_pause(args) -> int:
    if not _run_state._valid_task(args.task):
        return 0
    memory_dir = _memory_dir(args.project_root)
    record = _load_record(memory_dir, args.task)
    _write_halt(memory_dir, args.task, record, "paused by user")
    sid = args.session_id or (record or {}).get("session_id", "")
    if _valid_sid(sid):
        try:
            (memory_dir / ARM_TEMPLATE.format(sid=sid)).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def _cmd_status(args) -> int:
    memory_dir = _memory_dir(args.project_root)
    task = args.task
    counter = _load_counter(memory_dir, task) or _default_counter(task, None)
    record = _load_record(memory_dir, task)
    sid = (record or {}).get("session_id", "") or ""
    state = owner_state(sid, memory_dir) if sid else "unknown"
    out = {
        "task": task,
        "opted_out": not _auto_resume_enabled(),
        "attempts": counter.get("attempts", 0),
        "max_attempts": _max_attempts(),
        "consecutive_no_progress": counter.get("consecutive_no_progress", 0),
        "chain_blocks": counter.get("chain_blocks", 0),
        "supervisor_lock_live": _supervisor_lock_live(memory_dir, task),
        "owner_state": state,
        "done": _sentinel_exists(memory_dir, DONE_TEMPLATE, task),
        "halted": _sentinel_exists(memory_dir, HALT_TEMPLATE, task),
        "needs_decision": _sentinel_exists(memory_dir, NEEDS_DECISION_TEMPLATE, task),
    }
    print(json.dumps(out, sort_keys=True))
    return 0


def _cmd_cli_check(args) -> int:
    """Read-only diagnostic: prints the resolver's verdict as one JSON
    line. Touches nothing under the project memory dir (no lock, no
    counter, no notes) — safe to run from `quoin doctor` at any time."""
    try:
        res = resolve_cli(Path(args.project_root), "cli-check")
        print(json.dumps(res, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        print(json.dumps({"status": "error", "message": _sanitize_msg(str(exc))}, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run-continuation gate for interrupted autonomous /run spans. Always exits 0.",
    )
    sub = parser.add_subparsers(dest="command")

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--project-root", required=True, dest="project_root")

    p_start = sub.add_parser("start")
    p_start.add_argument("--project-root", required=True, dest="project_root")
    p_start.add_argument("--source", required=True)
    p_start.add_argument("--session-id", required=True, dest="session_id")

    p_arm = sub.add_parser("arm")
    p_arm.add_argument("--project-root", required=True, dest="project_root")
    p_arm.add_argument("--task", required=True)
    p_arm.add_argument("--session-id", required=True, dest="session_id")
    p_arm.add_argument("--entry", default="fresh", choices=["fresh", "resume"])

    p_handoff = sub.add_parser("handoff")
    p_handoff.add_argument("--project-root", required=True, dest="project_root")
    p_handoff.add_argument("--task", required=True)
    p_handoff.add_argument(
        "--reason", required=True,
        choices=["budget", "startup", "session-age", "context", "stop-cap"],
    )
    p_handoff.add_argument("--on-fail-halt", default=None, dest="on_fail_halt")

    p_pause = sub.add_parser("pause")
    p_pause.add_argument("--project-root", required=True, dest="project_root")
    p_pause.add_argument("--task", required=True)
    p_pause.add_argument("--session-id", default=None, dest="session_id")

    p_status = sub.add_parser("status")
    p_status.add_argument("--project-root", required=True, dest="project_root")
    p_status.add_argument("--task", required=True)

    p_cli_check = sub.add_parser("cli-check")
    p_cli_check.add_argument("--project-root", required=True, dest="project_root")

    return parser


_HANDLERS = {
    "stop": _cmd_stop,
    "start": _cmd_start,
    "arm": _cmd_arm,
    "handoff": _cmd_handoff,
    "pause": _cmd_pause,
    "status": _cmd_status,
    "cli-check": _cmd_cli_check,
}


def _log_unexpected_error(args, exc: BaseException) -> None:
    """Best-effort durable record of a fail-OPEN exception (I-01).

    `stop.sh` and `sessionstart.sh` invoke this helper with `2>/dev/null`,
    so the stderr warning below is normally invisible in real use — a
    crash here can silently kill auto-resume for a task with no trace
    anywhere. This writes the same event to a plain-text file under the
    memory dir instead (reusing the same bounded-append primitive
    `append_note` already uses for run-notes), so a fail-OPEN death always
    leaves a trail a human or a future dispatch can actually find."""
    project_root = getattr(args, "project_root", None) if args is not None else None
    if not project_root:
        return
    try:
        memory_dir = _memory_dir(project_root)
        memory_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    command = getattr(args, "command", None) if args is not None else None
    task = getattr(args, "task", None) if args is not None else None
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > 4000:
        tb = tb[-4000:]
    block = (
        f"## {_iso_now()} — command={command or '?'} task={task or '?'}\n"
        f"- {type(exc).__name__}: {exc}\n"
        f"```\n{tb}```\n\n"
    )
    try:
        _run_state._append_notes(
            memory_dir / ERROR_LOG_TEMPLATE,
            block,
            int(os.environ.get("QUOIN_RUN_NOTES_MAX_BYTES", "262144")),
        )
    except Exception:
        pass


def main(argv=None) -> int:
    args = None
    try:
        parser = _build_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return 0
        command = getattr(args, "command", None)
        if not command:
            return 0
        handler = _HANDLERS.get(command)
        if handler is None:
            return 0
        return handler(args)
    except BaseException as exc:  # noqa: BLE001 — fail-open contract (I-01)
        try:
            print(f"[auto_resume] WARNING: unexpected error: {exc}", file=sys.stderr)
        except Exception:
            pass
        try:
            _log_unexpected_error(args, exc)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
