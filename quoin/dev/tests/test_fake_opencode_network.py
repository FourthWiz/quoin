"""The fake executable's network verbs, driven without any Quoin driver code.

Every target here is loopback or a blocked TEST-NET address that the fake must
refuse before connecting. The positive controls show the fake really egresses
(so a zero-capture assertion elsewhere is not vacuous) and that its own
allowlist stops everything it is not told to allow.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

import _opencode_capture_helpers as ch
from test_fake_opencode import Harness, fake

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

KEY = "work-key-" + "a1b2c3d4e5f6a7b8"
KEY_ENV = "QUOIN_CORP_GW_API_KEY"
PLANNER = "example-vendor/example-planner"


def _compiled(base_url: str) -> dict:
    """A compiled document of the shape the compiler emits."""
    return {
        "model": "quoin-corp-gw/work-coder",
        "small_model": "quoin-corp-gw-b/" + PLANNER,
        "agent": {
            "quoin-implementer": {"model": "quoin-corp-gw/work-coder"},
            "quoin-reviewer": {"model": "quoin-corp-gw/not-whitelisted"},
            "title": {"model": "quoin-corp-gw-b/" + PLANNER},
        },
        "enabled_providers": ["quoin-corp-gw", "quoin-corp-gw-b"],
        "provider": {
            "quoin-corp-gw": {
                "options": {"baseURL": base_url, "apiKey": "{env:%s}" % KEY_ENV},
                "whitelist": ["work-coder"],
            },
            "quoin-corp-gw-b": {
                "options": {"baseURL": base_url, "apiKey": "{env:%s}" % KEY_ENV},
                "whitelist": [PLANNER],
            },
        },
    }


@pytest.fixture
def net(tmp_path):
    guard = ch.EgressGuard()
    guard.start()
    config = tmp_path / "compiled.json"
    config.write_text(json.dumps(_compiled("http://127.0.0.1:%d/v1" % guard.work.port)), encoding="utf-8")
    state = tmp_path / "state"
    guard.watch_network_log(state / "network.jsonl")
    yield guard, config, state
    guard.stop()


def _run(tmp_path, scenario, allowed, config, env_extra=None):
    doc = ch.with_allowed_targets(scenario, allowed) if allowed is not None else dict(scenario)
    harness = Harness(tmp_path, doc)
    env = harness.env()
    env["OPENCODE_CONFIG"] = str(config)
    env[KEY_ENV] = KEY
    env.update(env_extra or {})
    done = subprocess.run(
        [str(harness.shim), "run", "--format", "json", "--", "go"], cwd=str(harness.cwd), env=env,
        capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    return done, harness.state


def _records(state):
    path = state / "network.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _connects(state):
    path = state / "connect-attempts.log"
    return path.read_text().splitlines() if path.exists() else []


def test_provider_call_reaches_the_work_gateway_and_not_the_personal_one(net, tmp_path):
    guard, config, state = net
    done, state = _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), guard.allowed_targets(), config)
    assert done.returncode == 0, done.stderr
    assert [c["model"] for c in guard.work.captures()] == ["work-coder"]
    assert guard.work.captures()[0]["path"] == "/v1/chat/completions"
    assert guard.personal.captures() == [] and guard.proxy.records() == []
    assert [r["ok"] for r in _records(state)] == [True]
    guard.check()


def test_provider_call_sends_the_key_read_from_the_provider_env_reference(net, tmp_path):
    guard, config, state = net
    _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), guard.allowed_targets(), config)
    expected = hashlib.sha256(("Bearer " + KEY).encode()).hexdigest()
    assert guard.work.captures()[0]["auth_sha256"] == expected


def test_a_role_whose_model_is_outside_the_whitelist_sends_nothing_and_exits_1(net, tmp_path):
    guard, config, state = net
    done, state = _run(tmp_path, fake.SCENARIOS["provider_call"]("reviewer"), guard.allowed_targets(), config)
    assert done.returncode == 1
    assert b"whitelist" in done.stdout
    assert guard.work.captures() == [] and _connects(state) == []


@pytest.mark.parametrize("role", ["title", "compaction", "small"])
def test_auxiliary_roles_resolve_their_model_and_split_on_the_first_slash(net, tmp_path, role):
    guard, config, state = net
    done, _ = _run(tmp_path, fake.SCENARIOS["provider_call"](role), guard.allowed_targets(), config)
    assert done.returncode == 0, done.stdout
    assert [c["model"] for c in guard.work.captures()] == [PLANNER]


def test_provider_call_ignores_proxy_variables(net, tmp_path):
    guard, config, state = net
    env = {"HTTPS_PROXY": guard.proxy.url, "HTTP_PROXY": guard.proxy.url, "https_proxy": guard.proxy.url}
    done, _ = _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), guard.allowed_targets(), config, env)
    assert done.returncode == 0
    assert guard.proxy.records() == [] and len(guard.work.captures()) == 1


def test_shell_net_to_the_personal_listener_is_captured_when_allowed(net, tmp_path):
    """Positive control: the fake really egresses, so silence elsewhere means something."""
    guard, config, state = net
    guard.allow_personal = True
    url = "http://127.0.0.1:%d/exfil" % guard.personal.port
    done, state = _run(tmp_path, fake.SCENARIOS["shell_net"](url), guard.allowed_targets(), config)
    assert done.returncode == 0
    assert [c["path"] for c in guard.personal.captures()] == ["/exfil"]
    assert _records(state)[0]["ok"] is True


def test_shell_net_proxy_goes_through_the_proxy_named_in_the_environment(net, tmp_path):
    guard, config, state = net
    guard.allow_proxy = True
    env = {"HTTPS_PROXY": guard.proxy.url}
    done, _ = _run(tmp_path, fake.SCENARIOS["shell_net_proxy"]("https://personal.example.invalid/x"),
                   guard.allowed_targets(), config, env)
    assert done.returncode == 0
    assert guard.proxy.records() == [{"method": "CONNECT", "target": "personal.example.invalid:443"}]


def test_read_and_send_reports_an_unreadable_file_and_sends_nothing(net, tmp_path):
    guard, config, state = net
    guard.allow_personal = True
    url = "http://127.0.0.1:%d/x" % guard.personal.port
    done, state = _run(tmp_path, fake.SCENARIOS["read_and_send"](str(tmp_path / "missing"), url),
                       guard.allowed_targets(), config)
    assert done.returncode == 0
    assert _records(state)[0]["ok"] is False and guard.personal.captures() == []


def test_read_and_send_posts_the_digest_of_a_readable_file(net, tmp_path):
    guard, config, state = net
    guard.allow_personal = True
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("value", encoding="utf-8")
    url = "http://127.0.0.1:%d/x" % guard.personal.port
    done, state = _run(tmp_path, fake.SCENARIOS["read_and_send"](str(secret_file), url),
                       guard.allowed_targets(), config)
    assert done.returncode == 0 and _records(state)[0]["ok"] is True
    digest = hashlib.sha256(b"value").hexdigest()
    assert guard.personal.captures()[0]["body_sha256"] == hashlib.sha256(digest.encode()).hexdigest()


def test_the_allowlist_blocks_a_test_net_target_before_any_connect(net, tmp_path):
    """Positive control for the fake's own allowlist (mutation: skip the check in the child)."""
    guard, config, state = net
    done, state = _run(tmp_path, fake.SCENARIOS["shell_net"]("http://198.51.100.1:80/x"),
                       guard.allowed_targets(), config)
    assert done.returncode == fake.BLOCKED_EXIT
    records = _records(state)
    assert records and records[0]["blocked"] is True and records[0]["ok"] is False
    assert records[0]["target_host"] == "198.51.100.1"
    assert _connects(state) == []
    with pytest.raises(AssertionError, match="blocked egress"):
        guard.watch_network_log(state / "network.jsonl")
        guard.check()
    guard.skip_teardown_check = True


def test_a_scenario_without_allowed_targets_blocks_even_the_work_gateway(net, tmp_path):
    guard, config, state = net
    done, state = _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), None, config)
    assert done.returncode == fake.BLOCKED_EXIT
    assert guard.work.captures() == [] and _records(state)[0]["blocked"] is True


def test_an_empty_allowlist_blocks_everything(net, tmp_path):
    guard, config, state = net
    done, state = _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), [], config)
    assert done.returncode == fake.BLOCKED_EXIT and guard.work.captures() == []


def test_the_environment_is_never_consulted_for_the_allowlist(net, tmp_path):
    guard, config, state = net
    env = {"FAKE_OPENCODE_ALLOWED_TARGETS": ",".join(guard.allowed_targets())}
    done, state = _run(tmp_path, fake.SCENARIOS["provider_call"]("implementer"), None, config, env)
    assert done.returncode == fake.BLOCKED_EXIT and guard.work.captures() == []


def test_npm_install_writes_the_install_files_unless_offline(tmp_path):
    for offline, expected in (("", True), ("true", False)):
        sub = tmp_path / ("run-" + (offline or "online"))
        sub.mkdir()
        harness = Harness(sub, fake.SCENARIOS["npm_install"]())
        env = harness.env()
        env.pop("npm_config_offline", None)
        if offline:
            env["npm_config_offline"] = offline
        done = subprocess.run([str(harness.shim), "run", "--format", "json", "--", "go"], cwd=str(harness.cwd),
                              env=env, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        assert done.returncode == 0
        base = harness.cwd / ".opencode"
        assert (base / "bun.lock").exists() is expected
        assert (base / "node_modules/@opencode-ai/plugin/package.json").exists() is expected
