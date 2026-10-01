#!/usr/bin/env python3
"""Observe a handed-off autonomous run from the parent session.

After ``auto_resume.py handoff`` starts a supervisor and a headless child, the
interactive parent session that handed off has nothing to look at. This helper
is one watch window: it polls the run's files for a bounded time, prints exactly
one line, and exits. The parent re-arms it (the line carries the exact command)
until a terminal state is reported.

Line format (one line on stdout, always)::

    WATCH|<STATE>|task=<task>|detail=<text>|takeover=<pointer>|next=<cmd>|observe-only

States: DONE, HALTED, NEEDS_DECISION, DEAD, EXPIRED (terminal, exit 10);
PROGRESS, ALIVE, STALL (non-terminal, exit 0); ERROR (invalid input, exit 2).
``next=`` is ``report-and-stop`` for terminal states and ERROR, otherwise the
re-arm command.

Signals per window: the done, halt and needs-decision sentinels, new ``.done``
completion sentinels, new commits on the task branch, and supervisor/child
liveness named by the supervisor lock.

Knobs (absent or invalid values fall back to the default; values are clamped):
QUOIN_CHILD_WATCH_INTERVAL_SECS (600, 60..1500), QUOIN_CHILD_WATCH_STALL_WINDOWS
(3, 1..48), QUOIN_CHILD_WATCH_MAX_HOURS (12, 1..72).

The helper is observe-only: it reads files, runs ``git rev-parse`` and ``ps``
with fixed argv, and never signals a process, relaunches, or takes over. State
persists in ``child-watch-<task>.json`` under the memory directory; ``--once``
writes nothing.

Runs on a bare system Python 3.8.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

_AR_PATH = Path(__file__).resolve().parent / "auto_resume.py"
_AR_SPEC = importlib.util.spec_from_file_location("_quoin_child_watch_auto_resume", _AR_PATH)
assert _AR_SPEC is not None
_ar = importlib.util.module_from_spec(_AR_SPEC)
assert _AR_SPEC.loader is not None
sys.modules["_quoin_child_watch_auto_resume"] = _ar
_AR_SPEC.loader.exec_module(_ar)

STATE_TEMPLATE = "child-watch-{task}.json"
STATE_SCHEMA = 1
NOTE_PREFIX = "[quoin-child-watch]"
EXIT_NONTERMINAL = 0
EXIT_TERMINAL = 10
EXIT_ERROR = 2
DEAD_GRACE_SECS = 5.0
PS_TIMEOUT_SECS = 5.0

TERMINAL_STATES = ("DONE", "HALTED", "NEEDS_DECISION", "DEAD", "EXPIRED")
NONTERMINAL_STATES = ("PROGRESS", "ALIVE", "STALL")

INTERVAL_KNOB = "QUOIN_CHILD_WATCH_INTERVAL_SECS"
STALL_KNOB = "QUOIN_CHILD_WATCH_STALL_WINDOWS"
MAX_HOURS_KNOB = "QUOIN_CHILD_WATCH_MAX_HOURS"
GRACE_KNOB = "QUOIN_CHILD_WATCH_DEAD_GRACE_SECS"
INTERVAL_DEFAULT, INTERVAL_MIN, INTERVAL_MAX = 600, 60, 1500
STALL_DEFAULT, STALL_MIN, STALL_MAX = 3, 1, 48
MAX_HOURS_DEFAULT, MAX_HOURS_MIN, MAX_HOURS_MAX = 12, 1, 72
POLL_DEFAULT, POLL_MIN, POLL_MAX = 30, 5, 300

_FIELD_MAX = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACES_RE = re.compile(r" {2,}")


class WatchError(Exception):
    """Invalid input; reported as one ERROR line."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D401 - argparse hook
        raise WatchError(message)


class Deps:
    """Injectable collaborators (replaced by fakes in tests)."""

    def __init__(self):
        self.clock = time.time
        self.sleep = time.sleep
        self.pid_alive = _ar._pid_alive
        self.cmdline = _ps_cmdline
        self.probe = _ar._probe_task_heads


def _ps_cmdline(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=PS_TIMEOUT_SECS,
        )
    except Exception:  # noqa: BLE001 - no ps means the check is skipped
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def _env_number(name: str, default, lo, hi, cast):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(lo, min(hi, value))


def _interval_secs() -> int:
    return _env_number(INTERVAL_KNOB, INTERVAL_DEFAULT, INTERVAL_MIN, INTERVAL_MAX, int)


def _stall_windows() -> int:
    return _env_number(STALL_KNOB, STALL_DEFAULT, STALL_MIN, STALL_MAX, int)


def _max_hours() -> int:
    return _env_number(MAX_HOURS_KNOB, MAX_HOURS_DEFAULT, MAX_HOURS_MIN, MAX_HOURS_MAX, int)


def _grace_secs() -> float:
    return _env_number(GRACE_KNOB, DEAD_GRACE_SECS, 0.0, DEAD_GRACE_SECS, float)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _clean(text) -> str:
    text = str(text).replace("|", "/")
    text = _CONTROL_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text).strip()
    return text[:_FIELD_MAX]


def _script_path() -> str:
    core = Path(__file__).resolve()
    try:
        wrapper = core.parents[2] / "scripts" / "child_watch.py"
    except IndexError:
        return str(core)
    return str(wrapper) if wrapper.exists() else str(core)


def _rearm_command(root: str, task: str, key_pid: int) -> str:
    return " ".join(
        [
            "python3",
            shlex.quote(_script_path()),
            "--project-root",
            shlex.quote(root),
            "--task",
            task,
            "--supervisor-pid",
            str(key_pid),
        ]
    )


def _line(state, task, detail, takeover, nxt) -> str:
    return "WATCH|%s|task=%s|detail=%s|takeover=%s|next=%s|observe-only" % (
        state,
        _clean(task),
        _clean(detail),
        _clean(takeover),
        nxt,
    )


def _error_line(message, task="unknown") -> str:
    return _line("ERROR", task, message, "none", "report-and-stop")


# --------------------------------------------------------------------------
# Lock-derived liveness
# --------------------------------------------------------------------------


def _pos_int(value) -> bool:
    return type(value) is int and value > 0


def _liveness(lock, task: str, deps: Deps):
    """Return (supervisor_alive, child_alive, lock_pid)."""
    lock = lock or {}
    lock_pid = lock.get("pid")
    sup_alive = False
    if _pos_int(lock_pid) and deps.pid_alive(lock_pid):
        cmd = deps.cmdline(lock_pid)
        sup_alive = cmd is None or ("quoin" in cmd and task in cmd)
    child_alive = False
    child_pid = lock.get("child_pid")
    if _pos_int(child_pid) and deps.pid_alive(child_pid):
        cmd = deps.cmdline(child_pid)
        lock_sid = lock.get("child_session_id")
        if cmd is None:
            child_alive = True
        elif _ar._is_child_session_id(lock_sid):
            child_alive = lock_sid in cmd
        else:
            child_alive = "claude" in cmd
    return sup_alive, child_alive, (lock_pid if _pos_int(lock_pid) else None)


def _effective_sid(lock, fallback):
    lock_sid = (lock or {}).get("child_session_id")
    if _ar._is_child_session_id(lock_sid):
        return lock_sid
    if _ar._is_child_session_id(fallback):
        return fallback
    return None


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _num(value) -> bool:
    return type(value) in (int, float)


def _state_valid(st, task: str, key_pid) -> bool:
    if not isinstance(st, dict):
        return False
    if st.get("schema") != STATE_SCHEMA or type(st.get("schema")) is not int:
        return False
    if st.get("supervisor_pid") != key_pid or type(st.get("supervisor_pid")) is not int:
        return False
    if not _num(st.get("first_armed_at")):
        return False
    if type(st.get("baseline_done")) is not int:
        return False
    if type(st.get("no_change_windows")) is not int:
        return False
    if type(st.get("stall_reported")) is not bool:
        return False
    if not isinstance(st.get("baseline_heads"), list):
        return False
    nd = st.get("nd_mtime")
    if nd is not None and not _num(nd):
        return False
    return True


def _heads_to_json(heads) -> list:
    return [[k, s] for k, s in heads]


def _peek_nd_baseline(state_path: Path):
    """(has_baseline, mtime) from any parseable state file."""
    data = _ar._load_json(state_path)
    if data is not None and "nd_mtime" in data:
        nd = data["nd_mtime"]
        if nd is None or _num(nd):
            return True, nd
    return False, None


# --------------------------------------------------------------------------
# Terminal checks
# --------------------------------------------------------------------------


def _halt_reason(path: Path) -> str:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("reason:"):
                value = raw[len("reason:"):].strip()
                return value or "unknown"
    except OSError:
        pass
    return "unknown"


def _file_terminal(mem: Path, task: str, nd_baseline):
    """First of done > halt > new needs-decision, as (state, detail) or None."""
    if (mem / _ar.DONE_TEMPLATE.format(task=task)).exists():
        return "DONE", "autonomous-done sentinel present"
    halt = mem / _ar.HALT_TEMPLATE.format(task=task)
    if halt.exists():
        return "HALTED", "reason=" + _halt_reason(halt)
    nd = mem / _ar.NEEDS_DECISION_TEMPLATE.format(task=task)
    if nd.exists():
        has, value = nd_baseline
        if not has or _mtime(nd) != value:
            return "NEEDS_DECISION", "needs-decision sentinel written"
    return None


class _Ctx:
    def __init__(self, mem, task, key_pid, nd_baseline, first_armed_at, max_hours, deps, sid_fallback=None):
        self.mem = mem
        self.task = task
        self.key_pid = key_pid
        self.nd_baseline = nd_baseline
        self.first_armed_at = first_armed_at
        self.max_hours = max_hours
        self.deps = deps
        self.replaced_pid = None
        self.sid = None
        self.sid_fallback = sid_fallback


def _terminal(ctx: _Ctx):
    hit = _file_terminal(ctx.mem, ctx.task, ctx.nd_baseline)
    if hit:
        return hit
    lock = _ar._load_json(_ar._lock_path(ctx.mem, ctx.task))
    sup, child, lock_pid = _liveness(lock, ctx.task, ctx.deps)
    ctx.replaced_pid = lock_pid if (sup and lock_pid != ctx.key_pid) else None
    ctx.sid = _effective_sid(lock, ctx.sid_fallback)
    if not sup and not child:
        grace = _grace_secs()
        if grace > 0:
            ctx.deps.sleep(grace)
        hit = _file_terminal(ctx.mem, ctx.task, ctx.nd_baseline)
        if hit:
            return hit
        return "DEAD", "no live supervisor or child process named by the lock"
    if ctx.deps.clock() - ctx.first_armed_at > ctx.max_hours * 3600:
        return "EXPIRED", "watched for more than %d hours" % ctx.max_hours
    return None


# --------------------------------------------------------------------------
# One watch window
# --------------------------------------------------------------------------


def parse_args(argv):
    parser = _Parser(prog="child_watch.py", add_help=False)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--supervisor-pid", default=None)
    parser.add_argument("--child-session", default=None)
    parser.add_argument("--window-secs", type=float, default=None)
    parser.add_argument("--poll-secs", type=float, default=None)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def _validate(args):
    if not _ar._run_state._valid_task(args.task):
        raise WatchError("invalid task name")
    root = args.project_root
    if not isinstance(root, str) or not root or "|" in root or _CONTROL_RE.search(root):
        raise WatchError("project root is empty or contains a reserved character")
    if not Path(root).is_dir():
        raise WatchError("project root is not a directory")
    pid = None
    if args.supervisor_pid is not None:
        try:
            pid = int(str(args.supervisor_pid).strip())
        except ValueError:
            pid = None
        if pid is None or pid <= 0 or not str(args.supervisor_pid).strip().isdigit():
            raise WatchError("--supervisor-pid must be a positive integer")
    return pid


def _count_and_heads(mem: Path, task: str, root: str, deps: Deps):
    return _ar._count_done(mem, task), tuple(deps.probe(task, root))


def watch(args, deps: Deps):
    """Run one window. Returns (line, exit_code)."""
    arg_pid = _validate(args)
    task = args.task
    root = args.project_root
    mem = _ar._memory_dir(root)
    once = bool(args.once)
    state_path = mem / STATE_TEMPLATE.format(task=task)
    stall_windows = _stall_windows()
    max_hours = _max_hours()
    poll = args.poll_secs if args.poll_secs is not None else POLL_DEFAULT
    window = 0.0 if once else (
        args.window_secs if args.window_secs is not None else _interval_secs()
    )
    fallback_sid = args.child_session

    def finish(state, detail, ctx_sid, ctx_replaced, key_pid):
        terminal = state in TERMINAL_STATES
        pointer = _ar._takeover_notice_pointer(task, root)
        takeover = ("child_session=%s " % ctx_sid if ctx_sid else "") + pointer
        if ctx_replaced is not None:
            detail = "%s; supervisor replaced pid=%s" % (detail, ctx_replaced)
        nxt = "report-and-stop" if terminal else _rearm_command(root, task, key_pid)
        return _line(state, task, detail, takeover, nxt), (
            EXIT_TERMINAL if terminal else EXIT_NONTERMINAL
        )

    def note(state, detail):
        _ar.append_note(
            mem, task, "%s task=%s state=%s detail=%s" % (NOTE_PREFIX, task, state, detail)
        )

    lock0 = _ar._load_json(_ar._lock_path(mem, task))
    lock_pid0 = lock0.get("pid") if lock0 else None
    lock_pid0 = lock_pid0 if _pos_int(lock_pid0) else None

    if once:
        nd_baseline = _peek_nd_baseline(state_path)
        hit = _file_terminal(mem, task, nd_baseline)
        if hit:
            return finish(hit[0], hit[1], _effective_sid(lock0, fallback_sid), None, arg_pid or lock_pid0 or 0)
        key_pid = arg_pid or lock_pid0
        if key_pid is None:
            return finish("DEAD", "no supervisor lock and no completion sentinel", fallback_sid if _ar._is_child_session_id(fallback_sid) else None, None, 0)
    else:
        key_pid = arg_pid or lock_pid0
        if key_pid is None:
            raise WatchError(
                "no supervisor pid: pass --supervisor-pid from the hand-off result"
            )

    st = _ar._load_json(state_path)
    fresh = not _state_valid(st, task, key_pid)
    if fresh:
        count0, heads0 = _count_and_heads(mem, task, root, deps)
        st = {
            "schema": STATE_SCHEMA,
            "task": task,
            "supervisor_pid": key_pid,
            "first_armed_at": deps.clock(),
            "baseline_done": count0,
            "baseline_heads": _heads_to_json(heads0),
            "no_change_windows": 0,
            "stall_reported": False,
            "nd_mtime": _mtime(mem / _ar.NEEDS_DECISION_TEMPLATE.format(task=task)),
        }
        if not once:
            _ar._atomic_write_text(mem, state_path.name + ".", state_path, _dump(st))
    if not once:
        nd_baseline = (True, st["nd_mtime"])

    ctx = _Ctx(mem, task, key_pid, nd_baseline, st["first_armed_at"], max_hours, deps, fallback_sid)
    deadline = deps.clock() + window
    while True:
        hit = _terminal(ctx)
        if hit:
            if not once:
                note(hit[0], _clean(hit[1]))
            return finish(hit[0], hit[1], ctx.sid, ctx.replaced_pid, key_pid)
        remaining = deadline - deps.clock()
        if remaining <= 0:
            break
        deps.sleep(min(poll, remaining))

    count, heads = _count_and_heads(mem, task, root, deps)
    stored_heads = _ar._heads_from_json(st["baseline_heads"])
    count_up = count > st["baseline_done"]
    heads_moved = _ar.heads_changed(stored_heads, heads)
    progressed = count_up or heads_moved
    if progressed:
        state = "PROGRESS"
        parts = []
        if count_up:
            parts.append("completion sentinels %d -> %d" % (st["baseline_done"], count))
        if heads_moved:
            parts.append("new task-branch commit")
        detail = "; ".join(parts)
        st["no_change_windows"] = 0
        st["stall_reported"] = False
    else:
        st["no_change_windows"] += 1
        if once:
            if fresh:
                state = "ALIVE"
            elif st["no_change_windows"] >= stall_windows:
                state = "STALL"
            else:
                state = "ALIVE"
        elif st["no_change_windows"] >= stall_windows and not st["stall_reported"]:
            state = "STALL"
            st["stall_reported"] = True
        else:
            state = "ALIVE"
        if state == "STALL":
            detail = "no new completion sentinel or task-branch commit for %d window(s)" % st["no_change_windows"]
        else:
            detail = "run is alive; no new completion sentinel or commit this window"
    if not once:
        st["baseline_done"] = count
        if heads:
            st["baseline_heads"] = _heads_to_json(heads)
        _ar._atomic_write_text(mem, state_path.name + ".", state_path, _dump(st))
        if state == "STALL":
            note("STALL", _clean(detail))
    return finish(state, detail, ctx.sid, ctx.replaced_pid, key_pid)


def _dump(st) -> str:
    import json

    return json.dumps(st, sort_keys=True) + "\n"


def run(argv, deps: Optional[Deps] = None):
    """Parse, watch, and convert every failure into one ERROR line."""
    task = "unknown"
    try:
        args = parse_args(argv)
        task = args.task
        return watch(args, deps or Deps())
    except WatchError as exc:
        return _error_line(str(exc), task), EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - never a traceback on stdout
        sys.stderr.write(traceback.format_exc())
        return _error_line("unexpected %s: %s" % (type(exc).__name__, exc), task), EXIT_ERROR


def main(argv=None) -> int:
    line, code = run(sys.argv[1:] if argv is None else list(argv))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
