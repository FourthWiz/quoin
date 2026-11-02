"""Backend for an organization-approved CLI.

The configured `argv` is run as an argument array (never through a shell)
with the operation name appended, a minimal environment, the root directory
as working directory and a timeout. The request goes to the CLI's stdin as
JSON, so query text is never part of the command line:

    {"operation": "mail.search", "params": {"query": "...", "limit": 20, "cursor": null},
     "tenant": "acme", "account": null}

The CLI answers on stdout with one JSON object, either a result

    {"data": {...}, "url": "https://...", "cursor": "opaque-or-null"}

or an error `{"error": {"code": "NOT_FOUND"}}` using the envelope's codes.
Search data must hold an `items` list; the CLI's stderr is discarded so
service output never reaches the agent through diagnostics. Credentials
stay in the CLI's own store; the broker passes none.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

from ..config import real_home
from ..envelope import ERROR_CODES, OUTCOME_UNKNOWN, POLICY_DENIED, UNAVAILABLE, BrokerError
from ..operations import CURSOR_RE, OPERATIONS
from . import Result
from ._common import as_dict, text

NAME = "command"
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
TRIM_KEYS = {"search": "items", "jira.issue": "comments", "slack.thread": "messages"}

Runner = Callable[..., Tuple[int, bytes]]


def _minimal_env() -> Dict[str, str]:
    return {"PATH": SAFE_PATH, "HOME": str(real_home()), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def default_runner(argv: Sequence[str], *, stdin: bytes, timeout: float, max_bytes: int, env: Mapping[str, str]) -> Tuple[int, bytes]:
    """Run in a new session so a timeout or oversized output kills the whole
    process group, including helpers that still hold the output pipe;
    stdout is read incrementally up to the byte cap against a deadline."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argument array, no shell
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            cwd="/",
            shell=False,
            start_new_session=True,
        )
    except OSError:
        raise BrokerError(UNAVAILABLE, "the integration command could not be started", retryable=False)
    deadline = time.monotonic() + timeout
    chunks = []
    size = 0
    try:
        try:
            proc.stdin.write(stdin)  # the request is small (bounded query) and fits a pipe buffer
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_group(proc)
                    raise BrokerError(UNAVAILABLE, "the integration command did not finish within the timeout")
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65_536)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    _kill_group(proc)
                    raise BrokerError(
                        UNAVAILABLE, "the integration command output exceeded the size limit", retryable=False
                    )
                chunks.append(chunk)
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise BrokerError(UNAVAILABLE, "the integration command did not finish within the timeout")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            _kill_group(proc)
    return code, b"".join(chunks)


class Command:
    name = NAME
    default_base = None

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Any) -> Result:
        if not ctx.service.argv:
            raise BrokerError(POLICY_DENIED, "the command backend needs services.%s.argv" % ctx.service.service)
        request = {
            "operation": op_name,
            "params": dict(params),
            "tenant": ctx.service.tenant,
            "account": ctx.service.account,
        }
        runner: Runner = ctx.run_command or default_runner
        code, out = runner(
            list(ctx.service.argv) + [op_name],
            stdin=json.dumps(request).encode("utf-8"),
            timeout=ctx.limits.timeout_seconds,
            max_bytes=ctx.limits.max_response_bytes,
            env=_minimal_env(),
        )
        try:
            body = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BrokerError(UNAVAILABLE, "the integration command did not print a JSON object", retryable=False)
        if not isinstance(body, dict):
            raise BrokerError(UNAVAILABLE, "the integration command did not print a JSON object", retryable=False)
        error = body.get("error")
        if error is not None:
            err_code = text(as_dict(error).get("code"))
            if err_code not in ERROR_CODES or err_code == OUTCOME_UNKNOWN:
                err_code = UNAVAILABLE
            raise BrokerError(err_code, "the integration command reported %s" % err_code)
        if code != 0:
            raise BrokerError(UNAVAILABLE, "the integration command failed", retryable=False)
        data = body.get("data")
        op = OPERATIONS[op_name]
        trim_key = TRIM_KEYS.get("search" if op.kind == "search" else op_name)
        if not isinstance(data, dict) or (op.kind == "search" and not isinstance(data.get("items"), list)):
            raise BrokerError(UNAVAILABLE, "the integration command returned data in the wrong shape", retryable=False)
        cursor = body.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not CURSOR_RE.fullmatch(cursor)):
            raise BrokerError(UNAVAILABLE, "the integration command returned an invalid cursor", retryable=False)
        url = text(body.get("url"))
        if url is not None and not url.startswith("https://"):
            url = None
        if trim_key and not isinstance(data.get(trim_key), list):
            trim_key = None
        return Result(data=data, url=url, cursor=cursor, trim_key=trim_key)


BACKEND = Command()
