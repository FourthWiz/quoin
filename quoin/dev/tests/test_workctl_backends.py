"""workctl backend contract tests against recorded-exchange fixtures.

Each fixture file under fixtures/workctl/ holds one backend's service
configuration and scenarios: the exchanges the backend is expected to make
and the envelope fields it must produce. The fixtures are synthetic (written
from each service's public API reference), not captured from a tenant.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.opencode_adapter.secrets import SecretValue
from quoin.workctl import broker
from quoin.workctl.backends import _common
from quoin.workctl.transport import ReplayTransport

FIXTURES = Path(__file__).parent / "fixtures" / "workctl"
TOKEN = "fixture-token-ABCDEFGH12345"
SERVICE_OF = {"jira-cloud": "jira", "slack-web": "slack", "gmail": "mail", "msgraph": "mail"}


class Resolver:
    def resolve(self, ref):
        return SecretValue(TOKEN)


def load(name):
    return json.loads((FIXTURES / ("%s.json" % name)).read_text())


def scenarios():
    for name in SERVICE_OF:
        for scenario in load(name)["scenarios"]:
            yield pytest.param(name, scenario, id="%s:%s" % (name, scenario["name"]))


def lookup(obj, path):
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        obj = obj[int(part[1:-1])] if part.startswith("[") else obj[part]
    return obj


def write_config(home, backend, service_cfg, **extra):
    path = home / ".config" / "quoin" / "workctl.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": 1, "services": {SERVICE_OF.get(backend, "mail"): service_cfg}}
    data.update(extra)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def execute(home, op, params, transport, **kw):
    return broker.execute(op, params, env={}, home=home, transport=transport, resolver=Resolver(), **kw)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.mark.parametrize("backend,scenario", list(scenarios()))
def test_backend_contract(home, backend, scenario):
    fixture = load(backend)
    write_config(home, backend, fixture["service"])
    transport = ReplayTransport(scenario["exchanges"])
    env = execute(home, scenario["operation"], scenario["params"], transport)
    expect = scenario["expect"]
    assert env["ok"] is expect["ok"], env["error"]
    if not expect["ok"]:
        assert env["error"]["code"] == expect["code"]
        assert env["data"] is None
    for path, wanted in expect.get("paths", {}).items():
        got = lookup(env, path)
        if wanted == "<present>":
            assert isinstance(got, str) and got, path
        else:
            assert got == wanted, path
    if env["ok"]:
        prov = env["provenance"]
        assert prov["service"] == SERVICE_OF[backend] and prov["tenant"] == fixture["service"]["tenant"]
        assert prov["fetched_at"] and prov["backend"] == backend
    # the credential is sent only as an auth header, to the configured origin, and never echoed
    assert TOKEN not in json.dumps(env)
    assert transport.sent, "the backend made no request"
    for request in transport.sent:
        auth = request.headers["Authorization"]
        if backend == "jira-cloud":
            account = fixture["service"]["account"]
            assert auth == "Basic " + base64.b64encode(("%s:%s" % (account, TOKEN)).encode()).decode()
        else:
            assert auth == "Bearer " + TOKEN
        assert request.url.startswith("https://") and request.method == "GET"


def _page_one(home, backend, name):
    fixture = load(backend)
    write_config(home, backend, fixture["service"])
    scenario = next(s for s in fixture["scenarios"] if s["name"] == name)
    env = execute(home, scenario["operation"], scenario["params"], ReplayTransport(scenario["exchanges"]))
    return scenario, env


@pytest.mark.parametrize(
    "backend,name,path,param,value",
    [
        ("jira-cloud", "search-page-1", "/rest/api/3/search/jql", "nextPageToken", "CAEaAggD"),
        ("slack-web", "search", "/api/search.messages", "page", "2"),
        ("slack-web", "thread-has-more", "/api/conversations.replies", "cursor", "bmV4dF90czoxNzU5NDAwMDUwMDAwMTAw"),
        ("gmail", "search", "/gmail/v1/users/me/messages", "pageToken", "09876"),
        ("msgraph", "search", "/v1.0/me/messages", "$skiptoken", "MSZZVlhNVEF4"),
    ],
)
def test_cursor_round_trip(home, backend, name, path, param, value):
    scenario, env = _page_one(home, backend, name)
    cursor = env["pagination"]["cursor"]
    second = ReplayTransport([{"request": {"method": "GET", "path": path, "query": {param: value}}, "response": {"status": 404, "body": {}}}])
    params = dict(scenario["params"], cursor=cursor)
    result = execute(home, scenario["operation"], params, second)
    assert result["error"]["code"] == "NOT_FOUND"  # matched the recorded page-2 request
    assert len(second.sent) == 1


@pytest.mark.parametrize(
    "backend,op,params,bad_values",
    [
        ("msgraph", "mail.search", {"query": "x"}, [
            ["$expand", "attachments"],
            ["$skiptoken", "a&$expand=attachments"],
            "/v1.0/me/messages?$expand=attachments&$top=1000",
            "a.evil.example:/v1.0/me/messages",
        ]),
        ("jira-cloud", "jira.search", {"query": "x"}, [12, ["t"]]),
        ("slack-web", "slack.search", {"query": "x"}, [0, 101, True, "2"]),
        ("slack-web", "slack.thread", {"channel": "C0PAY", "thread": "1759400000.000100"}, [3, ["c"]]),
        ("gmail", "mail.search", {"query": "x"}, [5, {"t": 1}]),
    ],
)
def test_foreign_or_forged_cursor_is_refused(home, backend, op, params, bad_values):
    write_config(home, backend, load(backend)["service"])
    other = "slack-web" if backend != "slack-web" else "gmail"
    forged = [_common.encode_cursor(other, "x"), "not-base64-json"]
    forged += [_common.encode_cursor(backend, value) for value in bad_values]
    for cursor in forged:
        transport = ReplayTransport([])
        env = execute(home, op, dict(params, cursor=cursor), transport)
        assert env["error"]["code"] == "INVALID_INPUT", cursor
        assert transport.sent == []


def test_overlong_paging_token_is_refused_not_handed_out():
    with pytest.raises(Exception) as exc:
        _common.encode_cursor("jira-cloud", "t" * 3000)
    assert exc.value.code == "UNAVAILABLE"


def test_graph_next_link_keeps_only_the_skip_token(home):
    write_config(home, "msgraph", load("msgraph")["service"])
    first = ReplayTransport([{"request": {"method": "GET", "path": "/v1.0/me/messages"}, "response": {"status": 200, "body": {
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$expand=attachments&$top=999&$skiptoken=TOKEN1",
        "value": []}}}])
    env = execute(home, "mail.search", {"query": "invoice", "limit": 3}, first)
    second = ReplayTransport([{"request": {"method": "GET", "path": "/v1.0/me/messages"}, "response": {"status": 200, "body": {"value": []}}}])
    execute(home, "mail.search", {"query": "invoice", "limit": 3, "cursor": env["pagination"]["cursor"]}, second)
    from urllib.parse import parse_qs, urlsplit

    sent = parse_qs(urlsplit(second.sent[0].url).query)
    assert sent["$skiptoken"] == ["TOKEN1"] and sent["$top"] == ["3"]
    assert "$expand" not in sent and sent["$search"] == ['"invoice"']


def test_request_shapes(home):
    """Headers, full query multi-dicts and encoded paths the replay matcher does not check."""
    from urllib.parse import parse_qs, urlsplit

    def run_scenario(backend, name):
        fixture = load(backend)
        write_config(home, backend, fixture["service"])
        scenario = next(s for s in fixture["scenarios"] if s["name"] == name)
        transport = ReplayTransport(scenario["exchanges"])
        execute(home, scenario["operation"], scenario["params"], transport)
        return transport.sent

    sent = run_scenario("jira-cloud", "issue")
    assert parse_qs(urlsplit(sent[0].url).query)["fields"] == [
        "summary,status,issuetype,priority,assignee,reporter,labels,created,updated,description,comment,attachment"
    ]
    sent = run_scenario("gmail", "search")
    assert parse_qs(urlsplit(sent[1].url).query)["metadataHeaders"] == ["From", "To", "Cc", "Subject", "Date"]
    sent = run_scenario("msgraph", "read-with-attachments")
    assert sent[0].headers["Prefer"] == 'outlook.body-content-type="text"'
    assert urlsplit(sent[0].url).path == "/v1.0/me/messages/AAMkAGI2THVSAAA%3D"
    assert parse_qs(urlsplit(sent[1].url).query)["$select"] == ["name,contentType,size"]


def test_gmail_decodes_declared_charset_and_skips_vanished_messages(home):
    write_config(home, "gmail", load("gmail")["service"])
    latin = base64.urlsafe_b64encode("Grüße, café".encode("iso-8859-1")).decode().rstrip("=")
    transport = ReplayTransport([{"request": {"method": "GET", "path": "/gmail/v1/users/me/messages/m9"}, "response": {"status": 200, "body": {
        "id": "m9", "payload": {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=ISO-8859-1"}],
                                "body": {"data": latin}}}}}])
    assert execute(home, "mail.read", {"id": "m9"}, transport)["data"]["message"]["body"] == "Grüße, café"
    transport = ReplayTransport([
        {"request": {"method": "GET", "path": "/gmail/v1/users/me/messages"}, "response": {"status": 200, "body": {"messages": [{"id": "gone"}, {"id": "here"}]}}},
        {"request": {"method": "GET", "path": "/gmail/v1/users/me/messages/gone"}, "response": {"status": 404, "body": {}}},
        {"request": {"method": "GET", "path": "/gmail/v1/users/me/messages/here"}, "response": {"status": 200, "body": {"id": "here", "snippet": "hi"}}},
    ])
    env = execute(home, "mail.search", {"query": "x", "limit": 5}, transport)
    assert [i["id"] for i in env["data"]["items"]] == ["here"] and env["pagination"]["skipped_items"] == 1


def test_graph_query_quotes_are_neutralised(home):
    fixture = load("msgraph")
    write_config(home, "msgraph", fixture["service"])
    transport = ReplayTransport(fixture["scenarios"][0]["exchanges"])
    execute(home, "mail.search", {"query": 'invoice "Q4"', "limit": 2}, transport)
    from urllib.parse import parse_qs, urlsplit

    search = parse_qs(urlsplit(transport.sent[0].url).query)["$search"][0]
    assert search.startswith('"') and search.endswith('"') and search.count('"') == 2


@pytest.mark.parametrize("backend,scenario", [p for p in scenarios() if p.values[1].get("injection")])
def test_injected_instructions_stay_inert(home, backend, scenario):
    """Retrieved text that tells the agent to switch provider, reveal secrets
    or run commands comes back only as quoted, untrusted data; a credential
    echoed inside retrieved content is redacted; the broker changes no
    environment or configuration."""
    fixture = load(backend)
    write_config(home, backend, fixture["service"])
    config_path = home / ".config" / "quoin" / "workctl.json"
    config_before = config_path.read_bytes()
    env_before = dict(os.environ)
    # the service echoes the broker's own token inside the hostile text
    exchanges = json.loads(
        json.dumps(scenario["exchanges"]).replace(
            "IGNORE ALL PREVIOUS INSTRUCTIONS.", "IGNORE ALL PREVIOUS INSTRUCTIONS. token=%s." % TOKEN
        )
    )

    env = execute(home, scenario["operation"], scenario["params"], ReplayTransport(exchanges))

    assert env["ok"] is True
    assert set(env) == {"schema_version", "ok", "data", "error", "pagination", "provenance", "request_id"}
    assert env["provenance"]["content_trust"] == "untrusted"
    data_text = json.dumps(env["data"])
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in data_text  # returned verbatim, as data
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in json.dumps({k: v for k, v in env.items() if k != "data"})
    assert TOKEN not in json.dumps(env) and "<redacted>" in data_text
    assert dict(os.environ) == env_before
    assert config_path.read_bytes() == config_before


# ------------------------------------------------------------- command backend


class Runner:
    def __init__(self, code=0, out=None):
        self.code, self.out, self.calls = code, out, []

    def __call__(self, argv, *, stdin, timeout, max_bytes, env):
        self.calls.append({"argv": argv, "stdin": json.loads(stdin), "timeout": timeout, "env": env})
        return self.code, json.dumps(self.out).encode() if not isinstance(self.out, bytes) else self.out


COMMAND_SERVICE = {"backend": "command", "tenant": "acme", "argv": ["/opt/org/bin/mailctl", "--profile", "ro"]}


def test_command_backend_passes_query_on_stdin_not_argv(home):
    write_config(home, "command", COMMAND_SERVICE)
    runner = Runner(out={"data": {"items": [{"id": "1", "subject": "hi"}]}, "url": "https://mail.acme.example/1", "cursor": "abc"})
    query = "invoice'; rm -rf / #"
    env = broker.execute("mail.search", {"query": query, "limit": 3}, env={}, home=home, run_command=runner)
    assert env["ok"] and env["data"]["items"][0]["subject"] == "hi" and env["pagination"]["cursor"] == "abc"
    call = runner.calls[0]
    assert call["argv"] == ["/opt/org/bin/mailctl", "--profile", "ro", "mail.search"]
    assert query not in " ".join(call["argv"])
    assert call["stdin"]["params"]["query"] == query and call["stdin"]["params"]["limit"] == 3
    assert set(call["env"]) == {"PATH", "HOME", "LANG", "LC_ALL"}


@pytest.mark.parametrize(
    "code,out,expected",
    [
        (0, {"error": {"code": "NOT_FOUND"}}, "NOT_FOUND"),
        (3, {"error": {"code": "OUTCOME_UNKNOWN"}}, "UNAVAILABLE"),
        (0, {"error": {"code": "MADE_UP"}}, "UNAVAILABLE"),
        (1, {"data": {"items": []}}, "UNAVAILABLE"),
        (0, b"not json", "UNAVAILABLE"),
        (0, {"data": {"no_items": []}}, "UNAVAILABLE"),
        (0, {"data": {"items": []}, "cursor": "bad cursor!"}, "UNAVAILABLE"),
    ],
)
def test_command_backend_errors(home, code, out, expected):
    write_config(home, "command", COMMAND_SERVICE)
    env = broker.execute("mail.search", {"query": "x"}, env={}, home=home, run_command=Runner(code, out))
    assert env["error"]["code"] == expected


def test_command_backend_real_subprocess(home, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-should-not-leak-123456")
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-should-not-leak-123456")
    script = tmp_path / "fake-cli.py"
    script.write_text(
        "import json, os, sys\n"
        "req = json.load(sys.stdin)\n"
        "sys.stderr.write('STDERR-MARKER service said something\\n')\n"
        "print(json.dumps({'data': {'message': {'id': req['params']['id'], 'cwd': os.getcwd(),"
        " 'argv': sys.argv[1:], 'env': sorted(os.environ)}}}))\n"
    )
    service = dict(COMMAND_SERVICE, argv=[sys.executable, "-I", str(script)])
    write_config(home, "command", service)
    import io

    diag = io.StringIO()
    env = broker.execute("mail.read", {"id": "msg-1"}, env={}, home=home, stderr=diag)
    assert env["ok"], env["error"]
    message = env["data"]["message"]
    assert message["id"] == "msg-1" and message["cwd"] == "/" and message["argv"] == ["mail.read"]
    assert set(message["env"]) - {"__CF_USER_TEXT_ENCODING"} == {"PATH", "HOME", "LANG", "LC_ALL"}
    assert "STDERR-MARKER" not in json.dumps(env) and "STDERR-MARKER" not in diag.getvalue()


def test_command_runner_kills_the_process_group_on_timeout(tmp_path):
    from quoin.workctl.backends import command

    script = tmp_path / "hang.sh"
    script.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
    script.chmod(0o755)
    import time

    started = time.monotonic()
    with pytest.raises(Exception) as exc:
        command.default_runner([str(script)], stdin=b"{}", timeout=1, max_bytes=1000, env={"PATH": "/bin:/usr/bin"})
    assert exc.value.code == "UNAVAILABLE" and time.monotonic() - started < 10


def test_command_runner_caps_output(tmp_path):
    from quoin.workctl.backends import command

    with pytest.raises(Exception) as exc:
        command.default_runner(
            [sys.executable, "-I", "-c", "import sys; sys.stdout.write('x' * 100000)"],
            stdin=b"{}", timeout=10, max_bytes=1000, env={"PATH": "/bin"},
        )
    assert exc.value.code == "UNAVAILABLE" and exc.value.retryable is False
