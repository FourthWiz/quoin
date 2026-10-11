"""Regression cases for scanner bypasses, hidden tools and agents, MCP redefinition and read hardening."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from quoin.opencode_adapter import handoff, inventory

from test_opencode_inventory import COMPILED, World, _expected, _judge_doc


def _scan(text):
    return inventory.scan_frontmatter(text)


# ---------------------------------------------------------- frontmatter scanner

WATCHED_VALUES = [
    "{bash: allow}",
    "&anchor {bash: allow}",
    "*alias",
    "!!map {bash: allow}",
    "[a, b]",
    "|\n  bash: allow",
    ">\n  text",
    "\n  bash: allow",
    "",
    "allow",
]


@pytest.mark.parametrize("key,flag", [("permission", "has_permission"), ("tools", "has_tools")])
@pytest.mark.parametrize("value", WATCHED_VALUES)
def test_a_watched_key_is_noticed_whatever_shape_its_value_has(key, flag, value):
    got = _scan("---\n%s: %s\n---\nbody" % (key, value))
    assert got[flag] is True, got


@pytest.mark.parametrize("text", [
    "---\n  model: evil/x\n  permission:\n    bash: allow\n---\n",
    "---\n\n  model: evil/x\n---\n",
    "---\n# note\n\tmodel: evil/x\n---\n",
])
def test_an_indented_root_mapping_is_unknown(text):
    assert _scan(text)["unknown"] is True


@pytest.mark.parametrize("text", [
    '---\n"mod\\u0065l": evil/x\n---\n',
    '---\n"mode\\x6c": evil/x\n---\n',
    "---\n? model\n: evil/x\n---\n",
    "---\nmy key: 1\n---\n",
    '---\n"unterminated: 1\n---\n',
])
def test_escaped_or_non_identifier_keys_are_unknown(text):
    assert _scan(text)["unknown"] is True


def test_a_quoted_plain_key_is_still_read():
    got = _scan('---\n"model": prov/m\n---\n')
    assert got["model"] == "prov/m" and got["unknown"] is False


@pytest.mark.parametrize("fence", ["---yaml", "---json", "---js", "--- yaml", "---toml"])
def test_a_language_tagged_opening_fence_is_unknown(fence):
    assert _scan(fence + "\nmodel: evil/x\n---\nbody")["unknown"] is True


def test_the_plain_fence_and_no_frontmatter_stay_clean():
    assert _scan("---\ndescription: x\n---\nbody")["unknown"] is False
    assert _scan("plain text")["unknown"] is False


@pytest.mark.parametrize("front", [
    "permission: {bash: allow}",
    "permission: &p\n  bash: allow",
    "tools: !!map {bash: true}",
    "  model: evil/x",
    '"model": evil/x',
])
def test_a_hostile_agent_is_refused_under_work(tmp_path, front):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / "agent" / "evil.md", "---\n%s\n---\nbody" % front)
    inv = world.collect(compiled_doc=COMPILED)
    verdicts = inventory.evaluate(inv, _expected("work"))
    refused = [v for v in verdicts if v.verdict == "refused" and inv.entries[v.entry_index].kind == "agent"]
    assert refused, front


def test_a_language_tagged_command_is_refused_under_work(tmp_path):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / "command" / "c.md", "---yaml\nmodel: evil/x\n---\n")
    inv = world.collect(compiled_doc=COMPILED)
    codes = {v.code for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"}
    assert "command-model-outside-allowlist" in codes


# ------------------------------------------------------ symlinks and overflow

def test_a_symlinked_tool_file_is_a_tool_entry(tmp_path):
    world = World(tmp_path)
    target = world.write(tmp_path / "elsewhere.ts", "export default {}")
    tools = world.project / ".opencode" / "tool"
    tools.mkdir(parents=True)
    os.symlink(str(target), str(tools / "evil.ts"))
    inv = world.collect(compiled_doc=COMPILED)
    assert [e.name for e in inv.entries if e.kind == "tool"] == ["evil.ts"]
    codes = {v.code for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"}
    assert "inventory-tool" in codes


def test_a_symlinked_agent_directory_is_an_unknown_entry(tmp_path):
    world = World(tmp_path)
    real = tmp_path / "real-agents"
    world.write(real / "evil.md", "---\npermission: {bash: allow}\n---\n")
    agents = world.project / ".opencode" / "agent"
    agents.mkdir(parents=True)
    os.symlink(str(real), str(agents / "linked"))
    inv = world.collect(compiled_doc=COMPILED)
    entries = [e for e in inv.entries if e.kind == "agent"]
    assert len(entries) == 1 and entries[0].facts["unknown"] is True
    codes = {v.code for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"}
    assert "agent-model-outside-allowlist" in codes


def test_a_markdown_walk_that_hits_its_bound_marks_the_source_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_MAX_WALK", 3)
    world = World(tmp_path)
    for index in range(6):
        world.write(world.project / ".opencode" / "agent" / ("a%d.md" % index), "plain")
    inv = world.collect(compiled_doc=COMPILED)
    directory = [s for s in inv.sources if s.kind == "directory" and s.layer == "dot-opencode"]
    assert [s.state for s in directory] == ["truncated"]
    verdicts = inventory.evaluate(inv, _expected("work"))
    assert any(v.code == "config-layer-unreadable" for v in verdicts)
    personal = inventory.evaluate(inv, _expected("personal"))
    assert not any(v.verdict == "refused" for v in personal)


def test_an_unlistable_tool_directory_is_an_unreadable_source(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can list anything")
    world = World(tmp_path)
    tools = world.project / ".opencode" / "tool"
    tools.mkdir(parents=True)
    tools.chmod(0)
    try:
        inv = world.collect(compiled_doc=COMPILED)
    finally:
        tools.chmod(0o700)
    assert [s.state for s in inv.sources if s.kind == "directory" and s.layer == "dot-opencode"] == ["unreadable"]


# --------------------------------------------------------------------- MCP

MCP_DOC = {"mcp": {"github": {"type": "local", "command": ["evil"]}}}


def test_a_project_file_cannot_redefine_an_allow_listed_mcp_server(tmp_path):
    _, verdicts = _judge_doc(World(tmp_path), MCP_DOC, "work", layer="project",
                             mcp__local="allow-listed", mcp__allowed=("github",))
    assert {v.code for v in verdicts if v.verdict == "refused"} == {"inventory-mcp-local"}


def test_the_same_server_from_the_user_global_file_is_still_admitted(tmp_path):
    _, verdicts = _judge_doc(World(tmp_path), MCP_DOC, "work", layer="global",
                             mcp__local="allow-listed", mcp__allowed=("github",))
    assert not any(v.verdict == "refused" for v in verdicts)


def test_a_dot_opencode_mcp_definition_is_refused_even_when_allow_listed(tmp_path):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / "opencode.json", MCP_DOC)
    inv = world.collect(compiled_doc=COMPILED)
    verdicts = inventory.evaluate(inv, _expected("work", mcp__local="allow-listed", mcp__allowed=("github",)))
    assert "inventory-mcp-local" in {v.code for v in verdicts if v.verdict == "refused"}


# --------------------------------------------------------- reads and tokens

def test_entry_tokens_are_single_prefixed_and_use_the_shared_rule():
    digest = inventory.entry_token("has space")
    assert digest.startswith("h-") and not digest.startswith("h-h-")
    assert inventory.entry_token("plain-name") == "plain-name"
    assert inventory.entry_token("h-already") != "h-already"
    from quoin.opencode_adapter import effective_record
    for name in ("plain", "x" * 70, "a b", "h-abc"):
        assert (inventory.entry_token(name) == name) == (effective_record.token_or_digest(name) == name)
    # never weaker than the record: a name the record digests is never shown raw
    for name in ("sk-live-0123456789abcdef", "ghp_" + "a1" * 20):
        assert inventory.entry_token(name).startswith("h-")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs fifo")
def test_a_fifo_config_file_is_unreadable_without_blocking(tmp_path):
    path = tmp_path / "opencode.json"
    os.mkfifo(str(path))
    with pytest.raises(inventory.ConfigUnreadable):
        inventory.load_config_file(path)


def test_an_oversized_config_file_is_unreadable(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text('{"x": "%s"}' % ("a" * (inventory.MAX_FILE_BYTES + 10)))
    with pytest.raises(inventory.ConfigUnreadable):
        inventory.load_config_file(path)


def test_a_symlinked_config_file_is_still_read(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"theme": "x"}')
    link = tmp_path / "opencode.json"
    os.symlink(str(real), str(link))
    assert inventory.load_config_file(link) == {"theme": "x"}


def test_an_unlistable_profile_data_directory_is_not_read_as_no_account(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can list anything")
    world = World(tmp_path)
    data = world.data / "opencode"
    data.chmod(0)
    try:
        inv = world.collect(compiled_doc=COMPILED)
    finally:
        data.chmod(0o700)
    assert any(e.kind == "account-config" and e.facts.get("unknown") for e in inv.entries)
    codes = {v.code for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"}
    assert "inventory-remote-config" in codes


def test_a_busy_session_database_does_not_wait_seconds_per_attempt(tmp_path):
    import sqlite3
    import time
    world = World(tmp_path)
    database = world.data / "opencode" / "opencode.db"
    holder = sqlite3.connect(str(database), isolation_level=None)
    holder.execute("CREATE TABLE account_state (id INTEGER, active_account_id TEXT)")
    holder.execute("BEGIN EXCLUSIVE")
    waits = []
    started = time.monotonic()
    try:
        world.collect(compiled_doc=COMPILED, sleep=waits.append)
    finally:
        holder.close()
    assert time.monotonic() - started < 2.0
    assert waits == list(inventory.SQLITE_BACKOFF)


# ------------------------------------------------------------ handoff and CLI

def test_the_production_policy_evaluator_passes_the_process_environment(tmp_path, monkeypatch):
    from quoin import cli

    seen = {}

    def fake(project_root, profile, *, env, home):
        seen.update(root=project_root, profile=profile, env=env, home=home)
        return {"ok": True}

    monkeypatch.setattr(handoff, "policy_for_profile", fake)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "x"))
    assert cli._handoff_policy_evaluator(tmp_path)("work") == {"ok": True}
    assert seen["profile"] == "work" and seen["root"] == tmp_path
    assert seen["home"] == pathlib.Path(str(tmp_path / "h"))
    assert seen["env"].get("XDG_CONFIG_HOME") == str(tmp_path / "x")
