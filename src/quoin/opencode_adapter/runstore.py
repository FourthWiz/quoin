"""On-disk run store: ids, the event sidecar, atomic records, hashes, revisions.

Layout, under `memory/runtime/opencode/` in the project's artifact root:

    RUN_ID.jsonl            append-only event sidecar
    RUN_ID.run.json         run record (rewritten atomically)
    RUN_ID.checkpoint.json  resume checkpoint (rewritten atomically)
    task-TASK.json          pointer from a task to its latest run

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
import stat
import subprocess
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
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
GIT_TIMEOUT_S = 30.0
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
    """Run records in the store, read-only: up to `limit` record files in name
    order (filtered by task when given) and the number of files skipped.

    A symlink, an unreadable or unparsable file, and every file beyond the
    limit are skipped and counted; a record is never repaired or rewritten."""
    records: List[Dict[str, Any]] = []
    skipped = 0
    try:
        names_ = sorted(
            entry.name for entry in os.scandir(str(directory)) if _RECORD_FILE_RE.match(entry.name)
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
) -> Dict[str, Any]:
    """sha256 of every regular file under the task folder and each context
    reference, keyed by project-relative path. Symlinks are skipped."""
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


def _repo_entry(repo: str, project_root: str, run: GitRunner, deadline: float) -> Dict[str, Any]:
    rel = os.path.relpath(repo, project_root).replace(os.sep, "/")
    entry: Dict[str, Any] = {"path": rel, "head": None, "dirty": None, "error": None}
    def timeout() -> float:
        return min(GIT_TIMEOUT_S, deadline - time.monotonic())

    if timeout() <= 0:
        entry["error"] = "budget"
        return entry
    try:
        code, out = run(("git", "-C", repo, "rev-parse", "HEAD"), timeout())
        if code != 0 or not out.strip():
            entry["error"] = "rev-parse-failed"
            return entry
        entry["head"] = out.strip()
        if timeout() <= 0:
            entry["error"] = "budget"
            return entry
        code, out = run(("git", "-C", repo, "status", "--porcelain"), timeout())
        if code != 0:
            entry["error"] = "status-failed"
            return entry
        entry["dirty"] = bool(out.strip())
    except (OSError, subprocess.SubprocessError):
        entry["error"] = "git-unavailable"
    return entry


def repo_revisions(
    project_root, *, runner: Optional[GitRunner] = None, budget_s: float = REVISIONS_BUDGET_S,
) -> List[Dict[str, Any]]:
    """Head and dirtiness of the worktree holding the project root plus each
    immediate subdirectory that holds its own `.git`."""
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
    return [_repo_entry(repo, root, run, deadline) for repo in repos]


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
