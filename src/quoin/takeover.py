"""quoin.takeover — hand an autonomous run's headless child over to a human.

``quoin run --takeover <task>`` stops the supervisor and its headless
``claude`` child, proves both are gone, and only then prints the command that
resumes the child's session interactively. Two writers on one session
transcript would corrupt it, so the resume command is never printed while
anything that carries the child's session id may still be running.

Every side effect (signals, process scans, files the run owns, output) goes
through :class:`TakeoverOps`, so the whole sequence is testable with a fake
process table. Stdlib only.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from quoin import supervisor as _supervisor

#: Mirrors auto_resume.ARM_TEMPLATE (the file that arms an in-session
#: continuation for a session id); parity-tested.
ARM_TEMPLATE = "run-continue-arm-{sid}.txt"

#: Mirrors run_state._TASK_RE; parity-tested.
_TASK_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_TASK_RE = re.compile(_TASK_PATTERN)

#: Halt reason written by a takeover (listed in the autonomous-mode docs).
HALT_REASON = "taken over by user"

_POLL_SECS = 0.2
_PREVIOUS_SPAN_TOLERANCE_SECS = 10.0
_GRACE_ENV = "QUOIN_TAKEOVER_GRACE_SECS"
_WAIT_ENV = "QUOIN_TAKEOVER_WAIT_SECS"

EXIT_OK = 0
EXIT_NO_CHILD = 1
EXIT_BAD_TASK = 2
EXIT_UNSAFE = 4


def _clamp_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _valid_task(task: object) -> bool:
    return isinstance(task, str) and _TASK_RE.match(task) is not None and ".." not in task


@dataclass
class TakeoverOps:
    pid_alive: Callable[[int], bool]
    cmdline: Callable[[int], Optional[str]]
    find_pids_with_arg: Callable[[str], Optional[List[int]]]
    transcript_exists: Callable[[str], bool]
    kill: Callable[[int, int], object]
    sleep: Callable[[float], object]
    monotonic: Callable[[], float]
    write_halt: Callable[[str], bool]
    remove_arm: Callable[[str], object]
    out: Callable[[str], object]
    err: Callable[[str], object]
    repo_root: Callable[[object], Path]
    read_halt_reason: Callable[[], Optional[str]] = lambda: None  # noqa: E731


# ---------------------------------------------------------------------------
# Real implementations
# ---------------------------------------------------------------------------


def _ps(argv: "list[str]", timeout: float):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _real_cmdline(pid: int) -> Optional[str]:
    text = _ps(["ps", "-ww", "-o", "command=", "-p", str(pid)], 5)
    if text is None:
        return None
    text = text.strip()
    return text or None


def parse_ps_pids_with_arg(text: str, sid: str, own_pid: int) -> Optional[List[int]]:
    """Pids whose argv holds ``--session-id <sid>`` as two whole tokens.

    Returns None when the listing cannot be trusted (a line that does not
    start with a pid, or nothing parsed), so an unreadable scan never reads
    as "nothing is running".
    """
    found: List[int] = []
    parsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        tokens = line.split()
        try:
            pid = int(tokens[0])
        except ValueError:
            return None
        parsed += 1
        if pid == own_pid:
            continue
        for i in range(1, len(tokens) - 1):
            if tokens[i] == "--session-id" and tokens[i + 1] == sid:
                found.append(pid)
                break
    if parsed == 0:
        return None
    return found


def _real_find_pids_with_arg(sid: str) -> Optional[List[int]]:
    text = _ps(["ps", "-ww", "-A", "-o", "pid=,args="], 10)
    if text is None:
        return None
    return parse_ps_pids_with_arg(text, sid, os.getpid())


def _real_transcript_exists(sid: str) -> bool:
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return bool(glob.glob(str(Path(root) / "projects" / "*" / f"{sid}.jsonl")))


def _real_kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def real_ops(task: str, project_root: Path) -> TakeoverOps:
    from quoin import cli as _cli  # noqa: PLC0415  (function-level: no import cycle)

    paths = _cli._supervisor_paths(Path(project_root), task)
    memory_dir = paths["memory_dir"]
    halt_path = paths["halt"]

    def write_halt(content: str) -> bool:
        memory_dir.mkdir(parents=True, exist_ok=True)
        tmp = halt_path.with_name(halt_path.name + f".{os.getpid()}.takeover.tmp")
        tmp.write_text(content)
        try:
            os.link(str(tmp), str(halt_path))  # publishes fully written, never overwrites
        except FileExistsError:
            return False
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return True

    def remove_arm(sid: str) -> None:
        try:
            (memory_dir / ARM_TEMPLATE.format(sid=sid)).unlink()
        except OSError:
            pass

    def read_halt_reason() -> Optional[str]:
        return _supervisor.read_halt(task, project_root)

    return TakeoverOps(
        pid_alive=_cli._pid_alive,
        cmdline=_real_cmdline,
        find_pids_with_arg=_real_find_pids_with_arg,
        transcript_exists=_real_transcript_exists,
        kill=_real_kill,
        sleep=time.sleep,
        monotonic=time.monotonic,
        write_halt=write_halt,
        remove_arm=remove_arm,
        out=print,
        err=lambda line: print(line, file=sys.stderr),
        repo_root=_supervisor.resolve_repo_root,
        read_halt_reason=read_halt_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokens(cmdline: Optional[str]) -> "list[str]":
    return (cmdline or "").split()


def _is_supervisor_cmd(toks: "list[str]", task: str) -> bool:
    """True for ``... run --autonomous <task> ...`` (flag order after ``run`` free)."""
    if "run" not in toks:
        return False
    rest = toks[toks.index("run") + 1:]
    positional = [t for t in rest if not t.startswith("-")]
    return "--autonomous" in rest and bool(positional) and positional[0] == task


def _has_session_pair(toks: "list[str]", sid: Optional[str]) -> bool:
    """True when ``--session-id <sid>`` appears as an adjacent pair."""
    if not sid:
        return False
    return any(a == "--session-id" and b == sid for a, b in zip(toks, toks[1:]))


def _int_pid(value: object) -> Optional[int]:
    try:
        pid = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _halt_text(task: str, record: dict, sid: Optional[str], project_root: Path) -> str:
    phase = record.get("phase") or "run"
    resume = record.get("resume_command") or f"/run --resume {task}"
    hint = _supervisor.takeover_hint(task, project_root, sid)
    return (
        f"task: {task}\n"
        f"phase: {phase}\n"
        f"reason: {HALT_REASON}\n"
        f"timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"resume_hint: {resume}\n"
        f"takeover_hint: {hint}\n"
    )


def run_takeover(task: str, project_root: Path, ops: Optional[TakeoverOps] = None) -> int:
    """Stop the run for ``task`` and print the interactive resume command.

    Exit codes: 0 resume command printed; 1 no child recorded or none ever
    started; 2 invalid task name; 4 something that may still write to the
    session survives or cannot be verified (no resume command is printed).
    """
    if not _valid_task(task):
        print(f"quoin run --takeover: invalid task name {task!r}", file=sys.stderr)
        return EXIT_BAD_TASK

    from quoin import cli as _cli  # noqa: PLC0415

    project_root = Path(project_root)
    if ops is None:
        ops = real_ops(task, project_root)
    grace = _clamp_int(_GRACE_ENV, 5, 1, 60)
    wait_bound = _clamp_int(_WAIT_ENV, 20, 1, 300)

    paths = _cli._supervisor_paths(project_root, task)
    record_path = paths["memory_dir"] / f"run-state-{task}.json"

    def read(path: Path) -> dict:
        data = _cli._read_json(path)
        return data if isinstance(data, dict) else {}

    lock0 = read(paths["lock"])
    rec0 = read(record_path)

    def valid_sid(value: object) -> Optional[str]:
        return value if _supervisor.is_child_session_id(value) else None

    sid_hint = valid_sid(lock0.get("child_session_id")) or valid_sid(rec0.get("child_session_id"))

    # Halt first: it keeps every relauncher (supervisor loop, Stop hook,
    # SessionStart hook) from starting a new child while we stop the old one.
    if not ops.write_halt(_halt_text(task, rec0, sid_hint, project_root)):
        ops.err(f"halt already present: {ops.read_halt_reason() or 'no reason recorded'}")

    def term_then_kill(pid: int) -> None:
        import signal  # noqa: PLC0415

        ops.kill(pid, signal.SIGTERM)
        start = ops.monotonic()
        while ops.pid_alive(pid) and ops.monotonic() - start < grace:
            ops.sleep(_POLL_SECS)
        if ops.pid_alive(pid):
            ops.kill(pid, signal.SIGKILL)
            start = ops.monotonic()
            while ops.pid_alive(pid) and ops.monotonic() - start < wait_bound:
                ops.sleep(_POLL_SECS)

    # -- supervisor -----------------------------------------------------
    sup_pid = _int_pid(lock0.get("pid"))
    sup_state = "absent"
    if sup_pid is not None and ops.pid_alive(sup_pid):
        cl = ops.cmdline(sup_pid)
        if cl is None:
            sup_state = "unverifiable" if ops.pid_alive(sup_pid) else "dead"
        else:
            toks = _tokens(cl)
            if _is_supervisor_cmd(toks, task):
                term_then_kill(sup_pid)
                sup_state = "survivor" if ops.pid_alive(sup_pid) else "dead"
            else:
                # A recycled pid or another task's supervisor: never signalled.
                sup_state = "not-supervisor"
    if sup_state == "unverifiable":
        ops.err(f"cannot verify supervisor pid {sup_pid} (process listing failed)")
        return EXIT_UNSAFE
    if sup_state == "survivor":
        ops.err(f"supervisor pid {sup_pid} is still running after SIGKILL")
        return EXIT_UNSAFE

    # -- child ----------------------------------------------------------
    rec1 = read(record_path)
    lock1 = read(paths["lock"])
    # Newest started session first; ties keep lock0, lock1, record order.
    seen: dict = {}
    for snap in (lock0, lock1, rec1):
        s = valid_sid(snap.get("child_session_id"))
        if not s:
            continue
        ts = _parse_ts(snap.get("child_started_at"))
        prev = seen.get(s)
        if prev is None:
            seen[s] = [snap.get("child_started_at"), ts]
        elif prev[1] is None and ts is not None:
            seen[s] = [snap.get("child_started_at"), ts]
    order = list(seen)
    order.sort(key=lambda x: (seen[x][1] is not None,
                              seen[x][1].timestamp() if seen[x][1] is not None else 0.0),
               reverse=True)
    sids: List[str] = order
    if not sids:
        ops.err(f"no child session recorded for {task}")
        return EXIT_NO_CHILD

    handled: List[int] = []
    for snap in (lock0, lock1):
        cpid = _int_pid(snap.get("child_pid"))
        csid = valid_sid(snap.get("child_session_id"))
        if cpid is None or cpid in handled or not ops.pid_alive(cpid):
            continue
        handled.append(cpid)
        cl = ops.cmdline(cpid)
        if cl is None:
            if ops.pid_alive(cpid):
                ops.err(f"cannot verify recorded child pid {cpid}")
                return EXIT_UNSAFE
        elif _has_session_pair(_tokens(cl), csid):
            term_then_kill(cpid)

    for s in sids:
        for pid in ops.find_pids_with_arg(s) or []:
            if ops.pid_alive(pid) and _has_session_pair(_tokens(ops.cmdline(pid)), s):
                term_then_kill(pid)

    for s in sids:
        left = ops.find_pids_with_arg(s)
        if left is None:
            ops.err("cannot verify child processes are stopped (process scan failed)")
            return EXIT_UNSAFE
        if left:
            ops.err(f"child still running: pids {left}")
            return EXIT_UNSAFE

    started = [s for s in sids if ops.transcript_exists(s)]
    if not started:
        ops.err(f"recorded child sessions {', '.join(sids)} never started")
        return EXIT_NO_CHILD
    sid = started[0]
    for s in sids:
        if s not in started:
            ops.err(f"session {s} never started")

    ops.remove_arm(sid)

    cwd: Optional[str] = None
    for lock in (lock0, lock1):
        if lock.get("child_session_id") == sid and lock.get("child_cwd"):
            cwd = str(lock["child_cwd"])
            break
    if cwd is None and rec1.get("child_cwd"):
        cwd = str(rec1["child_cwd"])
    if cwd is None:
        cwd = str(ops.repo_root(project_root))

    ops.out(_supervisor.takeover_command(cwd, sid))

    started_at = seen[sid][0]
    label = ""
    child_ts = _parse_ts(started_at)
    lock_ts = _parse_ts(lock0.get("started_at"))
    if child_ts is not None and lock_ts is not None:
        if (lock_ts - child_ts).total_seconds() > _PREVIOUS_SPAN_TOLERANCE_SECS:
            label = " (previous span)"
    if started_at:
        ops.err(f"child session {sid} started {started_at}{label}")
    return EXIT_OK
