"""workctl approved writes: proposals, approval, the journaled writer."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import paths as oa_paths
from quoin.opencode_adapter.secrets import SecretValue
from quoin.workctl import cli, config as config_mod, proposal, writer, writer_cli
from quoin.workctl.backends import Context, Receipt, gmail, jira_cloud, slack_web
from quoin.workctl.config import Limits, ServiceConfig
from quoin.workctl.envelope import BrokerError
from quoin.workctl.transport import ReplayTransport

APPROVAL_KEY = "k" * 40
WRITE_TOKEN = "xoxb-writer-secret-0000000000"
NOW = 1_800_000_000.0


class Killed(BaseException):
    """Stands in for the process dying mid-send."""


class FakeWriter:
    name = "fake"
    default_base = "https://slack.com/api"

    def __init__(self, *, error=None, found=None):
        self.error, self.found = error, found
        self.writes, self.lookups = [], []

    def run(self, op_name, params, ctx):  # pragma: no cover - reads are not used here
        raise AssertionError("no reads")

    def write(self, op_name, params, ctx, *, marker):
        self.writes.append((op_name, dict(params), marker, ctx.credential.reveal()))
        if self.error is not None:
            raise self.error
        return Receipt(remote_id="1800000000.000100", url="https://acme.slack.com/archives/C1/p1")

    def lookup(self, op_name, params, ctx, *, marker, since):
        self.lookups.append((marker, since))
        return self.found


class Resolver:
    def __init__(self):
        self.refs = []

    def resolve(self, ref):
        self.refs.append(ref.service)
        return SecretValue(APPROVAL_KEY if ref.service.startswith("quoin-workctl-approver") else WRITE_TOKEN)


def config(writes=True, **extra):
    data = {
        "schema_version": 1,
        "services": {
            "slack": {"backend": "slack-web", "tenant": "T0001", "credential": "keychain:quoin-workctl-slack/reader"}
        },
    }
    if writes:
        data["writes"] = {
            "approval_boundary": "separate-user",
            "approval_key": "keychain:quoin-workctl-approver/acme",
            "operations": ["slack.post"],
            "credentials": {"slack": "keychain:quoin-workctl-writer/T0001"},
        }
    data.update(extra)
    return data


def write_config(home: Path, data) -> None:
    path = config_mod.default_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    write_config(h, config())
    return h


@pytest.fixture
def body(tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("Deploy is green again.\n\nThanks all.\n")
    return f


class Terminal:
    """Stands in for /dev/tty: records what was shown and answers the
    prompt, by default with the one-time code it was shown."""

    def __init__(self, answer=None):
        self.shown, self.answer = "", answer

    def write(self, text):
        self.shown += text

    def flush(self):
        pass

    def readline(self):
        if self.answer is not None:
            return self.answer + "\n"
        return re.findall(r"type ([0-9a-f]{6}):", self.shown)[-1] + "\n"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def make_proposal(home, body, tmp_path, clock=None, name="p.json"):
    out = tmp_path / name
    env = proposal.propose(
        "slack.post", {"channel": "C0123", "body_file": str(body)}, out=str(out), env={}, home=home,
        clock=clock or Clock(),
    )
    assert env["ok"], env
    return out, env


def make_approval(home, prop_path, clock=None, out=None):
    env = writer.approve(
        str(prop_path), out=out, env={}, home=home, resolver=Resolver(), clock=clock or Clock(), terminal=Terminal()
    )
    assert env["ok"], env
    return Path(env["data"]["approval_file"])


def execute(home, prop_path, approval_path, backend, clock=None):
    return writer.execute(
        str(prop_path), str(approval_path), env={}, home=home, resolver=Resolver(),
        backends={"slack-web": backend}, clock=clock or Clock(),
    )


def journal_of(home):
    return writer.Journal(home / ".local/state/quoin/workctl/journal", APPROVAL_KEY.encode())


def journal(home, prop_path):
    return journal_of(home).get(json.loads(prop_path.read_text())["idempotency_key"])


# --- proposals ---------------------------------------------------------------


def test_propose_writes_an_immutable_local_draft_without_credentials(home, body, tmp_path):
    out, env = make_proposal(home, body, tmp_path)
    data = json.loads(out.read_text())
    assert data["params"] == {"channel": "C0123", "thread": None, "body": "Deploy is green again.\n\nThanks all."}
    assert (data["operation"], data["tenant"], data["backend"]) == ("slack.post", "T0001", "slack-web")
    assert data["content_hash"] == proposal.content_hash(data) == env["data"]["content_hash"]
    assert env["data"]["writes_enabled"] is True and "workctl-write approve" in env["data"]["next_step"]
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    again = proposal.propose(
        "slack.post", {"channel": "C0123", "body_file": str(body)}, out=str(out), env={}, home=home
    )
    assert again["error"]["code"] == "INVALID_INPUT"  # never overwritten


def test_propose_without_writes_section_says_submit_by_hand(home, body, tmp_path):
    write_config(home, config(writes=False))
    _, env = make_proposal(home, body, tmp_path)
    assert env["data"]["writes_enabled"] is False and "manual submission" in env["data"]["next_step"]


@pytest.mark.parametrize(
    "op,params",
    [
        ("mail.send", {"to": ["a@b.example"], "subject": "hi\r\nBcc: x@evil.example", "body": "x"}),
        ("mail.send", {"to": ["not-an-address"], "subject": "hi", "body": "x"}),
        ("slack.post", {"channel": "C0123", "body": "x", "unfurl": True}),
        ("jira.comment", {"key": "bad key", "body": "x"}),
    ],
)
def test_write_parameters_are_validated(op, params):
    from quoin.workctl import operations

    with pytest.raises(BrokerError) as exc:
        operations.normalize_write(operations.OPERATIONS[op], params)
    assert exc.value.code == "INVALID_INPUT"


def test_cli_propose_and_write_operations_absent_from_the_read_path(home, body, tmp_path, capsys):
    out = tmp_path / "cli.json"
    code = cli.main(
        ["propose", "slack", "post", "C0123", "--thread", "1800000000.000100", "--body-file", str(body),
         "--out", str(out), "--json"],
        home=home, env={},
    )
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    assert json.loads(out.read_text())["params"]["thread"] == "1800000000.000100"
    from quoin.workctl import broker

    denied = broker.execute("slack.post", {"channel": "C0123", "body": "x"}, env={}, home=home, resolver=Resolver())
    assert denied["error"]["code"] == "POLICY_DENIED" and "workctl propose" in denied["error"]["message"]


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["writes"].pop("approval_key"),
        lambda c: c["writes"].update(approval_key="keychain:quoin-workctl-slack/x"),
        lambda c: c["writes"].update(approval_boundary="a-model-flag"),
        lambda c: c["writes"].update(operations=["slack.search"]),
        lambda c: c["writes"]["credentials"].update(slack="keychain:quoin-workctl-slack/reader"),
        lambda c: c["services"]["slack"].update(credential="keychain:quoin-workctl-writer/T0001"),
        lambda c: c["writes"].update(unknown=1),
        lambda c: c["writes"].pop("approval_boundary"),
        lambda c: c["writes"]["credentials"].pop("slack"),
        lambda c: c["services"]["slack"].update(backend="command", argv=["/bin/true"]),
        lambda c: c["services"]["slack"].update(operations=["slack.search", "slack.post"]),
    ],
)
def test_writes_configuration_is_strict(home, mutate):
    data = config()
    mutate(data)
    write_config(home, data)
    with pytest.raises(BrokerError) as exc:
        config_mod.load(config_mod.default_path(home), {"slack": ("slack-web",)})
    assert exc.value.code == "POLICY_DENIED"


# --- approval ----------------------------------------------------------------


def test_approve_needs_the_one_time_code_from_the_terminal(home, body, tmp_path):
    out, prop_env = make_proposal(home, body, tmp_path)
    common = dict(env={}, home=home, resolver=Resolver(), clock=Clock())
    for answer in ("y", prop_env["data"]["content_hash"][:6]):  # the hash is known to the agent
        assert writer.approve(str(out), terminal=Terminal(answer), **common)["error"]["code"] == "POLICY_DENIED"
    agent = dict(common, env={oa_paths.ENV_STATE_DIR: "/tmp/x"})
    assert writer.approve(str(out), terminal=Terminal(), **agent)["error"]["code"] == "POLICY_DENIED"
    assert not Path(str(out) + ".approval.json").exists()


def test_approval_display_cannot_hide_or_fake_content(home, tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("visible\x1b[2Khidden\u202eevil\u200b\nend of body; this approves:")
    out, _ = make_proposal(home, f, tmp_path)
    term = Terminal("no")
    writer.approve(str(out), env={}, home=home, resolver=Resolver(), clock=Clock(), terminal=term)
    shown = term.shown
    assert "\x1b" not in shown and all(e in shown for e in ("\\u001b", "\\u202e", "\\u200b"))
    assert "| end of body; this approves:" in shown
    assert shown.count("channel:     C0123") == 2  # destination repeated after the body


# --- writer: denial, tamper, replay ------------------------------------------


def test_writer_denied_when_writes_disabled_or_inside_an_agent_session(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter()
    inside = writer.execute(str(out), str(approval), env={oa_paths.ENV_STATE_DIR: "/x"}, home=home,
                            resolver=Resolver(), backends={"slack-web": backend}, clock=Clock())
    assert inside["error"]["code"] == "POLICY_DENIED"
    write_config(home, config(writes=False))
    disabled = execute(home, out, approval, backend)
    assert disabled["error"]["code"] == "POLICY_DENIED" and "manual submission" in disabled["error"]["message"]
    assert backend.writes == []


def test_execute_sends_once_with_the_write_credential_and_records_the_receipt(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    backend = FakeWriter()
    env = execute(home, out, make_approval(home, out), backend)
    assert env["ok"] and env["data"]["state"] == "DONE" and env["data"]["receipt"]["remote_id"] == "1800000000.000100"
    assert "proposal" not in env["data"] and "nonces" not in env["data"]
    (op, params, marker, credential), = backend.writes
    assert marker == json.loads(out.read_text())["idempotency_key"] and credential == WRITE_TOKEN
    assert journal(home, out)["state"] == "DONE"


@pytest.mark.parametrize("recompute", [False, True])
def test_tampered_payload_or_destination_is_refused(home, body, tmp_path, recompute):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    data = json.loads(out.read_text())
    data["params"]["channel"] = "C9999"  # a different destination
    if recompute:
        data["content_hash"] = proposal.content_hash(data)
    out.write_text(json.dumps(data))
    backend = FakeWriter()
    env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "POLICY_DENIED" and backend.writes == []


def test_forged_approval_is_refused(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    forged = writer.mint(json.loads(out.read_text()), b"x" * 40, now=NOW, ttl_seconds=600)
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(forged))
    backend = FakeWriter()
    assert execute(home, out, path, backend)["error"]["code"] == "POLICY_DENIED" and backend.writes == []


def test_reused_and_expired_approvals_are_refused(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter()
    assert execute(home, out, approval, backend)["ok"]
    replay = execute(home, out, approval, backend)
    assert replay["error"]["code"] == "POLICY_DENIED" and "already executed" in replay["error"]["message"]
    second = make_approval(home, out, out=str(tmp_path / "second.json"))
    assert execute(home, out, second, backend)["error"]["code"] == "POLICY_DENIED"
    assert len(backend.writes) == 1

    out2, _ = make_proposal(home, body, tmp_path, name="p2.json")
    late = execute(home, out2, make_approval(home, out2), FakeWriter(), clock=Clock(NOW + 901))
    assert late["error"]["code"] == "POLICY_DENIED" and "expired" in late["error"]["message"]


# --- writer: timeout, crash, reconciliation ----------------------------------


def test_timeout_then_lookup_finds_the_write(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    backend = FakeWriter(error=BrokerError("UNAVAILABLE", "timeout"), found=Receipt(remote_id="r1"))
    env = execute(home, out, make_approval(home, out), backend)
    assert env["ok"] and env["data"]["reconciled"] is True and env["data"]["receipt"]["remote_id"] == "r1"
    assert len(backend.writes) == 1 and len(backend.lookups) == 1


def test_ambiguous_timeout_is_outcome_unknown_and_never_resent(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter(error=BrokerError("UNAVAILABLE", "timeout"))
    env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "OUTCOME_UNKNOWN" and journal(home, out)["state"] == "UNKNOWN"
    again = execute(home, out, approval, backend)
    assert again["error"]["code"] == "OUTCOME_UNKNOWN" and len(backend.writes) == 1 and len(backend.lookups) == 2

    key = json.loads(out.read_text())["idempotency_key"]
    backend.found = Receipt(remote_id="late")
    done = writer.reconcile(key, env={}, home=home, resolver=Resolver(), backends={"slack-web": backend}, clock=Clock())
    assert done["ok"] and done["data"]["state"] == "DONE" and len(backend.writes) == 1


def test_crash_mid_send_leaves_pending_and_resume_does_not_duplicate(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter(error=Killed())
    with pytest.raises(Killed):
        execute(home, out, approval, backend)
    assert journal(home, out)["state"] == "PENDING"  # honest: intent recorded, outcome not

    backend.error = None
    resumed = execute(home, out, approval, backend)
    assert resumed["error"]["code"] == "OUTCOME_UNKNOWN" and len(backend.writes) == 1

    key = json.loads(out.read_text())["idempotency_key"]
    common = dict(env={}, home=home, resolver=Resolver(), backends={"slack-web": backend}, clock=Clock())
    refused = writer.reconcile(key, confirm_not_sent=True, terminal=Terminal("yes"), **common)
    assert refused["error"]["code"] == "POLICY_DENIED"
    confirmed = writer.reconcile(key, confirm_not_sent=True, terminal=Terminal("not sent"), **common)
    assert confirmed["ok"] and confirmed["data"]["state"] == "FAILED"
    assert execute(home, out, approval, backend)["error"]["code"] == "POLICY_DENIED"  # old approval spent
    fresh = make_approval(home, out, out=str(tmp_path / "fresh.json"))
    assert execute(home, out, fresh, backend)["ok"] and len(backend.writes) == 2


def test_definite_refusal_is_recorded_as_failed(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    backend = FakeWriter(error=BrokerError("FORBIDDEN", "no"))
    env = execute(home, out, make_approval(home, out), backend)
    assert env["error"]["code"] == "FORBIDDEN" and backend.lookups == []
    record = journal(home, out)
    assert record["state"] == "FAILED" and record["error"] == "FORBIDDEN"


def test_writer_cli_status_prints_one_envelope(home, body, tmp_path, capsys):
    out, _ = make_proposal(home, body, tmp_path)
    execute(home, out, make_approval(home, out), FakeWriter())
    key = json.loads(out.read_text())["idempotency_key"]
    assert writer_cli.main(["status", key], home=home, env={}, resolver=Resolver()) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["data"]["state"] == "DONE" and "proposal" not in printed["data"]


def test_confirm_not_sent_needs_a_lookup_first(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    with pytest.raises(Killed):
        execute(home, out, make_approval(home, out), FakeWriter(error=Killed()))
    key = json.loads(out.read_text())["idempotency_key"]
    env = writer.reconcile(key, env={}, home=home, resolver=Resolver(), confirm_not_sent=True,
                           terminal=Terminal("not sent"), backends={"slack-web": FakeWriter()}, clock=Clock())
    assert env["error"]["code"] == "POLICY_DENIED" and journal(home, out)["state"] == "PENDING"


def test_lookup_that_fails_leaves_the_outcome_unknown(home, body, tmp_path):
    class Broken(FakeWriter):
        def lookup(self, *a, **k):
            raise BrokerError("UNAVAILABLE", "down")

    out, _ = make_proposal(home, body, tmp_path)
    backend = Broken(error=BrokerError("UNAVAILABLE", "timeout"))
    env = execute(home, out, make_approval(home, out), backend)
    assert env["error"]["code"] == "OUTCOME_UNKNOWN" and len(backend.writes) == 1
    assert journal(home, out)["last_lookup_error"] == "UNAVAILABLE"


def test_edited_journal_record_is_not_trusted(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter(error=BrokerError("UNAVAILABLE", "timeout"))
    execute(home, out, approval, backend)
    key = json.loads(out.read_text())["idempotency_key"]
    path = home / ".local/state/quoin/workctl/journal" / (key + ".json")
    record = json.loads(path.read_text())
    record.update(state="FAILED", nonces=[])  # try to make the spent approval usable again
    path.write_text(json.dumps(record))
    env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "OUTCOME_UNKNOWN" and len(backend.writes) == 1


def test_lock_contention_refuses_without_sending(home, body, tmp_path):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    backend = FakeWriter()
    with journal_of(home).lock(json.loads(out.read_text())["idempotency_key"]):
        env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "POLICY_DENIED" and env["error"]["retryable"] is True and backend.writes == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["services"]["slack"].update(tenant="T0002"),
        lambda c: c["services"]["slack"].update(account="someone-else"),
    ],
)
def test_binding_change_after_approval_is_refused(home, body, tmp_path, mutate):
    out, _ = make_proposal(home, body, tmp_path)
    approval = make_approval(home, out)
    data = config()
    mutate(data)
    write_config(home, data)
    backend = FakeWriter()
    env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "POLICY_DENIED" and backend.writes == []


@pytest.mark.parametrize("field,value", [("created_at", NOW + 3600), ("expires_at", NOW + 30 * 86400)])
def test_proposal_times_outside_the_configured_lifetime_are_refused(home, body, tmp_path, field, value):
    out, _ = make_proposal(home, body, tmp_path)
    data = json.loads(out.read_text())
    data[field] = proposal.iso(value)
    data["content_hash"] = proposal.content_hash(data)
    out.write_text(json.dumps(data))
    approval = tmp_path / "a.json"  # genuinely signed, so only the lifetime check can refuse
    approval.write_text(json.dumps(writer.mint(data, APPROVAL_KEY.encode(), now=NOW, ttl_seconds=600)))
    backend = FakeWriter()
    env = execute(home, out, approval, backend)
    assert env["error"]["code"] == "POLICY_DENIED" and "lifetime" in env["error"]["message"] and backend.writes == []


def test_propose_reads_no_credential_and_opens_no_connection(home, body, tmp_path, monkeypatch):
    from quoin.workctl import broker, transport

    def boom(*a, **k):
        raise AssertionError("propose must not reach credentials or the network")

    monkeypatch.setattr(broker, "default_resolver", boom)
    monkeypatch.setattr(transport.HttpTransport, "send", boom)
    make_proposal(home, body, tmp_path)


# --- backends: write and lookup against synthetic exchanges ------------------


MARKER = "ab" * 16


def ctx_for(backend, transport, **svc):
    service = ServiceConfig(service=svc.pop("service"), backend=backend.name, tenant="t", **svc)
    origin = ("https", (svc.get("base_url") or backend.default_base).split("/")[2])
    return Context(service=service, limits=Limits(), transport=transport, credential=SecretValue(WRITE_TOKEN),
                   fetched_at="2027-01-15T08:00:00Z", origin=origin)


def exchange(method, path, body, status=200, query=None):
    return {"request": {"method": method, "path": path, "query": query or {}}, "response": {"status": status, "body": body}}


def test_slack_post_tags_the_message_and_lookup_finds_the_tag():
    t = ReplayTransport([
        exchange("POST", "/api/chat.postMessage", {"ok": True, "channel": "C0123", "ts": "1800000000.000100"}),
        exchange("GET", "/api/conversations.replies", {"ok": True, "messages": [
            {"ts": "1800000000.000100", "metadata": {"event_type": "quoin_workctl_write",
                                                     "event_payload": {"idempotency_key": MARKER}}}]},
                 query={"include_all_metadata": "true", "ts": "1799999999.000001"}),
    ])
    ctx = ctx_for(slack_web.BACKEND, t, service="slack", web_url="https://acme.slack.com")
    params = {"channel": "C0123", "thread": "1799999999.000001", "body": "hi"}
    receipt = slack_web.BACKEND.write("slack.post", params, ctx, marker=MARKER)
    assert receipt.remote_id == "1800000000.000100" and receipt.url.endswith("/archives/C0123/p1800000000000100")
    sent = json.loads(t.sent[0].body)
    assert sent["thread_ts"] == "1799999999.000001" and sent["metadata"]["event_payload"]["idempotency_key"] == MARKER
    assert t.sent[0].headers["Authorization"] == "Bearer " + WRITE_TOKEN
    assert slack_web.BACKEND.lookup("slack.post", params, ctx, marker=MARKER, since=NOW).remote_id == "1800000000.000100"
    assert slack_web.BACKEND.lookup("slack.post", params, ctx, marker="cd" * 16, since=NOW) is None


@pytest.mark.parametrize("answer,code", [
    ({"ok": False, "error": "is_archived"}, "FORBIDDEN"),
    ({"ok": False, "error": "some_new_error"}, "INVALID_INPUT"),
    ({"ok": False, "error": "internal_error"}, "UNAVAILABLE"),
    ({}, "UNAVAILABLE"),  # no clear refusal: the post may exist
    ([1], "UNAVAILABLE"),
])
def test_slack_refusal_is_definite_but_unclear_answers_stay_ambiguous(answer, code):
    t = ReplayTransport([exchange("POST", "/api/chat.postMessage", answer)])
    with pytest.raises(BrokerError) as exc:
        slack_web.BACKEND.write("slack.post", {"channel": "C0123", "thread": None, "body": "x"},
                                ctx_for(slack_web.BACKEND, t, service="slack"), marker=MARKER)
    assert exc.value.code == code


def test_jira_comment_carries_a_property_and_lookup_finds_it():
    base = "https://acme.atlassian.net"
    t = ReplayTransport([
        exchange("POST", "/rest/api/3/issue/ENG-1/comment", {"id": "10042"}, status=201),
        exchange("GET", "/rest/api/3/issue/ENG-1/comment", {"comments": [
            {"id": "10041", "properties": []},
            {"id": "10042", "properties": [{"key": "quoin.workctl", "value": {"idempotency_key": MARKER}}]}]},
                 query={"expand": "properties"}),
    ])
    ctx = ctx_for(jira_cloud.BACKEND, t, service="jira", base_url=base, account="bot@acme.example")
    params = {"key": "ENG-1", "body": "first line\nsecond\n\nnext paragraph"}
    receipt = jira_cloud.BACKEND.write("jira.comment", params, ctx, marker=MARKER)
    assert receipt.remote_id == "10042" and receipt.url == base + "/browse/ENG-1?focusedCommentId=10042"
    sent = json.loads(t.sent[0].body)
    assert [p["type"] for p in sent["body"]["content"]] == ["paragraph", "paragraph"]
    assert sent["properties"] == [{"key": "quoin.workctl", "value": {"idempotency_key": MARKER}}]
    assert jira_cloud.BACKEND.lookup("jira.comment", params, ctx, marker=MARKER, since=NOW).remote_id == "10042"


def test_gmail_send_sets_a_marker_message_id_and_lookup_searches_for_it():
    mid = "<%s@quoin-workctl.invalid>" % MARKER
    t = ReplayTransport([
        exchange("POST", "/gmail/v1/users/me/messages/send", {"id": "m1", "threadId": "t1"}),
        exchange("GET", "/gmail/v1/users/me/messages", {"messages": [{"id": "m1", "threadId": "t1"}]},
                 query={"q": "rfc822msgid:" + mid[1:-1]}),
    ])
    ctx = ctx_for(gmail.BACKEND, t, service="mail", account="me@acme.example")
    params = {"to": ["a@b.example"], "cc": [], "subject": "Status", "in_reply_to": "<x@y>", "body": "Done."}
    assert gmail.BACKEND.write("mail.send", params, ctx, marker=MARKER).remote_id == "m1"
    raw = json.loads(t.sent[0].body)["raw"]
    message = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "Message-ID: " + mid in message and "In-Reply-To: <x@y>" in message and "From:" not in message
    assert gmail.BACKEND.lookup("mail.send", params, ctx, marker=MARKER, since=NOW).remote_id == "m1"


def test_backends_without_write_support_are_refused():
    from quoin.workctl.backends import load

    for name in ("msgraph", "command"):
        assert not callable(getattr(load(name), "write", None))


def test_slack_channel_lookup_pages_within_a_window_and_never_claims_a_partial_scan():
    page = {"ok": True, "messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "more"}}
    t = ReplayTransport([exchange("GET", "/api/conversations.history", page, query={"latest": "%d.000000" % (NOW + 1200)})])
    ctx = ctx_for(slack_web.BACKEND, t, service="slack")
    with pytest.raises(BrokerError) as exc:
        slack_web.BACKEND.lookup("slack.post", {"channel": "C0123", "thread": None, "body": "x"}, ctx,
                                 marker=MARKER, since=NOW)
    assert exc.value.code == "UNAVAILABLE" and len(t.sent) == slack_web.LOOKUP_PAGES


def test_jira_lookup_stops_at_comments_older_than_the_send():
    t = ReplayTransport([exchange("GET", "/rest/api/3/issue/ENG-1/comment", {"total": 500, "comments": [
        {"id": "9", "created": "2020-01-01T00:00:00.000+0000", "properties": []}]})])
    ctx = ctx_for(jira_cloud.BACKEND, t, service="jira", base_url="https://acme.atlassian.net")
    assert jira_cloud.BACKEND.lookup("jira.comment", {"key": "ENG-1", "body": "x"}, ctx, marker=MARKER, since=NOW) is None
    assert len(t.sent) == 1


@pytest.mark.parametrize("status,body,code", [(503, {}, "UNAVAILABLE"), (200, "not json", "UNAVAILABLE"),
                                              (400, {}, "INVALID_INPUT"), (403, {}, "FORBIDDEN")])
def test_jira_write_errors_separate_maybe_sent_from_refused(status, body, code):
    t = ReplayTransport([exchange("POST", "/rest/api/3/issue/ENG-1/comment", body, status=status)])
    ctx = ctx_for(jira_cloud.BACKEND, t, service="jira", base_url="https://acme.atlassian.net")
    with pytest.raises(BrokerError) as exc:
        jira_cloud.BACKEND.write("jira.comment", {"key": "ENG-1", "body": "x"}, ctx, marker=MARKER)
    assert exc.value.code == code


def test_mail_subject_with_any_line_separator_is_refused_at_propose():
    from quoin.workctl import operations

    for subject in ("a\x0bb", "a\u2028b", "a\x85b", "a\tb"):
        with pytest.raises(BrokerError):
            operations.normalize_write(operations.OPERATIONS["mail.send"], {"to": ["a@b.example"], "subject": subject, "body": "x"})


def test_write_to_a_host_outside_the_service_origin_is_refused():
    t = ReplayTransport([])
    ctx = ctx_for(slack_web.BACKEND, t, service="slack")
    ctx.origin = ("https", "other.example")
    with pytest.raises(BrokerError) as exc:
        slack_web.BACKEND.write("slack.post", {"channel": "C0123", "thread": None, "body": "x"}, ctx, marker=MARKER)
    assert exc.value.code == "POLICY_DENIED" and t.sent == []
