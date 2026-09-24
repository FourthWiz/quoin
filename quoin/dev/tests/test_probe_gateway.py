from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import sys
import tempfile

import pytest

import _opencode_helpers as helpers

fake_server = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "quoin_opencode_fake_server")
probe = helpers.load_module(helpers.OPENCODE_DIR / "probe_gateway.py", "quoin_opencode_probe_gateway")


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


NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


def _nonce_factory():
    counter = {"n": 0}

    def _next():
        counter["n"] += 1
        return "nonce-%d" % counter["n"]

    return _next


def _run(server, model, tmp_path, extra_headers=None, active_error_checks=(), declared_context_limit=None, capsys=None, timeout=2.0):
    return _run_against_url(server.base_url, model, tmp_path, extra_headers=extra_headers,
                             active_error_checks=active_error_checks, declared_context_limit=declared_context_limit,
                             timeout=timeout)


def _run_against_url(base_url, model, tmp_path, extra_headers=None, active_error_checks=(), declared_context_limit=None, timeout=2.0, secret=None):
    out = str(tmp_path / "record.json")
    config = probe.ProbeConfig(
        base_url=base_url,
        model=model,
        credential_env="QUOIN_PROBE_TEST_KEY",
        provider="fake",
        output=out,
        timeout=timeout,
        active_error_checks=tuple(active_error_checks),
        declared_context_limit=declared_context_limit,
    )
    env = {"QUOIN_PROBE_TEST_KEY": secret if secret is not None else helpers.SEEDED_SECRET}
    code = probe.execute(config, env, extra_headers=extra_headers, now=NOW, nonce_factory=_nonce_factory())
    with open(out, "r", encoding="utf-8") as fh:
        record = json.loads(fh.read())
    return code, record


def test_default_ok_good_path_exit_0(tmp_path, server, capsys):
    code, record = _run(server, "default_ok", tmp_path)
    assert code == 0
    assert record["verdict"]["status"] == "qualified"
    for field in probe.CAPABILITY_FIELDS:
        assert field in record["capabilities"]
    assert record["capabilities"]["structured_output"]["status"] == "unknown"
    assert record["capabilities"]["reasoning_parameters"]["status"] == "unknown"
    assert record["capabilities"]["parallel_tool_calls"]["status"] == "unknown"
    assert record["capabilities"]["context_limit"]["status"] == "unknown"
    assert record["capabilities"]["usage_reporting"]["status"] == "supported"


def test_declared_limit_recorded_as_declared_not_observed(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, declared_context_limit=8000)
    assert record["capabilities"]["context_limit"] == {
        "status": "unknown",
        "source": "declared",
        "value": 8000,
        "detail": None,
    }


def test_usage_omitted_on_step1_gives_unknown_not_unsupported(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, extra_headers={"step1": {"X-Fake-Scenario": "usage_omitted"}})
    assert code == 0
    assert record["capabilities"]["usage_reporting"]["status"] == "unknown"


@pytest.mark.parametrize(
    "model,expected_capability",
    [
        ("auth_401", "text_generation"),
        ("rate_limited_429", "text_generation"),
        ("server_error_503", "text_generation"),
    ],
)
def test_inconclusive_failure_gives_unknown_not_unsupported(tmp_path, server, model, expected_capability):
    # A 401/429/5xx on step 1 means the step could not run, not that the
    # gateway lacks the capability — a maintainer must never read this as
    # evidence against the gateway (see field_status).
    code, record = _run(server, model, tmp_path)
    assert code == 2
    assert record["capabilities"][expected_capability]["status"] == "unknown"
    assert record["capabilities"][expected_capability]["status"] != "unsupported"


@pytest.mark.parametrize(
    "model,expected_code,expected_exit",
    [
        ("auth_401", "auth_invalid", 2),
        ("forbidden_403", "auth_forbidden", 2),
        ("server_error_500", "server_error", 2),
        ("server_error_503", "server_error", 2),
        ("context_overflow", "context_overflow", 1),
        ("not_found_404", "endpoint_or_model_not_found", 2),
        ("model_not_found_400", "endpoint_or_model_not_found", 2),
        ("tool_args_malformed", "malformed_tool_arguments", 1),
        ("tool_args_malformed_stream", "malformed_tool_arguments", 1),
        ("tool_call_missing_id", "tool_call_id_missing", 1),
        ("tool_call_id_mismatch", "tool_result_rejected", 1),
        ("invalid_json_200", "invalid_response", 1),
        ("prose_tool_call", "tool_call_missing", 1),
    ],
)
def test_per_class_negatives(tmp_path, server, model, expected_code, expected_exit, capsys):
    code, record = _run(server, model, tmp_path)
    assert code == expected_exit
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert expected_code in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err
    assert helpers.SEEDED_SECRET not in json.dumps(record)
    assert helpers.SEEDED_SECRET not in repr(server.snapshot_requests())


def test_rate_limited_retry_after_parsed(tmp_path, server):
    code, record = _run(server, "rate_limited_429", tmp_path)
    assert code == 2
    diag = record["diagnostics"][0]
    assert diag["code"] == "rate_limited"
    assert diag["retry_after_seconds"] == 7


def test_redirect_reports_credential_free_location_and_one_request(tmp_path, server):
    code, record = _run(server, "redirect_302", tmp_path)
    assert code == 2
    assert record["diagnostics"][0]["code"] == "redirected"
    assert len(server.snapshot_requests()) == 1


def test_tools_unsupported_404_steered_step2_marks_tool_calls_unsupported(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, extra_headers={"step2": {"X-Fake-Scenario": "tools_unsupported_404"}})
    assert code == 1
    assert record["capabilities"]["tool_calls"]["status"] == "unsupported"
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "tool_call_missing" in diag_codes


def test_tools_unsupported_404_steered_step3(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, extra_headers={"step3": {"X-Fake-Scenario": "tools_unsupported_404"}})
    assert code == 1
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "tool_call_missing" in diag_codes


def test_tool_result_404_steered_step2(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, extra_headers={"step2": {"X-Fake-Scenario": "tool_result_404"}})
    assert code == 1
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "tool_result_rejected" in diag_codes


def test_stream_truncated_gives_stream_interrupted(tmp_path, server, capsys):
    code, record = _run(server, "default_ok", tmp_path, extra_headers={"step3": {"X-Fake-Scenario": "stream_truncated"}})
    assert code == 1
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "stream_interrupted" in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err


def test_stream_stall_gives_stream_interrupted_not_timeout(tmp_path, server, capsys):
    code, record = _run(
        server, "default_ok", tmp_path,
        extra_headers={"step3": {"X-Fake-Scenario": "stream_stall"}},
        timeout=0.5,
    )
    assert code == 1
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "stream_interrupted" in diag_codes
    assert "timeout" not in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err


def test_connection_refused_gives_connection_failed(tmp_path, capsys):
    port = helpers.bind_closed_port()
    code, record = _run_against_url("http://127.0.0.1:%d/v1" % (port,), "any_model", tmp_path)
    assert code == 2
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "connection_failed" in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err


def test_tls_handshake_failure_gives_tls_failed(tmp_path, capsys):
    listener = helpers.start_tls_failure_listener()
    try:
        code, record = _run_against_url("https://127.0.0.1:%d/v1" % (listener.port,), "any_model", tmp_path, timeout=2.0)
    finally:
        listener.stop()
    assert code == 2
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "tls_failed" in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err


def test_accept_and_hang_gives_timeout(tmp_path, capsys):
    listener = helpers.start_hang_listener()
    try:
        code, record = _run_against_url("http://127.0.0.1:%d/v1" % (listener.port,), "any_model", tmp_path, timeout=0.5)
    finally:
        listener.stop()
    assert code == 2
    diag_codes = [d["code"] for d in record["diagnostics"]]
    assert "timeout" in diag_codes
    captured = capsys.readouterr()
    assert helpers.SEEDED_SECRET not in captured.out
    assert helpers.SEEDED_SECRET not in captured.err


def test_echo_secret_in_error_never_leaks(tmp_path, server, capsys):
    code, record = _run(server, "echo_secret_in_error", tmp_path)
    captured = capsys.readouterr()
    for form in helpers.secret_forms(helpers.SEEDED_SECRET):
        assert form not in captured.out
        assert form not in captured.err
        assert form not in json.dumps(record)
    assert helpers.SEEDED_SECRET not in repr(server.snapshot_requests())


def test_echo_secret_straddle_never_leaks(tmp_path, server, capsys):
    code, record = _run(server, "echo_secret_straddle", tmp_path)
    captured = capsys.readouterr()
    combined = captured.out + captured.err + json.dumps(record)
    assert helpers.SEEDED_SECRET not in combined
    # no substring of length >= 8 of the secret survives anywhere
    for length in range(8, len(helpers.SEEDED_SECRET) + 1):
        for start in range(0, len(helpers.SEEDED_SECRET) - length + 1):
            assert helpers.SEEDED_SECRET[start : start + length] not in combined


def test_echo_secret_escaped_against_untrusted_server_never_leaks(tmp_path, capsys):
    # The main `server` fixture rejects any wrong credential outright (a
    # generic 401 before the scenario's own turn ever runs), so the
    # escaped-secret form is never actually echoed back through it. A
    # second server with no expected_token accepts any credential and lets
    # echo_secret_in_error's echo_auth turn run for real.
    untrusted = fake_server.FakeProviderServer(expected_token=None)
    untrusted.start()
    try:
        secret = helpers.SEEDED_SECRET_ESCAPED
        code, record = _run_against_url(untrusted.base_url, "echo_secret_in_error", tmp_path, secret=secret)
    finally:
        untrusted.stop()
    captured = capsys.readouterr()
    combined = captured.out + captured.err + json.dumps(record) + repr(untrusted.snapshot_requests())
    for form in helpers.secret_forms(secret):
        assert form not in combined


def test_forced_exception_in_step_is_redacted_no_traceback(tmp_path, server, capsys, monkeypatch):
    secret = helpers.SEEDED_SECRET

    def _boom(*args, **kwargs):
        raise RuntimeError("boom while calling %s" % (secret,))

    monkeypatch.setattr(probe, "_post", _boom)
    code, record = _run(server, "default_ok", tmp_path)
    assert code == 2
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert secret not in json.dumps(record)


def test_registry_pairwise_distinct():
    messages = [v[0] for v in probe.DIAGNOSTICS.values()]
    next_actions = [v[1] for v in probe.DIAGNOSTICS.values()]
    assert len(messages) == len(set(messages))
    assert len(next_actions) == len(set(next_actions))
    for code, (msg, action, exit_class) in probe.DIAGNOSTICS.items():
        assert exit_class in (probe.EXIT_QUALIFIED, probe.EXIT_NOT_QUALIFIED, probe.EXIT_COULD_NOT_RUN)


def test_classification_precedence_401_on_tool_result_request():
    diag = probe.classify_exception(
        _http_error(401, b'{"error":{"code":"invalid_api_key"}}'),
        after_headers=False,
        is_tool_result_request=True,
        has_tools=True,
        step1_passed=True,
        now=NOW,
        secrets_tuple=(),
    )
    assert diag.code == "auth_invalid"


def test_classification_404_before_step1_vs_after():
    before = probe.classify_exception(
        _http_error(404, b'{"error":{"code":"not_found"}}'),
        after_headers=False, is_tool_result_request=False, has_tools=True,
        step1_passed=False, now=NOW, secrets_tuple=(),
    )
    assert before.code == "endpoint_or_model_not_found"

    after = probe.classify_exception(
        _http_error(404, b'{"error":{"code":"not_found"}}'),
        after_headers=False, is_tool_result_request=False, has_tools=True,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert after.code == "tool_call_missing"


def test_matches_model_not_found_table():
    class Err:
        def __init__(self, code=None, message=None):
            self.code = code
            self.error_type = None
            self.message = message

    assert probe.matches_model_not_found(Err(code="model_not_found"))
    assert probe.matches_model_not_found(Err(message="Unknown model"))
    assert probe.matches_model_not_found(Err(message="The model 'x/y:z' does not exist"))
    assert probe.matches_model_not_found(Err(message="no such model"))
    assert probe.matches_model_not_found(Err(message="model `openrouter/x-y:z` not found"))
    assert not probe.matches_model_not_found(Err(message="model X: tool_choice auto not supported"))
    assert not probe.matches_model_not_found(Err(message="model does not support tools"))
    assert not probe.matches_model_not_found(Err(message="tools not available for this model"))


def test_classification_non_http_reply_before_headers():
    import http.client

    for exc in (http.client.BadStatusLine("garbage"), http.client.LineTooLong("status line")):
        diag = probe.classify_exception(
            exc, after_headers=False, is_tool_result_request=False, has_tools=False,
            step1_passed=False, now=NOW, secrets_tuple=(),
        )
        assert diag.code == "connection_failed"
        assert diag.detail == "non_http_reply"


def test_classification_plain_value_error_after_headers():
    diag = probe.classify_exception(
        ValueError("malformed chunk"), after_headers=True, is_tool_result_request=False, has_tools=True,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert diag.code == "invalid_response"


def test_classification_socket_timeout_before_and_after_headers():
    import socket as _socket

    before = probe.classify_exception(
        _socket.timeout("timed out"), after_headers=False, is_tool_result_request=False, has_tools=False,
        step1_passed=False, now=NOW, secrets_tuple=(),
    )
    assert before.code == "timeout"

    after = probe.classify_exception(
        _socket.timeout("timed out"), after_headers=True, is_tool_result_request=False, has_tools=True,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert after.code == "stream_interrupted"


def test_parse_retry_after_variants():
    assert probe.parse_retry_after("7", NOW) == 7
    future = (NOW + datetime.timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert probe.parse_retry_after(future, NOW) == 30
    assert probe.parse_retry_after("garbage-not-a-date", NOW) is None
    assert probe.parse_retry_after(None, NOW) is None


def test_parse_retry_after_type_error_path(monkeypatch):
    def boom(_value):
        raise TypeError("py3.8 style")

    monkeypatch.setattr(probe, "parsedate_to_datetime", boom)
    assert probe.parse_retry_after("not-a-number", NOW) is None


@pytest.mark.parametrize("secret", [helpers.SEEDED_SECRET, helpers.SEEDED_SECRET_ESCAPED])
@pytest.mark.parametrize("pad", [280, 290, 295, 299])
def test_clip_redacts_before_cutting_for_every_secret_form(secret, pad):
    all_forms = tuple(helpers.secret_forms(secret))
    for form in all_forms:
        text = ("X" * pad) + form
        clipped = probe.clip(text, all_forms, 300)
        # No substring of length >= 8 of the raw secret may survive, whether
        # or not the whole secret straddles the cut (a partial prefix
        # leaking is exactly what redact-before-cut is meant to prevent).
        for length in range(8, len(secret) + 1):
            for start in range(0, len(secret) - length + 1):
                assert secret[start : start + length] not in clipped


def test_read_error_body_redacts_json_and_non_json(monkeypatch):
    class FakeResp:
        def __init__(self, data):
            self._data = data

        def read(self, n=-1):
            return self._data

    body = json.dumps({"error": {"code": "invalid_api_key", "message": "X" * 280 + helpers.SEEDED_SECRET}}).encode()
    info = probe.read_error_body(FakeResp(body), (helpers.SEEDED_SECRET,))
    assert helpers.SEEDED_SECRET not in info.message

    non_json = (b"X" * 280) + helpers.SEEDED_SECRET.encode()
    info2 = probe.read_error_body(FakeResp(non_json), (helpers.SEEDED_SECRET,))
    assert helpers.SEEDED_SECRET not in info2.message


def _assert_config_error_record_shape(record, provider, model):
    # The stage-2 template indexes record["key"] and record["capabilities"]
    # unconditionally, so a config-error record needs the same shape as a
    # normal one: a populated key, all nine capabilities present (as
    # "unknown", since nothing ran), and three explicitly skipped steps —
    # never a minimal record with only verdict/diagnostics.
    assert record["key"]["provider"] == provider
    assert record["key"]["model_id"] == model
    for field in probe.CAPABILITY_FIELDS:
        assert record["capabilities"][field]["status"] == "unknown"
    assert [s["name"] for s in record["steps"]] == ["auth_and_text", "tool_round_trip", "streaming"]
    assert all(s["result"] == "skipped" for s in record["steps"])


def test_config_missing_env_var(server, tmp_path, capsys):
    out = tmp_path / "r.json"
    code = probe.main(
        argv=[
            "--base-url", server.base_url, "--model", "text_ok", "--credential-env", "QUOIN_MISSING_VAR",
            "--provider", "fake", "--output", str(out), "--timeout", "2",
        ],
        env={},
    )
    assert code == 2
    captured = capsys.readouterr()
    # A config refusal must never disappear as a silent exit 2 — the user
    # needs to see why the run was refused and which variable is at fault.
    assert "config_error" in captured.err
    assert "QUOIN_MISSING_VAR" in captured.err
    record = json.loads(out.read_text())
    assert record["verdict"]["status"] == "could_not_run"
    assert record["diagnostics"][0]["code"] == "config_error"
    _assert_config_error_record_shape(record, "fake", "text_ok")


def test_config_bad_base_url_surfaces_diagnostic(tmp_path, capsys):
    out = tmp_path / "r.json"
    code = probe.main(
        argv=[
            "--base-url", "ftp://example.invalid/v1", "--model", "m", "--credential-env", "QUOIN_PROBE_TEST_KEY",
            "--provider", "fake", "--output", str(out), "--timeout", "2",
        ],
        env={"QUOIN_PROBE_TEST_KEY": helpers.SEEDED_SECRET},
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "config_error" in captured.err
    record = json.loads(out.read_text())
    assert record["diagnostics"][0]["code"] == "config_error"
    _assert_config_error_record_shape(record, "fake", "m")


def test_unwritable_output_on_qualified_run_reports_config_error(tmp_path, server, capsys):
    out = tmp_path / "missing-dir" / "r.json"
    config = probe.ProbeConfig(
        base_url=server.base_url, model="default_ok", credential_env="QUOIN_PROBE_TEST_KEY",
        provider="fake", output=str(out), timeout=2.0,
    )
    code = probe.execute(
        config, {"QUOIN_PROBE_TEST_KEY": helpers.SEEDED_SECRET}, now=NOW, nonce_factory=_nonce_factory(),
    )
    assert code == 2
    captured = capsys.readouterr()
    # A qualified run that cannot persist its record is the only artifact
    # stage 2 consumes, so it must not also print a success verdict.
    assert "qualified" not in captured.out
    assert "config_error" in captured.err
    assert "cannot write --output" in captured.err
    assert not out.exists()


def test_unwritable_output_on_config_error_also_reports_write_failure(tmp_path, capsys):
    out = tmp_path / "missing-dir" / "r.json"
    code = probe.main(
        argv=[
            "--base-url", "ftp://example.invalid/v1", "--model", "m", "--credential-env", "QUOIN_PROBE_TEST_KEY",
            "--provider", "fake", "--output", str(out), "--timeout", "2",
        ],
        env={"QUOIN_PROBE_TEST_KEY": helpers.SEEDED_SECRET},
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "cannot write --output" in captured.err
    assert not out.exists()


def test_config_userinfo_in_url_rejected(tmp_path):
    config = probe.ProbeConfig(
        base_url="https://user:pass@example.invalid/v1", model="m", credential_env="X", provider="p", output=str(tmp_path / "r.json"),
    )
    with pytest.raises(probe.ProbeConfigError):
        probe.validate_config(config)


def test_config_http_non_loopback_rejected():
    config = probe.ProbeConfig(base_url="http://example.invalid/v1", model="m", credential_env="X", provider="p")
    with pytest.raises(probe.ProbeConfigError):
        probe.validate_config(config)


def test_endpoint_identity_drops_query_and_fragment():
    assert probe.endpoint_identity("https://host:9443/v1/chat?api_key=abc#frag") == "https://host:9443/v1/chat"


def test_config_query_and_fragment_in_base_url_rejected():
    for url in ("https://example.invalid/v1?api_key=abc", "https://example.invalid/v1#frag"):
        config = probe.ProbeConfig(base_url=url, model="m", credential_env="X", provider="p")
        with pytest.raises(probe.ProbeConfigError):
            probe.validate_config(config)


def test_write_record_does_not_follow_symlink_and_leaves_no_tmp(tmp_path):
    canary = tmp_path / "canary.json"
    canary.write_text("do not touch")
    target = tmp_path / "record.json"
    target.symlink_to(canary)
    record = {
        "schema": probe.RECORD_SCHEMA, "schema_version": 1,
        "verdict": {"status": "qualified", "summary": "x", "blocking_step": None},
        "diagnostics": [],
    }
    assert probe.write_record(record, str(target), ()) is True
    # os.replace() on a symlink path replaces the symlink entry itself, so
    # the file it used to point at is untouched.
    assert canary.read_text() == "do not touch"
    assert not target.is_symlink()
    written = json.loads(target.read_text())
    assert written["verdict"]["status"] == "qualified"
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith(".probe-record-")]
    assert not leftover


def test_read_capped_raises_when_limit_exceeded():
    class _Resp:
        def __init__(self, total_len):
            self._remaining = total_len

        def read(self, n):
            if self._remaining <= 0:
                return b""
            chunk = b"x" * min(n, self._remaining)
            self._remaining -= len(chunk)
            return chunk

    assert probe._read_capped(_Resp(100), limit=1000) == b"x" * 100
    with pytest.raises(probe._ResponseTooLarge):
        probe._read_capped(_Resp(2000), limit=1000)


def test_classify_exception_response_too_large_is_invalid_response():
    diag = probe.classify_exception(
        probe._ResponseTooLarge("too big"), after_headers=True, is_tool_result_request=False, has_tools=False,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert diag.code == "invalid_response"


def test_classify_exception_closes_http_error(monkeypatch):
    closed = {"v": False}
    err = _http_error(401, b'{"error":{"code":"invalid_api_key"}}')
    real_close = err.close

    def _tracking_close():
        closed["v"] = True
        real_close()

    err.close = _tracking_close
    probe.classify_exception(
        err, after_headers=False, is_tool_result_request=False, has_tools=False,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert closed["v"] is True


def test_classification_generic_oserror_after_headers_is_stream_interrupted():
    diag = probe.classify_exception(
        ConnectionAbortedError("aborted mid-body"), after_headers=True, is_tool_result_request=False, has_tools=True,
        step1_passed=True, now=NOW, secrets_tuple=(),
    )
    assert diag.code == "stream_interrupted"


class _TrackingResp:
    def __init__(self, body):
        self._body = body
        self._read = False
        self.closed = False

    def read(self, n=-1):
        if self._read:
            return b""
        self._read = True
        return self._body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def test_run_step1_closes_response_on_success(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": None}).encode()
    resp = _TrackingResp(body)
    monkeypatch.setattr(probe, "_post", lambda *a, **k: resp)
    ctx = _make_report([]).context
    result = probe.run_step1(ctx)
    assert result.result == "pass"
    assert resp.closed is True


def test_run_step1_closes_response_on_parse_failure(monkeypatch):
    resp = _TrackingResp(b"not json")
    monkeypatch.setattr(probe, "_post", lambda *a, **k: resp)
    ctx = _make_report([]).context
    result = probe.run_step1(ctx)
    assert result.result == "fail"
    assert resp.closed is True


def test_cancellation_check_read_phase_exception_uses_after_headers_true(monkeypatch):
    class _StallResp:
        fp = None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.close()
            return False

    def _fake_post(*args, **kwargs):
        return _StallResp()

    def _fake_sse_lines(resp, timeout):
        raise socket.timeout("stalled before first line")
        yield  # pragma: no cover - unreachable; keeps this a generator function

    monkeypatch.setattr(probe, "_post", _fake_post)
    monkeypatch.setattr(probe, "_sse_lines", _fake_sse_lines)
    ctx = _make_report([]).context
    result = probe._run_cancellation_check(ctx, 3)
    assert result.result == "fail"
    # after_headers=True is what turns a bare socket.timeout into
    # stream_interrupted rather than timeout (classify_exception's rule);
    # this is the read-phase/connect-phase split the cancellation check
    # itself must respect.
    assert result.diagnostic.code == "stream_interrupted"


def test_sse_lines_enforces_total_deadline_not_per_read(monkeypatch):
    class _Sock:
        def settimeout(self, value):
            pass

    class _Raw:
        _sock = _Sock()

    class _Fp:
        raw = _Raw()

    class _FakeResp:
        fp = _Fp()

        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self, limit=None):
            if not self._lines:
                return b""
            return self._lines.pop(0)

    line = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":null}]}\n'
    resp = _FakeResp([line] * 5)
    # 1 call to establish the deadline, then one per loop iteration: budget
    # is 1.0s and each simulated iteration costs 0.3s, so the 4th iteration
    # (t=1.2) is past the deadline even though no single read ever waited
    # anywhere near the full 1.0s timeout on its own.
    times = iter([0.0, 0.3, 0.6, 0.9, 1.2])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(times))
    with pytest.raises(socket.timeout):
        for _ in probe._sse_lines(resp, 1.0):
            pass


def test_redact_stops_before_json_escaped_quote():
    # A token immediately followed by a JSON-escaped closing quote (`\"`)
    # must not have the backslash swallowed into the redaction — doing so
    # would leave the quote unescaped and corrupt the surrounding JSON.
    secret = helpers.SEEDED_SECRET
    payload = json.dumps({"d": 'x "Authorization: Bearer %s" z' % (secret,)})
    redacted = probe.redact(payload, (secret,))
    json.loads(redacted)  # must still parse
    assert secret not in redacted


def test_redact_stops_before_json_escaped_quote_for_token_param():
    # Same straddle risk as the Bearer case, but for the `token=`/`api_key=`
    # pattern: a value immediately followed by a JSON-escaped closing quote
    # must not have the backslash swallowed into the redaction.
    secret = helpers.SEEDED_SECRET
    payload = json.dumps({"d": 'x "token=%s" z' % (secret,)})
    redacted = probe.redact(payload, (secret,))
    json.loads(redacted)  # must still parse
    assert secret not in redacted


def test_write_record_survives_escaped_quote_after_bearer(tmp_path):
    secret = helpers.SEEDED_SECRET
    record = {
        "schema": probe.RECORD_SCHEMA,
        "schema_version": 1,
        "verdict": {"status": "could_not_run", "summary": 'x "Authorization: Bearer %s" z' % (secret,), "blocking_step": None},
        "diagnostics": [],
    }
    out = str(tmp_path / "record.json")
    assert probe.write_record(record, out, (secret,)) is True
    text = open(out, "r", encoding="utf-8").read()
    json.loads(text)  # must still be valid JSON, not the minimal fallback
    assert "diagnostics_note" not in text
    assert secret not in text


def test_write_record_returns_false_on_lone_surrogate_instead_of_crashing(tmp_path):
    # A lone surrogate from a gateway error body (e.g. an unpaired \udXXX
    # escape) raises UnicodeEncodeError from the UTF-8 file write, not
    # OSError. write_record must report this as a failed write rather than
    # let the exception escape and turn the whole run into an unhandled
    # crash with no record and no verdict line.
    record = {
        "schema": probe.RECORD_SCHEMA,
        "schema_version": 1,
        "verdict": {"status": "could_not_run", "summary": "\ud800", "blocking_step": None},
        "diagnostics": [],
    }
    out = str(tmp_path / "record.json")
    assert probe.write_record(record, out, ()) is False
    assert not os.path.exists(out)


def test_redact_covers_repr_and_str():
    secret = probe._Secret("hunter2")
    assert repr(secret) == "<redacted>"
    assert str(secret) == "<redacted>"
    assert "hunter2" not in repr(secret)


def test_proxy_env_ignored_by_default(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid:9")
    code, record = _run(server, "default_ok", tmp_path)
    assert code == 0


def test_use_env_proxy_true_routes_through_env_proxy(tmp_path, server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    config = probe.ProbeConfig(
        base_url=server.base_url, model="default_ok", credential_env="QUOIN_PROBE_TEST_KEY",
        provider="fake", output=str(tmp_path / "r.json"), timeout=2.0, use_env_proxy=True,
    )
    code = probe.execute(config, {"QUOIN_PROBE_TEST_KEY": helpers.SEEDED_SECRET}, now=NOW, nonce_factory=_nonce_factory())
    assert code == 2
    record = json.loads((tmp_path / "r.json").read_text())
    assert record["diagnostics"][0]["code"] == "connection_failed"


def _step1_checks(record):
    for step in record["steps"]:
        if step["name"] == "auth_and_text":
            return step["checks"]
    raise AssertionError("auth_and_text step missing from record")


def test_step4_invalid_token_live_check(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, active_error_checks=("invalid-token",))
    assert code == 0
    checks = {c["name"]: c for c in _step1_checks(record)}
    assert checks["invalid_token"]["ran_live"] is True
    assert checks["invalid_token"]["result"] == "pass"
    assert checks["context_overflow"]["ran_live"] is False


_CONTEXT_OVERFLOW_STEP4_HEADERS = {"step4_context_overflow": {"X-Fake-Scenario": "context_overflow"}}


def test_step4_context_overflow_live_check(tmp_path, server):
    code, record = _run(
        server, "default_ok", tmp_path,
        extra_headers=_CONTEXT_OVERFLOW_STEP4_HEADERS,
        active_error_checks=("context-overflow",),
        declared_context_limit=1000,
    )
    assert code == 0
    checks = {c["name"]: c for c in _step1_checks(record)}
    assert checks["context_overflow"]["ran_live"] is True
    assert checks["context_overflow"]["result"] == "pass"
    assert checks["invalid_token"]["ran_live"] is False


def test_step4_both_checks_together(tmp_path, server):
    code, record = _run(
        server, "default_ok", tmp_path,
        extra_headers=_CONTEXT_OVERFLOW_STEP4_HEADERS,
        active_error_checks=("invalid-token", "context-overflow"),
        declared_context_limit=1000,
    )
    assert code == 0
    checks = {c["name"]: c for c in _step1_checks(record)}
    assert checks["invalid_token"]["ran_live"] is True
    assert checks["context_overflow"]["ran_live"] is True


def test_step4_without_active_checks_is_classifier_only(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path)
    assert code == 0
    checks = {c["name"]: c for c in _step1_checks(record)}
    assert checks["invalid_token"]["ran_live"] is False
    assert checks["context_overflow"]["ran_live"] is False


def _make_report(steps, observations=None, step4=None, verdict="qualified", blocking_step=None):
    config = probe.ProbeConfig(base_url="https://example.invalid/v1", model="m", credential_env="X", provider="p")
    ctx = probe.ProbeContext(
        config=config, secret=probe._Secret("s"), secrets=("s",), opener=None,
        now=NOW, nonce_factory=lambda: "nonce", observations=observations or [],
    )
    ctx.step4 = step4 if step4 is not None else {"invalid_token": None, "context_overflow": None}
    return probe.ProbeReport(context=ctx, steps=steps, verdict=verdict, blocking_step=blocking_step)


def test_build_capability_record_folds_step4_checks_into_step1():
    steps = [
        {"step": 1, "name": "auth_and_text", "result": "pass", "diagnostic": None},
        {"step": 2, "name": "tool_round_trip", "result": "pass", "diagnostic": None},
        {"step": 3, "name": "streaming", "result": "pass", "diagnostic": None},
    ]
    report = _make_report(steps, step4={"invalid_token": {"ran_live": True, "result": "pass"}, "context_overflow": None})
    record = probe.build_capability_record(report)
    checks = {c["name"]: c for c in _step1_checks(record)}
    assert checks["invalid_token"] == {"name": "invalid_token", "ran_live": True, "result": "pass", "detail": None}
    assert checks["context_overflow"]["ran_live"] is False
    assert checks["context_overflow"]["result"] == "classifier_only"
    assert checks["context_overflow"]["detail"] == "not enabled via --active-error-checks"


def test_build_capability_record_folds_observations_into_matching_step():
    steps = [
        {"step": 1, "name": "auth_and_text", "result": "pass", "diagnostic": None},
        {"step": 2, "name": "tool_round_trip", "result": "warn", "diagnostic": None},
        {"step": 3, "name": "streaming", "result": "pass", "diagnostic": None},
    ]
    observations = [
        {"step": 2, "code": "argument_fidelity_mismatch", "result": "warn", "detail": '{"text": "wrong"}'},
        {"step": 3, "code": "nonce_miss", "result": "fail", "detail": "some answer"},
    ]
    report = _make_report(steps, observations=observations)
    record = probe.build_capability_record(report)
    step2_checks = next(s["checks"] for s in record["steps"] if s["name"] == "tool_round_trip")
    step3_checks = next(s["checks"] for s in record["steps"] if s["name"] == "streaming")
    assert step2_checks == [{"name": "argument_fidelity_mismatch", "result": "warn", "detail": '{"text": "wrong"}'}]
    assert step3_checks == [{"name": "nonce_miss", "result": "fail", "detail": "some answer"}]


def test_fidelity_coupling_with_fixtures():
    doc = fake_server.load_scenarios(fake_server.DEFAULT_SCENARIOS_PATH)
    turns = doc["scenarios"]["default_ok"]["turns"]
    stream_tool_turn = None
    for t in turns:
        if t.get("depth") == 0 and t.get("match", {}).get("tools") and t.get("match", {}).get("stream"):
            stream_tool_turn = t
            break
    assert stream_tool_turn is not None
    joined = "".join(
        c["tool_calls"][0].get("function", {}).get("arguments", "")
        for c in stream_tool_turn["chunks"]
        if c.get("tool_calls")
    )
    parsed = json.loads(joined)
    assert parsed == {"text": probe.FIDELITY_TEXT, "note": probe.FIDELITY_NOTE}


def test_readme_contract():
    readme = (helpers.OPENCODE_DIR / "README.md").read_text(encoding="utf-8")
    assert "probe_gateway.py" in readme
    assert "--credential-env" in readme
    assert "qualified" in readme and "not qualified" in readme and "could not run" in readme
    assert "fixtures/scenarios.json" in readme
    assert "--active-error-checks" in readme


def test_packaging_force_include_entry():
    root = helpers.OPENCODE_DIR.parent.parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"quoin/adapters/opencode" = "src/quoin/data/adapters/opencode"' in pyproject


def test_affected_tests_self_check():
    sys.path.insert(0, str(helpers.OPENCODE_DIR.parent.parent / "core" / "scripts"))
    import affected_tests  # type: ignore

    repo_root = helpers.OPENCODE_DIR.parent.parent.parent

    # Each shipped file, checked in isolation: every one of them must
    # select at least one opencode test on its own, with nothing unmatched
    # or ignored, so a change to just that file is never test-blind.
    per_file_expected = {
        "quoin/adapters/opencode/fixtures/scenarios.json": {
            "test_fake_openai_server.py",
            "test_probe_gateway.py",
            "test_probe_gateway_tool_loop.py",
        },
        "quoin/adapters/opencode/probe_gateway.py": {"test_probe_gateway.py"},
        "quoin/adapters/opencode/fake_openai_server.py": {"test_fake_openai_server.py"},
        "quoin/adapters/opencode/README.md": {"test_probe_gateway.py"},
        "pyproject.toml": {"test_probe_gateway.py"},
    }
    for path, expected in per_file_expected.items():
        selectors, unmatched, ignored = affected_tests.map_changed_to_tests([path], repo_root)
        assert not unmatched, (path, unmatched)
        assert not ignored, (path, ignored)
        selector_names = {os.path.basename(s) for s in selectors}
        assert expected <= selector_names, (path, expected, selector_names)


def test_subprocess_default_ok_exit_0(server, tmp_path):
    out = tmp_path / "record.json"
    env = dict(os.environ)
    env["QUOIN_PROBE_TEST_KEY"] = helpers.SEEDED_SECRET
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable, "-B", str(helpers.OPENCODE_DIR / "probe_gateway.py"),
            "--base-url", server.base_url, "--model", "default_ok",
            "--credential-env", "QUOIN_PROBE_TEST_KEY", "--provider", "fake",
            "--output", str(out), "--timeout", "5",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(out.read_text())["verdict"]["status"] == "qualified"


def test_subprocess_echo_secret_exit_2_and_redacted(server, tmp_path):
    out = tmp_path / "record.json"
    env = dict(os.environ)
    env["QUOIN_PROBE_TEST_KEY"] = helpers.SEEDED_SECRET
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable, "-B", str(helpers.OPENCODE_DIR / "probe_gateway.py"),
            "--base-url", server.base_url, "--model", "echo_secret_in_error",
            "--credential-env", "QUOIN_PROBE_TEST_KEY", "--provider", "fake",
            "--output", str(out), "--timeout", "5",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert helpers.SEEDED_SECRET not in result.stdout
    assert helpers.SEEDED_SECRET not in result.stderr
    assert helpers.SEEDED_SECRET not in out.read_text()


def test_catch_all_prints_exception_type_only_never_the_message(tmp_path, capsys, monkeypatch):
    secret = helpers.SEEDED_SECRET

    def _boom(config, env, extra_headers=None, now=None, nonce_factory=None):
        raise RuntimeError("unexpected failure leaking %s" % (secret,))

    monkeypatch.setattr(probe, "execute", _boom)
    code = probe.main(
        argv=[
            "--base-url", "https://example.invalid/v1", "--model", "m", "--credential-env", "QUOIN_PROBE_TEST_KEY",
            "--provider", "fake", "--output", str(tmp_path / "r.json"), "--timeout", "2",
        ],
        env={"QUOIN_PROBE_TEST_KEY": secret},
    )
    assert code == 2
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "RuntimeError" in captured.err
    assert "unexpected failure leaking" not in captured.err


def test_stage1_clean_content_over_shipped_files():
    forbidden = [r"IVG-\d+", r"\.workflow_artifacts", r"\b(AC|FR)-\d+\b", r"§\s?\d+"]
    import re

    for name in ("probe_gateway.py", "fake_openai_server.py", "README.md"):
        text = (helpers.OPENCODE_DIR / name).read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, text), (name, pattern)
    fixture_text = (helpers.OPENCODE_DIR / "fixtures" / "scenarios.json").read_text(encoding="utf-8")
    for pattern in forbidden:
        assert not re.search(pattern, fixture_text)


def _http_error(status, body):
    import io
    import urllib.error

    fp = io.BytesIO(body)
    return urllib.error.HTTPError("https://example.invalid/v1/chat/completions", status, "err", {}, fp)
