"""Tests for the vendored subset schema of the native OpenCode configuration
and the evidence rows the compiler relies on.

Everything here is offline: a trap makes any network or subprocess use fail
the test.
"""
from __future__ import annotations

import re
import socket
import subprocess

import pytest

from test_opencode_docs import COMPAT_PATH, _sections, _table_rows


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


# ---------------------------------------------------- compatibility evidence

# (section heading, distinctive substring of the claim). Every fact the
# compiler relies on must be a verified row.
RELIED_FACTS = (
    ("Models and variants", "are applied whether or not the model's `reasoning` capability"),
    ("Models and variants", "An agent's `variant` is used only when the request's model equals"),
    ("Models and variants", "A config model entry accepts `name`, `variants`"),
    ("Custom providers", "is both the whitelist key and the default upstream model id"),
    ("Custom providers", "A model reference is split at the first `/` only"),
    ("Custom providers", "inherits that id's behaviour"),
    ("Custom providers", "default factory of the configured package"),
    ("Custom providers", "A self-hosted OpenAI-compatible chat-completions endpoint"),
    ("Custom providers", "uses the `@ai-sdk/openai` package rather than"),
    ("Custom providers", "The provider id `openrouter` is a built-in provider"),
    ("Custom providers", "`whitelist` and `blacklist` apply to any provider"),
    ("Configuration sources and precedence", "The top-level keys the pinned V1 config schema defines"),
    ("Configuration sources and precedence", "The top-level `share` field accepts exactly"),
    ("Configuration sources and precedence", "Config layers are merged in this order"),
    ("Provider policy and tool permissions", "A policy statement has exactly the fields"),
    ("Provider policy and tool permissions", "`enabled_providers` and `disabled_providers` are enforced"),
    ("Provider policy and tool permissions", "`experimental.policies` is evaluated only by the newer catalog service"),
    ("Commands, agents, delegation and permissions", "`small_model` is read only by"),
    ("Commands, agents, delegation and permissions", "Compaction does not use `small_model`"),
    ("Commands, agents, delegation and permissions", "The built-in `summary` agent"),
    ("Configuration sources and precedence", "A same-named config-file `agent.NAME` entry"),
)


def _compat_rows():
    sections = _sections(COMPAT_PATH.read_text(encoding="utf-8"), 2)
    return {name: _table_rows(body) for name, body in sections.items()}


def test_compiler_relied_facts_have_rows():
    rows = _compat_rows()
    for heading, claim in RELIED_FACTS:
        matches = [cells for cells in rows[heading] if claim in cells[0]]
        assert len(matches) == 1, (heading, claim, len(matches))
        assert matches[0][1] == "verified", (heading, claim)


# Provider ids that carry a custom loader in the pinned release's loader
# table (`packages/opencode/src/provider/provider.ts` at tag v1.18.32, the
# object returned by `custom`, lines 174-904). Bundled SDK package names are
# not provider ids and are not listed.
BUILTIN_PROVIDER_IDS = frozenset(
    {
        "anthropic", "opencode", "openai", "meta", "xai", "github-copilot", "azure",
        "azure-cognitive-services", "amazon-bedrock", "llmgateway", "openrouter", "nvidia",
        "vercel", "google-vertex", "google-vertex-anthropic", "sap-ai-core", "zenmux",
        "gitlab", "cloudflare-workers-ai", "cloudflare-ai-gateway", "cerebras", "kilo",
        "snowflake-cortex",
    }
)


def test_builtin_provider_ids_not_prefixed():
    assert not any(item.startswith("quoin-") for item in BUILTIN_PROVIDER_IDS)
    assert {"openai", "anthropic", "azure", "openrouter"} <= BUILTIN_PROVIDER_IDS
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", item) for item in BUILTIN_PROVIDER_IDS)
    from quoin.opencode_adapter import compiler

    assert compiler.NATIVE_ID_PREFIX == "quoin-"
    for item in BUILTIN_PROVIDER_IDS:
        assert compiler.NATIVE_ID_PREFIX + item not in BUILTIN_PROVIDER_IDS


# ------------------------------------------------------------- the schema

import copy  # noqa: E402
import json  # noqa: E402

from _opencode_merge_helpers import World  # noqa: E402
from quoin.opencode_adapter import compiler, config, merge, names, paths, schema_check  # noqa: E402
from quoin.opencode_adapter.generate import ROLES  # noqa: E402

SCHEMA_PATH = paths.native_schema_path()
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
SOURCE_FILES = (
    "core/src/v1/config/config.ts",
    "core/src/v1/config/provider.ts",
    "core/src/v1/config/agent.ts",
    "core/src/policy.ts",
    "core/src/catalog.ts",
    "core/src/config/experimental.ts",
)


def violations(doc, entry="config"):
    return schema_check.validate(doc, SCHEMA, entry)


@pytest.fixture(scope="module")
def work_doc(tmp_path_factory):
    world = World(tmp_path_factory.mktemp("native-schema"))
    return compiler.build(world.evaluate()).document


def test_schema_file_shape():
    assert SCHEMA["$id"] == "urn:quoin:opencode:native-config:1.18.32"
    assert "$schema" not in SCHEMA and "json-schema.org" not in SCHEMA_PATH.read_text(encoding="utf-8")
    assert set(SCHEMA["$defs"]) >= {"config", "provider", "model", "variant", "policy", "agent_entry"}
    assert SCHEMA["$defs"]["config"]["additionalProperties"] is False
    assert set(SCHEMA["$defs"]["config"]["required"]) == set(SCHEMA["$defs"]["config"]["properties"])
    assert not any(k not in schema_check.SUPPORTED_KEYWORDS for _, k in schema_check.iter_keywords(SCHEMA))


def test_the_compiled_document_validates(work_doc):
    assert violations(work_doc) == []


def test_agent_names_match_the_generator():
    enum = SCHEMA["$defs"]["config"]["properties"]["agent"]["propertyNames"]["enum"]
    assert set(enum) == {names.role_agent_name(r) for r in ROLES} | {"title", "compaction"}
    assert "summary" not in enum


def test_effort_labels_match_the_resolver():
    efforts = {e for e in merge.EFFORTS if e != "max"}
    assert set(SCHEMA["$defs"]["effort"]["enum"]) == efforts
    pattern = SCHEMA["$defs"]["agent_entry"]["properties"]["variant"]["pattern"]
    assert re.fullmatch(r"\^quoin-\((.+)\)\$", pattern).group(1).split("|") == ["low", "medium", "high"]
    variant = SCHEMA["$defs"]["variant"]["properties"]
    assert set(variant["reasoningEffort"]["enum"]) == efforts
    assert set(variant["reasoning"]["properties"]["effort"]["enum"]) == efforts
    names_pattern = SCHEMA["$defs"]["model"]["properties"]["variants"]["propertyNames"]["pattern"]
    assert names_pattern == pattern
    assert compiler.VARIANT_PREFIX == "quoin-"


def test_package_enum_matches_the_compiler_table():
    enum = SCHEMA["$defs"]["provider"]["properties"]["npm"]["enum"]
    assert set(enum) == set(compiler.PACKAGES.values())


@pytest.mark.parametrize("provider_id", ["corp-gw", "a", "x" * 63, "a_b-c", "openrouter", "0abc"])
def test_api_key_pattern_accepts_the_generated_placeholder(provider_id):
    pattern = SCHEMA["$defs"]["provider"]["properties"]["options"]["properties"]["apiKey"]
    good = "{env:" + config.provider_env_name(provider_id) + "}"
    assert re.search(pattern["pattern"], good)
    for bad in ("sk-abc", "{env:HOME}", "{file:/x}", good + "x", "x" + good, "plain"):
        assert not re.search(pattern["pattern"], bad), bad


@pytest.mark.parametrize("provider_id", ["a", "x" * 63, "corp-gw", "a_b-c"])
def test_native_ids_fit_the_id_pattern(provider_id):
    pattern = SCHEMA["$defs"]["native_id"]["pattern"]
    assert re.search(pattern, "quoin-" + provider_id)
    ref = SCHEMA["$defs"]["native_model_ref"]["pattern"]
    assert re.search(ref, "quoin-%s/example-vendor/example-coder" % provider_id)
    assert re.search(ref, "openrouter/example-vendor/example-model")
    for bad in ("openai", "azure", "quoin-", "quoin-" + "x" * 64, "openrouter2", "Quoin-a"):
        assert not re.search(pattern, bad), bad


def test_comment_cites_the_sources():
    comment = SCHEMA["$comment"]
    assert "v1.18.32" in comment
    for rel in SOURCE_FILES:
        assert "github.com/anomalyco/opencode/blob/v1.18.32/packages/" + rel in comment, rel
    assert len(set(re.findall(r"\b[0-9a-f]{40}\b", comment))) == 6


def test_every_cited_source_has_a_compatibility_row():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    evidence = "\n".join(cells[2] for rows in _compat_rows().values() for cells in rows if len(cells) == 4)
    for path in set(re.findall(r"blob/v1\.18\.32/(packages/[\w./-]+\.ts)", SCHEMA_PATH.read_text(encoding="utf-8"))):
        assert "blob/v1.18.32/" + path in evidence, path
    assert text


def test_every_property_carries_a_citation():
    def walk(node, where):
        if not isinstance(node, dict):
            return
        for name, sub in node.get("properties", {}).items():
            assert "$comment" in sub or "$ref" in sub, where + (name,)
            walk(sub, where + (name,))
        for name, sub in node.get("$defs", {}).items():
            walk(sub, where + (name,))

    walk(SCHEMA, ())


def _mutate_unknown_key(doc):
    doc["extra"] = 1


def _mutate_summary(doc):
    doc["agent"]["summary"] = {"model": doc["model"]}


def _mutate_variant_max(doc):
    doc["agent"]["quoin-gate"]["variant"] = "quoin-max"


def _mutate_effort_max(doc):
    provider = doc["provider"]["quoin-corp-gw"]
    provider["models"]["example-vendor/example-coder"]["variants"] = {"quoin-low": {"reasoningEffort": "max"}}


def _mutate_literal_key(doc):
    doc["provider"]["quoin-corp-gw"]["options"]["apiKey"] = "sk-literal-value-0000"


def _mutate_share(doc):
    doc["share"] = "on"


def _mutate_scheme(doc):
    doc["provider"]["quoin-corp-gw"]["options"]["baseURL"] = "htp://gateway.example.invalid/v1"


def _mutate_one_policy(doc):
    doc["experimental"]["policies"] = doc["experimental"]["policies"][:1]


def _mutate_no_whitelist(doc):
    del doc["provider"]["quoin-corp-gw"]["whitelist"]


def _mutate_unprefixed(doc):
    doc["provider"]["openai"] = doc["provider"].pop("quoin-corp-gw")
    doc["enabled_providers"] = ["openai", "quoin-corp-gw-b"]


def _mutate_empty_name(doc):
    doc["provider"]["quoin-corp-gw"]["name"] = ""


def _mutate_no_npm(doc):
    del doc["provider"]["quoin-corp-gw"]["npm"]


def _mutate_autoupdate(doc):
    doc["autoupdate"] = "yes"


def _mutate_model_ref(doc):
    doc["model"] = "openai/example"


def _mutate_reasoning_shape(doc):
    provider = doc["provider"]["quoin-corp-gw"]
    provider["models"]["example-vendor/example-coder"]["variants"] = {"quoin-low": {"reasoning": {"level": "low"}}}


def _mutate_empty_enabled(doc):
    doc["enabled_providers"] = []


def _mutate_package(doc):
    doc["provider"]["quoin-corp-gw"]["npm"] = "@ai-sdk/anthropic"


NEGATIVES = {
    "unknown-top-level-key": _mutate_unknown_key,
    "summary-agent": _mutate_summary,
    "variant-max": _mutate_variant_max,
    "reasoning-effort-max": _mutate_effort_max,
    "literal-api-key": _mutate_literal_key,
    "share-on": _mutate_share,
    "scheme-typo": _mutate_scheme,
    "single-policy": _mutate_one_policy,
    "missing-whitelist": _mutate_no_whitelist,
    "unprefixed-provider": _mutate_unprefixed,
    "empty-name": _mutate_empty_name,
    "missing-npm": _mutate_no_npm,
    "autoupdate-string": _mutate_autoupdate,
    "foreign-model-ref": _mutate_model_ref,
    "bad-reasoning-shape": _mutate_reasoning_shape,
    "empty-enabled-list": _mutate_empty_enabled,
    "other-package": _mutate_package,
}


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_one_key_mutations_are_rejected(work_doc, name):
    doc = copy.deepcopy(work_doc)
    NEGATIVES[name](doc)
    assert violations(doc), name


def test_schema_accepts_the_documented_variants(work_doc):
    doc = copy.deepcopy(work_doc)
    models = doc["provider"]["quoin-corp-gw"]["models"]
    models["example-vendor/example-coder"]["variants"] = {
        "quoin-low": {"reasoningEffort": "low"},
        "quoin-high": {"reasoning": {"effort": "high"}},
    }
    doc["agent"]["quoin-gate"]["variant"] = "quoin-low"
    assert violations(doc) == []


GOLDENS = sorted((SCHEMA_PATH.parent.parent / "fixtures" / "compiled").glob("*.opencode.json"))


def test_golden_documents_validate_offline():
    assert [p.name for p in GOLDENS] == ["personal.opencode.json", "work.opencode.json"]
    for path in GOLDENS:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert violations(doc) == [], path.name
        assert list(doc) == [
            "$schema", "model", "small_model", "agent", "share", "autoupdate",
            "enabled_providers", "provider", "experimental",
        ]
