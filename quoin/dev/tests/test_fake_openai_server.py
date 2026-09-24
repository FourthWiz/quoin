from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

import _opencode_helpers as helpers

fake_server = helpers.load_module(
    helpers.OPENCODE_DIR / "fake_openai_server.py", "quoin_opencode_fake_server"
)


@pytest.fixture(autouse=True)
def _loopback(monkeypatch):
    helpers.install_loopback_guard(monkeypatch)


@pytest.fixture(scope="module")
def server():
    srv = fake_server.FakeProviderServer(expected_token=helpers.SEEDED_SECRET)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def _clear(server):
    server.clear_requests()
    yield


def _post(server, body, extra_headers=None):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET}
    headers.update(extra_headers or {})
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


REQUIRED_SCENARIOS = [
    "text_ok",
    "tool_call_ok",
    "tool_call_stream_fragments",
    "tool_call_correlation",
    "tool_args_malformed",
    "tool_args_malformed_stream",
    "auth_401",
    "forbidden_403",
    "rate_limited_429",
    "server_error_500",
    "server_error_503",
    "stream_truncated",
    "context_overflow",
    "usage_omitted",
    "long_stream",
    "prose_tool_call",
    "tool_call_missing_id",
    "echo_secret_in_error",
    "redirect_302",
    "default_ok",
    "not_found_404",
    "model_not_found_400",
    "tools_unsupported_404",
    "tool_result_404",
    "stream_stall",
    "invalid_json_200",
    "tool_call_id_mismatch",
    "echo_secret_straddle",
]


def test_start_stop_fast_and_port_released():
    srv = fake_server.FakeProviderServer()
    start = time.time()
    srv.start()
    port = srv.port
    srv.stop()
    assert time.time() - start < 5
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    with pytest.raises(OSError):
        s.connect(("127.0.0.1", port))
    s.close()


def test_models_endpoint_lists_all_scenarios(server):
    with urllib.request.urlopen(server.base_url + "/models") as resp:
        data = json.loads(resp.read().decode("utf-8"))
    names = {m["id"] for m in data["data"]}
    for name in REQUIRED_SCENARIOS:
        assert name in names


@pytest.mark.parametrize("name", REQUIRED_SCENARIOS)
def test_required_scenario_present_in_fixture(name):
    assert name in fake_server.load_scenarios(fake_server.DEFAULT_SCENARIOS_PATH)["scenarios"]


def test_text_ok_status_and_shape(server):
    status, body = _post(server, {"model": "text_ok", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "Hello from fake provider."
    assert body["usage"]["prompt_tokens"] == 10


def test_default_ok_depth2_request_returns_no_matching_turn(server):
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
    ]
    status, body = _post(server, {"model": "default_ok", "messages": msgs})
    assert status == 400
    assert body["error"]["code"] == "no_matching_turn"


def test_header_beats_model_beats_default(server):
    status, body = _post(
        server,
        {"model": "text_ok", "messages": [{"role": "user", "content": "hi"}]},
        extra_headers={"X-Fake-Scenario": "auth_401"},
    )
    assert status == 401


def test_expected_token_mismatch_records_false(server):
    data = json.dumps({"model": "text_ok", "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        server.base_url + "/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 401
    record = server.snapshot_requests()[-1]
    assert record["auth_matches_expected"] is False
    assert helpers.SEEDED_SECRET not in repr(record)
    assert "wrong" not in repr(record)


def test_recorded_headers_never_contain_token(server):
    data = json.dumps({"model": "text_ok", "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        server.base_url + "/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET},
    )
    urllib.request.urlopen(req, timeout=5).read()
    snap = server.snapshot_requests()
    assert helpers.SEEDED_SECRET not in repr(snap)


def test_load_scenarios_rejects_bad_schema_version(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema_version": 2, "scenarios": {}}))
    with pytest.raises(ValueError):
        fake_server.load_scenarios(p)


def test_load_scenarios_rejects_conflicting_reply_keys(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": {"x": {"turns": [{"status": 200, "body": {}, "message": {}}]}},
            }
        )
    )
    with pytest.raises(ValueError):
        fake_server.load_scenarios(p)


def test_constructor_rejects_non_loopback_host():
    with pytest.raises(ValueError):
        fake_server.FakeProviderServer(host="0.0.0.0")


def test_stream_framing_reassembles_split_writes(server):
    tools = [{"type": "function", "function": {"name": "quoin_probe_echo", "parameters": {}}}]
    data = json.dumps(
        {"model": "tool_call_stream_fragments", "messages": [{"role": "user", "content": "go"}], "tools": tools, "stream": True}
    ).encode()
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    assert raw.rstrip().endswith(b"data: [DONE]")
    lines = [l for l in raw.split(b"\n\n") if l.startswith(b"data: ") and l != b"data: [DONE]"]
    args = ""
    for l in lines:
        env = json.loads(l[6:])
        if not env.get("choices"):
            continue  # the trailing usage chunk carries an empty choices array
        for tc in env["choices"][0]["delta"].get("tool_calls", []):
            args += tc.get("function", {}).get("arguments", "")
    parsed = json.loads(args)
    assert parsed["note"] == ""
    record = server.snapshot_requests()[-1]
    offsets = record.get("split_offsets") or []
    assert offsets
    for offset in offsets:
        assert raw[offset] & 0xC0 == 0x80


def test_streamed_tool_call_opening_delta_has_role_and_type(server):
    tools = [{"type": "function", "function": {"name": "quoin_probe_echo", "parameters": {}}}]
    data = json.dumps(
        {"model": "tool_call_stream_fragments", "messages": [{"role": "user", "content": "go"}], "tools": tools, "stream": True}
    ).encode()
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    lines = [l for l in raw.split(b"\n\n") if l.startswith(b"data: ") and l != b"data: [DONE]"]
    opening = json.loads(lines[0][6:])
    delta = opening["choices"][0]["delta"]
    assert delta.get("role") == "assistant"
    assert delta["tool_calls"][0].get("type") == "function"


def test_trailing_usage_chunk_has_empty_choices_no_finish_reason(server):
    data = json.dumps({"model": "long_stream", "messages": [{"role": "user", "content": "go"}], "stream": True}).encode()
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    lines = [l for l in raw.split(b"\n\n") if l.startswith(b"data: ") and l != b"data: [DONE]"]
    usage_envelopes = [json.loads(l[6:]) for l in lines if b'"usage"' in l]
    assert usage_envelopes
    usage_chunk = usage_envelopes[-1]
    assert usage_chunk["choices"] == []
    assert "usage" in usage_chunk


def test_large_request_body_recorded_as_size_not_full_copy(server):
    big_content = "x" * (fake_server.FakeProviderServer._MAX_RECORDED_BODY_BYTES + 1024)
    data = json.dumps({"model": "text_ok", "messages": [{"role": "user", "content": big_content}]}).encode()
    req = urllib.request.Request(
        server.base_url + "/chat/completions", data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET},
    )
    urllib.request.urlopen(req, timeout=5).read()
    record = server.snapshot_requests()[-1]
    assert record["body"].get("_truncated") is True
    assert record["body"]["_size_bytes"] == len(data)


def test_fake_server_honors_port_flag():
    port = helpers.bind_closed_port()
    srv = fake_server.FakeProviderServer(port=port)
    try:
        srv.start()
        assert srv.port == port
    finally:
        srv.stop()


def test_fallback_tiebreak_last_equal_depth_wins():
    # Two match-less turns tied at the same depth; a query depth deeper
    # than both (so neither is an exact match) must fall back to the LAST
    # declared turn at the shared max depth, not the first.
    doc = {
        "schema_version": 1,
        "scenarios": {
            "tie": {
                "turns": [
                    {"depth": 0, "message": {"content": "first"}, "status": 200},
                    {"depth": 0, "message": {"content": "second"}, "status": 200},
                ]
            }
        },
    }
    srv = fake_server.FakeProviderServer(scenarios=doc)
    idx, turn = srv._select_turn(doc["scenarios"]["tie"]["turns"], depth=1, has_tools=False, wants_stream=False)
    assert turn["message"]["content"] == "second"


def test_stream_truncated_has_no_finish_or_done(server):
    data = json.dumps({"model": "stream_truncated", "messages": [{"role": "user", "content": "go"}], "stream": True}).encode()
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    assert b"[DONE]" not in raw
    assert b'"finish_reason": "stop"' not in raw and b'"finish_reason":"stop"' not in raw


def test_expect_mismatch_returns_tool_call_id_mismatch(server):
    tools = [{"type": "function", "function": {"name": "quoin_probe_echo", "parameters": {}}}]
    status, body = _post(
        server,
        {
            "model": "tool_call_correlation",
            "messages": [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call_corr_1", "type": "function", "function": {"name": "quoin_probe_echo", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "wrong_id", "content": "{}"},
                {"role": "user", "content": "reply"},
            ],
            "tools": tools,
        },
    )
    assert status == 400
    assert body["error"]["code"] == "tool_call_id_mismatch"


def test_client_disconnect_records_client_disconnected(server):
    data = json.dumps({"model": "long_stream", "messages": [{"role": "user", "content": "go"}], "stream": True}).encode()
    req = urllib.request.Request(server.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + helpers.SEEDED_SECRET})
    resp = urllib.request.urlopen(req, timeout=5)
    resp.read(10)
    resp.close()
    deadline = time.time() + 5
    outcome = None
    while time.time() < deadline:
        snap = server.snapshot_requests()
        if snap and snap[-1]["outcome"] == "client_disconnected":
            outcome = "client_disconnected"
            break
        time.sleep(0.1)
    assert outcome == "client_disconnected"


def test_unknown_request_fields_ignored(server):
    status, body = _post(
        server,
        {"model": "text_ok", "messages": [{"role": "user", "content": "hi"}], "some_unknown_field": 123},
    )
    assert status == 200


def test_no_pycache_created():
    import subprocess

    result = subprocess.run(["find", str(helpers.OPENCODE_DIR), "-name", "__pycache__"], capture_output=True, text=True)
    assert result.stdout.strip() == ""
