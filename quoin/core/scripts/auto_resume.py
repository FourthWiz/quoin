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
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


def _load_marker(path: Path):
    """Markers are `field: value` text (see run/SKILL.md), not JSON."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
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
    counter = _load_counter(memory_dir, task)
    if counter is None or counter.get("marker_timestamp") != marker_timestamp:
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


def settle_supervisor(memory_dir: Path, task: str, counter: dict) -> dict:
    """Consume a finished supervisor's `.result`, or charge a crashed
    supervisor's full grant when its lock names a dead pid (D-19)."""
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
            try:
                granted = int(lock.get("granted", 0) or 0)
            except (TypeError, ValueError):
                granted = 0
            counter["attempts"] = counter.get("attempts", 0) + granted
            try:
                lock_path.unlink()
            except OSError:
                pass
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
    try:
        cutoff = _now() - _ENDED_MARKER_PRUNE_DAYS * 86400
        for path in memory_dir.glob(ENDED_TEMPLATE.format(sid="*")):
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
        progressed = done_now > counter.get("last_done_count", 0) or last_phase != cur_phase
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


def _do_handoff(memory_dir: Path, project_root: Path, task: str, reason: str, counter: dict, record):
    """Attempt a detached `quoin run --autonomous` hand-off. Returns one of
    ``HANDOFF|<pid>|<n>/<cap>``, ``NO_CLI|``, ``LOCKED|<pid>``,
    ``OWNER_LIVE|<sid>``, ``DENIED|<reason>``."""
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
    if attempts >= cap:
        _write_halt(memory_dir, task, record, "auto-resume cap")
        _write_counter(memory_dir, task, counter)
        return "DENIED|cap"
    done_now = _count_done(memory_dir, task)
    last_phase = counter.get("last_phase")
    cur_phase = [record.get("phase"), record.get("phase_index")]
    progressed = done_now > counter.get("last_done_count", 0) or last_phase != cur_phase
    if not progressed:
        if counter.get("consecutive_no_progress", 0) + 1 >= 2:
            counter["consecutive_no_progress"] = counter.get("consecutive_no_progress", 0) + 1
            _write_halt(memory_dir, task, record, "no forward progress")
            _write_counter(memory_dir, task, counter)
            return "DENIED|no-progress"
    quoin_bin = _which("quoin")
    if not quoin_bin:
        fallback = _home() / ".local" / "bin" / "quoin"
        if fallback.exists() and os.access(str(fallback), os.X_OK):
            quoin_bin = str(fallback)
        else:
            return "NO_CLI|"
    remaining = max(cap - attempts, 1)
    log_path = memory_dir / LOG_TEMPLATE.format(task=task)
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(str(log_path), "ab")
    except OSError:
        return "NO_CLI|"
    argv = [
        quoin_bin, "run", "--autonomous", task,
        "--project-root", str(project_root),
        "--halt-on-abort", "--max-relaunch", str(remaining),
    ]
    # D-06: the child adopts this lock (rather than racing to create its own)
    # by presenting the same token back via `QUOIN_SUPERVISOR_LOCK_TOKEN`.
    token = uuid.uuid4().hex
    child_env = dict(os.environ)
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
    except OSError:
        return "NO_CLI|"
    finally:
        try:
            log_fh.close()
        except OSError:
            pass
    _write_lock(memory_dir, task, proc.pid, remaining, "handoff", token=token)
    attempts += 1
    counter["attempts"] = attempts
    counter["consecutive_no_progress"] = 0 if progressed else counter.get("consecutive_no_progress", 0)
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
    if counter["chain_blocks"] >= _handoff_at() and _which("quoin"):
        handoff_result = _do_handoff(memory_dir, Path(args.project_root), task, "stop-cap", counter, record)
        if handoff_result.startswith("HANDOFF|"):
            return 0
        if handoff_result.startswith("DENIED|cap") or handoff_result.startswith("DENIED|no-progress"):
            # I-13: a halt was already written — the Stop path stays terminal.
            return 0
        # LOCKED| / NO_CLI| / OWNER_LIVE| / DENIED|opt-out: fall through to a
        # plain in-session block; the harness's own cap remains the outer bound.

    attempts = counter.get("attempts", 0) + 1
    counter["attempts"] = attempts
    counter["consecutive_no_progress"] = 0 if result["progressed"] else counter.get("consecutive_no_progress", 0)
    counter["last_done_count"] = result["done_now"]
    counter["last_phase"] = result["cur_phase"]
    counter["in_flight"] = True
    counter["last_session_id"] = session_id
    counter["last_reason"] = "compact" if (memory_dir / COMPACT_TEMPLATE.format(sid=session_id)).exists() else "stop"
    _write_counter(memory_dir, task, counter)

    notice = _notice_line(task, record, counter["last_reason"], attempts, cap, "in-session")
    append_note(memory_dir, task, notice)
    resume_command = record.get("resume_command") or f"/run --resume {task}"
    print(json.dumps({
        "decision": "block",
        "reason": _reason_text(task, resume_command),
        "systemMessage": notice,
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
    handoff_result = _do_handoff(memory_dir, Path(args.project_root), task, "startup", counter, record)

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
    return 0


def _cmd_arm(args) -> int:
    if not _run_state._valid_task(args.task) or not _valid_sid(args.session_id):
        return 0
    memory_dir = _memory_dir(args.project_root)
    arm_path = memory_dir / ARM_TEMPLATE.format(sid=args.session_id)
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        arm_path.touch()
    except OSError:
        return 0

    marker_path = memory_dir / MARKER_TEMPLATE.format(task=args.task)
    marker = _load_marker(marker_path) if marker_path.exists() else None
    marker_ts = marker.get("timestamp") if marker else None

    counter = _load_counter(memory_dir, args.task)
    lock_live = _supervisor_lock_live(memory_dir, args.task)
    if counter is None or counter.get("marker_timestamp") != marker_ts:
        counter = _default_counter(args.task, marker_ts)
    elif not counter.get("in_flight") and not lock_live:
        # D-17: a human-typed `/run --resume` resets the budget; a
        # hook-driven or supervisor-child resume (in_flight, or a live
        # supervisor lock) never resets it.
        counter = _default_counter(args.task, marker_ts)
    else:
        counter["in_flight"] = False
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
    result = _do_handoff(memory_dir, Path(args.project_root), args.task, args.reason, counter, record)
    print(result)
    if not result.startswith("HANDOFF|") and args.on_fail_halt:
        if not _sentinel_exists(memory_dir, HALT_TEMPLATE, args.task):
            _write_halt(memory_dir, args.task, record, args.on_fail_halt)
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

    return parser


_HANDLERS = {
    "stop": _cmd_stop,
    "start": _cmd_start,
    "arm": _cmd_arm,
    "handoff": _cmd_handoff,
    "pause": _cmd_pause,
    "status": _cmd_status,
}


def main(argv=None) -> int:
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
        return 0


if __name__ == "__main__":
    sys.exit(main())
