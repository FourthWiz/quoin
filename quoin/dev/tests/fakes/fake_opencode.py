"""A stand-in for the OpenCode executable, driven by a scenario file.

Runs as a script under ``sys.executable`` and imports nothing from Quoin.
A generated ``opencode`` shim bakes in the scenario and state paths, so a
launcher that scrubs the environment cannot break it. The fake records what
it was called with (argument vector, working directory, environment variable
names but never values, process and group ids) so tests can assert on the
launch without a real runtime.

Scenario file: ``{"version_output"?, "require_session_after_first"?,
"attempts": [{"steps": [...]}, ...]}``. Attempt ``i`` runs
``attempts[min(i - 1, len - 1)]``. Only ``run`` calls count as attempts.
"""
from __future__ import annotations

import json
import os
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


# ---------------------------------------------------------------------------
# scenario builders
# ---------------------------------------------------------------------------


def _emit(type_: str, **data: Any) -> Dict[str, Any]:
    return {"do": "emit", "event": dict({"type": type_, "sessionID": "$session"}, **data)}


def _step_start(pid: str) -> Dict[str, Any]:
    return _emit("step_start", part={"id": pid, "messageID": "msg_fake", "type": "step-start"})


def _step_finish(pid: str, reason: str) -> Dict[str, Any]:
    return _emit(
        "step_finish",
        part={
            "id": pid, "reason": reason, "messageID": "msg_fake", "type": "step-finish",
            "tokens": {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0.001,
        },
    )


def _text(pid: str, body: str) -> Dict[str, Any]:
    return _emit(
        "text",
        part={"id": pid, "messageID": "msg_fake", "type": "text", "text": body,
              "time": {"start": 1, "end": 2}},
    )


def _tool_done(pid: str, tool: str) -> Dict[str, Any]:
    return _emit(
        "tool_use",
        part={"id": pid, "messageID": "msg_fake", "type": "tool", "tool": tool,
              "state": {"status": "completed", "title": "ok", "input": {}, "output": "", "metadata": {}}},
    )


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


def _grandchild(state: Path, ignore_term: bool, detach: bool = False) -> None:
    """Start a long-lived descendant and record its pid.

    The descendant touches ``grandchild-PID.ready`` once its signal handling
    is in place, so a test never signals it before it can ignore TERM. With
    ``detach`` it starts its own session and process group, as the real
    runtime's shell tool does, so a signal to the run's group misses it.
    """
    code = "import os, signal, sys, time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "open(os.path.join(sys.argv[1], 'grandchild-%d.ready' % os.getpid()), 'w').close()\n"
    code += "while True:\n    time.sleep(0.1)\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(state)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=detach,
    )
    with open(state / "grandchildren.txt", "a", encoding="utf-8") as fh:
        fh.write("%d\n" % proc.pid)


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


def _run_steps(steps: List[Dict[str, Any]], session: str, state: Path) -> int:
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
        elif verb == "stdout_oversized":
            _out("x" * int(step["bytes"]) + "\n")
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
            _grandchild(state, bool(step.get("ignore_term")), bool(step.get("detach")))
        elif verb == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif verb == "crash":
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
    session_file = None
    if requested:
        session_file = state / "sessions" / (requested + ".json")
        if not session_file.exists():
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
    return _run_steps(steps, session, state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
