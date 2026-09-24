from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_SCENARIOS_PATH = Path(__file__).resolve().parent / "fixtures" / "scenarios.json"

_REPLY_KEYS = ("body", "body_text", "message", "chunks")
_TEMPLATE_RE = re.compile(r"\{\{(last_tool_content|last_tool_call_id)\}\}")
_ECHO_RE = re.compile(r"\{\{echoed_credential\}\}")


def load_scenarios(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema_version") != 1:
        raise ValueError("scenarios file: unsupported schema_version %r" % (data.get("schema_version"),))
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        raise ValueError("scenarios file: 'scenarios' must be a non-empty object")
    for name, scenario in scenarios.items():
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("scenario %r: 'turns' must be a non-empty list" % (name,))
        for idx, turn in enumerate(turns):
            if not isinstance(turn.get("status"), int):
                raise ValueError("scenario %r turn %d: 'status' must be an int" % (name, idx))
            present = [k for k in _REPLY_KEYS if k in turn]
            if len(present) > 1:
                raise ValueError(
                    "scenario %r turn %d: conflicting reply keys %r" % (name, idx, present)
                )
    return data


class _NoMatchingTurn(Exception):
    pass


class _ToolCallIdMismatch(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class FakeProviderServer:
    """A minimal OpenAI-chat-completions-shaped fake, for offline gateway tests."""

    def __init__(
        self,
        scenarios=None,
        scenarios_path=None,
        default_scenario="default_ok",
        expected_token=None,
        host="127.0.0.1",
    ):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("FakeProviderServer only binds 127.0.0.1 or localhost, got %r" % (host,))
        if scenarios is not None:
            self._doc = scenarios
        else:
            self._doc = load_scenarios(scenarios_path or DEFAULT_SCENARIOS_PATH)
        self._scenarios = self._doc["scenarios"]
        self._default_scenario = default_scenario
        self._expected_token = expected_token
        self._host = host
        self._lock = threading.Lock()
        self._requests = []
        self._httpd = None
        self._thread = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
                pass

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

        httpd = ThreadingHTTPServer((self._host, 0), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % (self.port,)

    # -- introspection ---------------------------------------------------
    def snapshot_requests(self) -> list:
        with self._lock:
            return copy.deepcopy(self._requests)

    def clear_requests(self) -> None:
        with self._lock:
            self._requests = []

    def scenario_names(self) -> list:
        return sorted(self._scenarios.keys())

    def _record(self, method, path, scenario, turn_index, body, headers, outcome):
        auth_value = None
        recorded_headers = {}
        for key, value in headers.items():
            if key.lower() == "authorization":
                auth_value = value
                recorded_headers[key] = "<redacted>"
            else:
                recorded_headers[key] = value
        auth_present = auth_value is not None
        auth_matches_expected = None
        if self._expected_token is not None:
            auth_matches_expected = auth_value == ("Bearer " + self._expected_token)
        with self._lock:
            self._requests.append(
                {
                    "method": method,
                    "path": path,
                    "scenario": scenario,
                    "turn_index": turn_index,
                    "body": body,
                    "headers": recorded_headers,
                    "auth_present": auth_present,
                    "auth_matches_expected": auth_matches_expected,
                    "outcome": outcome,
                }
            )
        return auth_value

    # -- routing -----------------------------------------------------
    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = handler.path.split("?", 1)[0]
        length = int(handler.headers.get("Content-Length") or 0)
        raw_body = handler.rfile.read(length) if length else b""
        headers = {k: v for k, v in handler.headers.items()}

        if method == "GET" and path == "/v1/models":
            self._record("GET", path, None, None, None, headers, "ok")
            models = [{"id": name, "object": "model", "owned_by": "fake"} for name in self.scenario_names()]
            self._write_json(handler, 200, {"object": "list", "data": models})
            return

        if not (method == "POST" and path == "/v1/chat/completions"):
            self._record(method, path, None, None, None, headers, "ok")
            self._write_json(handler, 404, {"error": {"code": "not_found"}})
            return

        try:
            request_body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, ValueError):
            self._record(method, path, None, None, None, headers, "ok")
            self._write_json(handler, 400, {"error": {"code": "invalid_request_json"}})
            return

        scenario_name = headers.get("X-Fake-Scenario")
        if scenario_name is None:
            model = request_body.get("model")
            if isinstance(model, str) and model in self._scenarios:
                scenario_name = model
            else:
                scenario_name = self._default_scenario
        if scenario_name not in self._scenarios:
            self._record(method, path, scenario_name, None, request_body, headers, "ok")
            self._write_json(handler, 400, {"error": {"code": "unknown_scenario"}})
            return

        auth_value = self._record(method, path, scenario_name, None, request_body, headers, "ok")
        if self._expected_token is not None and auth_value != ("Bearer " + self._expected_token):
            self._write_json(handler, 401, {"error": {"code": "invalid_api_key", "message": "Invalid API key"}})
            return

        turns = self._scenarios[scenario_name]["turns"]
        depth = sum(1 for m in request_body.get("messages", []) if m.get("role") == "assistant")
        has_tools = bool(request_body.get("tools"))
        wants_stream = bool(request_body.get("stream"))

        try:
            turn_index, turn = self._select_turn(turns, depth, has_tools, wants_stream)
        except _NoMatchingTurn:
            self._write_json(handler, 400, {"error": {"code": "no_matching_turn"}})
            return

        expect = turn.get("expect")
        last_tool_content = None
        last_tool_call_id = None
        for msg in reversed(request_body.get("messages", [])):
            if msg.get("role") == "tool":
                last_tool_content = msg.get("content")
                last_tool_call_id = msg.get("tool_call_id")
                break
        if expect and "tool_call_id" in expect:
            if last_tool_call_id != expect["tool_call_id"]:
                self._write_json(
                    handler,
                    400,
                    {"error": {"code": "tool_call_id_mismatch", "message": "unexpected tool_call_id"}},
                )
                return

        echoed = None
        if turn.get("echo_auth"):
            raw = auth_value or ""
            if turn["echo_auth"] == "token" and raw.startswith("Bearer "):
                raw = raw[len("Bearer ") :]
            pad = turn.get("echo_pad", 0)
            echoed = ("X" * pad) + raw

        status = turn["status"]
        stream_flag = turn.get("stream", "auto")
        is_stream = wants_stream if stream_flag == "auto" else bool(stream_flag)

        substitutions = {"last_tool_content": last_tool_content, "last_tool_call_id": last_tool_call_id}

        if "body" in turn:
            body = _substitute(turn["body"], substitutions, echoed)
            self._write_json(handler, status, body, extra_headers=turn.get("headers"))
            return
        if "body_text" in turn:
            text = _substitute_text(turn["body_text"], substitutions, echoed)
            self._write_text(handler, status, text, extra_headers=turn.get("headers"))
            return

        if is_stream:
            self._write_stream(handler, status, turn, substitutions, echoed, turn_index, scenario_name)
            return

        message = _substitute(turn.get("message", {}), substitutions, echoed)
        envelope = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": request_body.get("model", scenario_name),
            "choices": [{"index": 0, "message": message, "finish_reason": turn.get("finish_reason")}],
        }
        if turn.get("usage") is not None:
            envelope["usage"] = turn["usage"]
        self._write_json(handler, status, envelope, extra_headers=turn.get("headers"))

    def _select_turn(self, turns, depth, has_tools, wants_stream):
        exact = None
        for idx, turn in enumerate(turns):
            turn_depth = turn.get("depth", idx)
            match = turn.get("match")
            if turn_depth != depth:
                continue
            if match is not None:
                if "tools" in match and bool(match["tools"]) != has_tools:
                    continue
                if "stream" in match and bool(match["stream"]) != wants_stream:
                    continue
            exact = (idx, turn)
            break
        if exact is not None:
            return exact
        best = None
        for idx, turn in enumerate(turns):
            turn_depth = turn.get("depth", idx)
            if turn.get("match") is not None:
                continue
            if turn_depth <= depth:
                if best is None or turn_depth > best[1].get("depth", best[0]):
                    best = (idx, turn)
        if best is not None:
            return best
        raise _NoMatchingTurn()

    def _write_stream(self, handler, status, turn, substitutions, echoed, turn_index, scenario_name):
        handler.send_response(status)
        handler.send_header("Content-Type", "text/event-stream")
        for key, value in (turn.get("headers") or {}).items():
            handler.send_header(key, value)
        handler.end_headers()

        chunk_delay = (turn.get("chunk_delay_ms") or 0) / 1000.0
        stall_after_ms = turn.get("stall_after_chunks_ms")
        truncate_after = turn.get("truncate_after_chunks")
        split_writes = bool(turn.get("split_writes"))
        chunks = turn.get("chunks") or []
        split_offsets = []
        outcome = "ok"
        sent = 0
        total_written = 0
        try:
            for i, delta in enumerate(chunks):
                if truncate_after is not None and i >= truncate_after:
                    break
                delta = _substitute(delta, substitutions, echoed)
                envelope = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": scenario_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                line = b"data: " + payload + b"\n\n"
                offset = _multibyte_split_point(payload)
                if split_writes and offset is not None:
                    split_point = len(b"data: ") + offset
                    split_offsets.append(total_written + split_point)
                    handler.wfile.write(line[:split_point])
                    handler.wfile.flush()
                    handler.wfile.write(line[split_point:])
                else:
                    handler.wfile.write(line)
                handler.wfile.flush()
                total_written += len(line)
                sent += 1
                if chunk_delay:
                    time.sleep(chunk_delay)
                if stall_after_ms is not None and i == 0:
                    time.sleep(stall_after_ms / 1000.0)
            else:
                if truncate_after is None:
                    finish_envelope = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": scenario_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": turn.get("finish_reason")}],
                    }
                    handler.wfile.write(b"data: " + json.dumps(finish_envelope, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    if turn.get("usage") is not None:
                        usage_envelope = dict(finish_envelope)
                        usage_envelope["usage"] = turn["usage"]
                        handler.wfile.write(b"data: " + json.dumps(usage_envelope, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    handler.wfile.write(b"data: [DONE]\n\n")
                    handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client_disconnected"
        with self._lock:
            if self._requests:
                self._requests[-1]["outcome"] = outcome
                self._requests[-1]["split_offsets"] = split_offsets

    # -- writers -----------------------------------------------------
    @staticmethod
    def _write_json(handler, status, body, extra_headers=None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            for key, value in (extra_headers or {}).items():
                handler.send_header(key, value)
            handler.end_headers()
            handler.wfile.write(payload)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _write_text(handler, status, text, extra_headers=None):
        payload = text.encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", str(len(payload)))
            for key, value in (extra_headers or {}).items():
                handler.send_header(key, value)
            handler.end_headers()
            handler.wfile.write(payload)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _substitute_text(value, substitutions, echoed):
    def repl(match):
        key = match.group(1)
        replacement = substitutions.get(key)
        return "" if replacement is None else str(replacement)

    text = _TEMPLATE_RE.sub(repl, value)
    if echoed is not None:
        text = _ECHO_RE.sub(lambda _m: echoed, text)
    return text


def _substitute(value, substitutions, echoed):
    if isinstance(value, str):
        return _substitute_text(value, substitutions, echoed)
    if isinstance(value, dict):
        return {k: _substitute(v, substitutions, echoed) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, substitutions, echoed) for v in value]
    return value


def _multibyte_split_point(payload: bytes):
    """Return a byte offset strictly inside a multi-byte UTF-8 sequence, if any."""
    for i, byte in enumerate(payload):
        if byte & 0xC0 == 0x80 and i > 0:
            return i
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fake_openai_server")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scenario", default="default_ok")
    parser.add_argument("--scenarios-file", default=None)
    args = parser.parse_args(argv)
    server = FakeProviderServer(
        scenarios_path=args.scenarios_file,
        default_scenario=args.scenario,
    )
    server.start()
    print(server.base_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
