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


# ================================================================== output


@pytest.fixture
def umask0():
    old = os.umask(0)
    yield
    os.umask(old)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def tree_state(root):
    """Paths, bytes and modes of everything below `root`."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = (mode(path), path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return out


def refusal(ev, **kw):
    with pytest.raises(compiler.OutputRefused) as info:
        compiler.resolve_output_dir(ev, env=kw.pop("env", None) or {}, home=kw.pop("home", None) or "/nonexistent", **kw)
    return info.value


def test_default_location_shape(work):
    ev = work.evaluate()
    target = compiler.resolve_output_dir(ev, output=None, env=work.env, home=work.home)
    assert target == work.tmp / "state" / "quoin" / "opencode" / "work" / paths.project_key(work.root)


def test_output_inside_the_project_is_refused_and_nothing_is_written(work):
    ev = work.evaluate()
    before = tree_state(work.tmp)
    for target in (work.root, work.root / "out", work.root / "deep" / "er" / "still"):
        err = refusal(ev, output=target, home=work.home)
        assert err.code == "inside-project" and str(work.root) not in str(err)
    assert tree_state(work.tmp) == before


def test_output_inside_a_nested_git_worktree_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    world = World(tmp_path / "w", root=repo / "packages" / "app")
    ev = world.evaluate()
    assert refusal(ev, output=repo / "elsewhere", home=world.home).code == "inside-project"
    assert refusal(ev, output=repo, home=world.home).code == "inside-project"
    outside = tmp_path / "outside" / "out"
    assert compiler.resolve_output_dir(ev, output=outside, env=world.env, home=world.home) == outside


def test_output_through_a_symlink_into_the_project_is_refused(work):
    link = work.tmp / "alias"
    link.symlink_to(work.root)
    ev = work.evaluate()
    assert refusal(ev, output=link / "out", home=work.home).code == "inside-project"
    assert refusal(ev, output=link, home=work.home).code == "inside-project"


def test_output_through_a_differently_cased_alias_is_refused(work):
    upper = work.tmp / "PROJECT"
    if not upper.exists():
        pytest.skip("the temporary filesystem is case sensitive")
    ev = work.evaluate()
    assert refusal(ev, output=upper / "out", home=work.home).code == "inside-project"


def test_state_home_inside_the_project_is_refused(work):
    work.env["XDG_STATE_HOME"] = str(work.root / "state")
    ev = work.evaluate()
    err = refusal(ev, output=None, env=work.env, home=work.home)
    assert err.code == "inside-project" and "XDG_STATE_HOME" in err.fix and "--output" in err.fix


def test_a_checkout_at_home_does_not_capture_the_default_location(tmp_path):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    world = World(tmp_path, root=home / "work" / "proj")
    del world.env["XDG_STATE_HOME"]
    ev = world.evaluate()
    target = compiler.resolve_output_dir(ev, output=None, env=world.env, home=world.home)
    assert str(target).startswith(str(home / ".local" / "state"))
    result = compiler.build(ev)
    compiler.write(result, target)
    assert (target / "opencode.json").is_file()


def test_output_argument_shapes(work):
    ev = work.evaluate()
    a_file = work.tmp / "a-file"
    a_file.write_text("x", encoding="utf-8")
    assert refusal(ev, output=a_file, home=work.home).code == "not-a-directory"
    err = refusal(ev, output=work.tmp / "new" / "opencode.json", home=work.home)
    assert err.code == "names-a-file" and "names a directory" in err.fix
    assert not (work.tmp / "new").exists()
    # An existing directory whose name ends in .json is still a directory.
    weird = work.tmp / "weird.json"
    weird.mkdir()
    assert compiler.resolve_output_dir(ev, output=weird, env=work.env, home=work.home) == weird


def test_output_refusal_messages_are_closed():
    assert set(compiler.OUTPUT_MESSAGES) == {"not-a-directory", "names-a-file", "inside-project"}
    with pytest.raises(ValueError):
        compiler.OutputRefused("other")


def test_write_makes_private_files_and_directories(work, umask0):
    result = compiler.build(work.evaluate())
    target = work.tmp / "out" / "nested"
    written = compiler.write(result, target)
    assert written == target / "opencode.json"
    assert mode(target) == 0o700 and mode(target.parent) == 0o700
    assert mode(target / "opencode.json") == 0o600 and mode(target / "quoin-compile.json") == 0o600
    assert (target / "opencode.json").read_bytes() == result.native_bytes
    assert (target / "quoin-compile.json").read_bytes() == result.sidecar_bytes


def test_write_refuses_a_group_writable_existing_directory(work):
    result = compiler.build(work.evaluate())
    target = work.tmp / "loose"
    target.mkdir()
    os.chmod(target, 0o770)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        compiler.write(result, target)
    assert list(target.iterdir()) == []


def test_write_replaces_an_existing_file_privately(work):
    result = compiler.build(work.evaluate())
    target = work.tmp / "out"
    compiler.write(result, target)
    os.chmod(target / "opencode.json", 0o644)
    compiler.write(result, target)
    assert mode(target / "opencode.json") == 0o600


@pytest.fixture
def written(work):
    ev = work.evaluate()
    target = work.tmp / "out"
    compiler.write(compiler.build(ev), target)
    return work, ev, target


def test_check_fresh_is_ok(written):
    _, ev, target = written
    assert compiler.check(ev, target) == compiler.CheckResult(True, ())


def test_check_reports_a_changed_profile_as_stale(written):
    work, _, target = written
    work.profile["providers"]["corp-gw"]["name"] = "Renamed"
    work.write()
    assert compiler.check(work.evaluate(), target) == compiler.CheckResult(False, ("stale",))


def test_check_reports_a_changed_byte_as_stale(written):
    _, ev, target = written
    data = bytearray((target / "opencode.json").read_bytes())
    data[-3] ^= 0x01
    (target / "opencode.json").write_bytes(bytes(data))
    assert compiler.check(ev, target).reasons == ("stale",)


def test_check_reports_an_edited_sidecar_as_stale(written):
    _, ev, target = written
    (target / "quoin-compile.json").write_text("not json", encoding="utf-8")
    assert compiler.check(ev, target).reasons == ("stale",)
    (target / "quoin-compile.json").write_text("[1]", encoding="utf-8")
    assert compiler.check(ev, target).reasons == ("stale",)


def test_check_missing_files(written):
    _, ev, target = written
    (target / "quoin-compile.json").unlink()
    assert compiler.check(ev, target) == compiler.CheckResult(False, ("missing",))
    assert compiler.check(ev, target / "nowhere").reasons == ("missing",)


def test_check_symlinked_output_counts_as_missing(written):
    _, ev, target = written
    real = target / "real.json"
    (target / "opencode.json").rename(real)
    (target / "opencode.json").symlink_to(real)
    assert compiler.check(ev, target).reasons == ("missing",)


def test_check_permissions(written):
    _, ev, target = written
    os.chmod(target / "opencode.json", 0o644)
    assert compiler.check(ev, target).reasons == ("not-private",)
    os.chmod(target / "opencode.json", 0o600)
    os.chmod(target / "quoin-compile.json", 0o640)
    assert compiler.check(ev, target).reasons == ("not-private",)
    os.chmod(target / "quoin-compile.json", 0o600)
    os.chmod(target, 0o770)
    assert compiler.check(ev, target).reasons == ("not-private",)
    os.chmod(target, 0o700)
    assert compiler.check(ev, target).ok


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_check_reports_a_readable_containing_directory_as_not_private(written, mode):
    _, ev, target = written
    os.chmod(target, mode)
    assert compiler.check(ev, target).reasons == ("not-private",)


def test_check_flag_mismatch_is_reported_without_a_rebuild(tmp_path):
    world = World(tmp_path)
    world.write_records(now=NOW - timedelta(days=40))
    ev = world.evaluate(allow_unqualified=True)
    target = tmp_path / "out"
    compiler.write(compiler.build(ev), target)
    assert compiler.check(ev, target).ok
    strict = world.evaluate()
    result = compiler.check(strict, target)
    assert result == compiler.CheckResult(False, ("flag-mismatch",))


def test_check_reads_stale_once_the_unqualified_models_are_qualified(tmp_path):
    world = World(tmp_path)
    world.write_records(now=NOW - timedelta(days=40))
    ev = world.evaluate(allow_unqualified=True)
    assert ev.resolutions.unqualified_models
    target = tmp_path / "out"
    compiler.write(compiler.build(ev), target)
    world.write_records()  # fresh records: every model is qualified now
    assert compiler.check(world.evaluate(), target) == compiler.CheckResult(False, ("stale",))


def test_check_flag_given_but_not_needed_is_not_a_mismatch(written):
    work, _, target = written
    assert compiler.check(work.evaluate(allow_unqualified=True), target).ok


def test_check_lets_a_blocked_rebuild_propagate(written):
    work, _, target = written
    (work.root / ".opencode" / "agents" / "quoin-gate.md").unlink()
    with pytest.raises(compiler.CompileBlocked):
        compiler.check(work.evaluate(), target)


def test_check_never_writes(written):
    work, ev, target = written
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in target.iterdir()}
    dir_before = tree_state(work.tmp)
    compiler.check(ev, target)
    work.profile["providers"]["corp-gw"]["name"] = "Renamed"
    work.write()
    compiler.check(work.evaluate(), target)
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in target.iterdir()} == before
    assert [k for k in tree_state(target)] == sorted(before)
    assert dir_before.keys() <= tree_state(work.tmp).keys()


def test_compile_and_check_leave_the_installed_project_untouched(tmp_path):
    import io

    from quoin.opencode_adapter import install

    world = World(tmp_path, agents=False)
    out_sink, err_sink = io.StringIO(), io.StringIO()
    code = install.run_install(str(world.root), helpers.SOURCE_DIR, None, False, out_sink, err_sink)
    assert code == 0, out_sink.getvalue() + err_sink.getvalue()
    before = tree_state(world.root)
    assert any(name.endswith("opencode.jsonc") for name in before)
    ev = world.evaluate()
    assert not [f for f in ev.findings if f.code == "agent-file-missing"]
    target = compiler.resolve_output_dir(ev, output=None, env=world.env, home=world.home)
    compiler.write(compiler.build(ev), target)
    compiler.check(ev, target)
    assert tree_state(world.root) == before
    assert target.is_dir() and not str(target).startswith(str(world.root))


# ================================================================== goldens

GOLDEN_DIR = helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "compiled"


GOLDEN_NAMES = ["work", "work-variants", "personal"]


def _report_reasoning_support(world):
    """Rewrite every qualification record so the model reports supported
    reasoning parameters, as a gateway that accepts an effort setting would."""
    for model in world.effective().models.values():
        target = paths.qualification_path(model.qualification_ref[len("local:"):], world.env, world.home)
        record = json.loads(target.read_text(encoding="utf-8"))
        record["capabilities"]["reasoning_parameters"] = {
            "status": "supported", "source": "observed", "value": None, "detail": None,
        }
        target.write_text(json.dumps(record), encoding="utf-8")


def golden_bytes(name, tmp_path):
    """The compiled document for a golden scenario, as written to disk.

    The work scenario is the work profile with a classified project that
    narrows nothing; the personal scenario is the personal profile; the
    work-variants scenario is the work scenario with every model's record
    reporting supported reasoning parameters, so effort variants are emitted.
    All use qualification records built by the probe itself at the fixed
    clock. To regenerate a golden after an intended change, call this
    function from a REPL (with the tests directory on `sys.path`) and write
    the result to `quoin/adapters/opencode/fixtures/compiled/<name>.opencode.json`.
    """
    profile = {"work": PROFILE_WORK, "work-variants": PROFILE_WORK, "personal": PROFILE_PERSONAL}[name]
    world = World(tmp_path, profile=profile)
    if name == "work-variants":
        _report_reasoning_support(world)
    return compiler.build(world.evaluate()).native_bytes


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_goldens_are_current_and_byte_identical(tmp_path, name):
    committed = (GOLDEN_DIR / ("%s.opencode.json" % name)).read_bytes()
    assert golden_bytes(name, tmp_path / "first") == committed
    assert golden_bytes(name, tmp_path / "second") == committed


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_goldens_validate_against_the_subset_schema(name):
    from quoin.opencode_adapter import schema_check

    schema = json.loads(paths.native_schema_path().read_text(encoding="utf-8"))
    doc = json.loads((GOLDEN_DIR / ("%s.opencode.json" % name)).read_text(encoding="utf-8"))
    assert schema_check.validate(doc, schema, "config") == []


def test_the_variant_golden_really_carries_variants():
    doc = json.loads((GOLDEN_DIR / "work-variants.opencode.json").read_text(encoding="utf-8"))
    models = [m for p in doc["provider"].values() for m in p["models"].values()]
    assert any(m.get("variants") for m in models)
    assert any("variant" in agent for name, agent in doc["agent"].items() if name.startswith("quoin-"))
    plain = json.loads((GOLDEN_DIR / "work.opencode.json").read_text(encoding="utf-8"))
    assert "variant" not in json.dumps(plain["agent"])


# ============================================ compiled matrix and boundaries

from _opencode_merge_helpers import (  # noqa: E402
    ALLOWED_ENV_READS,
    COMBINATIONS,
    PROFILES,
    run_pipeline,
)
from quoin.opencode_adapter import explain, secrets  # noqa: E402

MATRIX_KEYS = sorted(COMBINATIONS)
PROFILE_NAMES = {"work": "work", "minimal": "minimal", "personal": "personal"}
CREDENTIAL_ENV_NAMES = (
    "QUOIN_CORP_GW_API_KEY", "QUOIN_CORP_GW_B_API_KEY", "QUOIN_LOCAL_GW_API_KEY",
    "QUOIN_OPENROUTER_API_KEY", "LOCAL_GW_TOKEN", "OPENROUTER_API_KEY",
)


def _forbid_resolution(mp):
    def boom(*args, **kwargs):
        raise AssertionError("a credential was resolved, or the network or a subprocess was used")

    mp.setattr(secrets.EnvBackend, "resolve", boom)
    mp.setattr(secrets.MacKeychainBackend, "resolve", boom)
    mp.setattr(secrets.CredentialResolver, "resolve", boom)
    mp.setattr(secrets, "_default_runner", boom)
    mp.setattr(secrets.SecretValue, "__init__", boom)
    mp.setattr(socket.socket, "connect", boom)
    mp.setattr(socket, "create_connection", boom)
    mp.setattr(subprocess, "run", boom)
    mp.setattr(subprocess, "Popen", boom)


def _install_agent_stubs(root):
    agent_dir = root / ".opencode" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        (agent_dir / ("quoin-%s.md" % role)).write_text("stub\n", encoding="utf-8")


def _all_forms(ev):
    """Every rendering of an evaluation, plus the compile outcome."""
    texts = []
    for redact in (False, True):
        for as_json in (False, True):
            texts.append(explain.render(ev, redact=redact, as_json=as_json))
    return texts


def _matrix_row(key, tmp_path):
    out = run_pipeline(tmp_path, key)
    env, home, root = out.env, tmp_path / "home", tmp_path / "project"
    for name in CREDENTIAL_ENV_NAMES:
        env[name] = helpers.SEEDED_SECRET
    _install_agent_stubs(root)
    record = {"key": key, "env": env, "tmp": tmp_path, "texts": [], "raised": None}
    kwargs = dict(
        project_root=root, profile=PROFILE_NAMES[key[0]], env=env, home=home, now=NOW
    )
    try:
        ev = compiler.evaluate(**kwargs)
    except ConfigErrors as exc:
        record["raised"] = exc
        return record
    record["ev"] = ev
    record["texts"] += _all_forms(ev)
    try:
        result = compiler.build(ev)
    except compiler.CompileBlocked as exc:
        record["blocked"] = exc
        record["written"] = (tmp_path / "state").exists()
        return record
    target = tmp_path / "out"
    compiler.write(result, target)
    record["result"] = result
    record["check"] = compiler.check(ev, target)
    record["files"] = [(target / "opencode.json").read_text(encoding="utf-8"),
                       (target / "quoin-compile.json").read_text(encoding="utf-8")]
    return record


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    results = {}
    with pytest.MonkeyPatch.context() as mp:
        _forbid_resolution(mp)
        for key in MATRIX_KEYS:
            results[key] = _matrix_row(key, tmp_path_factory.mktemp("compiled-matrix"))
    return results


def _expects_block(key):
    expected = COMBINATIONS[key]
    return expected[5] is not None or key[1] in ("none", "unclassified")


def test_the_matrix_table_is_the_full_product():
    assert len(MATRIX_KEYS) == 45


@pytest.mark.parametrize("key", MATRIX_KEYS, ids=["-".join(k) for k in MATRIX_KEYS])
def test_compiled_matrix(key, matrix):
    record = matrix[key]
    expected = COMBINATIONS[key]
    assert set(record["env"].reads) <= ALLOWED_ENV_READS
    if expected[0] in ("load-error", "merge-error"):
        assert {e.rejection_class for e in record["raised"].errors} == {expected[1]}
        return
    assert record["raised"] is None
    if _expects_block(key):
        assert isinstance(record.get("blocked"), compiler.CompileBlocked)
        assert record["written"] is False
        return
    result, ev = record["result"], record["ev"]
    doc = result.document
    assert record["check"].ok
    policies = doc["experimental"]["policies"]
    assert policies[0] == {"action": "provider.use", "effect": "deny", "resource": "*"}
    allowed = [p["resource"] for p in policies[1:]]
    assert all(p["effect"] == "allow" for p in policies[1:])
    assert doc["enabled_providers"] == allowed == sorted(doc["provider"])
    permitted = {
        compiler.native_provider_id(ev.effective.providers[pid]) for pid in ev.effective.effective_providers
    }
    assert set(doc["enabled_providers"]) <= permitted
    referenced = {
        compiler.native_provider_id(ev.effective.providers[r.provider_id])
        for r in compiler._emitted_resolutions(ev.resolutions)
    }
    assert set(doc["provider"]) == referenced
    for quoin_id, native in result.sidecar["native_provider"].items():
        view = ev.effective.providers[quoin_id]
        if view.kind == "openai-compatible":
            assert native == "quoin-" + quoin_id and native not in BUILTIN_PROVIDER_IDS
        if ev.effective.classification == "work":
            assert ev.effective.profile_classification == "work"
            assert view.kind != "openrouter" and native != "openrouter"
    if ev.effective.classification == "work":
        assert not any(name == "openrouter" for name in doc["provider"])
    assert "max" not in json.dumps(doc) and "summary" not in doc["agent"]


def test_the_matrix_is_not_vacuous(matrix):
    built = {k: r for k, r in matrix.items() if "result" in r}
    assert any(r["ev"].effective.classification == "work" for r in built.values())
    assert any(r["ev"].effective.classification == "personal" for r in built.values())
    blocked = [k for k, r in matrix.items() if "blocked" in r]
    raised = [k for k, r in matrix.items() if r["raised"] is not None]
    assert built and len(blocked) + len(raised) + len(built) == 45
    assert len(raised) == 12


def test_a_managed_work_row_builds(tmp_path):
    profile = fixture(PROFILE_WORK)
    profile["models"]["work-planner"]["provider"] = "corp-gw"
    world = World(tmp_path, profile=profile, managed=MANAGED_WORK)
    with pytest.MonkeyPatch.context() as mp:
        _forbid_resolution(mp)
        ev = world.evaluate()
        assert ev.effective.managed_present and ev.effective.classification == "work"
        result = compiler.build(ev)
    assert list(result.document["provider"]) == ["quoin-corp-gw"]
    assert compiler.launchable(ev)


def test_the_matrix_and_renders_never_hold_a_seeded_secret(matrix):
    forms = helpers.secret_forms(helpers.SEEDED_SECRET)
    checked = 0
    for record in matrix.values():
        chunks = list(record["texts"]) + list(record.get("files", ()))
        if "result" in record:
            chunks.append(json.dumps(record["result"].sidecar))
            chunks.append(record["result"].digest)
            chunks.append(
                jsonio.dump_canonical(compiler.digest_input(record["ev"], record["result"].document)).decode("utf-8")
            )
        if "blocked" in record:
            chunks += [str(record["blocked"]), repr(record["blocked"])]
        for chunk in chunks:
            for form in forms:
                assert form not in chunk
            checked += 1
    assert checked > 200


def test_error_objects_never_carry_secrets_or_paths(work, monkeypatch):
    work.env["QUOIN_CORP_GW_API_KEY"] = helpers.SEEDED_SECRET
    ev = work.evaluate()
    errors_seen = []
    for gate in compiler.GATE_MESSAGES:
        errors_seen.append(compiler.CompileGateError(gate))
    for code in compiler.OUTPUT_MESSAGES:
        errors_seen.append(compiler.OutputRefused(code))
    (work.root / ".opencode" / "agents" / "quoin-gate.md").unlink()
    with pytest.raises(compiler.CompileBlocked) as info:
        compiler.build(work.evaluate())
    errors_seen.append(info.value)
    for exc in errors_seen:
        for text in (str(exc), repr(exc)):
            for form in helpers.secret_forms(helpers.SEEDED_SECRET):
                assert form not in text
            assert str(work.tmp) not in text and "example.invalid" not in text
    assert ev


def test_compiler_and_explain_never_name_resolvers_or_processes():
    forbidden = {
        "EnvBackend", "MacKeychainBackend", "CredentialResolver", "SecretValue",
        "default_resolver", "subprocess", "_default_runner", "SecretResolver",
    }
    for name in ("compiler", "explain"):
        tree = ast.parse((SRC_DIR / (name + ".py")).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                found |= {a.name for a in node.names}
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
        assert not (found & forbidden), (name, found & forbidden)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                assert node.attr not in ("environ", "getenv", "getenvb"), name
