from __future__ import annotations

import datetime
import json
import os
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


def _run(server, model, tmp_path, extra_headers=None, active_error_checks=(), declared_context_limit=None, capsys=None):
    out = str(tmp_path / "record.json")
    config = probe.ProbeConfig(
        base_url=server.base_url,
        model=model,
        credential_env="QUOIN_PROBE_TEST_KEY",
        provider="fake",
        output=out,
        timeout=2.0,
        active_error_checks=tuple(active_error_checks),
        declared_context_limit=declared_context_limit,
    )
    env = {"QUOIN_PROBE_TEST_KEY": helpers.SEEDED_SECRET}
    code = probe.execute(config, env, extra_headers=extra_headers, now=NOW, nonce_factory=_nonce_factory())
    record = json.loads(open(out, "r", encoding="utf-8").read())
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
    assert not probe.matches_model_not_found(Err(message="model X: tool_choice auto not supported"))
    assert not probe.matches_model_not_found(Err(message="model does not support tools"))
    assert not probe.matches_model_not_found(Err(message="tools not available for this model"))


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


def test_config_missing_env_var(server, tmp_path):
    code = probe.main(
        argv=[
            "--base-url", server.base_url, "--model", "text_ok", "--credential-env", "QUOIN_MISSING_VAR",
            "--provider", "fake", "--output", str(tmp_path / "r.json"), "--timeout", "2",
        ],
        env={},
    )
    assert code == 2


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


def test_step4_invalid_token_live_check(tmp_path, server):
    code, record = _run(server, "default_ok", tmp_path, active_error_checks=("invalid-token",))
    assert code == 0


def test_step4_context_overflow_live_check(tmp_path, server):
    code, record = _run(
        server, "default_ok", tmp_path,
        extra_headers=None,
        active_error_checks=("context-overflow",),
        declared_context_limit=1000,
    )
    assert code == 0


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
