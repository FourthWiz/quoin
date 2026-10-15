"""Observation only: never emits hook output or makes approval decisions.

Runs as `python -E -s monitor.py <claude|codex>` with the hook event JSON on
stdin. It records status metadata for the dashboard and always exits 0 with
empty stdout and stderr, whatever the input or the machine state.
"""
import fcntl
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import deckenv
except Exception:
    if __name__ == "__main__":
        os._exit(0)
    raise

PS = "/bin/ps"
DEADLINE_S = 1.5
STDIN_CAP = 1 << 20
CLAUDE_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "PostToolUse", "Stop", "PreCompact", "PostCompact", "PostToolUseFailure",
    "Notification", "StopFailure", "Elicitation", "ElicitationResult",
)
KNOWN_EVENTS = frozenset(CLAUDE_EVENTS + ("Interrupt",))
RECORD_KEYS = (
    "id", "kind", "session", "pane", "socket", "socket_inferred", "project", "status",
    "pending", "event", "created", "updated", "pid", "process_started", "via",
)
VIA_VALUES = ("legacy", "managed")
_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_.:-]")


def _ps(args, timeout):
    """Run ps with the pinned C-locale environment; None on any failure."""
    if timeout <= 0:
        return None
    try:
        result = subprocess.run([PS] + list(args), env=deckenv.ps_env(), capture_output=True,
                                text=True, errors="replace", timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout


def process_identity(pid, timeout=1.0):
    out = _ps(["-p", str(pid), "-o", "lstart="], timeout)
    return out.strip() if out else ""


def _remaining(deadline):
    return deadline - time.monotonic()


def agent_process(kind, deadline=None):
    """Find the agent process among our ancestors: one process-table read, in memory."""
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_S
    out = _ps(["-axo", "pid=,ppid=,comm="], min(1.0, _remaining(deadline)))
    if not out:
        return None, ""
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                table[int(parts[0])] = (int(parts[1]), parts[2])
            except ValueError:
                continue
    pid = os.getppid()
    for _ in range(8):
        row = table.get(pid)
        if row is None:
            break
        parent, command = row
        name = os.path.basename(command).lower()
        if name == kind or name.startswith(kind + "-") or (kind == "claude" and "/claude/versions/" in command):
            started = process_identity(pid, min(1.0, _remaining(deadline)))
            return pid, started if deckenv.is_c_lstart(started) else ""
        pid = parent
        if pid < 2:
            break
    return None, ""


def _text(value):
    return value if isinstance(value, str) else ""


def transition(old, event):
    """Keep unrelated parallel completions from hiding a pending question."""
    state = dict(old)
    pending = dict(state.get("pending", {}))
    name = _text(event.get("hook_event_name", ""))
    tool = _text(event.get("tool_name", ""))
    raw = event.get("tool_use_id") or tool or "approval"
    token = _TOKEN_BAD.sub("_", raw if isinstance(raw, str) else "approval")[:64]
    if name == "UserPromptSubmit":
        pending = {}
        state["status"] = "working"
    elif name in ("PermissionRequest", "Elicitation") or (
            name == "PreToolUse" and tool in ("AskUserQuestion", "request_user_input")):
        pending[token] = "question" if name == "Elicitation" or tool in (
            "AskUserQuestion", "request_user_input") else "permission"
        state["status"] = "attention"
    # idle_prompt is only a reminder that a finished turn got no reply (often while a background
    # job still runs); it asks nothing, so the agent keeps its done/idle status.
    elif name == "Notification" and event.get("notification_type") in ("permission_prompt", "elicitation_dialog"):
        # PermissionRequest carries the matching tool id; don't add a second unresolvable key.
        if not pending:
            pending["notification"] = "attention"
        state["status"] = "attention"
    elif name in ("PostToolUse", "PostToolUseFailure", "ElicitationResult"):
        pending.pop(token, None)
        pending.pop("notification", None)
        state["status"] = "attention" if pending else "working"
    elif name in ("PreToolUse", "PreCompact", "PostCompact"):
        state["status"] = "attention" if pending else "working"
    elif name in ("Stop", "StopFailure", "Interrupt"):
        pending = {}
        state["status"] = "done" if name == "Stop" else "attention"
    elif name == "SessionStart":
        pending = {}
        state["status"] = "idle"
    elif name == "SessionEnd":
        pending = {}
        state["status"] = "closed"
    state["pending"] = pending
    state["event"] = name if name in KNOWN_EVENTS else "other"
    state["updated"] = time.time()
    return state


def _lock(path, deadline):
    fd = deckenv.open_lock(path)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if _remaining(deadline) <= 0:
                os.close(fd)
                return None
            time.sleep(0.02)


def record(kind, event, environ=None, identity=None, deadline=None):
    if kind not in ("claude", "codex"):
        return
    env = os.environ if environ is None else environ
    session = env.get("ZELLIJ_SESSION_NAME")
    pane = env.get("ZELLIJ_PANE_ID")
    sid = event.get("session_id")
    # Desktop threads and background subagents are not separately addressable panes.
    if not session or not pane or not isinstance(sid, str) or not sid or event.get("agent_id"):
        return
    if not deckenv.valid_session(session):
        return
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_S
    deck = deckenv.current()
    pid, started = identity if identity is not None else agent_process(kind, deadline)
    if _remaining(deadline) <= 0:
        return
    key = hashlib.sha256((kind + ":" + sid).encode()).hexdigest()[:24]
    socket = env.get("ZELLIJ_SOCKET_DIR")
    inferred = not (isinstance(socket, str) and socket)
    via = env.get("QUOIN_DECK_VIA")
    deckenv.ensure_dir(deck.agents_dir)
    fd = _lock(deck.monitor_lock, deadline)
    if fd is None:
        return
    try:
        path = os.path.join(deck.agents_dir, key + ".json")
        old = deckenv.read_json(path, {})
        old = old if isinstance(old, dict) else {}
        value = transition(old, event)
        cwd = event.get("cwd")
        value.update(
            id=key, kind=kind, session=session, pane=str(pane),
            socket=deck.socket_dir if inferred else socket, socket_inferred=inferred,
            project=cwd[:4096] if isinstance(cwd, str) else "",
            created=old.get("created", time.time()),
            via=via if via in VIA_VALUES else "managed",
        )
        if pid:
            value.update(pid=pid, process_started=started)
        # No prompts, tool arguments, transcripts, responses, or permission decisions are persisted.
        deckenv.write_json_atomic(path, {k: value[k] for k in RECORD_KEYS if k in value})
    finally:
        os.close(fd)


def _read_chunk(stream, size, deadline):
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.read(size)
    remaining = _remaining(deadline)
    if remaining <= 0:
        return None
    ready, _, _ = select.select([fd], [], [], remaining)
    return os.read(fd, size) if ready else None


def read_stdin(stream, deadline):
    """Event bytes, or None when oversized, unreadable or out of time."""
    data = b""
    while len(data) <= STDIN_CAP:
        chunk = _read_chunk(stream, 65536, deadline)
        if chunk is None:
            return None
        if not chunk:
            return data
        data += chunk
    while True:  # drain so the sender never sees a broken pipe
        chunk = _read_chunk(stream, 65536, deadline)
        if not chunk:
            return None


def main(argv=None, stdin=None):
    start = time.monotonic()
    deadline = start + DEADLINE_S
    argv = sys.argv if argv is None else argv
    if len(argv) < 2 or argv[1] not in ("claude", "codex"):
        return
    stream = sys.stdin.buffer if stdin is None else stdin
    data = read_stdin(stream, deadline)
    if not data:
        return
    try:
        event = json.loads(data.decode("utf-8"))
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    record(argv[1], event, deadline=deadline)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        try:
            deckenv.log_error_type_only(deckenv.current(), exc)
        except BaseException:
            pass
    # Failure of the indicator must never block the agent or alter its approval flow.
    os._exit(0)
