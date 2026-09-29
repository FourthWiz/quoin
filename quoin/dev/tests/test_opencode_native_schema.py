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
