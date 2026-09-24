from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

TOOL_NAME = "quoin_probe_echo"
FIDELITY_TEXT = 'Grüße, 世界 \U0001F642 — "quoted" back\\slash\nsecond line'
FIDELITY_NOTE = ""
FOLLOW_UP_PROMPT = "Reply with exactly the nonce value returned by the tool."
CAPABILITY_FIELDS = (
    "text_generation",
    "tool_calls",
    "streamed_tool_arguments",
    "structured_output",
    "context_limit",
    "output_limit",
    "reasoning_parameters",
    "parallel_tool_calls",
    "usage_reporting",
)
RECORD_SCHEMA = "quoin.opencode.capability-record"

EXIT_QUALIFIED = 0
EXIT_NOT_QUALIFIED = 1
EXIT_COULD_NOT_RUN = 2

# code -> (message, next_action, exit_class)
DIAGNOSTICS = {
    "config_error": (
        "bad configuration",
        "fix the named argument; the credential value is never printed",
        EXIT_COULD_NOT_RUN,
    ),
    "connection_failed": (
        "could not connect to the endpoint",
        "check host/port/VPN; if a proxy is required, pass --use-env-proxy",
        EXIT_COULD_NOT_RUN,
    ),
    "tls_failed": (
        "TLS handshake or certificate verification failed",
        "supply the corporate CA bundle with --ca-file; verification is never disabled",
        EXIT_COULD_NOT_RUN,
    ),
    "redirected": (
        "the endpoint responded with a redirect",
        "the base URL is not the API endpoint; use the final API URL",
        EXIT_COULD_NOT_RUN,
    ),
    "timeout": (
        "no response before the timeout",
        "raise --timeout or check gateway health",
        EXIT_COULD_NOT_RUN,
    ),
    "auth_invalid": (
        "the credential was rejected",
        "the credential is invalid or expired; refresh it; the probe does not retry",
        EXIT_COULD_NOT_RUN,
    ),
    "auth_forbidden": (
        "the credential lacks access",
        "the credential lacks access to this model/endpoint; request entitlement",
        EXIT_COULD_NOT_RUN,
    ),
    "rate_limited": (
        "the endpoint rate-limited this request",
        "wait Retry-After seconds, then rerun; the probe does not retry",
        EXIT_COULD_NOT_RUN,
    ),
    "context_overflow": (
        "the prompt exceeded the context window",
        "reduce prompt size or record the real context limit",
        EXIT_NOT_QUALIFIED,
    ),
    "endpoint_or_model_not_found": (
        "the endpoint or model could not be found",
        "check the base URL and model id; nothing has been learned about the protocol yet",
        EXIT_COULD_NOT_RUN,
    ),
    "bad_request": (
        "the endpoint rejected the request shape",
        "compare with the gateway's documented API family",
        EXIT_NOT_QUALIFIED,
    ),
    "server_error": (
        "the endpoint returned a server error",
        "retry later and report to the gateway owner",
        EXIT_COULD_NOT_RUN,
    ),
    "invalid_response": (
        "the response was not chat-completions shaped",
        "endpoint is not chat-completions compatible at this path",
        EXIT_NOT_QUALIFIED,
    ),
    "tool_call_missing": (
        "no structured tool call was returned",
        "native tool calling unsupported; agent execution cannot be qualified",
        EXIT_NOT_QUALIFIED,
    ),
    "tool_call_id_missing": (
        "the tool call had no id",
        "gateway drops call IDs; results cannot be correlated",
        EXIT_NOT_QUALIFIED,
    ),
    "malformed_tool_arguments": (
        "the tool call arguments were not valid JSON",
        "gateway/model emits invalid argument JSON; do not add a repair parser",
        EXIT_NOT_QUALIFIED,
    ),
    "tool_result_rejected": (
        "the tool-result request was rejected",
        "gateway cannot consume tool results",
        EXIT_NOT_QUALIFIED,
    ),
    "tool_result_ignored": (
        "the final answer did not contain the nonce",
        "model did not consume the tool result",
        EXIT_NOT_QUALIFIED,
    ),
    "stream_interrupted": (
        "the stream ended before a terminal event",
        "streaming unreliable; check gateway/proxy buffering and timeouts",
        EXIT_NOT_QUALIFIED,
    ),
    "cancellation_unclean": (
        "closing the stream mid-flight did not complete cleanly",
        "client cancellation not clean",
        EXIT_NOT_QUALIFIED,
    ),
}


@dataclass
class ProbeConfig:
    base_url: str
    model: str
    credential_env: str
    provider: str
    runtime_version: str = "unknown"
    ca_file: str = None
    use_env_proxy: bool = False
    declared_context_limit: int = None
    declared_output_limit: int = None
    output: str = None
    timeout: float = 30.0
    active_error_checks: tuple = ()


class ProbeConfigError(Exception):
    pass


class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def __repr__(self):
        return "<redacted>"

    def __str__(self):
        return "<redacted>"

    def reveal(self) -> str:
        return self._value


def load_secret(config: ProbeConfig, env: dict) -> _Secret:
    value = env.get(config.credential_env)
    if not value:
        raise ProbeConfigError(
            "environment variable %s is missing or empty" % (config.credential_env,)
        )
    return _Secret(value)


def validate_config(config: ProbeConfig) -> None:
    parts = urlsplit(config.base_url)
    if parts.scheme not in ("http", "https"):
        raise ProbeConfigError("base URL scheme must be http or https")
    if "@" in (parts.netloc or ""):
        raise ProbeConfigError("base URL must not contain embedded credentials")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ProbeConfigError("http:// is only accepted for loopback hosts; use https://")
    if config.timeout is None or config.timeout <= 0:
        raise ProbeConfigError("--timeout must be greater than 0")
    if config.ca_file is not None and not os.path.exists(config.ca_file):
        raise ProbeConfigError("--ca-file does not exist: %s" % (config.ca_file,))
    allowed_checks = {"invalid-token", "context-overflow"}
    for check in config.active_error_checks:
        if check not in allowed_checks:
            raise ProbeConfigError("unknown --active-error-checks value: %s" % (check,))


def endpoint_identity(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += ":%d" % (parts.port,)
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_location(value: str) -> str:
    return endpoint_identity(value)


def _secret_forms(secrets_tuple) -> tuple:
    return tuple(secrets_tuple)


def redact(text: str, secrets_tuple) -> str:
    if text is None:
        return text
    out = text
    for form in secrets_tuple:
        if form:
            out = out.replace(form, "<redacted>")
    out = re.sub(r"Authorization:\s*\S.*", "Authorization: <redacted>", out)
    out = re.sub(r"Bearer\s+\S+", "Bearer <redacted>", out)
    out = re.sub(r"(?i)(api[_-]?key|token)=[^&\s\"]+", r"\1=<redacted>", out)
    return out


def _emit(stream, line: str, secrets_tuple) -> None:
    print(redact(line, secrets_tuple), file=stream)


def clip(text: str, secrets_tuple, limit: int) -> str:
    if text is None:
        return text
    redacted = redact(text, secrets_tuple)
    return redacted[:limit]


class ErrorInfo:
    def __init__(self, code=None, error_type=None, message=None, raw=None):
        self.code = code
        self.error_type = error_type
        self.message = message
        self.raw = raw


def read_error_body(resp, secrets_tuple) -> ErrorInfo:
    max_form_len = max((len(f) for f in secrets_tuple if f), default=0)
    margin = 4 * max_form_len + 64
    limit = 65536 + margin
    try:
        raw_bytes = resp.read(limit)
    except Exception:
        raw_bytes = b""
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    raw_text = redact(raw_text, secrets_tuple)
    info = ErrorInfo(raw=raw_text)
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        info.message = clip(raw_text, secrets_tuple, 300)
        return info
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        info.code = error.get("code")
        info.error_type = error.get("type")
        message = error.get("message")
    else:
        message = None
    info.message = clip(message if message is not None else raw_text, secrets_tuple, 300)
    return info


def build_opener(config: ProbeConfig):
    handlers = []
    if not config.use_env_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    context = ssl.create_default_context(cafile=config.ca_file)
    handlers.append(urllib.request.HTTPSHandler(context=context))

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def _post(opener, config: ProbeConfig, secret: _Secret, payload: dict, headers: dict, stream: bool):
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req_headers.update(headers or {})
    req = urllib.request.Request(config.base_url + "/chat/completions", data=data, headers=req_headers, method="POST")
    req.add_unredirected_header("Authorization", "Bearer " + secret.reveal())
    return opener.open(req, timeout=config.timeout)


_MODEL_NOT_FOUND_RE = re.compile(
    r"unknown model|no such model|invalid model|"
    r"\bmodel\s+['\"`]?[\w./:-]+['\"`]?\s+(not found|does not exist)\b",
    re.IGNORECASE,
)


def matches_model_not_found(err: ErrorInfo) -> bool:
    if err is None:
        return False
    if err.code == "model_not_found":
        return True
    haystack = " ".join(str(v) for v in (err.code, err.error_type, err.message) if v is not None)
    return bool(_MODEL_NOT_FOUND_RE.search(haystack))


def parse_retry_after(value, now):
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    delta = (parsed - now).total_seconds()
    return max(0, int(delta))


@dataclass
class Diagnostic:
    code: str
    message: str
    next_action: str
    step: int = None
    http_status: int = None
    retry_after_seconds: int = None
    detail: str = None

    @property
    def exit_class(self) -> int:
        return DIAGNOSTICS[self.code][2]


def _make_diagnostic(code, step=None, http_status=None, retry_after_seconds=None, detail=None, message=None):
    base_message, next_action, _exit_class = DIAGNOSTICS[code]
    return Diagnostic(
        code=code,
        message=message or base_message,
        next_action=next_action,
        step=step,
        http_status=http_status,
        retry_after_seconds=retry_after_seconds,
        detail=detail,
    )


_CONTEXT_OVERFLOW_RE = re.compile(
    r"context_length_exceeded|maximum context length|context window|too many tokens",
    re.IGNORECASE,
)


def classify_exception(
    exc,
    *,
    after_headers: bool,
    is_tool_result_request: bool,
    has_tools: bool,
    step1_passed: bool,
    now,
    secrets_tuple,
    step: int = None,
) -> Diagnostic:
    if isinstance(exc, urllib.error.HTTPError) and 300 <= exc.code < 400:
        location = exc.headers.get("Location") if exc.headers else None
        return _make_diagnostic(
            "redirected", step=step, http_status=exc.code,
            detail=redact_location(location) if location else None,
        )

    reason = exc.reason if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError) else None
    ssl_err = exc if isinstance(exc, ssl.SSLError) else (reason if isinstance(reason, ssl.SSLError) else None)
    if ssl_err is not None:
        return _make_diagnostic("tls_failed", step=step, detail=clip(str(ssl_err), secrets_tuple, 300))

    timeout_exc = exc if isinstance(exc, (socket.timeout, TimeoutError)) else (
        reason if isinstance(reason, (socket.timeout, TimeoutError)) else None
    )
    if timeout_exc is not None:
        code = "stream_interrupted" if after_headers else "timeout"
        return _make_diagnostic(code, step=step)

    stream_exc = exc if isinstance(exc, (http.client.IncompleteRead, ConnectionResetError, BrokenPipeError)) else (
        reason if isinstance(reason, (http.client.IncompleteRead, ConnectionResetError, BrokenPipeError)) else None
    )
    if stream_exc is not None and after_headers:
        return _make_diagnostic("stream_interrupted", step=step)

    conn_exc = exc if isinstance(exc, (ConnectionRefusedError, socket.gaierror, http.client.RemoteDisconnected)) else (
        reason if isinstance(reason, (ConnectionRefusedError, socket.gaierror, http.client.RemoteDisconnected)) else None
    )
    if conn_exc is not None and not after_headers:
        return _make_diagnostic("connection_failed", step=step, detail=clip(str(conn_exc), secrets_tuple, 300))

    if isinstance(exc, OSError) and not isinstance(exc, urllib.error.HTTPError) and not after_headers:
        return _make_diagnostic("connection_failed", step=step, detail=clip(str(exc), secrets_tuple, 300))

    if not isinstance(exc, urllib.error.HTTPError):
        detail = "non_http_reply" if not after_headers else "invalid_response"
        code = "connection_failed" if not after_headers else "invalid_response"
        return _make_diagnostic(code, step=step, detail=detail, message=clip(str(exc), secrets_tuple, 300))

    status = exc.code
    err_info = read_error_body(exc, secrets_tuple)

    if status == 401:
        return _make_diagnostic("auth_invalid", step=step, http_status=status, detail=err_info.message)
    if status == 403:
        return _make_diagnostic("auth_forbidden", step=step, http_status=status, detail=err_info.message)
    if status == 429:
        retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None, now)
        return _make_diagnostic("rate_limited", step=step, http_status=status, retry_after_seconds=retry_after, detail=err_info.message)

    haystack = " ".join(str(v) for v in (err_info.code, err_info.error_type, err_info.message) if v is not None)
    if status in (400, 413, 422) and _CONTEXT_OVERFLOW_RE.search(haystack):
        return _make_diagnostic("context_overflow", step=step, http_status=status, detail=err_info.message)

    if not step1_passed:
        if status in (404, 405):
            return _make_diagnostic("endpoint_or_model_not_found", step=step, http_status=status, detail=err_info.message)
        if status in (400, 404, 422) and matches_model_not_found(err_info):
            return _make_diagnostic("endpoint_or_model_not_found", step=step, http_status=status, detail=err_info.message)
    else:
        if status in (404, 405):
            if has_tools and not is_tool_result_request:
                detail = "http_404_tools_unavailable" if status == 404 else "http_405_tools_unavailable"
                return _make_diagnostic("tool_call_missing", step=step, http_status=status, detail=detail)
        elif status in (400, 422) and matches_model_not_found(err_info):
            pass  # not-found handling only applies before step 1 passes; fall through

    if status >= 500:
        return _make_diagnostic("server_error", step=step, http_status=status, detail=err_info.message)

    if is_tool_result_request and status in (400, 404, 405, 422):
        return _make_diagnostic("tool_result_rejected", step=step, http_status=status, detail=err_info.message)

    if 400 <= status < 500:
        return _make_diagnostic("bad_request", step=step, http_status=status, detail=err_info.message)

    return _make_diagnostic("invalid_response", step=step, http_status=status, detail=err_info.message)


@dataclass
class ProbeContext:
    config: ProbeConfig
    secret: _Secret
    secrets: tuple
    opener: object
    now: object
    nonce_factory: object
    extra_headers: dict = field(default_factory=dict)
    step1_passed: bool = False
    step1_usage: dict = None
    observations: list = field(default_factory=list)

    def __repr__(self):
        return "ProbeContext(endpoint=%r, model=%r, step1_passed=%r)" % (
            endpoint_identity(self.config.base_url),
            self.config.model,
            self.step1_passed,
        )


def _secret_form_set(secret: _Secret) -> tuple:
    raw = secret.reveal()
    return (
        raw,
        "Bearer " + raw,
        json.dumps(raw)[1:-1],
        json.dumps(raw, ensure_ascii=True)[1:-1],
    )


def make_context(config: ProbeConfig, env: dict, *, now=None, nonce_factory=None, extra_headers=None) -> ProbeContext:
    validate_config(config)
    secret = load_secret(config, env)
    opener = build_opener(config)
    return ProbeContext(
        config=config,
        secret=secret,
        secrets=_secret_form_set(secret),
        opener=opener,
        now=now or datetime.datetime.now(datetime.timezone.utc),
        nonce_factory=nonce_factory or (lambda: secrets.token_hex(8)),
        extra_headers=extra_headers or {},
    )


@dataclass
class StepResult:
    result: str
    diagnostic: Diagnostic = None
    detail: str = None
    tool_call_id: str = None
    arguments: dict = None
    fidelity_ok: bool = None


def _extra(ctx: ProbeContext, key: str) -> dict:
    return ctx.extra_headers.get(key) or {}


def run_step1(ctx: ProbeContext) -> StepResult:
    payload = {
        "model": ctx.config.model,
        "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
        "stream": False,
    }
    try:
        resp = _post(ctx.opener, ctx.config, ctx.secret, payload, _extra(ctx, "step1"), stream=False)
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=False, is_tool_result_request=False, has_tools=False,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=1,
        )
        return StepResult("fail", diagnostic=diag)
    try:
        raw = resp.read()
        body = json.loads(raw.decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        if not content:
            raise ValueError("empty content")
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=True, is_tool_result_request=False, has_tools=False,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=1,
        )
        return StepResult("fail", diagnostic=diag)
    ctx.step1_passed = True
    ctx.step1_usage = body.get("usage")
    return StepResult("pass")


def _tool_schema():
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "Echo tool used to qualify tool-calling support.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["text"],
                },
            },
        }
    ]


def run_tool_round_trip(ctx: ProbeContext, stream: bool, step_key: str) -> StepResult:
    step = 2 if not stream else 3
    tools = _tool_schema()
    messages1 = [
        {
            "role": "user",
            "content": (
                "Call the %s tool with text set to exactly %r and note set to an empty string."
                % (TOOL_NAME, FIDELITY_TEXT)
            ),
        }
    ]
    payload1 = {"model": ctx.config.model, "messages": messages1, "tools": tools, "stream": stream}
    headers1 = _extra(ctx, step_key)
    try:
        resp1 = _post(ctx.opener, ctx.config, ctx.secret, payload1, headers1, stream=stream)
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=False, is_tool_result_request=False, has_tools=True,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=step,
        )
        return StepResult("fail", diagnostic=diag)

    try:
        if stream:
            tool_call_id, name, args_text, finish_ok = _parse_sse_tool_call(resp1, ctx.config.timeout)
        else:
            raw = resp1.read()
            body = json.loads(raw.decode("utf-8"))
            message = body["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return StepResult("fail", diagnostic=_make_diagnostic("tool_call_missing", step=step))
            call = tool_calls[0]
            tool_call_id = call.get("id")
            name = call.get("function", {}).get("name")
            args_text = call.get("function", {}).get("arguments")
    except _StreamInterrupted:
        return StepResult("fail", diagnostic=_make_diagnostic("stream_interrupted", step=step))
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=True, is_tool_result_request=False, has_tools=True,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=step,
        )
        return StepResult("fail", diagnostic=diag)

    if name != TOOL_NAME or not tool_calls_present(name):
        return StepResult("fail", diagnostic=_make_diagnostic("tool_call_missing", step=step, detail="unexpected tool name"))
    if not tool_call_id:
        return StepResult("fail", diagnostic=_make_diagnostic("tool_call_id_missing", step=step))
    try:
        arguments = json.loads(args_text)
    except (ValueError, TypeError):
        return StepResult("fail", diagnostic=_make_diagnostic("malformed_tool_arguments", step=step))

    fidelity_ok = arguments == {"text": FIDELITY_TEXT, "note": FIDELITY_NOTE}
    if not fidelity_ok and isinstance(arguments, dict):
        ctx.observations.append(
            {"step": step, "code": "argument_fidelity_mismatch", "result": "warn", "detail": clip(json.dumps(arguments), ctx.secrets, 300)}
        )

    nonce = ctx.nonce_factory()
    tool_message_content = json.dumps({"echo": arguments.get("text") if isinstance(arguments, dict) else None, "nonce": nonce})
    messages2 = messages1 + [
        {"role": "assistant", "content": None, "tool_calls": [{"id": tool_call_id, "type": "function", "function": {"name": TOOL_NAME, "arguments": args_text}}]},
        {"role": "tool", "tool_call_id": tool_call_id, "content": tool_message_content},
        {"role": "user", "content": FOLLOW_UP_PROMPT},
    ]
    payload2 = {"model": ctx.config.model, "messages": messages2, "tools": tools, "stream": stream}
    try:
        resp2 = _post(ctx.opener, ctx.config, ctx.secret, payload2, headers1, stream=stream)
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=False, is_tool_result_request=True, has_tools=True,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=step,
        )
        return StepResult("fail", diagnostic=diag)

    try:
        if stream:
            answer = _parse_sse_text(resp2, ctx.config.timeout)
        else:
            raw2 = resp2.read()
            body2 = json.loads(raw2.decode("utf-8"))
            answer = body2["choices"][0]["message"].get("content") or ""
    except _StreamInterrupted:
        return StepResult("fail", diagnostic=_make_diagnostic("stream_interrupted", step=step))
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=True, is_tool_result_request=True, has_tools=True,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=step,
        )
        return StepResult("fail", diagnostic=diag)

    normalized_answer = " ".join(answer.split()).lower()
    normalized_nonce = " ".join(nonce.split()).lower()
    if normalized_nonce not in normalized_answer:
        ctx.observations.append({"step": step, "code": "nonce_miss", "detail": clip(answer, ctx.secrets, 300)})
        return StepResult("fail", diagnostic=_make_diagnostic("tool_result_ignored", step=step))

    return StepResult(
        "pass" if fidelity_ok else "warn",
        tool_call_id=tool_call_id,
        arguments=arguments,
        fidelity_ok=fidelity_ok,
    )


def run_step3(ctx: ProbeContext) -> StepResult:
    """Streamed tool round trip, then the cancellation sub-check (once, not per iteration)."""
    result = run_tool_round_trip(ctx, stream=True, step_key="step3")
    if result.result == "fail":
        return result
    cancel_result = _run_cancellation_check(ctx, 3)
    if cancel_result.result != "pass":
        return cancel_result
    return result


def tool_calls_present(name) -> bool:
    return name is not None


class _StreamInterrupted(Exception):
    pass


def _parse_sse_tool_call(resp, timeout):
    content_parts = []
    tool_calls = {}
    finish_reason = None
    done = False
    for line in _sse_lines(resp, timeout):
        if line == "[DONE]":
            done = True
            break
        try:
            env = json.loads(line)
        except ValueError:
            raise ValueError("invalid SSE data line")
        choice = env["choices"][0]
        delta = choice.get("delta", {})
        if delta.get("content"):
            content_parts.append(delta["content"])
        for tc in delta.get("tool_calls", []) or []:
            idx = tc.get("index", 0)
            entry = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
            if tc.get("id"):
                entry["id"] = tc["id"]
            func = tc.get("function") or {}
            if func.get("name"):
                entry["name"] = func["name"]
            if func.get("arguments"):
                entry["arguments"] += func["arguments"]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    if not (finish_reason and done):
        raise _StreamInterrupted()
    if not tool_calls:
        return None, None, None, True
    first = tool_calls[min(tool_calls)]
    return first["id"], first["name"], first["arguments"], True


def _parse_sse_text(resp, timeout):
    content_parts = []
    finish_reason = None
    done = False
    for line in _sse_lines(resp, timeout):
        if line == "[DONE]":
            done = True
            break
        env = json.loads(line)
        choice = env["choices"][0]
        delta = choice.get("delta", {})
        if delta.get("content"):
            content_parts.append(delta["content"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    if not (finish_reason and done):
        raise _StreamInterrupted()
    return "".join(content_parts)


def _sse_lines(resp, timeout):
    resp.fp.raw._sock.settimeout(timeout) if hasattr(resp, "fp") and hasattr(resp.fp, "raw") else None
    while True:
        raw_line = resp.readline()
        if not raw_line:
            return
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            yield line[len("data:"):].strip()


def _run_cancellation_check(ctx: ProbeContext, step: int) -> StepResult:
    payload = {
        "model": ctx.config.model,
        "messages": [{"role": "user", "content": "Count slowly to a large number."}],
        "stream": True,
    }
    start = ctx.now
    import time as _time

    t0 = _time.monotonic()
    try:
        resp = _post(ctx.opener, ctx.config, ctx.secret, payload, _extra(ctx, "step3_cancel"), stream=True)
        first_line = None
        for line in _sse_lines(resp, ctx.config.timeout):
            first_line = line
            break
        resp.close()
    except Exception as exc:
        diag = classify_exception(
            exc, after_headers=False, is_tool_result_request=False, has_tools=False,
            step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=step,
        )
        return StepResult("fail", diagnostic=diag)
    elapsed = _time.monotonic() - t0
    if elapsed >= ctx.config.timeout:
        return StepResult("fail", diagnostic=_make_diagnostic("cancellation_unclean", step=step))
    return StepResult("pass")


@dataclass
class ProbeReport:
    context: ProbeContext
    steps: list
    verdict: str
    blocking_step: int = None


def run_probe(config: ProbeConfig, env: dict, now=None, nonce_factory=None, extra_headers=None) -> ProbeReport:
    try:
        ctx = make_context(config, env, now=now, nonce_factory=nonce_factory, extra_headers=extra_headers)
    except ProbeConfigError as exc:
        diag = _make_diagnostic("config_error", message=str(exc))
        steps = [{"step": s, "name": n, "result": "skipped", "diagnostic": None} for s, n in enumerate(("auth_and_text", "tool_round_trip", "streaming"), start=1)]
        return ProbeReport(context=None, steps=steps, verdict="could_not_run", blocking_step=None)

    step_defs = [
        (1, "auth_and_text", lambda: run_step1(ctx)),
        (2, "tool_round_trip", lambda: run_tool_round_trip(ctx, stream=False, step_key="step2")),
        (3, "streaming", lambda: run_step3(ctx)),
    ]
    steps = []
    blocking_step = None
    stopped = False
    for step_num, name, fn in step_defs:
        if stopped:
            steps.append({"step": step_num, "name": name, "result": "skipped", "diagnostic": None})
            continue
        result = fn()
        entry = {
            "step": step_num,
            "name": name,
            "result": result.result,
            "diagnostic": result.diagnostic,
        }
        steps.append(entry)
        if result.result == "fail":
            blocking_step = step_num
            stopped = True

    if blocking_step is None:
        verdict = "qualified"
    else:
        exit_class = steps[blocking_step - 1]["diagnostic"].exit_class
        verdict = "not_qualified" if exit_class == EXIT_NOT_QUALIFIED else "could_not_run"

    run_step4(ctx, config, env)

    return ProbeReport(context=ctx, steps=steps, verdict=verdict, blocking_step=blocking_step)


def run_step4(ctx: ProbeContext, config: ProbeConfig, env: dict) -> None:
    ctx.step4 = {"invalid_token": None, "context_overflow": None}
    if "invalid-token" in config.active_error_checks:
        bad_secret = _Secret("quoin-probe-invalid-token")
        payload = {"model": config.model, "messages": [{"role": "user", "content": "hello"}], "stream": False}
        try:
            _post(ctx.opener, config, bad_secret, payload, _extra(ctx, "step4_invalid_token"), stream=False)
            passed = False
        except Exception as exc:
            diag = classify_exception(
                exc, after_headers=False, is_tool_result_request=False, has_tools=False,
                step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=4,
            )
            passed = diag.code == "auth_invalid"
        ctx.step4["invalid_token"] = {"ran_live": True, "result": "pass" if passed else "fail"}
    if "context-overflow" in config.active_error_checks:
        limit = config.declared_context_limit or 262144
        filler_len = min(4 * limit + 1024, 8 * 1024 * 1024)
        payload = {"model": config.model, "messages": [{"role": "user", "content": "x" * filler_len}], "stream": False}
        try:
            _post(ctx.opener, config, ctx.secret, payload, _extra(ctx, "step4_context_overflow"), stream=False)
            passed = False
        except Exception as exc:
            diag = classify_exception(
                exc, after_headers=False, is_tool_result_request=False, has_tools=False,
                step1_passed=ctx.step1_passed, now=ctx.now, secrets_tuple=ctx.secrets, step=4,
            )
            passed = diag.code == "context_overflow"
        ctx.step4["context_overflow"] = {"ran_live": True, "result": "pass" if passed else "fail"}


def build_capability_record(report: ProbeReport) -> dict:
    config = report.context.config if report.context else None
    now = report.context.now if report.context else datetime.datetime.now(datetime.timezone.utc)

    def field_status(names, unsupported_from=None):
        for entry in report.steps:
            if entry["name"] in names:
                if entry["result"] in ("pass", "warn"):
                    return {"status": "supported", "source": "observed", "value": None, "detail": entry["result"]}
                if entry["result"] == "fail":
                    return {"status": "unsupported", "source": "observed", "value": None, "detail": entry["diagnostic"].code if entry["diagnostic"] else None}
        return {"status": "unknown", "source": "not_tested", "value": None, "detail": None}

    capabilities = {}
    capabilities["text_generation"] = field_status(["auth_and_text"])
    capabilities["tool_calls"] = field_status(["tool_round_trip"])
    capabilities["streamed_tool_arguments"] = field_status(["streaming"])
    capabilities["structured_output"] = {"status": "unknown", "source": "not_tested", "value": None, "detail": None}
    capabilities["reasoning_parameters"] = {"status": "unknown", "source": "not_tested", "value": None, "detail": None}
    capabilities["parallel_tool_calls"] = {"status": "unknown", "source": "not_tested", "value": None, "detail": None}

    for field_name, declared in (
        ("context_limit", config.declared_context_limit if config else None),
        ("output_limit", config.declared_output_limit if config else None),
    ):
        if declared is not None:
            capabilities[field_name] = {"status": "unknown", "source": "declared", "value": declared, "detail": None}
        else:
            capabilities[field_name] = {"status": "unknown", "source": "not_tested", "value": None, "detail": None}

    usage = report.context.step1_usage if report.context else None
    if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
        capabilities["usage_reporting"] = {"status": "supported", "source": "observed", "value": None, "detail": None}
    else:
        capabilities["usage_reporting"] = {"status": "unknown", "source": "not_tested", "value": None, "detail": None}

    steps_out = []
    diagnostics_out = []
    for entry in report.steps:
        diag = entry["diagnostic"]
        diag_dict = None
        if diag is not None:
            diag_dict = {
                "code": diag.code,
                "message": diag.message,
                "next_action": diag.next_action,
                "http_status": diag.http_status,
                "retry_after_seconds": diag.retry_after_seconds,
            }
            diagnostics_out.append(diag_dict)
        steps_out.append(
            {
                "step": entry["step"],
                "name": entry["name"],
                "result": entry["result"],
                "checks": [],
                "diagnostic": diag_dict,
            }
        )

    summary = "agent execution qualified" if report.verdict == "qualified" else (
        "not qualified: %s" % (report.steps[report.blocking_step - 1]["name"],) if report.blocking_step else report.verdict
    )

    return {
        "schema": RECORD_SCHEMA,
        "schema_version": 1,
        "key": {
            "provider": config.provider if config else None,
            "model_id": config.model if config else None,
            "endpoint": endpoint_identity(config.base_url) if config else None,
            "runtime": {"name": "opencode", "version": config.runtime_version if config else "unknown"},
            "probe_date": now.date().isoformat(),
        },
        "probed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe": {"name": "probe_gateway", "record_format": 1},
        "capabilities": capabilities,
        "steps": steps_out,
        "verdict": {
            "status": report.verdict,
            "summary": summary,
            "blocking_step": report.blocking_step,
        },
        "diagnostics": diagnostics_out,
    }


def write_record(record: dict, path: str, secrets_tuple) -> bool:
    try:
        text = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False)
        text = redact(text, secrets_tuple)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
        return True
    except OSError:
        return False


def execute(config: ProbeConfig, env: dict, extra_headers=None, now=None, nonce_factory=None) -> int:
    secrets_tuple = ()
    try:
        report = run_probe(config, env, now=now, nonce_factory=nonce_factory, extra_headers=extra_headers)
    except ProbeConfigError as exc:
        diag = _make_diagnostic("config_error", message=str(exc))
        _emit(sys.stderr, "probe: %s: %s Next: %s" % (diag.code, diag.message, diag.next_action), secrets_tuple)
        if config.output:
            record = {
                "schema": RECORD_SCHEMA,
                "schema_version": 1,
                "verdict": {"status": "could_not_run", "summary": diag.message, "blocking_step": None},
                "diagnostics": [{"code": diag.code, "message": diag.message, "next_action": diag.next_action}],
            }
            write_record(record, config.output, secrets_tuple)
        return EXIT_COULD_NOT_RUN

    if report.context is not None:
        secrets_tuple = report.context.secrets

    for entry in report.steps:
        if entry["diagnostic"] is not None:
            diag = entry["diagnostic"]
            _emit(sys.stderr, "probe: %s: %s Next: %s" % (diag.code, diag.message, diag.next_action), secrets_tuple)

    record = build_capability_record(report)
    if config.output:
        write_record(record, config.output, secrets_tuple)

    print(redact(report.verdict, secrets_tuple))

    exit_map = {"qualified": EXIT_QUALIFIED, "not_qualified": EXIT_NOT_QUALIFIED, "could_not_run": EXIT_COULD_NOT_RUN}
    return exit_map[report.verdict]


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_gateway",
        description="Qualify a gateway/model for OpenCode agent execution over the chat-completions protocol.",
        epilog=(
            "Exit codes: 0 = qualified, 1 = not qualified (protocol incompatibility observed), "
            "2 = could not run (inconclusive: configuration, connection, auth, rate limit, "
            "server error, redirect, or not-found)."
        ),
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-version", default="unknown")
    parser.add_argument("--ca-file", default=None)
    parser.add_argument("--use-env-proxy", action="store_true")
    parser.add_argument("--declared-context-limit", type=int, default=None)
    parser.add_argument("--declared-output-limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--active-error-checks", default="")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_COULD_NOT_RUN

    checks = tuple(c for c in args.active_error_checks.split(",") if c)
    config = ProbeConfig(
        base_url=args.base_url,
        model=args.model,
        credential_env=args.credential_env,
        provider=args.provider,
        runtime_version=args.runtime_version,
        ca_file=args.ca_file,
        use_env_proxy=args.use_env_proxy,
        declared_context_limit=args.declared_context_limit,
        declared_output_limit=args.declared_output_limit,
        output=args.output,
        timeout=args.timeout,
        active_error_checks=checks,
    )
    effective_env = env if env is not None else os.environ

    try:
        return execute(config, effective_env)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - last-resort catch-all, never a bare traceback
        _emit(sys.stderr, "probe: config_error: unexpected failure: %s Next: check the arguments and retry" % (exc,), ())
        return EXIT_COULD_NOT_RUN


if __name__ == "__main__":
    sys.exit(main())
