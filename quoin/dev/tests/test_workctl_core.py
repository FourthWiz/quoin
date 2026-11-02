"""workctl broker core: envelope, policy, config, credentials, limits, cache, CLI."""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from quoin.opencode_adapter import paths as oa_paths
from quoin.opencode_adapter import profile_dirs
from quoin.opencode_adapter.secrets import CredentialResolver, SecretResolutionError, SecretValue
from quoin.workctl import broker, cli, envelope, limits, operations
from quoin.workctl.backends import Result
from quoin.workctl.transport import Response, status_error

SECRET = "xoxb-test-secret-value-123456"


class FakeBackend:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def run(self, op_name, params, ctx):
        self.calls.append((op_name, dict(params), ctx))
        if self.error:
            raise self.error
        return self.result or Result(
            data={"items": [{"id": "1", "text": "hello"}]}, url="https://x.example/1", cursor="next", trim_key="items"
        )


class FakeResolver:
    def __init__(self, value=SECRET, fail=None):
        self.value, self.fail, self.refs = value, fail, []

    def resolve(self, ref):
        self.refs.append(ref)
        if self.fail:
            raise SecretResolutionError(self.fail, ref)
        return SecretValue(self.value)


def write_config(home: Path, data) -> Path:
    path = home / ".config" / "quoin" / "workctl.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


def base_config(**services):
    return {
        "schema_version": 1,
        "services": services
        or {
            "slack": {
                "backend": "slack-web",
                "tenant": "T0001",
                "credential": "keychain:quoin-workctl-slack/reader",
            }
        },
    }


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def query(tmp_path):
    q = tmp_path / "q.txt"
    q.write_text("deploy failure\n")
    return q


def run(home, op, params, backend=None, resolver=None, env=None, **kw):
    return broker.execute(
        op,
        params,
        env=env or {},
        home=home,
        backends={"slack-web": backend or FakeBackend(), "jira-cloud": backend or FakeBackend()},
        resolver=resolver or FakeResolver(),
        **kw,
    )


def test_envelope_keys_and_success_shape(home, query):
    write_config(home, base_config())
    env = run(home, "slack.search", {"query_file": str(query), "limit": 5})
    assert tuple(env) == envelope.ENVELOPE_KEYS
    assert env["ok"] is True and env["error"] is None and env["schema_version"] == 1
    assert env["pagination"]["cursor"] == "next" and env["pagination"]["truncated"] is True
    assert env["pagination"]["truncation_reasons"] == ["more_results"]
    prov = env["provenance"]
    assert prov["service"] == "slack" and prov["tenant"] == "T0001" and prov["resource_id"] == "search"
    assert prov["url"] == "https://x.example/1" and prov["content_trust"] == "untrusted"
    assert prov["fetched_at"].endswith("Z")
    assert len(env["request_id"]) == 32


def test_missing_config_is_policy_denied_without_backend_call(home, query):
    backend = FakeBackend()
    env = run(home, "slack.search", {"query_file": str(query)}, backend=backend)
    assert env["ok"] is False and env["error"]["code"] == "POLICY_DENIED"
    assert "workctl.json" in env["error"]["message"] and env["error"]["retryable"] is False
    assert backend.calls == []


def test_unconfigured_service_and_disallowed_operation_denied(home, query):
    write_config(home, base_config())
    assert run(home, "jira.issue", {"key": "AB-1"})["error"]["code"] == "POLICY_DENIED"
    cfg = base_config()
    cfg["services"]["slack"]["operations"] = ["slack.thread"]
    write_config(home, cfg)
    assert run(home, "slack.search", {"query_file": str(query)})["error"]["code"] == "POLICY_DENIED"


def test_write_operations_denied_by_policy(home):
    write_config(home, base_config())
    from quoin.workctl import config as config_mod, policy

    cfg = config_mod.load(config_mod.default_path(home), {"slack": ("slack-web",)})
    with pytest.raises(envelope.BrokerError) as exc:
        policy.authorize(operations.Operation("slack.post", "slack", False, "record"), cfg, launcher_profile=None)
    assert exc.value.code == "POLICY_DENIED"


@pytest.mark.parametrize(
    "credential",
    ["env:SLACK_TOKEN", "keychain:openrouter/api-key", "plaintext-token-value"],
)
def test_credential_must_be_quoin_workctl_keychain(home, query, credential):
    cfg = base_config()
    cfg["services"]["slack"]["credential"] = credential
    write_config(home, cfg)
    env = run(home, "slack.search", {"query_file": str(query)})
    assert env["error"]["code"] == "POLICY_DENIED"
    assert "plaintext-token-value" not in json.dumps(env)


def test_default_resolver_has_no_environment_backend(home, query):
    write_config(home, base_config())
    resolver = broker.default_resolver("linux")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("env:SLACK_TOKEN")


def test_credential_failure_is_auth_required(home, query):
    write_config(home, base_config())
    env = run(home, "slack.search", {"query_file": str(query)}, resolver=FakeResolver(fail="keychain-not-found"))
    assert env["error"]["code"] == "AUTH_REQUIRED" and "keychain-not-found" in env["error"]["message"]


def test_secret_is_redacted_from_data_and_errors(home, query):
    write_config(home, base_config())
    leaky = FakeBackend(result=Result(data={"items": [{"text": "token is %s" % SECRET}]}, trim_key="items"))
    env = run(home, "slack.search", {"query_file": str(query)}, backend=leaky)
    assert SECRET not in json.dumps(env)
    failing = FakeBackend(error=envelope.BrokerError("UNAVAILABLE", "bad %s" % SECRET))
    env = run(home, "slack.search", {"query_file": str(query)}, backend=failing)
    assert SECRET not in json.dumps(env)


def test_unexpected_exception_still_yields_envelope(home, query):
    write_config(home, base_config())
    env = run(home, "slack.search", {"query_file": str(query)}, backend=FakeBackend(error=KeyError("body text")))
    assert env["ok"] is False and env["error"]["message"] == "internal broker error"
    assert "body text" not in json.dumps(env)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda p: os.chmod(p, 0o666), "writable"),
        (lambda p: p.write_text("{not json"), "JSON"),
        (lambda p: p.write_text(json.dumps({"schema_version": 1, "services": {"slack": {"backend": "nope", "tenant": "T"}}})), "backend"),
        (lambda p: p.write_text(json.dumps({"schema_version": 1, "surprise": 1})), "unknown key"),
        (lambda p: p.write_text(json.dumps({"schema_version": 1, "limits": {"max_bytes": 10**9}})), "max_bytes"),
        (lambda p: p.write_text(json.dumps({"schema_version": 1, "services": {"jira": {"backend": "jira-cloud", "tenant": "a", "base_url": "http://plain.example"}}})), "https"),
    ],
)
def test_bad_config_is_denied(home, query, mutate, fragment):
    path = write_config(home, base_config())
    mutate(path)
    env = run(home, "slack.search", {"query_file": str(query)})
    assert env["error"]["code"] == "POLICY_DENIED" and fragment in env["error"]["message"]


def test_symlinked_config_is_refused(home, tmp_path, query):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(base_config()))
    os.chmod(real, 0o600)
    link = home / ".config" / "quoin" / "workctl.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert run(home, "slack.search", {"query_file": str(query)})["error"]["code"] == "POLICY_DENIED"


@pytest.mark.parametrize(
    "op,params",
    [
        ("jira.issue", {"key": "ab-1"}),
        ("jira.issue", {"key": "AB-1; rm -rf /"}),
        ("slack.thread", {"channel": "C123", "thread": "not-a-ts"}),
        ("mail.read", {"id": "../etc/passwd"}),
        ("slack.search", {"query": "x", "query_file": "/tmp/q"}),
        ("slack.search", {"query": "x", "limit": 0}),
        ("slack.search", {"query": "x", "cursor": "bad cursor!"}),
        ("slack.search", {"query": "x", "extra": "y"}),
    ],
)
def test_invalid_parameters(op, params):
    with pytest.raises(envelope.BrokerError) as exc:
        operations.normalize(operations.get(op), params, max_items=50)
    assert exc.value.code == "INVALID_INPUT"


def test_limit_is_capped_by_config(query):
    params = operations.normalize(operations.get("slack.search"), {"query_file": str(query), "limit": 1000}, max_items=30)
    assert params == {"query": "deploy failure", "limit": 30, "cursor": None}


def test_query_file_validation(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (operations.MAX_QUERY_FILE_BYTES + 1))
    link = tmp_path / "link.txt"
    link.symlink_to(big)
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"\xff\xfe")
    for path in (big, link, binary, tmp_path, tmp_path / "missing"):
        with pytest.raises(envelope.BrokerError) as exc:
            operations.read_query_file(str(path))
        assert exc.value.code == "INVALID_INPUT"


def test_bound_trims_text_then_items():
    data = {"items": [{"text": "x" * 5000} for _ in range(50)]}
    out, meta = limits.bound(data, trim_key="items", max_text_chars=1000, max_bytes=20_000, overhead=1000)
    assert limits.size(out) <= 19_000
    assert meta["text_truncated"] and meta["omitted_items"] > 0
    assert all(len(i["text"]) <= 1000 for i in out["items"])
    assert data["items"][0]["text"] == "x" * 5000  # input untouched


def test_byte_bound_is_reported_in_pagination(home, query):
    cfg = base_config()
    cfg["limits"] = {"max_bytes": 8192, "max_text_chars": 1000}
    write_config(home, cfg)
    big = FakeBackend(result=Result(data={"items": [{"text": "y" * 900} for _ in range(40)]}, trim_key="items"))
    env = run(home, "slack.search", {"query_file": str(query)}, backend=big)
    page = env["pagination"]
    assert env["ok"] and page["truncated"] and "bytes" in page["truncation_reasons"]
    assert page["returned"] + page["omitted_items"] == 40
    assert len(json.dumps(env).encode()) <= 8192


@pytest.mark.parametrize(
    "status,code,retryable",
    [(401, "AUTH_REQUIRED", False), (403, "FORBIDDEN", False), (404, "NOT_FOUND", False), (429, "RATE_LIMITED", True), (400, "INVALID_INPUT", False), (503, "UNAVAILABLE", True), (302, "UNAVAILABLE", False)],
)
def test_status_mapping(status, code, retryable):
    err = status_error(Response(status, {"retry-after": "7"}, b"secret body"))
    assert err.code == code and err.retryable is retryable and "secret body" not in err.message
    if status == 429:
        assert err.retry_after == 7.0


def _stamped_root(home: Path, profile: str) -> Path:
    adapter = oa_paths.state_dir({}, home)
    dirs = profile_dirs.prepare(adapter, profile, "work")
    return dirs.root


def test_cache_is_profile_bound_and_short_lived(home, query):
    root = _stamped_root(home, "work")
    cfg = base_config()
    cfg["profile"] = "work"
    write_config(home, cfg)
    backend = FakeBackend()
    env_vars = {oa_paths.ENV_STATE_DIR: str(root)}
    now = [1_000_000.0]
    first = run(home, "slack.search", {"query_file": str(query)}, backend=backend, env=env_vars, clock=lambda: now[0])
    second = run(home, "slack.search", {"query_file": str(query)}, backend=backend, env=env_vars, clock=lambda: now[0])
    assert len(backend.calls) == 1 and second["provenance"]["cached"] is True
    assert second["request_id"] != first["request_id"]
    cache_dir = root / "cache" / "workctl"
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    for entry in cache_dir.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
        assert "deploy" not in entry.name
    now[0] += 301
    run(home, "slack.search", {"query_file": str(query)}, backend=backend, env=env_vars, clock=lambda: now[0])
    assert len(backend.calls) == 2


def test_no_cache_without_stamped_profile(home, query):
    write_config(home, base_config())
    backend = FakeBackend()
    run(home, "slack.search", {"query_file": str(query)}, backend=backend)
    run(home, "slack.search", {"query_file": str(query)}, backend=backend)
    assert len(backend.calls) == 2


def test_config_bound_to_other_profile_is_denied(home, query):
    root = _stamped_root(home, "personal")
    cfg = base_config()
    cfg["profile"] = "work"
    write_config(home, cfg)
    env = run(home, "slack.search", {"query_file": str(query)}, env={oa_paths.ENV_STATE_DIR: str(root)})
    assert env["error"]["code"] == "POLICY_DENIED"


def test_diagnostics_go_to_stderr_without_content(home, query):
    write_config(home, base_config())
    err = io.StringIO()
    run(home, "slack.search", {"query_file": str(query)}, stderr=err)
    line = err.getvalue()
    assert line.startswith("workctl: request_id=") and "outcome=ok" in line
    assert "hello" not in line and "deploy" not in line


def test_cli_emits_one_envelope_and_exit_status(home, query, capsys):
    write_config(home, base_config())
    kwargs = dict(home=home, env={}, backends={"slack-web": FakeBackend()}, resolver=FakeResolver())
    assert cli.main(["slack", "search", "--query-file", str(query), "--limit", "3", "--json"], **kwargs) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["ok"] is True and out.err == ""
    backend = kwargs["backends"]["slack-web"]
    calls = len(backend.calls)
    assert cli.main(["jira", "issue", "bad key", "--json"], **kwargs) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "POLICY_DENIED"  # jira not configured
    assert cli.main(["slack", "thread", "C123", "bad-ts", "--json"], **kwargs) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_INPUT"
    assert len(backend.calls) == calls


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["jira"],
        ["slack", "search"],
        ["mail", "read"],
        ["slack", "search", "--query-file", "q", "--limit", "x"],
        ["jira", "search", "--query", "/etc/hosts"],  # no prefix matching to --query-file
    ],
)
def test_cli_usage_errors_are_json(argv, capsys):
    assert cli.main(argv, home=Path("/nonexistent"), env={}) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["error"]["code"] == "INVALID_INPUT"
    assert out.err.startswith("workctl: ")


@pytest.mark.parametrize("argv", [["--help"], ["jira", "issue", "-h"]])
def test_cli_help_is_an_envelope(argv, capsys):
    assert cli.main(argv, home=Path("/nonexistent"), env={}) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and "usage: workctl" in body["data"]["usage"]


def test_cli_scrubs_proxy_and_ca_variables():
    environ = {"SSL_CERT_FILE": "/tmp/evil.pem", "HTTPS_PROXY": "http://127.0.0.1:1", "PATH": "/bin"}
    cli.scrub_environment(environ)
    assert environ == {"PATH": "/bin"}


def test_profile_bound_config_refused_outside_a_session(home, query):
    cfg = base_config()
    cfg["profile"] = "work"
    write_config(home, cfg)
    assert run(home, "slack.search", {"query_file": str(query)})["error"]["code"] == "POLICY_DENIED"


def test_context_send_pins_the_service_origin():
    from quoin.workctl.backends import Context, origin_of
    from quoin.workctl.config import Limits, ServiceConfig
    from quoin.workctl.transport import Request

    class Recorder:
        sent = []

        def send(self, request, *, timeout, max_bytes):
            self.sent.append((request.url, timeout, max_bytes))
            return Response(200, {}, b"{}")

    ctx = Context(
        service=ServiceConfig("jira", "jira-cloud", "acme"),
        limits=Limits(),
        transport=Recorder(),
        credential=None,
        fetched_at="",
        origin=origin_of("https://acme.atlassian.net"),
    )
    ctx.send(Request("GET", "https://acme.atlassian.net/rest/api/3/issue/A-1"))
    assert Recorder.sent == [("https://acme.atlassian.net/rest/api/3/issue/A-1", 20, Limits().max_response_bytes)]
    for url in ("https://evil.example/x", "http://acme.atlassian.net/x", "https://acme.atlassian.net.evil.example/x"):
        with pytest.raises(envelope.BrokerError) as exc:
            ctx.send(Request("GET", url))
        assert exc.value.code == "POLICY_DENIED"


def test_console_script_declared():
    text = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text()
    assert 'workctl = "quoin.workctl.cli:main"' in text
