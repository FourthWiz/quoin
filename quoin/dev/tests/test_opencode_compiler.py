"""Tests for the native configuration compiler: evaluation findings, the
native document, post-build gates, the digest and sidecar, the output
location and `--check`.

Everything is offline. The environment, home directory, clock and pinned
version are always injected.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import os
import socket
import stat
import subprocess
from datetime import timedelta

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import (
    MANAGED_WORK,
    NOW,
    PROFILE_MINIMAL,
    PROFILE_PERSONAL,
    PROFILE_WORK,
    World,
    fixture,
    write_probe_records,
)
from test_opencode_native_schema import BUILTIN_PROVIDER_IDS
from quoin.opencode_adapter import compiler, config, jsonio, merge, names, paths, roles
from quoin.opencode_adapter.errors import ConfigErrors
from quoin.opencode_adapter.generate import ROLES
from quoin.opencode_adapter.qualification import QualificationResult

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"


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


def with_reasoning(ev):
    """The same evaluation with every model reporting reasoning support."""
    quals = {
        name: dataclasses.replace(q, reasoning_supported=q.state == "qualified")
        for name, q in ev.qualifications.items()
    }
    res = roles.resolve_all(ev.effective, quals, allow_unqualified=ev.allow_unqualified)
    return dataclasses.replace(ev, qualifications=quals, resolutions=res)


def codes(findings):
    return [f.code for f in findings]


def dumped(doc):
    return json.dumps(doc, sort_keys=True)


@pytest.fixture
def work(tmp_path):
    return World(tmp_path)


@pytest.fixture
def personal(tmp_path):
    return World(tmp_path, profile=PROFILE_PERSONAL)


# ================================================================ document


def test_work_document_shape_and_order(work):
    doc = compiler.build(work.evaluate()).document
    assert list(doc) == [
        "$schema", "model", "small_model", "agent", "share", "autoupdate",
        "enabled_providers", "provider", "experimental",
    ]
    assert doc["$schema"] == "https://opencode.ai/config.json"
    assert doc["share"] == "disabled" and doc["autoupdate"] is False
    assert doc["model"] == "quoin-corp-gw-b/example-vendor/example-planner"
    assert doc["small_model"] == "quoin-corp-gw-b/example-vendor/example-planner"
    assert list(doc["agent"]) == [names.role_agent_name(r) for r in ROLES] + ["title", "compaction"]
    assert doc["agent"]["title"] == {"model": doc["small_model"]}
    assert doc["agent"]["compaction"] == {"model": doc["small_model"]}
    assert doc["agent"]["quoin-implementer"] == {"model": "quoin-corp-gw/example-vendor/example-coder"}
    assert "summary" not in doc["agent"] and "summary" not in dumped(doc)
    assert "max" not in json.dumps(doc)


def test_deny_all_policy_first_and_allow_set_equals_providers(work):
    doc = compiler.build(work.evaluate()).document
    policies = doc["experimental"]["policies"]
    assert policies[0] == {"action": "provider.use", "effect": "deny", "resource": "*"}
    assert all(p["effect"] == "allow" and p["action"] == "provider.use" for p in policies[1:])
    assert [p["resource"] for p in policies[1:]] == sorted(doc["provider"])
    assert doc["enabled_providers"] == sorted(doc["provider"]) == ["quoin-corp-gw", "quoin-corp-gw-b"]
    for entry in doc["provider"].values():
        assert set(entry["whitelist"]) == set(entry["models"])


def test_package_per_endpoint_family_and_credential_placeholder(work):
    doc = compiler.build(work.evaluate()).document
    chat, responses = doc["provider"]["quoin-corp-gw"], doc["provider"]["quoin-corp-gw-b"]
    assert chat["npm"] == "@ai-sdk/openai-compatible"
    assert responses["npm"] == "@ai-sdk/openai"
    assert chat["options"]["apiKey"] == "{env:QUOIN_CORP_GW_API_KEY}"
    assert responses["options"]["apiKey"] == "{env:QUOIN_CORP_GW_B_API_KEY}"
    assert chat["options"]["baseURL"] == "https://gateway.example.invalid/v1"
    assert chat["name"] == "Corporate gateway"
    assert responses["name"] == "corp-gw-b"


def test_openrouter_profile_uses_the_builtin_id(personal):
    doc = compiler.build(personal.evaluate()).document
    assert list(doc["provider"]) == ["openrouter"] == doc["enabled_providers"]
    entry = doc["provider"]["openrouter"]
    assert entry["npm"] == "@openrouter/ai-sdk-provider"
    assert entry["options"]["apiKey"] == "{env:QUOIN_OPENROUTER_API_KEY}"
    assert doc["model"] == "openrouter/example-vendor/example-model"


def test_openrouter_kind_with_another_quoin_id_maps_to_the_builtin_id(tmp_path):
    profile = fixture(PROFILE_PERSONAL)
    profile["providers"]["or-main"] = profile["providers"].pop("openrouter")
    profile["models"]["personal-model"]["provider"] = "or-main"
    world = World(tmp_path, profile=profile)
    result = compiler.build(world.evaluate())
    assert list(result.document["provider"]) == ["openrouter"]
    assert result.document["provider"]["openrouter"]["options"]["apiKey"] == "{env:QUOIN_OR_MAIN_API_KEY}"
    assert result.sidecar["native_provider"] == {"or-main": "openrouter"}
    assert result.sidecar["credential_env"] == {"QUOIN_OR_MAIN_API_KEY": "or-main"}


def test_two_openrouter_kind_providers_conflict(tmp_path):
    profile = fixture(PROFILE_PERSONAL)
    second = copy.deepcopy(profile["providers"]["openrouter"])
    profile["providers"]["or-second"] = second
    profile["models"]["second-model"] = {
        "provider": "or-second", "model_id": "example-vendor/example-two",
        "qualification_ref": "local:second-model",
    }
    profile["roles"] = {"planner": {"model": "second-model"}}
    world = World(tmp_path, profile=profile)
    ev = world.evaluate()
    conflicts = [f for f in ev.findings if f.code == "native-id-conflict"]
    assert sorted(f.subject for f in conflicts) == [("openrouter",), ("or-second",)]
    assert all(f.blocking for f in conflicts)
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(ev)
    with pytest.raises(ValueError):
        compiler.build_document(ev)


@pytest.mark.parametrize("quoin_id", ["openai", "azure", "anthropic", "openrouter"])
def test_builtin_looking_ids_get_the_reserved_prefix(tmp_path, quoin_id):
    profile = fixture(PROFILE_MINIMAL)
    profile["providers"] = {quoin_id: profile["providers"].pop("local-gw")}
    profile["models"]["local-model"]["provider"] = quoin_id
    world = World(tmp_path, profile=profile)
    ev = world.evaluate()
    assert not [f for f in compiler.all_findings(ev) if f.blocking]
    result = compiler.build(ev)
    doc = result.document
    native = "quoin-" + quoin_id
    assert native not in BUILTIN_PROVIDER_IDS
    assert list(doc["provider"]) == doc["enabled_providers"] == [native]
    assert doc["model"].startswith(native + "/") and doc["small_model"].startswith(native + "/")
    assert [p["resource"] for p in doc["experimental"]["policies"][1:]] == [native]
    assert doc["provider"][native]["options"]["apiKey"] == "{env:%s}" % config.provider_env_name(quoin_id)
    assert result.sidecar["native_provider"] == {quoin_id: native}


def test_openai_compatible_openrouter_and_openrouter_kind_do_not_conflict(tmp_path):
    profile = fixture(PROFILE_PERSONAL)
    profile["providers"]["or-main"] = profile["providers"].pop("openrouter")
    profile["providers"]["openrouter"] = {
        "kind": "openai-compatible", "endpoint_family": "chat-completions",
        "base_url": "https://gateway.example.invalid/v1", "credential_ref": "env:GW_KEY",
    }
    profile["models"]["personal-model"]["provider"] = "or-main"
    profile["models"]["gw-model"] = {
        "provider": "openrouter", "model_id": "example-vendor/example-gw",
        "qualification_ref": "local:gw-model",
    }
    profile["roles"] = {"planner": {"model": "gw-model"}}
    world = World(tmp_path, profile=profile)
    ev = world.evaluate()
    assert not [f for f in ev.findings if f.blocking]
    doc = compiler.build(ev).document
    assert sorted(doc["provider"]) == ["openrouter", "quoin-openrouter"]
    assert doc["provider"]["openrouter"]["npm"] == "@openrouter/ai-sdk-provider"
    assert doc["provider"]["quoin-openrouter"]["npm"] == "@ai-sdk/openai-compatible"


def test_empty_display_name_falls_back_to_the_quoin_id(tmp_path):
    profile = fixture(PROFILE_MINIMAL)
    profile["providers"]["local-gw"]["name"] = ""
    world = World(tmp_path, profile=profile)
    doc = compiler.build(world.evaluate()).document
    assert doc["provider"]["quoin-local-gw"]["name"] == "local-gw"


@pytest.mark.parametrize("model_id", ["has space", "trailing\n", "tab\there"])
def test_unwritable_model_id_is_a_blocking_finding(tmp_path, model_id):
    profile = fixture(PROFILE_MINIMAL)
    profile["models"]["local-model"]["model_id"] = model_id
    world = World(tmp_path, profile=profile)
    ev = world.evaluate()
    found = [f for f in ev.findings if f.code == "native-ref-invalid"]
    assert [f.subject for f in found] == [("local-model",)] and found[0].blocking
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(ev)


def test_responses_family_kill_switch(work, monkeypatch):
    monkeypatch.setattr(compiler, "RESPONSES_FAMILY_VERIFIED", False)
    ev = work.evaluate()
    found = [f for f in ev.findings if f.code == "endpoint-family-unverified"]
    assert [f.subject for f in found] == [("corp-gw-b",)] and found[0].blocking
    assert [f for f in compiler.compile_blockers(ev) if f.code == "endpoint-family-unverified"] == found
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(ev)
    monkeypatch.setattr(compiler, "RESPONSES_FAMILY_VERIFIED", True)
    assert not [f for f in work.evaluate().findings if f.code == "endpoint-family-unverified"]


def test_standing_findings_never_block(work):
    ev = work.evaluate()
    assert [(f.code, f.blocking, f.subject) for f in ev.findings] == [
        ("later-layers-can-override", False, ()), ("policies-supplementary", False, ()),
    ]
    assert compiler.compile_blockers(ev) == ()


def test_effort_variants_for_openai_compatible_chat(work):
    ev = with_reasoning(work.evaluate())
    doc = compiler.build(ev).document
    coder = doc["provider"]["quoin-corp-gw"]["models"]["example-vendor/example-coder"]
    assert coder["variants"] == {
        "quoin-low": {"reasoningEffort": "low"},
        "quoin-medium": {"reasoningEffort": "medium"},
    }
    assert doc["agent"]["quoin-gate"]["variant"] == "quoin-low"
    assert doc["agent"]["quoin-implementer"]["variant"] == "quoin-medium"
    assert doc["agent"]["quoin-investigator"]["variant"] == "quoin-medium"
    # The responses family has no recorded option mapping, and max is never mapped.
    planner = doc["provider"]["quoin-corp-gw-b"]["models"]["example-vendor/example-planner"]
    assert "variants" not in planner
    assert "variant" not in doc["agent"]["quoin-planner"]
    assert "variant" not in doc["agent"]["quoin-architect"]
    assert "title" in doc["agent"] and "variant" not in doc["agent"]["title"]
    assert "max" not in json.dumps(doc)


def test_effort_variants_for_openrouter(personal):
    doc = compiler.build(with_reasoning(personal.evaluate())).document
    model = doc["provider"]["openrouter"]["models"]["example-vendor/example-model"]
    assert model["variants"]["quoin-high"] == {"reasoning": {"effort": "high"}}
    assert set(model["variants"]) == {"quoin-low", "quoin-medium", "quoin-high"}
    assert doc["agent"]["quoin-architect"]["variant"] == "quoin-high"


def test_real_shaped_records_emit_no_variants(work):
    doc = compiler.build(work.evaluate()).document
    assert "variant" not in json.dumps(doc["agent"])
    assert all("variants" not in m for p in doc["provider"].values() for m in p["models"].values())


def test_missing_agent_file_blocks_and_names_the_role(work):
    (work.root / ".opencode" / "agents" / "quoin-critic.md").unlink()
    ev = work.evaluate()
    found = [f for f in ev.findings if f.code == "agent-file-missing"]
    assert [f.subject for f in found] == [("critic",)] and found[0].blocking
    with pytest.raises(compiler.CompileBlocked) as info:
        compiler.build(ev)
    assert [f.code for f in info.value.findings] == ["agent-file-missing"]


def test_agent_stub_must_be_a_regular_file(work):
    path = work.root / ".opencode" / "agents" / "quoin-gate.md"
    path.unlink()
    path.mkdir()
    assert [f.subject for f in work.evaluate().findings if f.code == "agent-file-missing"] == [("gate",)]


def test_excluded_unreferenced_provider_is_not_emitted(tmp_path):
    project = fixture("valid/project-work.json")
    world = World(tmp_path, project=project)
    ev = world.evaluate()
    # corp-gw-b is excluded by the project allow list, so the planner-side
    # roles are blocked and nothing is compiled.
    assert ev.effective.excluded_providers == {"corp-gw-b": "not-allowed"}
    with pytest.raises(compiler.CompileBlocked) as info:
        compiler.build(ev)
    assert {f.code for f in info.value.findings} == {"role-blocked"}
    with pytest.raises(ValueError):
        compiler.build_document(ev)


def test_unreferenced_provider_stays_out_of_the_document(tmp_path):
    profile = fixture(PROFILE_WORK)
    profile["models"]["work-planner"]["provider"] = "corp-gw"
    world = World(tmp_path, profile=profile)
    doc = compiler.build(world.evaluate()).document
    assert list(doc["provider"]) == ["quoin-corp-gw"]
    assert doc["enabled_providers"] == ["quoin-corp-gw"]


def test_blocked_role_refuses_build(tmp_path):
    world = World(tmp_path, managed=MANAGED_WORK)
    ev = world.evaluate()
    assert "role-blocked" in codes(compiler.all_findings(ev))
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(ev)


def test_missing_classification_blocks(tmp_path):
    world = World(tmp_path, project=fixture("valid/project-unclassified.json"))
    ev = world.evaluate()
    assert "missing-classification" in codes(compiler.all_findings(ev))
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(ev)


def test_all_findings_order(work):
    ev = work.evaluate()
    got = compiler.all_findings(ev)
    assert got == tuple(ev.effective.findings) + tuple(ev.resolutions.findings) + tuple(ev.findings)


def test_evaluate_lets_load_errors_propagate(tmp_path):
    world = World(tmp_path, records=False)
    with pytest.raises(ConfigErrors):
        compiler.evaluate(
            project_root=world.root, profile="nope", env=world.env, home=world.home, now=NOW
        )


def test_evaluation_repr_and_equality_hide_the_project_root(work):
    ev = work.evaluate()
    assert str(work.root) not in repr(ev)


def test_module_imports_are_limited():
    tree = ast.parse((SRC_DIR / "compiler.py").read_text(encoding="utf-8"))
    relative = set()
    external = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                relative |= {node.module} if node.module else {a.name for a in node.names}
            else:
                external.add(node.module)
        elif isinstance(node, ast.Import):
            external |= {a.name for a in node.names}
    assert relative <= {
        "config", "errors", "jsonio", "merge", "names", "paths", "qualification", "roles",
        "schema_check", "generate",
    }
    assert external <= {
        "__future__", "functools", "hashlib", "json", "os", "re", "dataclasses", "pathlib", "typing",
    }


# ==================================================================== gates


def _patch_document(monkeypatch, mutate):
    real = compiler.build_document

    def patched(ev):
        doc = real(ev)
        mutate(doc)
        return doc

    monkeypatch.setattr(compiler, "build_document", patched)


def _gate_failure(work, monkeypatch, mutate):
    ev = work.evaluate()
    _patch_document(monkeypatch, mutate)
    with pytest.raises(compiler.CompileGateError) as info:
        compiler.build(ev)
    return info.value


def _first(doc, native="quoin-corp-gw"):
    return doc["provider"][native]


def test_placeholder_gate_catches_a_smuggled_file_reference(work, monkeypatch):
    ev = work.evaluate()
    monkeypatch.setattr(
        merge, "provider_base_url", lambda view: "https://gateway.example.invalid/{file:/etc/hostname}"
    )
    with pytest.raises(compiler.CompileGateError) as info:
        compiler.build(ev)
    assert info.value.gate == "placeholder"
    assert "etc/hostname" not in str(info.value) and "gateway" not in str(info.value)


def test_placeholder_gate_catches_env_in_a_models_key(work, monkeypatch):
    def mutate(doc):
        models = _first(doc)["models"]
        models["x{env:HOME}"] = models.pop("example-vendor/example-coder")

    err = _gate_failure(work, monkeypatch, mutate)
    assert err.gate == "placeholder" and "HOME" not in str(err)


def test_placeholder_gate_catches_env_in_a_whitelist_entry(work, monkeypatch):
    err = _gate_failure(
        work, monkeypatch, lambda doc: _first(doc)["whitelist"].append("y{env:SECRET_NAME}")
    )
    assert err.gate == "placeholder" and "SECRET_NAME" not in str(err)


def test_placeholder_gate_catches_a_foreign_credential_placeholder(work, monkeypatch):
    def mutate(doc):
        _first(doc)["options"]["apiKey"] = "{env:QUOIN_OTHER_API_KEY}"

    assert _gate_failure(work, monkeypatch, mutate).gate == "placeholder"


def test_placeholder_gate_catches_dollar_brace_and_replace_markers(work, monkeypatch):
    assert _gate_failure(
        work, monkeypatch, lambda doc: _first(doc).__setitem__("name", "n${HOME}")
    ).gate == "placeholder"


def test_allowlist_gate_catches_an_enabled_provider_without_a_rule(work, monkeypatch):
    def mutate(doc):
        doc["experimental"]["policies"].pop()

    assert _gate_failure(work, monkeypatch, mutate).gate == "allowlist"


def test_allowlist_gate_catches_an_extra_enabled_entry(work, monkeypatch):
    assert _gate_failure(
        work, monkeypatch, lambda doc: doc["enabled_providers"].append("openrouter")
    ).gate == "allowlist"


def test_allowlist_gate_catches_a_misplaced_deny_all(work, monkeypatch):
    def mutate(doc):
        policies = doc["experimental"]["policies"]
        policies.append(policies.pop(0))

    assert _gate_failure(work, monkeypatch, mutate).gate == "allowlist"


def test_allowlist_gate_rejects_a_provider_outside_the_effective_set(tmp_path):
    world = World(tmp_path, managed=MANAGED_WORK)
    profile_two = world.profile
    ev = world.evaluate()
    assert ev.effective.effective_providers == ("corp-gw",)
    doc = {
        "provider": {"quoin-corp-gw-b": {}},
        "enabled_providers": ["quoin-corp-gw-b"],
        "experimental": {"policies": [
            {"action": "provider.use", "effect": "deny", "resource": "*"},
            {"action": "provider.use", "effect": "allow", "resource": "quoin-corp-gw-b"},
        ]},
    }
    with pytest.raises(compiler.CompileGateError) as info:
        compiler._gate_allowlist(doc, ev)
    assert info.value.gate == "allowlist" and profile_two


def test_reference_gate_catches_a_dangling_agent_model(work, monkeypatch):
    def mutate(doc):
        doc["agent"]["quoin-gate"]["model"] = "quoin-corp-gw/missing-model"

    assert _gate_failure(work, monkeypatch, mutate).gate == "reference"


def test_reference_gate_catches_an_unknown_variant(work, monkeypatch):
    def mutate(doc):
        doc["agent"]["quoin-gate"]["variant"] = "quoin-high"

    assert _gate_failure(work, monkeypatch, mutate).gate == "reference"


def test_reference_gate_catches_an_empty_variant(work, monkeypatch):
    def mutate(doc):
        _first(doc)["models"]["example-vendor/example-coder"]["variants"] = {"quoin-low": {}}

    assert _gate_failure(work, monkeypatch, mutate).gate == "reference"


def test_constants_gate_catches_a_changed_share(work, monkeypatch):
    err = _gate_failure(work, monkeypatch, lambda doc: doc.__setitem__("share", "auto"))
    assert err.gate == "constants"


def test_constants_gate_catches_autoupdate(work, monkeypatch):
    assert _gate_failure(
        work, monkeypatch, lambda doc: doc.__setitem__("autoupdate", "notify")
    ).gate == "constants"


def test_schema_gate_catches_an_unknown_key_and_a_summary_agent(work, monkeypatch):
    assert _gate_failure(work, monkeypatch, lambda doc: doc.__setitem__("extra", 1)).gate == "schema"
    assert _gate_failure(
        work, monkeypatch, lambda doc: doc["agent"].__setitem__("summary", {"model": doc["model"]})
    ).gate == "schema"


def test_gate_messages_are_fixed_text():
    assert set(compiler.GATE_MESSAGES) == {"schema", "placeholder", "allowlist", "reference", "constants"}
    for gate, message in compiler.GATE_MESSAGES.items():
        assert str(compiler.CompileGateError(gate)) == message
    with pytest.raises(ValueError):
        compiler.CompileGateError("other")


# ================================================================== digest


def test_build_is_byte_identical_across_runs(tmp_path):
    world = World(tmp_path)
    one, two = compiler.build(world.evaluate()), compiler.build(world.evaluate())
    assert one.native_bytes == two.native_bytes
    assert one.sidecar_bytes == two.sidecar_bytes
    assert one.digest == two.digest and one.digest.startswith("sha256:") and len(one.digest) == 71


def _digest(world, reasoning=False, **kw):
    ev = world.evaluate(**kw)
    if reasoning:
        ev = with_reasoning(ev)
    return compiler.build(ev).digest


def _profile_edit(fn, reasoning=False):
    def apply(world):
        fn(world.profile)
        world.write()
        world.write_records()

    return apply, reasoning


def _set(*path_and_value):
    *path, value = path_and_value

    def edit(profile):
        node = profile
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return edit


MUTATIONS = {
    "role-model": _profile_edit(_set("roles", "gate", "model", "work-planner")),
    "default-model": _profile_edit(_set("default_model", "work-planner")),
    "auxiliary-model": _profile_edit(_set("auxiliary_model", "work-coder")),
    "display-name": _profile_edit(_set("providers", "corp-gw", "name", "Another name")),
    "base-url-path": _profile_edit(
        _set("providers", "corp-gw", "base_url", "https://gateway.example.invalid/v2")
    ),
    "credential-ref": _profile_edit(
        _set("providers", "corp-gw-b", "credential_ref", "env:ANOTHER_GW_B_TOKEN")
    ),
    "denied-host": _profile_edit(
        lambda p: p["policy"]["denied_hosts"].append("other.example.invalid")
    ),
    "sharing": _profile_edit(_set("policy", "sharing", "manual")),
    "external-writes": _profile_edit(_set("policy", "external_writes", "deny")),
    "isolation": _profile_edit(_set("policy", "isolation", "convenience")),
    "integrations-enabled": _profile_edit(_set("integrations", "enabled", ["jira"])),
    "integrations-mode": _profile_edit(_set("integrations", "mode", "approved-write")),
    "limit-run-seconds": _profile_edit(_set("limits", "max_run_seconds", 1800)),
    "limit-tool-calls": _profile_edit(_set("limits", "max_tool_calls", 150)),
    "limit-context": _profile_edit(_set("limits", "max_context_tokens", 64000)),
    "limit-output": _profile_edit(_set("limits", "max_output_tokens", 4096)),
    "limit-retries": _profile_edit(_set("limits", "max_transient_retries", 1)),
    "role-effort": (_profile_edit(_set("roles", "implementer", "effort", "low"))[0], True),
}


def _baseline(tmp_path, reasoning=False):
    return _digest(World(tmp_path / "baseline"), reasoning=reasoning)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_each_input_changes_the_digest(tmp_path, name):
    apply, reasoning = MUTATIONS[name]
    world = World(tmp_path / "mutated")
    apply(world)
    assert _digest(world, reasoning=reasoning) != _baseline(tmp_path, reasoning=reasoning), name


def test_base_url_host_change_changes_the_digest(tmp_path):
    world = World(tmp_path / "mutated")
    world.profile["providers"]["corp-gw"]["base_url"] = "https://gateway-new.example.invalid/v1"
    world.profile["policy"]["allowed_hosts"] = [
        "gateway-new.example.invalid", "gateway-b.example.invalid"
    ]
    world.write()
    world.write_records()
    assert _digest(world) != _baseline(tmp_path)


def test_no_op_project_narrowing_changes_the_digest(tmp_path):
    world = World(tmp_path / "mutated")
    world.project["policy"] = {"allowed_providers": ["corp-gw", "corp-gw-b"]}
    world.write()
    assert _digest(world) != _baseline(tmp_path)


def test_managed_layer_presence_changes_the_digest(tmp_path):
    world = World(tmp_path / "mutated", managed={"schema_version": 1, "policy": {"sharing": "deny"}})
    assert _digest(world) != _baseline(tmp_path)


def test_probed_at_changes_the_digest(tmp_path):
    world = World(tmp_path / "mutated")
    world.write_records(now=NOW - timedelta(days=2))
    assert _digest(world) != _baseline(tmp_path)


def test_pinned_version_changes_the_digest(work):
    ev = work.evaluate()
    other = dataclasses.replace(ev, pinned_version="1.18.33")
    assert compiler.build(other).digest != compiler.build(ev).digest


def test_whitespace_only_profile_rewrite_keeps_the_digest(tmp_path):
    world = World(tmp_path / "rewritten")
    before = _digest(world)
    (paths.profile_path("work", world.env, world.home)).write_text(
        json.dumps(world.profile, indent=7, sort_keys=False) + "\n\n", encoding="utf-8"
    )
    assert _digest(world) == before == _baseline(tmp_path)


def test_unqualified_build_differs_and_is_not_launchable(tmp_path):
    qualified = compiler.build(World(tmp_path / "q").evaluate())
    world = World(tmp_path / "u")
    world.write_records(now=NOW - timedelta(days=40))
    ev = world.evaluate(allow_unqualified=True)
    assert ev.resolutions.unqualified_models
    result = compiler.build(ev)
    assert result.digest != qualified.digest
    assert result.launchable is False and result.sidecar["launchable"] is False
    assert result.sidecar["unqualified_models"] == list(ev.resolutions.unqualified_models)
    assert all(f.code != "role-unqualified" for f in compiler.compile_blockers(ev))
    with pytest.raises(compiler.CompileBlocked):
        compiler.build(world.evaluate())


def test_digest_input_holds_no_seeded_secret_forms(work):
    ev = work.evaluate()
    doc = compiler.build_document(ev)
    blob = compiler.digest_input(ev, doc)
    assert "endpoints" in blob
    text = json.dumps(blob)
    for form in helpers.secret_forms(helpers.SEEDED_SECRET):
        assert form not in text


# ================================================================== sidecar


def test_sidecar_fields_and_privacy(work):
    result = compiler.build(work.evaluate())
    side = result.sidecar
    assert side["sidecar_format"] == 1 and side["digest"] == result.digest
    assert side["profile"] == "work" and side["classification"] == "work"
    assert side["credential_env"] == {
        "QUOIN_CORP_GW_API_KEY": "corp-gw", "QUOIN_CORP_GW_B_API_KEY": "corp-gw-b",
    }
    for env_name, provider_id in side["credential_env"].items():
        assert env_name == config.provider_env_name(provider_id)
    assert side["native_provider"] == {"corp-gw": "quoin-corp-gw", "corp-gw-b": "quoin-corp-gw-b"}
    assert side["launch_requirements"] == {
        "config_path_env": "OPENCODE_CONFIG",
        "protected_keys": [
            "$schema", "model", "small_model", "agent", "share", "autoupdate",
            "enabled_providers", "provider", "experimental.policies",
        ],
        "credential_env_required": True,
    }
    assert [r["role"] for r in side["role_resolutions"]] == list(ROLES)
    summary = [r for r in side["auxiliary_resolutions"] if r["role"] == "summary"][0]
    assert summary["emitted"] is False and summary["agent"] == "summary"
    assert all(r["emitted"] for r in side["role_resolutions"])
    text = result.sidecar_bytes.decode("utf-8")
    for needle in ("example.invalid", "http", "keychain", "work-gateway", "env:", str(work.root), str(work.tmp)):
        assert needle not in text, needle
    assert side["native_sha256"] == hashlib.sha256(result.native_bytes).hexdigest()


def test_serialization_forms(work):
    result = compiler.build(work.evaluate())
    assert result.native_bytes.endswith(b"}\n") and result.native_bytes.startswith(b'{\n  "$schema"')
    assert json.loads(result.native_bytes) == result.document
    assert json.loads(result.sidecar_bytes) == result.sidecar
    assert list(json.loads(result.native_bytes)) == list(result.document)
    assert result.sidecar_bytes == (
        json.dumps(result.sidecar, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    assert "digest" in repr(result) and "provider" not in repr(result)
