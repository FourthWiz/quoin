"""Effective-configuration record: build, token rule, digest, bounding and widening."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from quoin.opencode_adapter import effective_record, inventory, launch_env, merge, profile_policy
from test_opencode_inventory import COMPILED, SECRET, World
from test_opencode_launch_env import SECRET as LAUNCH_SECRET
from test_opencode_launch_env import _Resolver, _sidecar, _view


def _effective(classification="work", **policy):
    values = {
        "policy." + k.replace("__", "."): merge.Value(v, ("profile",)) for k, v in policy.items()
    }
    return SimpleNamespace(values=values, classification=classification)


def _build(world, inv=None, classification="work", **overrides):
    inv = inv or world.collect(compiled_doc=COMPILED)
    effective = _effective(classification)
    policy = effective_record.policy_section(effective)
    expected = inventory.Expected.from_compiled(COMPILED, {}, profile_policy.view(effective))
    kw = dict(
        inventory=inv, verdicts=inventory.evaluate(inv, expected), profile="work", classification=classification,
        opencode_version="1.18.32", compiled_digest="sha256:abc", native_sha256="n" * 8,
        env_names=["PATH", "OPENCODE_CONFIG"], launcher_names=["OPENCODE_CONFIG"], stripped_names=["OPENCODE_DB"],
        dirs_display={"data": "~/p/data"}, credentials=[{"provider": "prov", "ref_masked": "env:K", "kind": "static-api-key"}],
        policy=policy,
    )
    kw.update(overrides)
    return effective_record.build(**kw)


def test_shape_and_pending_sections(tmp_path):
    record = _build(World(tmp_path))
    assert record["record_format"] == 1
    assert record["pending_sections"] == ["enforcement", "isolation", "network"]
    assert record["isolation"]["status"] == "pending" and record["network"]["status"] == "pending"
    assert record["enforcement"] == []
    assert set(record) >= {"sources", "entries", "entries_sha256", "compiled", "env", "dirs", "credentials",
                           "static_key_in_use", "logging", "retention", "policy", "digest"}
    assert record["static_key_in_use"] is True
    assert record["digest"] == effective_record.digest(record)


def test_digest_is_stable_across_builds_and_insertion_order(tmp_path):
    world = World(tmp_path)
    first, second = _build(world), _build(world)
    assert first["digest"] == second["digest"]
    reordered = dict(reversed(list(first.items())))
    assert effective_record.digest(reordered) == first["digest"]


MUTATIONS = [
    {"plugin": ["x"]}, {"provider": {"prov": {}}}, {"mcp": {"s": {"type": "local", "command": ["x"]}}},
    {"lsp": {"a": {}}}, {"tools": {"bash": False}}, {"agent": {"a": {"model": "prov/m"}}},
    {"command": {"c": {"template": "t"}}}, {"permission": {"edit": "allow"}}, {"instructions": ["a.md"]},
    {"theme": "x"}, {"surprise": 1},
]


@pytest.mark.parametrize("doc", MUTATIONS)
def test_digest_changes_for_each_entry_kind(tmp_path, doc):
    base = _build(World(tmp_path / "a"))
    changed_world = World(tmp_path / "b")
    changed_world.write(changed_world.global_dir / "opencode.json", doc)
    assert _build(changed_world)["digest"] != base["digest"]


def test_digest_moves_with_policy_env_and_compiled_changes(tmp_path):
    world = World(tmp_path)
    base = _build(world)["digest"]
    assert _build(world, compiled_digest="sha256:other")["digest"] != base
    assert _build(world, env_names=["PATH"])["digest"] != base
    other = effective_record.policy_section(_effective(web_tools="allow"))
    assert _build(world, policy=other)["digest"] != base


def test_no_timestamp_run_id_or_absolute_path_in_the_record(tmp_path):
    world = World(tmp_path)
    world.write(world.project / "opencode.json", {"theme": "x"})
    text = json.dumps(_build(world))
    assert str(tmp_path) not in text
    for forbidden in ("run_id", "timestamp", "started_at", "attempt"):
        assert forbidden not in text


@pytest.mark.parametrize("value,kept", [
    ("plain-name", True), ("with_underscore", True), ("h-" + "a" * 32, False),
    ("a" * 65, False), ("naïve", False), ("a b", False), ("ghp_" + "A1" * 20, False), ("", False),
])
def test_token_rule(value, kept):
    out = effective_record.token_or_digest(value)
    assert (out == value) is kept
    if not kept:
        assert out.startswith("h-") and len(out) == 34
        assert effective_record.token_or_digest(out) != out  # an h- name is never taken as already hashed


def test_a_name_equal_to_a_registered_secret_is_digested():
    redactor = launch_env.Redactor()
    redactor.add(SECRET)
    assert effective_record.token_or_digest(SECRET) == SECRET
    assert effective_record.token_or_digest(SECRET, redactor).startswith("h-")


def test_bounding_keeps_the_digest_sensitive_to_one_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(effective_record, "MAX_RECORD_BYTES", 2000)
    world = World(tmp_path / "a")
    world.write(world.global_dir / "opencode.json", {"provider": {"p%d" % i: {} for i in range(300)}})
    big = _build(world, inv=world.collect(compiled_doc=COMPILED, max_entries=10000))
    assert big["entries"]["truncated"] is True and big["entries"]["count"] >= 300
    assert big["entries_sha256"]
    other = World(tmp_path / "b")
    other.write(other.global_dir / "opencode.json", {"provider": {**{"p%d" % i: {} for i in range(299)}, "px": {}}})
    changed = _build(other, inv=other.collect(compiled_doc=COMPILED, max_entries=10000))
    assert changed["digest"] != big["digest"]


def test_serialise_redactor_is_a_no_op_for_a_seeded_world(tmp_path):
    world = World(tmp_path)
    world.write(world.global_dir / "opencode.json", {
        "provider": {SECRET: {"options": {"apiKey": SECRET}}},
        "mcp": {"s": {"type": "remote", "url": "https://u:" + SECRET + "@host.invalid/x", "headers": {"Authorization": SECRET}}},
        "instructions": ["https://u:" + SECRET + "@host.invalid/i"],
    })
    redactor = launch_env.Redactor()
    redactor.add(SECRET)
    record = _build(world, redactor=redactor)
    assert all(e["name"] != SECRET for e in record["entries"])
    data = effective_record.serialise(record, redactor)
    assert SECRET.encode() not in data
    assert json.loads(data) == record
    assert effective_record.digest(json.loads(data)) == record["digest"]


def test_policy_section_has_no_raw_hosts_or_paths():
    effective = _effective(allowed_hosts=("Gateway.Example.INVALID",), allowed_providers=("prov",))
    section = effective_record.policy_section(effective)
    assert all(h.startswith("h-") for h in section["allowed_hosts"])
    assert "example" not in json.dumps(section).lower()
    roots = effective_record.policy_section(_effective(repositories__allowed_roots=("/tmp/some/repo",)))
    assert all(r.startswith("h-") for r in roots["repositories.allowed_roots"])


# --------------------------------------------------------------- widening


def _policy(**kw):
    base = effective_record.policy_section(_effective())
    base.update(kw)
    return base


WIDEN_CASES = [
    ({"web_tools": "deny"}, {"web_tools": "allow"}, ["web_tools"]),
    ({"mcp.allowed": []}, {"mcp.allowed": ["s"]}, ["mcp.allowed"]),
    ({"allowed_hosts": ["h-a"]}, {"allowed_hosts": ["h-a", "h-b"]}, ["allowed_hosts"]),
    ({"logging.content": "metadata"}, {"logging.content": "summary"}, ["logging.content"]),
    ({"retention.runs_days": 30}, {"retention.runs_days": 90}, ["retention.runs_days"]),
    ({"retention.runs_days": 30}, {"retention.runs_days": None}, ["retention.runs_days"]),
    ({"network.allowed_addresses": ["h-a"]}, {"network.allowed_addresses": None}, ["network.allowed_addresses"]),
    ({"network.allowed_addresses": ["h-a"]}, {"network.allowed_addresses": ["h-a", "h-b"]}, ["network.allowed_addresses"]),
    ({"denied_hosts": ["h-a"]}, {"denied_hosts": []}, ["denied_hosts"]),
    ({"sharing": "deny"}, {"sharing": "manual"}, ["sharing"]),
    ({"cross_profile_fallback": False}, {"cross_profile_fallback": True}, ["cross_profile_fallback"]),
    ({"classification": "work"}, {"classification": "personal"}, ["classification"]),
]


@pytest.mark.parametrize("recorded,requested,expected", WIDEN_CASES)
def test_widened_per_field_class(recorded, requested, expected):
    got = effective_record.widened(_policy(**recorded), _policy(**requested))
    assert set(expected) <= set(got)


def test_equal_and_narrowing_policies_do_not_widen():
    assert effective_record.widened(_policy(), _policy()) == []
    assert effective_record.widened(_policy(web_tools="allow"), _policy(web_tools="deny")) == []
    assert effective_record.widened(_policy(classification="personal"), _policy(classification="work")) == []
    assert effective_record.widened(_policy(**{"mcp.allowed": ["s"]}), _policy(**{"mcp.allowed": []})) == []
    assert effective_record.widened(_policy(**{"network.allowed_addresses": None}), _policy(**{"network.allowed_addresses": ["h-a"]})) == []


def test_a_field_missing_from_the_recorded_policy_is_not_compared():
    recorded = _policy()
    del recorded["web_tools"]
    assert effective_record.widened(recorded, _policy(web_tools="allow")) == []


# ------------------------------------------------------ expected env names


@pytest.mark.parametrize("classification", ["work", "personal"])
@pytest.mark.parametrize("proxy", [False, True])
def test_expected_env_names_equal_the_built_environment(tmp_path, classification, proxy):
    ambient = {"PATH": "/bin", "HTTPS_PROXY": "http://p", "no_proxy": "x", "LC_ALL": "C", "OPENCODE_DB": "x", "HOME": "/h"}
    hardening = launch_env.hardening_env(
        classification=classification, sharing="deny", package_install="deny",
        npm_cache=tmp_path / "c", npm_userconfig=tmp_path / "u",
    )
    storage = {"XDG_STATE_HOME": str(tmp_path / "s"), "XDG_CACHE_HOME": str(tmp_path / "k"), "TMPDIR": str(tmp_path / "t")}
    kw = dict(ambient=ambient, compile_sidecar=_sidecar(), providers=[_view(proxy=proxy)], storage=storage, hardening=hardening)
    built = launch_env.build_env(
        resolver=_Resolver({"env:PROV_KEY": LAUNCH_SECRET}), data_dir=tmp_path / "d", config_path=tmp_path / "c.json",
        state_dir=tmp_path / "p", **kw,
    )
    assert launch_env.expected_env_names(state_dir_set=True, **kw) == built.names()
    no_state = launch_env.build_env(
        resolver=_Resolver({"env:PROV_KEY": LAUNCH_SECRET}), data_dir=tmp_path / "d", config_path=tmp_path / "c.json", **kw,
    )
    assert launch_env.expected_env_names(state_dir_set=False, **kw) == no_state.names()
