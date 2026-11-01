"""The security-test fixtures are checked on their own, with positive controls,
so a zero-capture assertion elsewhere can never pass because a listener was
unreachable or silently dropped a record."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.request

import pytest

import _opencode_capture_helpers as ch
from _opencode_capture_helpers import egress_guard  # noqa: F401  (fixture)
from _opencode_merge_helpers import World
from quoin.opencode_adapter import config


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _post(server, model="some/model", token="tok-123", body=None):
    payload = json.dumps(body or {"model": model, "messages": []}).encode("utf-8")
    request = urllib.request.Request(
        server.base_url + "/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


# ------------------------------------------------------------ CaptureServer


def test_a_post_matching_no_provider_scenario_still_gets_200_and_is_captured(egress_guard):
    status, reply = _post(egress_guard.work, model="no-such-scenario-anywhere")
    assert status == 200 and reply["choices"]
    (capture,) = egress_guard.work.captures()
    assert capture["method"] == "POST" and capture["model"] == "no-such-scenario-anywhere"


def test_the_personal_listener_positive_control_records_model_and_auth_digest(egress_guard):
    egress_guard.allow_personal = True
    _post(egress_guard.personal, model="openrouter/x", token="personal-token")
    (capture,) = egress_guard.personal.captures()
    assert capture["model"] == "openrouter/x"
    assert capture["auth_sha256"] == _sha("Bearer personal-token")
    assert egress_guard.personal.label == ch.PERSONAL_LABEL and egress_guard.work.label == ch.WORK_LABEL


def test_no_capture_stores_the_raw_authorization_value_or_the_body(egress_guard):
    _post(egress_guard.work, token="raw-secret-value", body={"model": "m", "messages": [{"content": "body-text-marker"}]})
    dumped = json.dumps(egress_guard.work.captures())
    assert "raw-secret-value" not in dumped and "body-text-marker" not in dumped
    assert set(egress_guard.work.captures()[0]) == {"method", "path", "model", "body_sha256", "auth_sha256"}


def test_captures_returns_a_copy(egress_guard):
    _post(egress_guard.work)
    egress_guard.work.captures().clear()
    assert len(egress_guard.work.captures()) == 1


def test_a_request_without_authorization_has_a_null_digest(egress_guard):
    request = urllib.request.Request(
        egress_guard.work.base_url + "/chat/completions", data=b"{}", method="POST")
    urllib.request.urlopen(request, timeout=5).read()
    assert egress_guard.work.captures()[0]["auth_sha256"] is None


# ------------------------------------------------------------- CaptureProxy


def _raw(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(payload)
        sock.settimeout(3)
        return sock.recv(4096)


def test_a_connect_through_the_proxy_is_recorded_and_refused(egress_guard):
    reply = _raw(egress_guard.proxy.port, b"CONNECT personal.example.invalid:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert reply.startswith(b"HTTP/1.0 403")
    assert egress_guard.proxy.records() == [{"method": "CONNECT", "target": "personal.example.invalid:443"}]
    egress_guard.skip_teardown_check = True


def test_an_absolute_uri_request_is_recorded_and_refused(egress_guard):
    reply = _raw(egress_guard.proxy.port, b"GET http://personal.example.invalid/x HTTP/1.1\r\nHost: p\r\n\r\n")
    assert reply.startswith(b"HTTP/1.0 403")
    assert egress_guard.proxy.records()[0]["method"] == "GET"
    egress_guard.skip_teardown_check = True


def test_a_listed_connect_target_is_tunnelled_to_loopback_only(egress_guard):
    proxy = ch.CaptureProxy({"gw.example.invalid:443": ("127.0.0.1", egress_guard.work.port)})
    with proxy:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as sock:
            sock.sendall(b"CONNECT gw.example.invalid:443 HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.settimeout(3)
            assert b" 200 " in sock.recv(4096)
            body = b'{"model": "tunnelled"}'
            sock.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            assert b"200" in sock.recv(4096)
    assert egress_guard.work.captures()[0]["model"] == "tunnelled"
    with pytest.raises(ValueError):
        ch.CaptureProxy({"x:1": ("192.0.2.1", 1)})


# ---------------------------------------------------------- secrets scanning


def test_the_scanner_finds_each_form_planted_in_a_file(tmp_path):
    seed = 'sk/+=\\"quoin' + ch.make_seed()
    forms = ch.secret_forms(seed)
    assert {"raw", "base64", "base64url", "url-encoded", "json-escaped", "head8", "tail8"} <= set(forms)
    for name, form in forms.items():
        folder = tmp_path / name.replace("-", "_")
        folder.mkdir()
        (folder / "out.txt").write_text("prefix %s suffix" % form, encoding="utf-8")
        hits = ch.scan_for_secrets([folder], forms)
        assert (str(folder / "out.txt"), name) in hits, name


def test_a_clean_corpus_has_no_hits_and_an_empty_one_is_refused(tmp_path):
    forms = ch.secret_forms(ch.make_seed())
    (tmp_path / "a.txt").write_text("nothing here", encoding="utf-8")
    assert ch.scan_for_secrets([tmp_path], forms) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="empty corpus"):
        ch.scan_for_secrets([empty], forms)
    with pytest.raises(AssertionError, match="empty corpus"):
        ch.scan_for_secrets([("blank", "")], forms)
    assert ch.scan_for_secrets([("text", "has %s inside" % forms["raw"])], forms)[0] == ("text", "raw")


def test_the_scanner_skips_symlinks_and_excluded_paths(tmp_path):
    seed = ch.make_seed()
    forms = ch.secret_forms(seed)
    outside = tmp_path / "outside.txt"
    outside.write_text(seed, encoding="utf-8")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "keep.txt").write_text("clean", encoding="utf-8")
    os.symlink(outside, corpus / "link.txt")
    creds = corpus / "creds.json"
    creds.write_text(seed, encoding="utf-8")
    assert ch.scan_for_secrets([corpus], forms, exclude=[creds]) == []
    assert any(where == str(creds) for where, _ in ch.scan_for_secrets([corpus], forms))


# --------------------------------------------------------------- the guard


def test_the_guard_passes_when_nothing_unexpected_happened(egress_guard, tmp_path):
    protected = tmp_path / "protected.txt"
    protected.write_text("same", encoding="utf-8")
    egress_guard.protect(protected)
    log = tmp_path / "network.jsonl"
    log.write_text(json.dumps({"target": "127.0.0.1:1", "blocked": False}) + "\n", encoding="utf-8")
    egress_guard.watch_network_log(log, required=True)
    egress_guard.check()


def test_the_guard_fails_when_the_personal_listener_got_a_request(egress_guard):
    _post(egress_guard.personal)
    with pytest.raises(AssertionError, match="personal listener"):
        egress_guard.check()
    egress_guard.skip_teardown_check = True


def test_the_guard_allows_the_personal_listener_only_on_request(egress_guard):
    assert egress_guard.allowed_targets() == ["127.0.0.1:%d" % egress_guard.work.port]
    egress_guard.allow_personal = True
    egress_guard.allow_proxy = True
    assert len(egress_guard.allowed_targets()) == 3
    _post(egress_guard.personal)
    egress_guard.check()


def test_the_guard_fails_on_a_blocked_network_record_and_a_missing_required_log(egress_guard, tmp_path):
    log = tmp_path / "network.jsonl"
    log.write_text(json.dumps({"blocked": True}) + "\n", encoding="utf-8")
    egress_guard.watch_network_log(log)
    with pytest.raises(AssertionError, match="blocked"):
        egress_guard.check()
    egress_guard.skip_teardown_check = True
    other = ch.EgressGuard()
    other.watch_network_log(tmp_path / "absent.jsonl", required=True)
    with pytest.raises(AssertionError, match="missing"):
        other.check()
    optional = ch.EgressGuard()
    optional.watch_network_log(tmp_path / "absent3.jsonl")
    optional.check()


def test_the_guard_fails_when_a_protected_file_changed_or_appeared(egress_guard, tmp_path):
    folder = tmp_path / "opposite"
    folder.mkdir()
    secret_file = folder / "auth.json"
    secret_file.write_text("v1", encoding="utf-8")
    egress_guard.protect(folder)
    secret_file.write_text("v2", encoding="utf-8")
    with pytest.raises(AssertionError, match="protected path changed"):
        egress_guard.check()
    egress_guard.skip_teardown_check = True


def test_the_guard_fails_when_a_file_appears_under_a_protected_folder(egress_guard, tmp_path):
    folder = tmp_path / "opposite"
    folder.mkdir()
    (folder / "auth.json").write_text("v1", encoding="utf-8")
    egress_guard.protect(folder)
    egress_guard.check()
    (folder / "new.txt").write_text("x", encoding="utf-8")
    with pytest.raises(AssertionError, match="gained a file"):
        egress_guard.check()
    egress_guard.skip_teardown_check = True


def test_the_loopback_guard_is_active_in_the_fixture(egress_guard):
    with pytest.raises(AssertionError, match="non-loopback"):
        socket.create_connection(("192.0.2.9", 80), timeout=1)


def test_allowed_targets_land_at_the_top_level_of_the_scenario_dict():
    doc = ch.with_allowed_targets("record_only", ["127.0.0.1:9"])
    assert doc["allowed_targets"] == ["127.0.0.1:9"] and "attempts" in doc


# ------------------------------------------------------------ world helpers


def _world(tmp_path):
    return World(tmp_path, agents=False, root=None)


def test_binding_the_work_providers_rewrites_both_and_the_records(tmp_path, egress_guard):
    world = _world(tmp_path)
    ch.bind_work_providers(world, egress_guard.work)
    for provider_id in ch.WORK_PROVIDERS:
        assert world.profile["providers"][provider_id]["base_url"] == egress_guard.work.base_url
    assert "127.0.0.1" in world.profile["policy"]["allowed_hosts"]
    effective = world.effective()
    assert {p.base_url for p in effective.providers.values()} == {egress_guard.work.base_url}


def test_the_personal_profile_is_added_beside_the_work_profile(tmp_path):
    world = _world(tmp_path)
    work_file = next(
        p for p in (tmp_path / "xdg").rglob("work.json")
    )
    before = hashlib.sha256(work_file.read_bytes()).hexdigest()
    seed = ch.make_seed("sk-")
    auth = ch.install_personal_profile(world, seed)
    empty = tmp_path / "personal-empty-project"
    loaded = config.load_all(project_root=empty, profile="personal", env=world.env, home=world.home)
    assert loaded.profile.data["profile"] == "personal"
    assert json.loads(auth.read_text(encoding="utf-8"))["openrouter"]["key"] == seed
    assert auth.parent.parent.name == "data" and oct(auth.stat().st_mode & 0o777) == "0o600"
    assert hashlib.sha256(work_file.read_bytes()).hexdigest() == before
