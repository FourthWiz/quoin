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

An optional ``commands: {KEY: {"attempts": [...]}}`` table selects steps per
command: a ``run`` call looks up ``COMMAND@STAGE`` (for example
``quoin-plan@2``), then ``COMMAND``, then the top-level ``attempts``. The
attempt index inside a ``commands`` entry is counted per lookup key (recorded
as ``scenario_key``); the global ``attempt`` field is unchanged. String values
in a step may use ``$task``, ``$stage``, ``$stagedir`` and ``$context``, parsed
from the ``--command`` argument.

Helper processes (the grandchildren) exit on their own when ``STATE/stop``
exists or after ``max_lifetime_s`` seconds (default 120), so a failed test
cannot leave one running forever; each writes its own pid to
``STATE/grandchildren.txt`` before it creates its ready marker.

Crash steps disable core dumps for the process before signalling. Use only
signals macOS does not treat as a crash (SIGKILL, SIGHUP, SIGTERM): SIGSEGV,
SIGABRT, SIGBUS and the like make the system crash reporter pop a "Python quit
unexpectedly" dialog on the developer's desktop for every run.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
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


def _step_finish(pid: str, reason: str, ts: Optional[int] = None, *,
                 tokens: Optional[Dict[str, Any]] = None, cost: float = 0.001,
                 omit_cost: bool = False) -> Dict[str, Any]:
    """A step-finish event. ``tokens`` replaces the default usage verbatim (so a
    test can drop a field); ``omit_cost`` leaves the cost key out."""
    part: Dict[str, Any] = {
        "id": pid, "reason": reason, "messageID": "msg_fake", "type": "step-finish",
        "tokens": tokens if tokens is not None else {
            "input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }
    if not omit_cost:
        part["cost"] = cost
    return _stamped(_emit("step_finish", part=part), ts)


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
        {"do": "hash_env", "name": "PROV_KEY"},
        _text("prt_t1", "text carries " + secret),
        _tool_done("prt_b1", "bash", title="title carries " + secret),
        {"do": "stderr", "text": "stderr carries " + secret},
        _session_error("error carries " + secret),
        {"do": "exit", "code": 1},
    )


def _reasoning(pid: str, body: str) -> Dict[str, Any]:
    return _emit(
        "reasoning",
        part={"id": pid, "messageID": "msg_fake", "type": "reasoning", "text": body,
              "time": {"start": 1, "end": 2}},
    )


def _tool_error(pid: str, tool: str, error: str) -> Dict[str, Any]:
    return _emit(
        "tool_use",
        part={"id": pid, "messageID": "msg_fake", "type": "tool", "tool": tool,
              "state": {"status": "error", "input": {}, "error": error}},
    )


def _seeded_content(seed: str = "SEED") -> Dict[str, Any]:
    """Every content site carries ``seed`` plus a per-site suffix; no approval
    notice, so the attempt runs to a normal finish."""
    return _scenario(
        _step_start("prt_s1"),
        _text("prt_t1", "text carries %s-text" % seed),
        _reasoning("prt_r1", "reasoning carries %s-reasoning" % seed),
        _tool_done("prt_b1", "bash", title="title carries %s-title" % seed),
        _tool_error("prt_b2", "bash", "failed with %s-toolerr" % seed),
        _session_error("native carries %s-native" % seed),
        {"do": "stderr", "text": "stderr carries %s-stderr" % seed},
        *_clean_finish(),
    )


def _seeded_approval(seed: str = "SEED") -> Dict[str, Any]:
    """A stderr approval notice arrives well after stdout so the two reader
    threads cannot race; the fake then waits to be terminated."""
    return _scenario(
        _step_start("prt_s1"),
        {"do": "sleep", "seconds": 0.2},
        {"do": "stderr", "text": "! permission requested: bash (rm %s-pattern); auto-rejecting" % seed},
        {"do": "hang"},
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


def _usage(input_tokens: int, output_tokens: int) -> Dict[str, Any]:
    return {"input": input_tokens, "output": output_tokens, "reasoning": 0,
            "cache": {"read": 0, "write": 0}}


def _usage_revised() -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        _step_finish("prt_f1", "tool-calls"),
        _step_finish("prt_f1", "tool-calls", tokens=_usage(40, 5)),
        _step_finish("prt_f2", "stop"),
        {"do": "exit", "code": 0},
    )


def _usage_unknown_tokens() -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        _step_finish("prt_f1", "stop", tokens={
            "input": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}}),
        {"do": "exit", "code": 0},
    )


def _usage_zero_cost() -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        _step_finish("prt_f1", "stop", tokens=_usage(100, 20), cost=0),
        {"do": "exit", "code": 0},
    )


_LEDGER_HEADER = "# Cost Ledger \u2014 %s\n"
_AGENT_ROW = "agent-row-1 | 2026-01-01 | plan | some-model | task | agent wrote this | 0\n"


def _ledger_append(task: str = "demo") -> Dict[str, Any]:
    return _scenario(
        _step_start("prt_s1"),
        {"do": "append_file", "path": ".workflow_artifacts/%s/cost-ledger.md" % task,
         "content": _AGENT_ROW},
        _step_finish("prt_f1", "stop"),
        {"do": "exit", "code": 0},
    )


def _ledger_rewrite(task: str = "demo") -> Dict[str, Any]:
    changed = "seed-row-1 | 2026-01-01 | plan | changed-model | task | rewritten by agent | 0\n"
    return _scenario(
        _step_start("prt_s1"),
        {"do": "write_file", "path": ".workflow_artifacts/%s/cost-ledger.md" % task,
         "content": (_LEDGER_HEADER % "demo") + changed},
        _step_finish("prt_f1", "stop"),
        {"do": "exit", "code": 0},
    )

# Copies of valid review and critic bodies, so the fake still imports nothing
# from Quoin.
_FOR_HUMAN = "## For human\n\nA short summary for the reader.\n\n"
CRITIC_PASS_TEXT = (
    "## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Summary\n\ntext\n\n## Issues\n\nnone\n\n"
    "## What's good\n\ntext\n\n## Scorecard\n\ntext\n"
)
REVIEW_APPROVED_TEXT = (
    "---\ntask: fixture\n---\n" + _FOR_HUMAN
    + "## Summary\n\ntext\n\n## Verdict\n\nAPPROVED\n\n## Plan Compliance\n\ntext\n\n"
    "## Issues Found\n\nnone\n\n## Integration Safety\n\ntext\n\n## Test Coverage\n\ntext\n\n"
    "## Risk Assessment\n\ntext\n\n## Dimension Verdicts\n\n| Dimension | Verdict |\n|---|---|\n| all | ok |\n"
)


def _try_write(path: str, content: str = "") -> Dict[str, Any]:
    return {"do": "try_write", "path": path, "content": content}


def _clean_finish() -> List[Dict[str, Any]]:
    return [_step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}]


def _boundary_escape(real_root: str = "/nonexistent",
                     outbox: str = ".workflow_artifacts/demo/stage-1",
                     name: str = "review-1.md", body: str = REVIEW_APPROVED_TEXT,
                     absolute: bool = False) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = [
        _step_start("prt_s1"),
        _try_write("src/app.py", "escaped\n"),
        {"do": "shell_write", "path": "src/shell.txt", "content": "shell\n"},
        {"do": "spawn_write", "path": "src/spawned.txt", "content": "spawned\n"},
        _try_write("%s/%s" % (outbox, name), body),
        _try_write("%s/%s.tmp" % (outbox, name), body),
    ]
    if absolute:
        steps.append({"do": "write_file", "path": "%s/src/escaped.txt" % real_root,
                      "content": "escaped\n"})
    return _scenario(*steps, *_clean_finish())


def _edits_path(path: str = ".opencode/commands/quoin-plan.md", content: str = "x") -> Dict[str, Any]:
    return _scenario({"do": "write_file", "path": path, "content": content}, *_clean_finish())


def _writes_paths(paths: Any = ()) -> Dict[str, Any]:
    return _scenario(
        *[{"do": "write_file", "path": p, "content": c} for p, c in paths], *_clean_finish())


def _opencode_managed_writes(extra: Any = (), offline_env: bool = True) -> Dict[str, Any]:
    """What the pinned OpenCode does to every scanned config directory at launch.

    The bookkeeping files are always written; the dependency install (lock file
    and node_modules) happens only when ``npm_config_offline`` is not ``true``
    in the child environment. ``offline_env=False`` makes the fake behave as if
    the variable were unset, whatever the launcher passed.
    """
    always = [
        (".opencode/.gitignore", "node_modules\npackage.json\nbun.lock\n"),
        (".opencode/package.json", "{}\n"),
    ]
    install = [
        (".opencode/bun.lock", "\n"),
        (".opencode/node_modules/@opencode-ai/plugin/package.json", "{}\n"),
    ]
    steps = [_try_write(p, c) for p, c in always]
    for p, c in install:
        step = _try_write(p, c)
        if offline_env:
            step["unless_offline"] = True
        steps.append(step)
    return _scenario(
        *steps,
        *[_try_write(p, c) for p, c in extra],
        *_clean_finish())


def _npm_fetch(dir: str = ".opencode") -> Dict[str, Any]:
    """A dependency install that fetches code whatever the offline setting says."""
    return _scenario(_try_write("%s/node_modules/evil/index.js" % dir, "module.exports = 1\n"), *_clean_finish())


def _empty_node_modules(dir: str = ".opencode") -> Dict[str, Any]:
    """A failed offline install that leaves only an empty node_modules directory."""
    return _scenario({"do": "make_dir", "path": "%s/node_modules" % dir}, *_clean_finish())


def _provider_call(role: str = "implementer") -> Dict[str, Any]:
    """One model call for `role` through the compiled configuration."""
    return _scenario({"do": "provider_call", "role": role}, *_clean_finish())


def _shell_net(url: str = "http://127.0.0.1:9/") -> Dict[str, Any]:
    return _scenario({"do": "shell_net", "url": url}, *_clean_finish())


def _shell_net_proxy(url: str = "http://127.0.0.1:9/") -> Dict[str, Any]:
    return _scenario({"do": "shell_net_proxy", "url": url}, *_clean_finish())


def _read_and_send(path: str = "input.txt", url: str = "http://127.0.0.1:9/") -> Dict[str, Any]:
    return _scenario({"do": "read_and_send", "path": path, "url": url}, *_clean_finish())


def _npm_install(dir: str = ".opencode") -> Dict[str, Any]:
    return _scenario({"do": "npm_install", "dir": dir}, *_clean_finish())


SCENARIOS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "provider_call": _provider_call,
    "shell_net": _shell_net,
    "shell_net_proxy": _shell_net_proxy,
    "read_and_send": _read_and_send,
    "npm_install": _npm_install,
    "npm_fetch": _npm_fetch,
    "empty_node_modules": _empty_node_modules,
    "boundary_escape": _boundary_escape,
    "edits_path": _edits_path,
    "writes_paths": _writes_paths,
    "opencode_managed_writes": _opencode_managed_writes,
    "usage_revised": _usage_revised,
    "usage_unknown_tokens": _usage_unknown_tokens,
    "usage_zero_cost": _usage_zero_cost,
    "ledger_append": _ledger_append,
    "ledger_rewrite": _ledger_rewrite,
    "replay": replay,
    "crash_after_start": lambda: _scenario(_step_start("prt_s1"), {"do": "crash", "signal": "SIGKILL"}),
    "crash_mid_stream": lambda: _scenario(
        _step_start("prt_s1"), _step_finish("prt_f1", "tool-calls"), _step_start("prt_s2"),
        _text("prt_t2", "working"), {"do": "crash", "signal": "SIGHUP"}),
    "crash_in_open_step": lambda: _scenario(
        _step_start("prt_s1"), _tool_done("prt_b1", "bash"), {"do": "crash", "signal": "SIGTERM"}),
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
    "seeded_content": _seeded_content,
    "seeded_approval": _seeded_approval,
    "grandchild_holds_stdout": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_stdout_holder"},
        {"do": "emit_fixture", "name": "plain-complete.jsonl"}, {"do": "exit", "code": 0}),
    "slow_finish": _slow_finish,
    "task_background_then_boundary_crash": lambda: _task_then_boundary_crash(_BACKGROUND_TASK_STATE),
    "task_denied_tail_then_boundary_crash": lambda: _task_then_boundary_crash(_DENIED_TAIL_TASK_STATE),
    "grandchild_detached_finish": lambda: _scenario(
        _step_start("prt_s1"), {"do": "spawn_grandchild", "ignore_term": False, "detach": True},
        {"do": "sleep", "seconds": 0.8}, _step_finish("prt_f1", "stop"), {"do": "exit", "code": 0}),
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


_MARKER = " (non-interactive run)"
_ARG_RE = re.compile(
    r"^(?:stage (?P<stage>[0-9]+) of )?(?P<task>[^\s()]+)"
    r"(?: \(context: (?P<context>[^()]*)\))?"
    r"(?P<marker>" + re.escape(_MARKER) + r")?$")
_PLACEHOLDER_RE = re.compile(r"\$(stagedir|stage|task|context)\b")


def _parse_command_arg(text: str) -> Optional[Dict[str, str]]:
    """Parse the driver-built argument; None when the order is not the driver's."""
    match = _ARG_RE.match(text)
    if match is None:
        return None
    task = match.group("task")
    stage = match.group("stage") or ""
    context = (match.group("context") or "").replace(", ", " ")
    stagedir = ".workflow_artifacts/%s/stage-%s" % (task, stage) if stage else ".workflow_artifacts/%s" % task
    return {"task": task, "stage": stage, "stagedir": stagedir, "context": context}


def _substitute(value: Any, subs: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda m: subs[m.group(1)], value)
    if isinstance(value, list):
        return [_substitute(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
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
        flag, eq, inline = token.partition("=")
        if eq and flag in _VALUE_FLAGS:
            parsed[flag[2:]] = inline
            i += 1
            continue
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


def _prior_key_runs(state: Path, key: str) -> int:
    path = state / "invocations.jsonl"
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip() and json.loads(raw).get("scenario_key") == key:
            count += 1
    return count


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


_SPAWN_WRITE_CODE = (
    "import sys\n"
    "with open(sys.argv[2], 'w', encoding='utf-8') as fh:\n"
    "    fh.write(sys.argv[1])\n"
)


def _log_effect(state: Path, line: str) -> None:
    with open(state / "effects.log", "a", encoding="utf-8") as fh:
        fh.write(line.replace("\n", " ") + "\n")


def _boundary_write(verb: str, step: Dict[str, Any], state: Path) -> None:
    """Write a file the way a tool, a shell or a spawned process would.

    A refusal by the operating system is logged as ``denied PATH ERRNO`` and
    the step list continues; nothing here claims how the real runtime reports
    a refused write.
    """
    path = step["path"]
    content = step.get("content", "")
    if step.get("unless_offline") and os.environ.get("npm_config_offline") == "true":
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if verb == "try_write":
            Path(path).write_text(content, encoding="utf-8")
        elif verb == "shell_write":
            res = subprocess.run(
                ["/bin/sh", "-c", 'printf %s "$1" > "$2"', "sh", content, path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode != 0:
                _log_effect(state, "denied %s exit%d" % (path, res.returncode))
                return
        else:
            res = subprocess.run(
                [sys.executable, "-c", _SPAWN_WRITE_CODE, content, path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode != 0:
                _log_effect(state, "denied %s exit%d" % (path, res.returncode))
                return
    except OSError as exc:
        _log_effect(state, "denied %s %s" % (path, errno.errorcode.get(exc.errno or 0, "EIO")))
        return
    _log_effect(state, path)


def _run_cmd(step: Dict[str, Any], state: Path) -> None:
    """Run a child with this process's own environment and working directory."""
    try:
        res = subprocess.run(
            [str(a) for a in step["argv"]], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        _log_effect(state, "ran 127 %s" % exc)
        return
    tail = res.stdout[:200].decode("utf-8", "replace").strip()
    _log_effect(state, "ran %d %s" % (res.returncode, tail))


# ---------------------------------------------------------------------------
# network verbs
#
# Each verb models only behaviour backed by a verified compatibility row: the
# compiled document restricts providers (`enabled_providers`) and models
# (provider `whitelist`), `apiKey` is an `{env:NAME}` reference, and a call is
# an HTTP POST to the provider's `options.baseURL`. Proxy variables are not
# honoured by `provider_call` (the proxy behaviour of the provider fetch is
# unverified), and `endpoint_family` is ignored: every call is a
# chat-completions POST, so the fake is not a model of a `responses` call.
#
# The fake enforces an egress allowlist on itself and its children. The list
# is the top-level `allowed_targets` of the scenario file (never an
# environment variable: the launcher strips names it does not know) and is
# handed to the child processes on their argument vector. A missing or empty
# list blocks every target.
# ---------------------------------------------------------------------------

BLOCKED_EXIT = 95
_NET_CHILD_FLAG = "--net-child"
_AUX_AGENTS = {"title": "title", "compaction": "compaction"}


def _split_target(url: str) -> "tuple[str, int]":
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


def _network_log(state: Path, verb: str, host: str, port: int, ok: bool, blocked: bool) -> None:
    record = {"verb": verb, "target_host": host, "target_port": port, "ok": ok, "blocked": blocked}
    with open(state / "network.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _connect_note(state: Path, verb: str, host: str, port: int) -> None:
    """Written immediately before any connect, so a test can prove none happened."""
    with open(state / "connect-attempts.log", "a", encoding="utf-8") as fh:
        fh.write("%s %s:%d\n" % (verb, host, port))


def _is_allowed(allowed: List[str], host: str, port: int) -> bool:
    return "%s:%d" % (host, port) in allowed


def _proxy_url(url: str) -> Optional[str]:
    names = ("HTTPS_PROXY", "https_proxy") if url.startswith("https") else ("HTTP_PROXY", "http_proxy")
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return None


def _net_post(verb: str, url: str, payload: bytes, allowed: List[str], state: Path, use_proxy: bool) -> int:
    """POST `payload` to `url` over a raw socket (through the proxy named in
    the environment when `use_proxy`). Returns 0, BLOCKED_EXIT, or 1 when the
    connection failed."""
    host, port = _split_target(url)
    proxy = _proxy_url(url) if use_proxy else None
    conn_host, conn_port = _split_target(proxy) if proxy else (host, port)
    if not _is_allowed(allowed, conn_host, conn_port):
        _network_log(state, verb, conn_host, conn_port, False, True)
        return BLOCKED_EXIT
    path = urlsplit(url).path or "/"
    try:
        _connect_note(state, verb, conn_host, conn_port)
        sock = socket.create_connection((conn_host, conn_port), timeout=3)
        try:
            if proxy and url.startswith("https"):
                sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode())
            else:
                target = url if proxy else path
                head = "POST %s HTTP/1.1\r\nHost: %s:%d\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (
                    target, host, port, len(payload))
                sock.sendall(head.encode() + payload)
            sock.settimeout(2)
            try:
                sock.recv(512)
            except OSError:
                pass
        finally:
            sock.close()
    except OSError:
        _network_log(state, verb, conn_host, conn_port, False, False)
        return 1
    _network_log(state, verb, conn_host, conn_port, True, False)
    return 0


def _net_child(args: List[str]) -> int:
    verb, url, state_arg, allowed_csv, mode = args[:5]
    allowed = [a for a in allowed_csv.split(",") if a]
    return _net_post(verb, url, b"{}", allowed, Path(state_arg), mode == "proxy")


def _spawn_net_child(verb: str, url: str, scenario: Dict[str, Any], state: Path, proxy: bool) -> int:
    allowed = ",".join(scenario.get("allowed_targets") or [])
    try:
        res = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), _NET_CHILD_FLAG, verb, url, str(state), allowed,
             "proxy" if proxy else "direct"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except subprocess.TimeoutExpired:
        return 1
    return res.returncode


def _document_model(doc: Dict[str, Any], role: str) -> Optional[str]:
    agents = doc.get("agent") or {}
    if role in _AUX_AGENTS:
        entry = agents.get(_AUX_AGENTS[role]) or {}
        return entry.get("model") or doc.get("small_model") or doc.get("model")
    if role == "small":
        return doc.get("small_model") or doc.get("model")
    entry = agents.get("quoin-" + role) or {}
    return entry.get("model") or doc.get("model")


def _env_reference(value: Any) -> str:
    match = re.fullmatch(r"\{env:([A-Za-z0-9_]+)\}", value) if isinstance(value, str) else None
    return os.environ.get(match.group(1), "") if match else ""


def _verb_provider_call(step: Dict[str, Any], session: str, state: Path, scenario: Dict[str, Any]) -> Optional[int]:
    """A model call for a role. Refuses (session error, exit 1, no socket)
    when the provider is not enabled or the model is not whitelisted."""
    role = step.get("role", "implementer")
    doc = json.loads(Path(os.environ["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    ref = _document_model(doc, role)

    def refuse(message: str) -> int:
        obj = _fill(_session_error_event({"message": message}), session)
        obj.setdefault("timestamp", _now_ms())
        _out(json.dumps(obj) + "\n")
        return 1

    if not isinstance(ref, str) or "/" not in ref:
        return refuse("no model for role")
    provider_id, _, model_id = ref.partition("/")
    providers = doc.get("provider") or {}
    entry = providers.get(provider_id)
    if provider_id not in (doc.get("enabled_providers") or []) or not isinstance(entry, dict):
        return refuse("provider not enabled")
    if model_id not in (entry.get("whitelist") or []):
        return refuse("model not in the provider whitelist")
    options = entry.get("options") or {}
    base = str(options.get("baseURL", "")).rstrip("/")
    key = _env_reference(options.get("apiKey"))
    host, port = _split_target(base)
    if not _is_allowed(scenario.get("allowed_targets") or [], host, port):
        _network_log(state, "provider_call", host, port, False, True)
        return BLOCKED_EXIT
    body = json.dumps({"model": model_id, "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ok = False
    try:
        _connect_note(state, "provider_call", host, port)
        with opener.open(request, timeout=5) as response:
            response.read()
            ok = True
    except OSError:
        ok = False
    _network_log(state, "provider_call", host, port, ok, False)
    return None


def _verb_read_and_send(step: Dict[str, Any], state: Path, scenario: Dict[str, Any]) -> Optional[int]:
    host, port = _split_target(step["url"])
    try:
        digest = hashlib.sha256(Path(step["path"]).read_bytes()).hexdigest()
    except OSError:
        _network_log(state, "read_and_send", host, port, False, False)
        return None
    code = _net_post("read_and_send", step["url"], digest.encode(), list(scenario.get("allowed_targets") or []),
                     state, False)
    return code if code == BLOCKED_EXIT else None


def _verb_npm_install(step: Dict[str, Any], state: Path) -> None:
    """The offline-conditional half of the dependency install."""
    if os.environ.get("npm_config_offline") == "true":
        return
    base = Path(step.get("dir", ".opencode"))
    for rel, content in (("bun.lock", "\n"), ("node_modules/@opencode-ai/plugin/package.json", "{}\n")):
        _boundary_write("try_write", {"path": str(base / rel), "content": content}, state)



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
        elif verb == "hash_env":
            value = os.environ.get(step["name"], "")
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            with open(state / "env-hashes.txt", "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (step["name"], digest))
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
        elif verb == "make_dir":
            Path(step["path"]).mkdir(parents=True, exist_ok=True)
        elif verb == "append_file":
            target = Path(step["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(step.get("content", ""))
            with open(state / "effects.log", "a", encoding="utf-8") as fh:
                fh.write("%s\n" % step["path"])
        elif verb in ("try_write", "shell_write", "spawn_write"):
            _boundary_write(verb, step, state)
        elif verb == "run_cmd":
            _run_cmd(step, state)
        elif verb == "move_path":
            target = Path(step["to"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(step["from"], step["to"])
            with open(state / "effects.log", "a", encoding="utf-8") as fh:
                fh.write("%s -> %s\n" % (step["from"], step["to"]))
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
        elif verb == "provider_call":
            code = _verb_provider_call(step, session, state, scenario)
            if code is not None:
                return code
        elif verb in ("shell_net", "shell_net_proxy"):
            code = _spawn_net_child(verb, step["url"], scenario, state, verb == "shell_net_proxy")
            if code == BLOCKED_EXIT:
                return code
        elif verb == "read_and_send":
            code = _verb_read_and_send(step, state, scenario)
            if code is not None:
                return code
        elif verb == "npm_install":
            _verb_npm_install(step, state)
        elif verb == "exit":
            return int(step["code"])
        else:
            _err("fake opencode: unknown step %r\n" % verb)
            return 99
    return 0


def main(argv: List[str]) -> int:
    if argv and argv[0] == _NET_CHILD_FLAG:
        return _net_child(argv[1:])
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
    session_path = state / "sessions" / (session + ".json")
    try:
        previous = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    session_path.write_text(
        json.dumps(dict(previous, id=session, last_attempt=attempt)) + "\n", encoding="utf-8")
    command_arg = parsed.get("command")
    commands = scenario.get("commands")
    chosen_key: Optional[str] = None
    chosen: Optional[Dict[str, Any]] = None
    arg_info: Optional[Dict[str, str]] = None
    if commands and isinstance(command_arg, str):
        arg_info = _parse_command_arg(" ".join(parsed["message"]))
        stage = arg_info["stage"] if arg_info else ""
        for candidate in ((command_arg + "@" + stage) if stage else None, command_arg):
            if candidate and candidate in commands:
                chosen_key, chosen = candidate, commands[candidate]
                break
    session_file = state / "sessions" / (session + ".json")
    if commands and chosen_key is None and requested:
        # a continuation names no command: it runs the steps of the command that opened the session
        stored = previous
        if stored.get("scenario_key") in commands and isinstance(stored.get("arg_info"), dict):
            chosen_key, chosen, arg_info = stored["scenario_key"], commands[stored["scenario_key"]], stored["arg_info"]
    if chosen_key is not None and arg_info is not None:
        session_file.write_text(json.dumps(
            {"id": session, "last_attempt": attempt, "scenario_key": chosen_key, "arg_info": arg_info}) + "\n",
            encoding="utf-8")
    entry = dict(base_entry, attempt=attempt, session_id=session, parsed=parsed)
    if chosen_key is not None:
        entry["scenario_key"] = chosen_key
    key_attempt = (_prior_key_runs(state, chosen_key) + 1) if chosen_key is not None else attempt
    _record(state, entry)

    if scenario.get("require_session_after_first") and attempt > 1 and not requested:
        _err("fake opencode: continuation attempt arrived without --session\n")
        return 97

    if chosen is not None:
        attempts = chosen["attempts"]
        steps = attempts[min(key_attempt - 1, len(attempts) - 1)]["steps"]
        if arg_info is None:
            _log_effect(state, "argument-order-invalid")
            return 96
        steps = _substitute(steps, arg_info)
    else:
        attempts = scenario["attempts"]
        steps = attempts[min(attempt - 1, len(attempts) - 1)]["steps"]
    return _run_steps(steps, session, state, scenario)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
