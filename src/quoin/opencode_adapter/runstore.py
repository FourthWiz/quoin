"""On-disk run store: ids, the event sidecar, atomic records, hashes, revisions.

Layout, under `memory/runtime/opencode/` in the project's artifact root:

    RUN_ID.jsonl            append-only event sidecar
    RUN_ID.run.json         run record (rewritten atomically)
    RUN_ID.checkpoint.json  resume checkpoint (rewritten atomically)
    task-TASK.json          pointer from a task to its latest run
    workflow-TASK.json      workflow evidence record for a task's gated phases

The directory is created mode 700 and every component from
the artifact root down must be a real directory, never a symlink. Files
are mode 600. A record is replaced with a temporary file plus rename, so a
crash leaves either the old or the new record and never a partial one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import jsonio
from .errors import ConfigErrors
from .events import (
    ArtifactReferencePayload,
    EventType,
    RUN_ID_RE,
    RuntimeEvent,
    summarize_steps,
)
from .paths import git_worktree_root
from .proctree import Identity, ProcInfo, alive, group_members

SCHEMA_VERSION = 1
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
STORE_PARTS = (".workflow_artifacts", "memory", "runtime", "opencode")
MAX_RECORD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
REVISIONS_BUDGET_S = 60.0
SHORT_REVISIONS_BUDGET_S = 10.0
# End-of-attempt hashing after a timeout or cancel must not hold the caller.
SHORT_HASH_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SUBREPOS = 64
# Project-relative directories that hold coordinator or tool output, never
# source: a write into any of them must not change a repository's source state.
SOURCE_EXCLUDED_DIRS = (".workflow_artifacts", ".opencode", ".quoin", ".workspaces")
SOURCE_DIGEST_MAX_BYTES = 64 * 1024 * 1024
SOURCE_MAX_UNTRACKED = 5000
GIT_TIMEOUT_S = 30.0
# A repository's own config must not be able to hide edits from the source
# state (a file-system monitor hook can, and also runs a command) or serve a
# stale untracked listing.
_SOURCE_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")
_HASH_CHUNK = 1024 * 1024


class RunStoreError(Exception):
    """A store operation was refused; `code` is a short stable identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# directory and names
# ---------------------------------------------------------------------------


def _reject_symlink_or_file(path: Path) -> bool:
    """True when `path` exists as a real directory; refuse a symlink or file."""
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RunStoreError("unsafe-path")
    return True


def store_dir(project_root, *, create: bool = False) -> Path:
    """The run store directory for a project.

    Every component from the artifact root down is checked with `lstat`;
    a symlink anywhere refuses with `RunStoreError("unsafe-path")`. With
    `create`, missing components are made (mode 700)."""
    current = Path(project_root)
    for part in STORE_PARTS:
        current = current / part
        exists = _reject_symlink_or_file(current)
        if not exists and create:
            try:
                os.mkdir(str(current), 0o700)
            except FileExistsError:
                _reject_symlink_or_file(current)
            os.chmod(str(current), 0o700)
    if create:
        info = os.lstat(str(current))
        if info.st_uid != os.getuid():
            raise RunStoreError("unsafe-path")
        if info.st_mode & 0o077:
            os.chmod(str(current), 0o700)
    return current


def inspect_store(project_root) -> Optional[Path]:
    """The run store directory for read-only inspection, never creating or
    changing anything.

    `None` when the store does not exist. Refuses with
    `RunStoreError("unsafe-path")` on a symlink or file component and, unlike
    `store_dir(create=False)`, on a final directory owned by another user."""
    directory = store_dir(project_root, create=False)
    try:
        info = os.lstat(str(directory))
    except FileNotFoundError:
        return None
    if info.st_uid != os.getuid():
        raise RunStoreError("unsafe-path")
    return directory


def _inside(directory: Path, path: Path) -> bool:
    base = os.path.realpath(str(directory))
    target = os.path.realpath(str(path))
    return target == base or target.startswith(base + os.sep)


def check_task_name(task: str) -> str:
    if not isinstance(task, str) or not TASK_RE.match(task) or ".." in task:
        raise RunStoreError("invalid-task-name")
    return task


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise RunStoreError("invalid-run-id")
    return run_id


def _checked(directory: Path, name: str) -> Path:
    path = directory / name
    if not _inside(directory, path) or os.path.dirname(name):
        raise RunStoreError("unsafe-path")
    return path


@dataclass(frozen=True)
class RunPaths:
    sidecar: Path
    record: Path
    checkpoint: Path


def run_paths(directory: Path, run_id: str) -> RunPaths:
    check_run_id(run_id)
    return RunPaths(
        sidecar=_checked(directory, run_id + ".jsonl"),
        record=_checked(directory, run_id + ".run.json"),
        checkpoint=_checked(directory, run_id + ".checkpoint.json"),
    )


_RECORD_FILE_RE = re.compile(r"^(" + RUN_ID_RE.pattern.strip("^$") + r")\.run\.json$")


def list_records(directory, task: Optional[str] = None, limit: int = 1000) -> Tuple[List[Dict[str, Any]], int]:
    """Run records in the store, read-only: up to `limit` record files, newest
    name first (filtered by task when given) and the number of files skipped.

    A symlink, an unreadable or unparsable file, and every file beyond the
    limit are skipped and counted; a record is never repaired or rewritten."""
    records: List[Dict[str, Any]] = []
    skipped = 0
    try:
        names_ = sorted(
            (entry.name for entry in os.scandir(str(directory)) if _RECORD_FILE_RE.match(entry.name)),
            reverse=True,
        )
    except OSError:
        return [], 0
    read = 0
    for name in names_:
        if read >= limit:
            skipped += 1
            continue
        read += 1
        got = jsonio.read_regular_bytes(Path(directory) / name, max_bytes=MAX_RECORD_BYTES)
        if got is None:
            skipped += 1
            continue
        try:
            data = json.loads(got[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            skipped += 1
            continue
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            skipped += 1
            continue
        if task is not None and data.get("task") != task:
            continue
        records.append(data)
    return records, skipped


def pointer_path(directory: Path, task: str) -> Path:
    return _checked(directory, "task-%s.json" % check_task_name(task))


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    return "oc-%s-%s" % (stamp, _secrets.token_hex(4))


def reserve_run_id(directory: Path, clock: Callable[[], float] = time.time) -> Tuple[str, RunPaths]:
    """Pick an id and claim it by creating its sidecar with `O_EXCL`, so two
    concurrent callers can never share an id."""
    for _ in range(32):
        run_id = new_run_id(clock)
        paths = run_paths(directory, run_id)
        try:
            fd = os.open(str(paths.sidecar), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_id, paths
    raise RunStoreError("run-id-exhausted")


# ---------------------------------------------------------------------------
# sidecar
# ---------------------------------------------------------------------------


class SidecarWriter:
    """Append one event line per `os.write`, so a crash tears at most the
    final line."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.offset = os.fstat(self._fd).st_size

    def write(self, event: RuntimeEvent) -> int:
        data = (event.to_json() + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        self.offset += len(data)
        return self.offset

    def fsync(self) -> None:
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None  # type: ignore[assignment]

    def __enter__(self) -> "SidecarWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class SidecarRead:
    events: Tuple[RuntimeEvent, ...]
    byte_offset: int
    torn_tail: bool


def read_sidecar(path, *, repair: bool = False) -> SidecarRead:
    """Stream a sidecar. A final line that lacks a newline or does not parse
    is a torn tail: it is ignored (and, with `repair`, truncated away). A bad
    line anywhere else is corruption."""
    events: List[RuntimeEvent] = []
    good = 0
    torn = False
    try:
        handle = open(str(path), "rb")
    except FileNotFoundError:
        return SidecarRead((), 0, False)
    with handle:
        while True:
            line = handle.readline()
            if not line:
                break
            parsed = None
            if line.endswith(b"\n"):
                try:
                    parsed = RuntimeEvent.from_json(line[:-1].decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    parsed = None
            if parsed is None:
                if handle.read(1):
                    raise RunStoreError("corrupt-sidecar")
                torn = True
                break
            events.append(parsed)
            good += len(line)
    if torn and repair:
        fd = os.open(str(path), os.O_WRONLY)
        try:
            os.ftruncate(fd, good)
            os.fsync(fd)
        finally:
            os.close(fd)
    return SidecarRead(tuple(events), good, torn)


def last_event(path, tail_bytes: int = 65536) -> Tuple[Optional[RuntimeEvent], bool]:
    """The last complete event of a sidecar and whether the file ends in a
    torn tail, reading only the final `tail_bytes`. Never repairs the file and
    never follows a symlink. `(None, False)` when the file is absent, not a
    regular file, or holds no complete event in the window."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
    except OSError:
        return None, False
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None, False
            start = max(0, info.st_size - tail_bytes)
            handle.seek(start)
            data = handle.read(tail_bytes)
    except OSError:
        return None, False
    if not data:
        return None, False
    torn = not data.endswith(b"\n")
    lines = data.split(b"\n")
    if torn:
        lines.pop()  # the unterminated final fragment
    elif lines and lines[-1] == b"":
        lines.pop()
    if start > 0 and lines:
        lines.pop(0)  # may begin mid-line
    for line in reversed(lines):
        try:
            return RuntimeEvent.from_json(line.decode("utf-8")), torn
        except (ValueError, UnicodeDecodeError):
            continue
    return None, torn


# ---------------------------------------------------------------------------
# atomic JSON records
# ---------------------------------------------------------------------------


def atomic_write_json(path, obj: Any) -> None:
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    jsonio.write_private_atomic(path, (text + "\n").encode("utf-8"))


def read_json(path) -> Optional[Dict[str, Any]]:
    """The parsed record, `None` when absent; corrupt content is an error."""
    if not os.path.lexists(str(path)):
        return None
    try:
        data = jsonio.load_strict(path, file_label="run-record", max_bytes=MAX_RECORD_BYTES)
    except ConfigErrors:
        raise RunStoreError("corrupt-record") from None
    if not isinstance(data, dict):
        raise RunStoreError("corrupt-record")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RunStoreError("unsupported-schema")
    return data


def _now(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def new_run_record(
    run_id: str, task: str, request: Mapping[str, Any], prepared: Mapping[str, Any],
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    now = _now(clock)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": check_run_id(run_id),
        "task": check_task_name(task),
        "request": dict(request),
        "prepared": dict(prepared),
        "state": "prepared",
        "history": [{"state": "prepared", "at": now, "reason": None}],
        "attempts": [],
        "input_hashes": {},
        "repo_revisions": [],
        "counters_total": {},
        "usage_totals": {},
        "retry_usage": {},
        "stderr_tail": "",
        "refusal": None,
        "resume_blocked": None,
        "resume_hint": None,
        "created_at": now,
        "updated_at": now,
    }


def new_attempt(
    number: int, *, pid: Optional[int], pgid: Optional[int], child_start: Optional[str],
    driver_pid: int, driver_start: Optional[str], resume_mode: str,
    input_hashes_before: Optional[Mapping[str, Any]] = None,
    repo_revisions_before: Optional[Sequence[Mapping[str, Any]]] = None,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    return {
        "attempt": number,
        "pid": pid,
        "pgid": pgid,
        "child_start": child_start,
        "driver_pid": driver_pid,
        "driver_start": driver_start,
        "resume_mode": resume_mode,
        "input_hashes_before": dict(input_hashes_before or {}),
        "repo_revisions_before": [dict(r) for r in (repo_revisions_before or ())],
        "driver_lost": False,
        "started_at": _now(clock),
        "ended_at": None,
        "exit_code": None,
        "signal": None,
        "state": "running",
        "reason": None,
        "evidence": None,
        "counters": {},
        "descendants": [],
        "leftovers_reaped": False,
    }


def set_state(record: Dict[str, Any], state: str, reason: Optional[str],
              clock: Callable[[], float] = time.time) -> None:
    now = _now(clock)
    record["state"] = state
    record["history"].append({"state": state, "at": now, "reason": reason})
    record["updated_at"] = now


def new_checkpoint(
    run_id: str, attempt: int, *, last_sequence: int, sidecar_offset: int,
    native_session_id: Optional[str], repo_revisions: Sequence[Mapping[str, Any]],
    step_open: bool, ran_anything: bool, state_changing_part_ids: Iterable[str],
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": check_run_id(run_id),
        "attempt": attempt,
        "last_sequence": last_sequence,
        "sidecar_offset": sidecar_offset,
        "native_session_id": native_session_id,
        "repo_revisions": [dict(r) for r in repo_revisions],
        "step_open": bool(step_open),
        "ran_anything": bool(ran_anything),
        "state_changing_part_ids": sorted(set(state_changing_part_ids)),
        "updated_at": _now(clock),
    }


def new_pointer(task: str, run_id: str, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task": check_task_name(task),
        "run_id": check_run_id(run_id),
        "updated_at": _now(clock),
    }


def write_record(directory: Path, record: Mapping[str, Any]) -> None:
    atomic_write_json(run_paths(directory, record["run_id"]).record, record)


def load_record(directory: Path, run_id: str) -> Optional[Dict[str, Any]]:
    return read_json(run_paths(directory, run_id).record)


def write_checkpoint(directory: Path, checkpoint: Mapping[str, Any]) -> None:
    atomic_write_json(run_paths(directory, checkpoint["run_id"]).checkpoint, checkpoint)


def load_checkpoint(directory: Path, run_id: str) -> Optional[Dict[str, Any]]:
    return read_json(run_paths(directory, run_id).checkpoint)


def write_pointer(directory: Path, pointer: Mapping[str, Any]) -> None:
    atomic_write_json(pointer_path(directory, pointer["task"]), pointer)


def load_pointer(directory: Path, task: str) -> Optional[Dict[str, Any]]:
    return read_json(pointer_path(directory, task))


# ---------------------------------------------------------------------------
# run-wide facts and counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunFacts:
    ran_anything: bool
    delegation_downgrades: frozenset
    denied_halt_seen: bool
    last_attempt_step_open: bool


def run_facts(events: Iterable[RuntimeEvent]) -> RunFacts:
    """Facts that hold for the whole run, derived from a repaired sidecar.
    Anything that lowers confidence in a delegated result is collected from
    every attempt, so a later attempt can never hide it."""
    ran = False
    downgrades: Set[str] = set()
    denied_halt = False
    items = list(events)
    for event in items:
        if event.origin == "native":
            ran = True
        if event.type is EventType.PROGRESS:
            payload = event.payload
            if payload.delegation in ("background", "denied-tail", "failed"):
                downgrades.add(payload.delegation)
            if payload.kind == "halted" and payload.permission_outcome == "denied":
                denied_halt = True
    last_attempt = max((e.attempt for e in items), default=None)
    last_events = [e for e in items if e.attempt == last_attempt]
    return RunFacts(
        ran_anything=ran,
        delegation_downgrades=frozenset(downgrades),
        denied_halt_seen=denied_halt,
        last_attempt_step_open=summarize_steps(last_events).open_step if last_events else False,
    )


def add_counters(total: Mapping[str, int], attempt_counters: Mapping[str, int]) -> Dict[str, int]:
    """Sum per-attempt pipeline counters into run-wide totals."""
    out = dict(total)
    for key, value in attempt_counters.items():
        out[key] = out.get(key, 0) + int(value)
    return out


# ---------------------------------------------------------------------------
# input hashing
# ---------------------------------------------------------------------------


def _sha256_file(path: str, max_bytes: int) -> Any:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > max_bytes:
        return {"skipped": "too-large"}
    digest = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    # Read no more than the size that was measured and charged against the
    # caps; a file that keeps growing is reported as changed instead.
    remaining = info.st_size
    with os.fdopen(fd, "rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(_HASH_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            digest.update(chunk)
        if remaining == 0 and handle.read(1):
            return {"skipped": "changed"}
    return digest.hexdigest()


def _walk_files(top: str) -> Iterable[str]:
    for base, dirs, files in os.walk(top, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(files):
            yield os.path.join(base, name)


def hash_inputs(
    project_root, task: str, context_refs: Sequence[str] = (), *,
    max_files: int = DEFAULT_MAX_FILES, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    exclude: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    """sha256 of every regular file under the task folder and each context
    reference, keyed by project-relative path. Symlinks are skipped. A path
    for which `exclude(rel)` is true is dropped before it counts toward any
    cap."""
    root = os.path.realpath(str(project_root))
    tops = [os.path.join(str(project_root), ".workflow_artifacts", check_task_name(task))]
    for ref in context_refs:
        target = os.path.realpath(os.path.join(root, ref))
        if os.path.isabs(ref) or not (target == root or target.startswith(root + os.sep)):
            raise RunStoreError("unsafe-path")
        tops.append(os.path.join(root, ref))
    result: Dict[str, Any] = {}
    total = 0
    for top in tops:
        if os.path.islink(top):
            continue
        if os.path.isfile(top):
            files: Iterable[str] = [top]
        elif os.path.isdir(top):
            files = _walk_files(top)
        else:
            continue
        for path in files:
            rel = os.path.relpath(path, str(project_root)).replace(os.sep, "/")
            if exclude is not None and exclude(rel):
                continue
            if rel in result:
                continue
            if len(result) >= max_files:
                result["<truncated>"] = {"skipped": "file-cap"}
                return dict(sorted(result.items()))
            try:
                size = os.lstat(path).st_size
            except OSError:
                continue
            if size <= max_file_bytes:
                if total + size > max_total_bytes:
                    result["<truncated>"] = {"skipped": "byte-cap"}
                    return dict(sorted(result.items()))
                total += size
            value = _sha256_file(path, max_file_bytes)
            if value is not None:
                result[rel] = value
    return dict(sorted(result.items()))


def _sha_of(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def diff_hashes(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[ArtifactReferencePayload]:
    out: List[ArtifactReferencePayload] = []
    # A capped snapshot does not cover every file, so absence on either side
    # proves nothing: only report changes to files present in both.
    incomplete = "<truncated>" in before or "<truncated>" in after
    for path in sorted(set(before) | set(after)):
        if path == "<truncated>":
            continue
        if path not in before:
            if incomplete:
                continue
            change = "created"
        elif path not in after:
            if incomplete:
                continue
            change = "deleted"
        elif before[path] != after[path]:
            change = "modified"
        else:
            continue
        out.append(ArtifactReferencePayload(
            path=path, sha256_before=_sha_of(before.get(path)),
            sha256_after=_sha_of(after.get(path)), change=change,
        ))
    return out


# ---------------------------------------------------------------------------
# repository revisions
# ---------------------------------------------------------------------------

GitRunner = Callable[[Sequence[str], float], Tuple[int, str]]


def _default_git_runner(argv: Sequence[str], timeout_s: float) -> Tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        list(argv), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=timeout_s, check=False,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


GitBytesRunner = Callable[[Sequence[str], float, int], Tuple[int, bytes, bool]]


def _default_git_bytes_runner(argv: Sequence[str], timeout_s: float, max_bytes: int) -> Tuple[int, bytes, bool]:
    """Run git and return `(exit code, stdout bytes, truncated)`. Output past
    `max_bytes` kills the process and sets `truncated`; running past
    `timeout_s` raises `subprocess.TimeoutExpired`. The child leads its own
    session so the timeout also stops anything it started that still holds
    the output pipe."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.Popen(
        list(argv), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    fired = threading.Event()

    def _stop() -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - non-POSIX
                proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def _expire() -> None:
        fired.set()
        _stop()

    timer = threading.Timer(max(timeout_s, 0.0), _expire)
    timer.daemon = True
    timer.start()
    chunks: List[bytes] = []
    size = 0
    truncated = False
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(min(_HASH_CHUNK, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                truncated = True
                _stop()
                break
        proc.wait()
    finally:
        timer.cancel()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            _stop()
            proc.wait()
    if fired.is_set() and not truncated and proc.returncode == -signal.SIGKILL:
        raise subprocess.TimeoutExpired(list(argv), timeout_s)
    data = b"".join(chunks)
    del chunks
    if truncated:
        data = data[:max_bytes]
    return proc.returncode, data, truncated


def source_pathspecs(repo: str, project_root: str, repos: Sequence[str]) -> List[str]:
    """Pathspecs that scope a repository's source state: the whole tree minus
    the project's output directories and every other repository nested inside
    this one. Names are matched literally."""
    specs = {"."}

    def add(target: str) -> None:
        rel = os.path.relpath(target, repo)
        if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            return
        specs.add(":(exclude,literal)" + rel.replace(os.sep, "/"))

    for name in SOURCE_EXCLUDED_DIRS:
        add(os.path.join(project_root, name))
    for other in repos:
        if other != repo:
            add(other)
    return sorted(specs)


def _source_digest(
    repo: str, specs: Sequence[str], bytes_run: GitBytesRunner, deadline: float,
    max_source_bytes: int, max_untracked_files: int,
) -> Tuple[Optional[str], Optional[str]]:
    """`(digest, error)` for a dirty tree; exactly one of them is set."""

    def timeout() -> float:
        return min(GIT_TIMEOUT_S, deadline - time.monotonic())

    if timeout() <= 0:
        return None, "budget"
    try:
        code, diff, cut = bytes_run(
            ("git", "-C", repo, *_SOURCE_GIT_CONFIG, "-c", "core.quotepath=off", "diff", "--binary", "--no-ext-diff",
             "--no-textconv", "--no-color", "--no-renames", "HEAD", "--", *specs),
            timeout(), max_source_bytes,
        )
        if cut:
            return None, "too-large"
        if code != 0:
            return None, "git-failed"
        if timeout() <= 0:
            return None, "budget"
        code, listing, cut = bytes_run(
            ("git", "-C", repo, *_SOURCE_GIT_CONFIG, "ls-files", "-z", "--others", "--exclude-standard", "--", *specs),
            timeout(), max_source_bytes,
        )
    except subprocess.TimeoutExpired:
        return None, "budget"
    except (OSError, subprocess.SubprocessError):
        return None, "git-failed"
    if cut:
        return None, "too-large"
    if code != 0:
        return None, "git-failed"
    if listing.count(b"\0") > max_untracked_files:
        return None, "too-many-files"
    entries = sorted(item for item in listing.split(b"\0") if item)
    if len(entries) > max_untracked_files:
        return None, "too-many-files"
    digest = hashlib.sha256()
    digest.update(b"quoin-source/1\0diff\0")
    digest.update(diff)
    digest.update(b"\0untracked\0")
    remaining = max_source_bytes - len(diff)
    for raw in entries:
        if timeout() <= 0:
            return None, "budget"
        full = os.path.join(repo, os.fsdecode(raw))
        try:
            info = os.lstat(full)
        except OSError:
            return None, "changed-during-hash"
        if stat.S_ISLNK(info.st_mode):
            try:
                kind, sha = "l", hashlib.sha256(os.fsencode(os.readlink(full))).hexdigest()
            except OSError:
                return None, "changed-during-hash"
        elif stat.S_ISREG(info.st_mode):
            value = _sha256_file(full, max(remaining, 0))
            if isinstance(value, dict):
                return None, "changed-during-hash" if value.get("skipped") == "changed" else "too-large"
            if value is None:
                return None, "changed-during-hash"
            remaining -= info.st_size
            if remaining < 0:
                return None, "too-large"
            kind, sha = "f", value
        elif stat.S_ISDIR(info.st_mode):
            kind, sha = "d", ""
        else:
            kind, sha = "o", ""
        digest.update(raw + b"\0" + kind.encode("ascii") + b"\0" + sha.encode("ascii") + b"\0")
    return digest.hexdigest(), None


def _source_state(
    repo: str, project_root: str, repos: Sequence[str], run: GitRunner, bytes_run: GitBytesRunner,
    deadline: float, max_source_bytes: int, max_untracked_files: int,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"source_dirty": None, "source_digest": None, "source_error": None}
    specs = source_pathspecs(repo, project_root, repos)
    left = min(GIT_TIMEOUT_S, deadline - time.monotonic())
    if left <= 0:
        out["source_error"] = "budget"
        return out
    try:
        code, text = run(
            ("git", "-C", repo, *_SOURCE_GIT_CONFIG, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
             "--ignore-submodules=none", "--", *specs),
            left,
        )
    except subprocess.TimeoutExpired:
        out["source_error"] = "budget"
        return out
    except (OSError, subprocess.SubprocessError):
        out["source_error"] = "git-failed"
        return out
    if code != 0:
        out["source_error"] = "git-failed"
        return out
    out["source_dirty"] = bool(text.strip("\0 \n"))
    if out["source_dirty"]:
        out["source_digest"], out["source_error"] = _source_digest(
            repo, specs, bytes_run, deadline, max_source_bytes, max_untracked_files,
        )
    return out


def _repo_entry(
    repo: str, project_root: str, run: GitRunner, deadline: float, source: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    rel = os.path.relpath(repo, project_root).replace(os.sep, "/")
    entry: Dict[str, Any] = {"path": rel, "head": None, "dirty": None, "error": None}
    def timeout() -> float:
        return min(GIT_TIMEOUT_S, deadline - time.monotonic())

    def finish() -> Dict[str, Any]:
        if source is not None:
            if entry["error"] is not None:
                entry.update(source_dirty=None, source_digest=None,
                             source_error="budget" if entry["error"] == "budget" else "git-failed")
            else:
                entry.update(_source_state(
                    repo, project_root, source["repos"], run, source["bytes_runner"], deadline,
                    source["max_source_bytes"], source["max_untracked_files"],
                ))
        return entry

    if timeout() <= 0:
        entry["error"] = "budget"
        return finish()
    try:
        code, out = run(("git", "-C", repo, "rev-parse", "HEAD"), timeout())
        if code != 0 or not out.strip():
            entry["error"] = "rev-parse-failed"
            return finish()
        entry["head"] = out.strip()
        if timeout() <= 0:
            entry["error"] = "budget"
            return finish()
        code, out = run(("git", "-C", repo, "status", "--porcelain"), timeout())
        if code != 0:
            entry["error"] = "status-failed"
            return finish()
        entry["dirty"] = bool(out.strip())
    except (OSError, subprocess.SubprocessError):
        entry["error"] = "git-unavailable"
    return finish()


def repo_revisions(
    project_root, *, runner: Optional[GitRunner] = None, budget_s: float = REVISIONS_BUDGET_S,
    source: bool = False, bytes_runner: Optional[GitBytesRunner] = None,
    max_source_bytes: int = SOURCE_DIGEST_MAX_BYTES, max_untracked_files: int = SOURCE_MAX_UNTRACKED,
) -> List[Dict[str, Any]]:
    """Head and dirtiness of the worktree holding the project root plus each
    immediate subdirectory that holds its own `.git`.

    With `source=True` every entry also carries `source_dirty`, `source_digest`
    and `source_error`: the tree's state with the project's output directories
    and nested repositories left out, so a write into those never reads as a
    source change. Repositories are found only as immediate subdirectories; an
    untracked nested repository deeper than that is hashed as a bare directory
    entry and a submodule appears only as a gitlink plus a dirty marker, so
    edits inside either are not reflected in the digest."""
    run = runner or _default_git_runner
    root = os.path.realpath(str(project_root))
    repos: List[str] = []
    top = git_worktree_root(Path(root))
    if top is not None:
        repos.append(str(top))
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        child = os.path.join(root, name)
        if os.path.isdir(child) and not os.path.islink(child) and os.path.lexists(os.path.join(child, ".git")):
            if child not in repos:
                repos.append(child)
        if len(repos) >= MAX_SUBREPOS:
            break
    deadline = time.monotonic() + budget_s
    options: Optional[Dict[str, Any]] = None
    if source:
        options = {
            "repos": list(repos), "bytes_runner": bytes_runner or _default_git_bytes_runner,
            "max_source_bytes": max_source_bytes, "max_untracked_files": max_untracked_files,
        }
    return [_repo_entry(repo, root, run, deadline, options) for repo in repos]


# ---------------------------------------------------------------------------
# workflow state
# ---------------------------------------------------------------------------

WORKFLOW_KIND = "quoin-opencode-workflow"
WORKFLOW_PHASES = ("discover", "architect", "plan", "critic", "implement", "review")
GATED_PHASES = ("discover", "architect", "plan", "implement", "review")
EVIDENCE_ORIGINS = ("coordinator", "phase-run", "adopted", "continuation")
STAGELESS_PHASES = ("discover", "architect")

# Run phases each gated phase accepts, taken from the commands the shipped
# feature manifest marks supported. A plan is produced by `plan` or by
# `thorough_plan` and may also list the critic runs of its loop; `revise` has
# no supported command, so a run of it never completes and is not listed.
RUN_PHASES_FOR = {
    "discover": frozenset({"discover"}),
    "architect": frozenset({"architect"}),
    "plan": frozenset({"plan", "thorough_plan", "critic"}),
    "implement": frozenset({"implement"}),
    "review": frozenset({"review"}),
}
PLAN_PRODUCER_PHASES = frozenset({"plan", "thorough_plan"})

# These phases have a supported OpenCode command and run under `--phase`, but
# never record a gated entry: a gate run evaluates entries, checkpoint and
# continue_work only save or restore session state, and end_of_task ships work
# that was already gated. A phase added to the manifest as runnable must be
# mapped by `entry_phase_for_run` or listed here.
UNMAPPED_RUN_PHASES = ("gate", "checkpoint", "continue_work", "end_of_task")

_ENTRY_PHASE_FOR_RUN = {
    "discover": "discover",
    "architect": "architect",
    "plan": "plan",
    "thorough_plan": "plan",
    "critic": "plan",
    "implement": "implement",
    "review": "review",
}


def normalize_phase(phase: Any) -> str:
    """The run phase spelling used everywhere: hyphens become underscores."""
    return str(phase).replace("-", "_")


def normalize_stage(value: Any) -> Optional[int]:
    """A stage as an int or None. The CLI passes `--stage` through as text,
    so digit strings are accepted; anything else raises `ValueError`."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid stage")
    if isinstance(value, int):
        if value >= 1:
            return value
        raise ValueError("invalid stage")
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) >= 1:
        return int(value)
    raise ValueError("invalid stage")


def entry_phase_for_run(run_phase: Any) -> Optional[str]:
    """The gated entry a single-phase run records, or None when it records
    none. A critic run changes the plan stage's evidence, so it maps to plan."""
    return _ENTRY_PHASE_FOR_RUN.get(normalize_phase(run_phase))


def workflow_state_path(directory: Path, task: str) -> Path:
    return _checked(directory, "workflow-%s.json" % check_task_name(task))


def new_workflow_state(task: str, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    now = _now(clock)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKFLOW_KIND,
        "task": check_task_name(task),
        "created_at": now,
        "updated_at": now,
        "settings": {},
        "entries": [],
    }


def load_workflow_state(directory: Path, task: str) -> Optional[Dict[str, Any]]:
    """The task's workflow state, `None` when absent. A damaged, foreign or
    other-task file is an error, never silently replaced."""
    data = read_json(workflow_state_path(directory, task))
    if data is None:
        return None
    if (
        data.get("kind") != WORKFLOW_KIND
        or not isinstance(data.get("entries"), list)
        or not isinstance(data.get("settings"), dict)
    ):
        raise RunStoreError("corrupt-record")
    for item in data["entries"]:
        if not isinstance(item, dict) or not isinstance(item.get("origin"), str) or not isinstance(item.get("phase"), str):
            raise RunStoreError("corrupt-record")
        for key in ("runs", "critic_responses", "harvested"):
            if item.get(key) is not None and not isinstance(item[key], list):
                raise RunStoreError("corrupt-record")
        for key in ("runs", "critic_responses"):
            if any(not isinstance(value, str) for value in item.get(key) or []):
                raise RunStoreError("corrupt-record")
        if item.get("evidence") is not None and not isinstance(item["evidence"], dict):
            raise RunStoreError("corrupt-record")
        stage = item.get("stage")
        if stage is not None and (not isinstance(stage, int) or isinstance(stage, bool) or stage < 1):
            raise RunStoreError("corrupt-record")
    if data.get("task") != task:
        raise RunStoreError("state-task-mismatch")
    return data


def write_workflow_state(directory: Path, state: Mapping[str, Any]) -> None:
    atomic_write_json(workflow_state_path(directory, state["task"]), state)


def _new_entry(entry: Mapping[str, Any], clock: Callable[[], float]) -> Dict[str, Any]:
    phase = entry.get("phase")
    origin = entry.get("origin")
    if phase not in WORKFLOW_PHASES:
        raise ValueError("invalid phase")
    if origin not in EVIDENCE_ORIGINS:
        raise ValueError("invalid origin")
    stage = normalize_stage(entry.get("stage"))
    if phase in STAGELESS_PHASES and stage is not None:
        raise ValueError("stage not allowed for this phase")
    full: Dict[str, Any] = {
        "stage": stage,
        "phase": phase,
        "origin": origin,
        "recorded_at": _now(clock),
        "superseded": False,
        "runs": [],
        "boundary": None,
        "critic_responses": [],
        "harvested": [],
        "envelope_path": None,
        "tests": None,
        "continuation_validation": None,
        "ledger_uuids": [],
        "ledger_lines_appended_during_run": [],
        "evidence": {},
        "gate": None,
    }
    for key, value in entry.items():
        if key not in ("stage", "phase", "origin", "superseded"):
            full[key] = value
    return full


def record_phase_entry(
    state: Dict[str, Any], entry: Mapping[str, Any], clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Append an entry; every earlier live entry for the same stage and phase
    is kept as history and marked superseded."""
    new = _new_entry(entry, clock)
    for old in state["entries"]:
        if old.get("stage") == new["stage"] and old.get("phase") == new["phase"] and not old.get("superseded"):
            old["superseded"] = True
    state["entries"].append(new)
    state["updated_at"] = _now(clock)
    return new


def current_entry(state: Mapping[str, Any], stage: Optional[int], phase: str) -> Optional[Dict[str, Any]]:
    """The latest non-superseded entry for a stage and phase."""
    for entry in reversed(state.get("entries") or []):
        if entry.get("stage") == stage and entry.get("phase") == phase and not entry.get("superseded"):
            return entry
    return None


def update_current_entry(state: Dict[str, Any], stage: Optional[int], phase: str, **fields: Any) -> Optional[Dict[str, Any]]:
    entry = current_entry(state, stage, phase)
    if entry is not None:
        entry.update(fields)
    return entry


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------


def _last_attempt(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    attempts = record.get("attempts") or []
    return attempts[-1] if attempts else None


def orphan_state(record: Mapping[str, Any], table: Optional[Mapping[int, ProcInfo]]) -> Optional[str]:
    """`"driver-lost"` when the run says it is running but the driver process
    that recorded it is gone. Unknown (`None`) when the table is unavailable."""
    if record.get("state") != "running" or not table:
        return None
    attempt = _last_attempt(record)
    if not attempt or attempt.get("driver_pid") is None or attempt.get("driver_start") is None:
        return None
    if alive(Identity(attempt["driver_pid"], attempt["driver_start"]), table):
        return None
    return "driver-lost"


def pid_alive(pid: Any, table: Optional[Mapping[int, ProcInfo]]) -> Optional[bool]:
    """Whether `pid` is a live (non-zombie) entry of a process table taken
    once for the whole invocation. `None` when there is no table: liveness is
    then unknown, never guessed and never probed with a signal."""
    if table is None:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    info = table.get(pid)
    return info is not None and not info.zombie


def live_identities(record: Mapping[str, Any], table: Optional[Mapping[int, ProcInfo]]) -> List[Identity]:
    """Recorded child and descendants whose identity still matches, plus live
    members of the recorded process group while that group is still anchored
    by a matching process."""
    attempt = _last_attempt(record)
    if not attempt or not table:
        return []
    found: Dict[int, Identity] = {}
    recorded: List[Identity] = []
    if attempt.get("pid") is not None and attempt.get("child_start") is not None:
        recorded.append(Identity(attempt["pid"], attempt["child_start"]))
    for item in attempt.get("descendants") or []:
        recorded.append(Identity(item["pid"], item["start"]))
    for ident in recorded:
        if alive(ident, table):
            found[ident.pid] = ident
    pgid = attempt.get("pgid")
    if pgid is not None and any(table[i.pid].pgid == pgid for i in found.values()):
        for info in group_members(pgid, table):
            found.setdefault(info.pid, Identity(info.pid, info.start))
    return [found[pid] for pid in sorted(found)]
