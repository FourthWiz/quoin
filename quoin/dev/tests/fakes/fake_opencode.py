"""A stand-in for the OpenCode executable, driven by a scenario file.

Runs as a script under ``sys.executable`` and imports nothing from Quoin.
A generated ``opencode`` shim bakes in the scenario and state paths, so a
launcher that scrubs the environment cannot break it. The fake records what
it was called with (argument vector, working directory, environment variable
names but never values, process and group ids) so tests can assert on the
launch without a real runtime.

Scenario file: ``{"version_output"?, "require_session_after_first"?,
"max_lifetime_s"?, "attempts": [{"steps": [...]}, ...]}``. Attempt ``i`` runs
``attempts[min(i - 1, len - 1)]``. Only ``run`` calls count as attempts.

Helper processes (the grandchildren) exit on their own when ``STATE/stop``
exists or after ``max_lifetime_s`` seconds (default 120), so a failed test
cannot leave one running forever; each writes its own pid to
``STATE/grandchildren.txt`` before it creates its ready marker.

Crash steps disable core dumps for the process before signalling, but macOS
may still write a crash report for SIGSEGV and SIGABRT. Tests that only need
a signalled exit should use SIGKILL, and at most one test per other signal
kind should exercise it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

FIXTURE_DIR = (
    Path(__file__).resolve().parents[3] / "adapters" / "opencode" / "fixtures" / "runtime-events"
)
DEFAULT_VERSION = "1.18.32"

_VALUE_FLAGS = ("--format", "--command", "--session", "--agent", "--dir", "--model", "--variant")
_BOOL_FLAGS = ("--continue", "--fork")
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
DEFAULT_MAX_LIFETIME_S = 120.0


# ---------------------------------------------------------------------------
# scenario builders
# ---------------------------------------------------------------------------


def _emit(type_: str, **data: Any) -> Dict[str, Any]:
    return {"do": "emit", "event": dict({"type": type_, "sessionID": "$session"}, **data)}


def _stamped(step: Dict[str, Any], ts: Optional[int]) -> Dict[str, Any]:
    """Pin the envelope timestamp so a replayed line is byte-identical."""
    if ts is not None:
        step["event"]["timestamp"] = ts
    return step


def _step_start(pid: str, ts: Optional[int] = None) -> Dict[str, Any]:
    return _stamped(
        _emit("step_start", part={"id": pid, "messageID": "msg_fake", "type": "step-start"}), ts)


def _step_finish(pid: str, reason: str, ts: Optional[int] = None) -> Dict[str, Any]:
    return _stamped(_emit(
        "step_finish",
        part={
            "id": pid, "reason": reason, "messageID": "msg_fake", "type": "step-finish",
            "tokens": {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0.001,
        },
    ), ts)


def _text(pid: str, body: str, ts: Optional[int] = None) -> Dict[str, Any]:
    return _stamped(_emit(
        "text",
        part={"id": pid, "messageID": "msg_fake", "type": "text", "text": body,
              "time": {"start": 1, "end": 2}},
    ), ts)


def _tool_done(pid: str, tool: str, title: str = "ok") -> Dict[str, Any]:
    return _emit(
        "tool_use",
        part={"id": pid, "messageID": "msg_fake", "type": "tool", "tool": tool,
              "state": {"status": "completed", "title": title, "input": {}, "output": "", "metadata": {}}},
    )


def _task_part(pid: str, state: Dict[str, Any]) -> Dict[str, Any]:
    return _emit(
        "tool_use",
        part={"id": pid, "messageID": "msg_fake", "type": "tool", "tool": "task",
              "callID": "call_" + pid, "state": state},
    )


def _session_error(message: Optional[str] = None, status: Optional[int] = None,
                   ts: Optional[int] = None) -> Dict[str, Any]:
    step: Dict[str, Any] = {"do": "emit_session_error"}
    if message is not None:
        step["message"] = message
    if status is not None:
        step["status"] = status
    if ts is not None:
        step["timestamp"] = ts
    return step


def _scenario(*steps: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    out = {"attempts": [{"steps": list(steps)}]}
    out.update(extra)
    return out


def _notice(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()[0]


def replay(fixture: str, exit_code: int = 0) -> Dict[str, Any]:
    return _scenario({"do": "emit_fixture", "name": fixture}, {"do": "exit", "code": exit_code})


def _fixture_scenario(name: str, exit_code: int = 0) -> Callable[[], Dict[str, Any]]:
    return lambda: replay(name, exit_code)


def _approval_stderr_notice() -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        {"do": "stderr", "text": _notice("stderr-approval-notice.txt")},
        {"do": "hang"},
    )


def _agent_fallback() -> Dict[str, Any]:
    return _scenario(
        {"do": "stderr", "text": _notice("stderr-agent-fallback.txt")},
        {"do": "emit_fixture", "name": "plain-complete.jsonl"},
        {"do": "exit", "code": 0},
    )


def _session_continuation() -> Dict[str, Any]:
    first = [
        {"do": "write_file", "path": "effect.txt", "content": "written once\n"},
        _step_start("prt_s1"),
        _step_finish("prt_f1", "tool-calls"),
        {"do": "crash", "signal": "SIGKILL"},
    ]
    second = [_step_start("prt_s2"), _text("prt_t2", "resumed"), _step_finish("prt_f2", "stop"),
              {"do": "exit", "code": 0}]
    return {"attempts": [{"steps": first}, {"steps": second}], "require_session_after_first": True}


def _continuation(first: List[Dict[str, Any]], second: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"attempts": [{"steps": first}, {"steps": second}], "require_session_after_first": True}


def _clean_second_attempt(tag: str) -> List[Dict[str, Any]]:
    return [_step_start("prt_s" + tag), _text("prt_t" + tag, "resumed"),
            _step_finish("prt_f" + tag, "stop"), {"do": "exit", "code": 0}]


def _session_continuation_replay() -> Dict[str, Any]:
    lines = [_step_start("prt_s1", 1000), _text("prt_t1", "first attempt", 1001),
             _step_finish("prt_f1", "tool-calls", 1002)]
    return _continuation(
        lines + [{"do": "crash", "signal": "SIGKILL"}],
        lines + [_step_start("prt_s2"), _text("prt_t2", "new work"),
                 _step_finish("prt_f2", "stop"), {"do": "exit", "code": 0}],
    )


def _transient_error_then_continue() -> Dict[str, Any]:
    error = _session_error("Rate limit reached", 429, ts=2000)
    return _continuation(
        [_step_start("prt_s1", 1000), _step_finish("prt_f1", "tool-calls", 1001),
         error, {"do": "exit", "code": 1}],
        [error, _step_start("prt_s2"), _text("prt_t2", "recovered"),
         _step_finish("prt_f2", "stop"), {"do": "exit", "code": 0}],
    )


def _secret_echo(secret: str = "sk-FAKEECHOSECRET0123456789") -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        _text("prt_t1", "text carries " + secret),
        _tool_done("prt_b1", "bash", title="title carries " + secret),
        {"do": "stderr", "text": "stderr carries " + secret},
        _session_error("error carries " + secret),
        {"do": "exit", "code": 1},
    )


def _secret_echo_straddle(secret: str = "sk-FAKESTRADDLE0123456789") -> Dict[str, Any]:
    # The secret starts 4 bytes before the 8 KiB boundary of the stderr stream.
    return _scenario(
        {"do": "stderr", "text": "x" * (8192 - 4) + secret},
        {"do": "emit_fixture", "name": "plain-complete.jsonl"},
        {"do": "exit", "code": 0},
    )


def _slow_finish(delay_s: float = 0.3) -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"), {"do": "sleep", "seconds": delay_s},
        _text("prt_t1", "slow"), {"do": "sleep", "seconds": delay_s},
        _step_finish("prt_f1", "stop"), {"do": "exit", "code": 0},
    )


_BACKGROUND_TASK_STATE = {
    "status": "completed", "input": {"run_in_background": True}, "title": "explore",
    "output": "done", "metadata": {"background": True},
}
_DENIED_TAIL_TASK_STATE = {
    "status": "error", "input": {},
    "error": (
        "Subagent failed (task_id: ses_fakechild): The user has specified a rule which "
        "prevents you from using this specific tool call. Here are some of the relevant "
        'rules [{"permission": "bash", "pattern": "rm *", "action": "deny"}]'
    ),
}


def _task_then_boundary_crash(state: Dict[str, Any]) -> Dict[str, Any]:
    return _continuation(
        [_step_start("prt_s1"), _task_part("prt_k1", state), _step_finish("prt_f1", "tool-calls"),
         {"do": "crash", "signal": "SIGKILL"}],
        _clean_second_attempt("2"),
    )


SCENARIOS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "replay": replay,
    "crash_after_start": lambda: _scenario(_step_start("prt_s1"), {"do": "crash", "signal": "SIGKILL"}),
    "crash_mid_stream": lambda: _scenario(
        _step_start("prt_s1"), _step_finish("prt_f1", "tool-calls"), _step_start("prt_s2"),
        _text("prt_t2", "working"), {"do": "crash", "signal": "SIGSEGV"}),
    "crash_in_open_step": lambda: _scenario(
        _step_start("prt_s1"), _tool_done("prt_b1", "bash"), {"do": "crash", "signal": "SIGABRT"}),
    "crash_after_last_finish": lambda: _scenario(
        _step_start("prt_s1"), _step_finish("prt_f1", "stop"), {"do": "crash", "signal": "SIGKILL"}),
    "hang": lambda: _scenario(_step_start("prt_s1"), {"do": "hang"}),
    "exit_early": lambda: _scenario({"do": "exit", "code": 1}),
    "exit0_without_step_finish": lambda: _scenario(
        _step_start("prt_s1"), _text("prt_t1", "no finish"), {"do": "exit", "code": 0}),
    "approval_tool_error": _fixture_scenario("tool-rejected.jsonl"),
    "approval_stderr_notice": _approval_stderr_notice,
    "approval_task_error": _fixture_scenario("task-errored-rejection.jsonl"),
    "task_errored_then_clean_finish": _fixture_scenario("task-errored-other.jsonl"),
    "task_denied_tail": _fixture_scenario("task-errored-denied-tail.jsonl"),
    "task_sync_completed": _fixture_scenario("task-sync-completed.jsonl"),
    "task_background": _fixture_scenario("task-background.jsonl"),
    "task_promoted": _fixture_scenario("task-promoted.jsonl"),
    "agent_fallback": _agent_fallback,
    "native_error": _fixture_scenario("native-error-unknown.jsonl", 1),
    "doom_loop_rejected": _fixture_scenario("doom-loop-rejected.jsonl", 1),
    "doom_loop_denied": _fixture_scenario("doom-loop-denied.jsonl", 1),
    "denied_tool_then_clean_finish": _fixture_scenario("tool-denied-then-finish.jsonl"),
    "grandchild": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_grandchild", "ignore_term": False}, {"do": "hang"}),
    "ignore_term": lambda: _scenario(_step_start("prt_s1"), {"do": "ignore_term"}, {"do": "hang"}),
    "grandchild_detached": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_grandchild", "ignore_term": False, "detach": True},
        {"do": "hang"}),
    "grandchild_ignore_term": lambda: _scenario(
        _step_start("prt_s1"), {"do": "ignore_term"},
        {"do": "spawn_grandchild", "ignore_term": True}, {"do": "hang"}),
    "record_only": lambda: _scenario(
        _step_start("prt_s1"), _step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}),
    "session_continuation": _session_continuation,
    "version_mismatch": lambda: dict(
        replay("plain-complete.jsonl"), version_output="1.18.31"),
    "session_continuation_replay": _session_continuation_replay,
    "transient_error_then_continue": _transient_error_then_continue,
    "endless_line": lambda nbytes=2 * 1024 * 1024: _scenario(
        {"do": "stdout_oversized", "bytes": nbytes, "newline": False},
        {"do": "emit_fixture", "name": "plain-complete.jsonl"}, {"do": "exit", "code": 0}),
    "stdout_closed_hang": lambda: _scenario(
        _step_start("prt_s1"), {"do": "close_stdout"}, {"do": "hang"}),
    "grandchild_reparented": lambda linger_s=0.3: _scenario(
        _step_start("prt_s1"), {"do": "spawn_reparented_grandchild", "linger_s": linger_s},
        {"do": "hang"}),
    "approval_notice_then_finish": lambda: _scenario(
        {"do": "emit_fixture", "name": "plain-complete.jsonl"}, {"do": "close_stdout"},
        {"do": "sleep", "seconds": 0.3},
        {"do": "stderr", "text": _notice("stderr-approval-notice.txt")},
        {"do": "exit", "code": 0}),
    "secret_echo": _secret_echo,
    "secret_echo_straddle": _secret_echo_straddle,
    "grandchild_holds_stdout": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_stdout_holder"},
        {"do": "emit_fixture", "name": "plain-complete.jsonl"}, {"do": "exit", "code": 0}),
    "slow_finish": _slow_finish,
    "task_background_then_boundary_crash": lambda: _task_then_boundary_crash(_BACKGROUND_TASK_STATE),
    "task_denied_tail_then_boundary_crash": lambda: _task_then_boundary_crash(_DENIED_TAIL_TASK_STATE),
    "grandchild_detached_finish": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_grandchild", "ignore_term": False, "detach": True},
        _step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}),
    "step_closed_then_hang": lambda: _scenario(
        {"do": "write_file", "path": "effect.txt", "content": "written once\n"},
        _step_start("prt_s1"), _step_finish("prt_f1", "tool-calls"), {"do": "hang"}),
    "effect_before_first_line": lambda: _scenario(
        {"do": "write_file", "path": "effect.txt", "content": "written once\n"}, {"do": "hang"}),
}


def write_scenario(path: Path, scenario: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenario, indent=1) + "\n", encoding="utf-8")
    return path


def write_shim(bin_dir: Path, scenario: Path, state_dir: Path) -> Path:
    """Write an executable ``opencode`` script with every path baked in."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "opencode"
    body = "#!/bin/sh\nexec %s %s --fake-scenario %s --fake-state %s -- \"$@\"\n" % (
        shlex.quote(sys.executable),
        shlex.quote(str(Path(__file__).resolve())),
        shlex.quote(str(scenario)),
        shlex.quote(str(state_dir)),
    )
    shim.write_text(body, encoding="utf-8")
    shim.chmod(0o755)
    return shim


# ---------------------------------------------------------------------------
# the executable itself
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fill(value: Any, session: str) -> Any:
    if isinstance(value, str):
        return session if value == "$session" else value
    if isinstance(value, list):
        return [_fill(v, session) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, session) for k, v in value.items()}
    return value


def _out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _parse_run(args: List[str]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"message": [], "unknown_flags": []}
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            parsed["message"].extend(args[i + 1:])
            break
        if token in _VALUE_FLAGS and i + 1 < len(args):
            parsed[token[2:]] = args[i + 1]
            i += 2
            continue
        if token in _BOOL_FLAGS:
            parsed[token[2:]] = True
        elif token.startswith("-"):
            parsed["unknown_flags"].append(token)
        else:
            parsed["message"].append(token)
        i += 1
    return parsed


def _record(state: Path, entry: Dict[str, Any]) -> None:
    with open(state / "invocations.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _prior_runs(state: Path) -> int:
    path = state / "invocations.jsonl"
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and json.loads(raw).get("attempt") is not None:
            count += 1
    return count


_GRANDCHILD_CODE = (
    "import os, signal, sys, time\n"
    "state, ignore_term, lifetime = sys.argv[1], sys.argv[2] == '1', float(sys.argv[3])\n"
    "if ignore_term:\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "with open(os.path.join(state, 'grandchildren.txt'), 'a') as fh:\n"
    "    fh.write('%d\\n' % os.getpid())\n"
    "open(os.path.join(state, 'grandchild-%d.ready' % os.getpid()), 'w').close()\n"
    "stop = os.path.join(state, 'stop')\n"
    "deadline = time.time() + lifetime\n"
    "while time.time() < deadline and not os.path.exists(stop):\n"
    "    time.sleep(0.1)\n"
)

_INTERMEDIATE_CODE = (
    "import os, subprocess, sys, time\n"
    "state, ignore_term, lifetime, linger, code = sys.argv[1:6]\n"
    "with open(os.path.join(state, 'intermediates.txt'), 'a') as fh:\n"
    "    fh.write('%d\\n' % os.getpid())\n"
    "subprocess.Popen([sys.executable, '-c', code, state, ignore_term, lifetime],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "    close_fds=True, start_new_session=True)\n"
    "time.sleep(float(linger))\n"
)


def _lifetime(scenario: Dict[str, Any], step: Dict[str, Any]) -> float:
    return float(step.get("max_lifetime_s", scenario.get("max_lifetime_s", DEFAULT_MAX_LIFETIME_S)))


def _grandchild(state: Path, ignore_term: bool, lifetime: float, detach: bool = False,
                inherit_stdout: bool = False) -> None:
    """Start a long-lived descendant.

    The descendant writes its own pid to ``grandchildren.txt`` and then touches
    ``grandchild-PID.ready`` once its signal handling is in place, so a test
    never signals it before it can ignore TERM. It ends when ``STATE/stop``
    exists or after ``lifetime`` seconds. With ``detach`` it starts its own
    session and process group, as the real runtime's shell tool does, so a
    signal to the run's group misses it. With ``inherit_stdout`` it keeps the
    run's stdout open.
    """
    subprocess.Popen(
        [sys.executable, "-c", _GRANDCHILD_CODE, str(state), "1" if ignore_term else "0", repr(lifetime)],
        stdin=subprocess.DEVNULL, stdout=None if inherit_stdout else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=detach,
    )


def _reparented_grandchild(state: Path, lifetime: float, linger_s: float) -> None:
    """Start an intermediate that starts a detached grandchild and then exits.

    The intermediate stays in the run's process group and lingers for
    ``linger_s`` seconds; once it is gone the grandchild has been re-parented
    to init and no longer descends from the run in the process table.
    """
    subprocess.Popen(
        [sys.executable, "-c", _INTERMEDIATE_CODE, str(state), "0", repr(lifetime),
         repr(float(linger_s)), _GRANDCHILD_CODE],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _no_core_dumps() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def _close_stdout() -> None:
    """Close this process's end of the stdout pipe, keeping fd 1 occupied."""
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


def _session_error_event(step: Dict[str, Any]) -> Dict[str, Any]:
    status = step.get("status")
    message = step.get("message")
    if status is not None:
        error: Dict[str, Any] = {"name": "APIError", "data": {
            "message": message or "http error", "statusCode": int(status),
            "isRetryable": int(status) == 429 or int(status) >= 500}}
    else:
        error = {"name": "UnknownError", "data": {"message": message or "error"}}
    obj: Dict[str, Any] = {"type": "error", "sessionID": "$session", "error": error}
    if "timestamp" in step:
        obj["timestamp"] = step["timestamp"]
    return obj


def _emit_fixture(name: str) -> None:
    base = _now_ms()
    data = (FIXTURE_DIR / name).read_bytes()
    pieces = data.split(b"\n")
    if pieces and pieces[-1] == b"":
        pieces.pop()
    for piece in pieces:
        try:
            obj = json.loads(piece)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("timestamp"), int):
            obj["timestamp"] = base + obj["timestamp"]
            _out(json.dumps(obj) + "\n")
        else:
            _out(piece.decode("utf-8", "replace") + "\n")


def _run_steps(steps: List[Dict[str, Any]], session: str, state: Path,
               scenario: Optional[Dict[str, Any]] = None) -> int:
    scenario = scenario or {}
    for step in steps:
        verb = step["do"]
        if verb == "emit":
            obj = _fill(step["event"], session)
            obj.setdefault("timestamp", _now_ms())
            _out(json.dumps(obj) + "\n")
        elif verb == "emit_fixture":
            _emit_fixture(step["name"])
        elif verb == "stdout_raw":
            _out(step["text"] + ("\n" if step.get("newline", True) else ""))
        elif verb == "stdout_no_newline":
            _out(step["text"])
        elif verb == "stdout_oversized":
            _out("x" * int(step["bytes"]) + ("\n" if step.get("newline", True) else ""))
        elif verb == "close_stdout":
            _close_stdout()
        elif verb == "emit_session_error":
            obj = _fill(_session_error_event(step), session)
            obj.setdefault("timestamp", _now_ms())
            _out(json.dumps(obj) + "\n")
        elif verb == "touch_stop":
            (state / "stop").write_text("", encoding="utf-8")
        elif verb == "stderr":
            _err(step["text"] + ("\n" if step.get("newline", True) else ""))
        elif verb == "sleep":
            time.sleep(float(step["seconds"]))
        elif verb == "hang":
            while True:
                time.sleep(1)
        elif verb == "write_file":
            target = Path(step["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(step.get("content", ""), encoding="utf-8")
            with open(state / "effects.log", "a", encoding="utf-8") as fh:
                fh.write("%s\n" % step["path"])
        elif verb == "spawn_grandchild":
            _grandchild(state, bool(step.get("ignore_term")), _lifetime(scenario, step),
                        bool(step.get("detach")))
        elif verb == "spawn_stdout_holder":
            _grandchild(state, False, _lifetime(scenario, step), inherit_stdout=True)
        elif verb == "spawn_reparented_grandchild":
            _reparented_grandchild(state, _lifetime(scenario, step), float(step.get("linger_s", 0.3)))
        elif verb == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif verb == "crash":
            _no_core_dumps()
            os.kill(os.getpid(), getattr(signal, step["signal"]))
            time.sleep(5)
        elif verb == "exit":
            return int(step["code"])
        else:
            _err("fake opencode: unknown step %r\n" % verb)
            return 99
    return 0


def main(argv: List[str]) -> int:
    scenario_path: Optional[str] = None
    state_arg: Optional[str] = None
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--fake-scenario":
            scenario_path = argv[i + 1]
            i += 2
        elif argv[i] == "--fake-state":
            state_arg = argv[i + 1]
            i += 2
        else:
            i += 1
    rest = argv[i + 1:] if i < len(argv) else []
    if scenario_path is None or state_arg is None:
        _err("fake opencode: missing --fake-scenario or --fake-state\n")
        return 98
    state = Path(state_arg)
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    base_entry = {
        "argv": rest, "cwd": os.getcwd(), "env_names": sorted(os.environ),
        "pid": os.getpid(), "pgid": os.getpgid(0),
    }

    if rest and rest[0] in ("--version", "-v"):
        _record(state, dict(base_entry, attempt=None, session_id=None))
        _out(scenario.get("version_output", DEFAULT_VERSION) + "\n")
        return 0
    if not rest or rest[0] != "run":
        _record(state, dict(base_entry, attempt=None, session_id=None))
        _err("fake opencode: unsupported command\n")
        return 2

    parsed = _parse_run(rest[1:])
    attempt = _prior_runs(state) + 1
    requested = parsed.get("session")
    if requested:
        session_file = state / "sessions" / (requested + ".json")
        if not SESSION_ID_RE.fullmatch(requested) or not session_file.exists():
            _record(state, dict(base_entry, attempt=attempt, session_id=None, parsed=parsed))
            _err("Session not found\n")
            return 1
        session = requested
    elif parsed.get("continue"):
        existing = sorted((state / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime)
        session = existing[-1].stem if existing else "ses_fake%d" % attempt
    else:
        session = "ses_fake%d" % attempt
    (state / "sessions" / (session + ".json")).write_text(
        json.dumps({"id": session, "last_attempt": attempt}) + "\n", encoding="utf-8")
    _record(state, dict(base_entry, attempt=attempt, session_id=session, parsed=parsed))

    if scenario.get("require_session_after_first") and attempt > 1 and not requested:
        _err("fake opencode: continuation attempt arrived without --session\n")
        return 97

    attempts = scenario["attempts"]
    steps = attempts[min(attempt - 1, len(attempts) - 1)]["steps"]
    return _run_steps(steps, session, state, scenario)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
