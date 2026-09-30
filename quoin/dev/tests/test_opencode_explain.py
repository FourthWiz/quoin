"""Tests for the explain renderer: stable text and JSON, effective values with
origins, findings, redaction and closed-token text tables."""
from __future__ import annotations

import ast
import json
import socket
import subprocess
from datetime import timedelta

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW, PROFILE_PERSONAL, World, fixture
from quoin.opencode_adapter import compiler, errors, explain, roles

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
HOSTS = ("gateway.example.invalid", "gateway-b.example.invalid", "untrusted.example.invalid")
KEYCHAIN_ACCOUNT = "work-gateway"


@pytest.fixture(autouse=True)
def offline_traps(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network or subprocess use is not allowed in this module")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
    ):
        monkeypatch.setattr(owner, name, boom)


@pytest.fixture
def work(tmp_path):
    return World(tmp_path)


def render(ev, **kw):
    kw.setdefault("redact", False)
    kw.setdefault("as_json", False)
    return explain.render(ev, **kw)


# ---------------------------------------------------------------- stability


def test_json_render_is_stable_and_parses(work):
    ev = work.evaluate()
    one, two = render(ev, as_json=True), render(ev, as_json=True)
    assert one == two
    doc = json.loads(one)
    assert list(doc) == [
        "explain_format", "profile", "project", "classification", "managed_policy", "providers",
        "models", "values", "security", "roles", "auxiliaries", "findings", "digest",
        "digest_note", "launchable", "output_location",
    ]
    assert doc["explain_format"] == 1
    assert doc["digest"].startswith("sha256:") and doc["digest_note"] is None


def test_text_render_is_stable_and_comes_from_the_same_facts(work):
    ev = work.evaluate()
    text = render(ev)
    assert text == render(ev)
    doc = json.loads(render(ev, as_json=True))
    assert doc["digest"] in text
    for item in doc["providers"]:
        assert item["id"] in text and item["native_id"] in text
    order = [text.index(h) for h in (
        "Profile\n", "Project\n", "Providers\n", "Models\n", "Effective values\n",
        "Security merge result\n", "Role resolutions\n", "Auxiliary resolutions\n", "Findings\n", "Result\n",
    )]
    assert order == sorted(order)


def test_every_effective_value_is_rendered_with_its_origin(work):
    ev = work.evaluate()
    doc = json.loads(render(ev, as_json=True))
    assert set(doc["values"]) == set(ev.effective.values)
    text = render(ev)
    for key, item in ev.effective.values.items():
        line = [l for l in text.splitlines() if l.startswith(key + " = ")]
        assert len(line) == 1, key
        assert "origin: " + ", ".join(item.origin) in line[0]


def test_summary_is_marked_as_not_emitted(work):
    doc = json.loads(render(work.evaluate(), as_json=True))
    summary = [r for r in doc["auxiliaries"] if r["role"] == "summary"][0]
    assert summary["emitted"] is False and summary["note"] == explain.SUMMARY_NOTE
    assert "not used by the pinned runtime; not emitted" in render(work.evaluate())


def test_real_record_wording_for_effort(work):
    text = render(work.evaluate())
    assert (
        "the model's capability record does not report reasoning support; "
        "the effort takes effect once a probe records it"
    ) in text


def test_standing_findings_are_listed(work):
    doc = json.loads(render(work.evaluate(), as_json=True))
    codes = [f["code"] for f in doc["findings"]]
    assert "later-layers-can-override" in codes and "policies-supplementary" in codes
    assert "summary-unused" in codes and "no-managed-policy" in codes
    assert doc["managed_policy"] == "no managed policy"


def test_unclassified_project_shows_the_blocker_and_no_digest(tmp_path):
    world = World(tmp_path, project=fixture("valid/project-unclassified.json"))
    ev = world.evaluate()
    doc = json.loads(render(ev, as_json=True))
    blockers = [f for f in doc["findings"] if f["blocking"]]
    assert [f["code"] for f in blockers] == ["missing-classification"]
    assert doc["digest"] is None and doc["digest_note"] == "not available: blocking findings"
    assert doc["launchable"] is False
    assert "null (not available: blocking findings)" in render(ev)


def test_no_build_is_attempted_while_blockers_exist(tmp_path, monkeypatch):
    world = World(tmp_path, project=fixture("valid/project-unclassified.json"))
    ev = world.evaluate()
    monkeypatch.setattr(compiler, "build", lambda *_: pytest.fail("built while blocked"))
    render(ev)


def test_explain_never_builds_with_allow_unqualified(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.write_records(now=NOW - timedelta(days=40))
    ev = world.evaluate(allow_unqualified=True)
    monkeypatch.setattr(compiler, "build", lambda *_: pytest.fail("built with allow_unqualified"))
    doc = json.loads(render(ev, as_json=True))
    assert doc["digest"] is None and "unqualified" in doc["digest_note"]
    assert any(r["status"] == "unqualified" for r in doc["roles"])


def test_a_gate_failure_is_shown_as_a_blocking_finding(work, monkeypatch):
    real = compiler.build_document

    def patched(ev):
        doc = real(ev)
        doc["share"] = "auto"
        return doc

    monkeypatch.setattr(compiler, "build_document", patched)
    doc = json.loads(render(work.evaluate(), as_json=True))
    gate = [f for f in doc["findings"] if f["code"].startswith("gate-")]
    assert [f["code"] for f in gate] == ["gate-constants"] and gate[0]["blocking"]
    assert doc["digest"] is None and doc["launchable"] is False


def test_personal_profile_renders(tmp_path):
    world = World(tmp_path, profile=PROFILE_PERSONAL)
    doc = json.loads(render(world.evaluate(), as_json=True))
    assert doc["classification"] == "personal"
    assert doc["providers"][0]["native_id"] == "openrouter"


def test_output_location(work):
    ev = work.evaluate()
    target = work.tmp / "somewhere"
    assert json.loads(render(ev, as_json=True, output_dir=target))["output_location"] == str(target)
    redacted = json.loads(render(ev, as_json=True, output_dir=target, redact=True))["output_location"]
    assert redacted == "$XDG_STATE_HOME/quoin/opencode/work/%s" % ev.project_key
    assert str(work.tmp) not in redacted
    assert json.loads(render(ev, as_json=True))["output_location"] is None


# ---------------------------------------------------------------- redaction


def test_unredacted_output_shows_endpoints_and_references(work):
    text = render(work.evaluate())
    assert "https://gateway.example.invalid/v1" in text
    assert "keychain:quoin/%s" % KEYCHAIN_ACCOUNT in text
    for host in HOSTS[:2]:
        assert host in text


@pytest.mark.parametrize("as_json", [False, True])
def test_redaction_masks_hosts_accounts_and_lists(work, as_json):
    ev = work.evaluate()
    text = render(ev, redact=True, as_json=as_json)
    for host in HOSTS:
        assert host not in text, host
    assert KEYCHAIN_ACCOUNT not in text
    assert "https://***/v1" in text.replace('\\"', '"')
    assert "keychain:quoin/***" in text
    # Everything else stays.
    for keep in ("QUOIN_CORP_GW_API_KEY", "example-vendor/example-coder", "corp-gw-b", "env:QUOIN_CORP_GW_B_API_KEY"):
        assert keep in text, keep
    assert "***" in text


def test_redaction_keeps_the_digest_and_the_rest_of_the_document(work):
    ev = work.evaluate()
    plain = json.loads(render(ev, as_json=True))
    masked = json.loads(render(ev, as_json=True, redact=True))
    assert plain["digest"] == masked["digest"]
    assert [r["native_model"] for r in plain["roles"]] == [r["native_model"] for r in masked["roles"]]
    assert plain["values"].keys() == masked["values"].keys()
    assert masked["security"]["allowed_hosts"] == ["***"] * len(plain["security"]["allowed_hosts"])
    assert masked["values"]["policy.allowed_hosts"]["value"] == ["***"] * len(
        plain["values"]["policy.allowed_hosts"]["value"]
    )


def test_seeded_secret_never_appears(work, monkeypatch):
    for name in ("QUOIN_CORP_GW_API_KEY", "QUOIN_CORP_GW_B_API_KEY"):
        work.env[name] = helpers.SEEDED_SECRET
    ev = work.evaluate()
    for redact in (False, True):
        for as_json in (False, True):
            text = render(ev, redact=redact, as_json=as_json)
            for form in helpers.secret_forms(helpers.SEEDED_SECRET):
                assert form not in text


# ------------------------------------------------------------- text tables


def test_every_reason_token_has_text():
    assert set(explain.REASON_TEXT) == set(errors.REASON_CODES)
    assert all(explain.REASON_TEXT[t] and "%" not in explain.REASON_TEXT[t] for t in errors.REASON_CODES)


def test_every_effort_diagnostic_has_text():
    assert set(explain.DIAGNOSTIC_TEXT) == {
        "effort-max", "effort-no-capability", "effort-no-mapping", "effort-unqualified"
    }
    assert explain.DIAGNOSTIC_TEXT["effort-no-capability"] == (
        "the model's capability record does not report reasoning support; "
        "the effort takes effect once a probe records it"
    )
    assert {r for r in explain.DIAGNOSTIC_TEXT} <= errors.ROLE_REASONS
    assert roles.ROLE_DEFAULT_EFFORT


def test_module_imports_are_limited():
    tree = ast.parse((SRC_DIR / "explain.py").read_text(encoding="utf-8"))
    relative, external = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                relative |= {node.module} if node.module else {a.name for a in node.names}
            else:
                external.add(node.module)
        elif isinstance(node, ast.Import):
            external |= {a.name for a in node.names}
    assert relative <= {"compiler", "errors", "merge", "roles", "secrets"}
    assert external <= {"__future__", "json", "pathlib", "typing", "urllib.parse"}
