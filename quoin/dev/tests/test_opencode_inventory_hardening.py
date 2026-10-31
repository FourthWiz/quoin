"""Regression cases for scanner bypasses, hidden tools and agents, MCP redefinition and read hardening."""
from __future__ import annotations

import dataclasses
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


RETRY_PROBES = [
    "---\nmodel: anthropic/claude-sonnet-4-5 # note: x\ndescription: a: b\n---\n",
    "---\nmodel: # p/m: x\ndescription: a: b\n---\n",
    "---\nmodel: anthropic/claude-sonnet-4-5\t# note: x\ndescription: a: b\n---\n",
]


BEDROCK = "amazon-bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
OLLAMA = "ollama/llama3:8b"

COLON_MODEL_CASES = [
    "---\nmodel: %s\ndescription: ok\n---\n" % BEDROCK,
    "---\nmodel: %s\ndescription: a: b\n---\n" % OLLAMA,
    "---\nmodel: '%s'\ndescription: a: b\n---\n" % OLLAMA,
    '---\nmodel: "%s"\ndescription: a: b\n---\n' % BEDROCK,
    "---\nmodel: %s\n  # c\ndescription: a: b\n---\n" % OLLAMA,
    "---\n# c\rmodel: a/b\n---\n",
    "---\r\nmodel: a/b\r\ndescription: Note: fine\r\n---\r\n",
]


@pytest.mark.parametrize("text", RETRY_PROBES)
def test_an_unquoted_model_with_a_colon_lists_the_raw_reading(text):
    # OpenCode reparses after a parse error and keeps the raw value, comment included.
    got = _scan(text)
    assert got["unknown"] is True or any("#" in reading for reading in got["model_readings"])


@pytest.mark.parametrize("model", [BEDROCK, OLLAMA])
def test_a_plain_colon_model_id_stays_known_with_one_reading(model):
    got = _scan("---\nmodel: %s\ndescription: a: b\n---\n" % model)
    assert got["unknown"] is False and got["model"] == model and got["model_readings"] == [model]


def test_a_following_indented_comment_line_adds_a_reading():
    got = _scan("---\nmodel: %s\n  # c\ndescription: a: b\n---\n" % OLLAMA)
    assert got["unknown"] is False
    assert OLLAMA in got["model_readings"] and OLLAMA + "\n" in got["model_readings"]


@pytest.mark.parametrize("model", [BEDROCK, OLLAMA])
def test_a_quoted_colon_model_id_has_no_extra_readings(model):
    for quote in ("'", '"'):
        got = _scan("---\nmodel: %s%s%s\ndescription: a: b\n---\n" % (quote, model, quote))
        assert got["unknown"] is False and got["model"] == model and "model_readings" not in got


def _agent_verdicts(tmp_path, front, classification, allowlist=()):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / "agent" / "x.md", "---\n%s\n---\nbody" % front)
    inv = world.collect(compiled_doc=COMPILED)
    expected = _expected(classification)
    expected = dataclasses.replace(expected, model_allowlist=frozenset(allowlist))
    return [v for v in inventory.evaluate(inv, expected) if v.verdict == "refused"]


@pytest.mark.parametrize("classification", ["personal", "work"])
@pytest.mark.parametrize("front", [
    "model: evil/x:1",
    "model: %s # note: x" % OLLAMA,
    "model: %s\n  # c\ndescription: a: b" % OLLAMA,
    "model: %s\n   \ndescription: Reviews code: carefully" % OLLAMA,
])
def test_an_off_allowlist_colon_model_is_refused_under_both_profiles(tmp_path, classification, front):
    refused = _agent_verdicts(tmp_path, front, classification, allowlist=[BEDROCK, OLLAMA])
    assert {v.code for v in refused} == {"agent-model-outside-allowlist"}


@pytest.mark.parametrize("classification", ["personal", "work"])
@pytest.mark.parametrize("model", [BEDROCK, OLLAMA])
def test_an_allow_listed_colon_model_is_accepted_under_both_profiles(tmp_path, classification, model):
    assert _agent_verdicts(tmp_path, "model: %s\ndescription: a: b" % model, classification,
                           allowlist=[BEDROCK, OLLAMA]) == []


def test_a_comment_line_holding_a_bare_carriage_return_is_unknown():
    # gray-matter drops the keys after it while js-yaml would read them.
    assert _scan("---\n# c\rmodel: a/b\n---\n")["unknown"] is True


def test_a_crlf_file_whose_last_line_has_two_colons_stays_known():
    got = _scan("---\r\nmodel: a/b\r\ndescription: Note: fine\r\n---\r\nbody")
    assert got == {"model": "a/b", "has_permission": False, "has_tools": False, "unknown": False}


@pytest.mark.parametrize("text", [
    "---\nmodel: 'a/b: x'\ndescription: ok\n---\n",
    "---\nmodel: a/b\ndescription: Note: fine\n---\n",
])
def test_a_quoted_model_or_a_colon_elsewhere_stays_known(text):
    assert _scan(text)["unknown"] is False


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
    assert "inventory-agent-unreadable" in codes


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
    assert "inventory-agent-unreadable" in codes


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


# ------------------------------------------- round 2: line breaks, tabs, empty model

@pytest.mark.parametrize("model_line", ["model:", "model: ''", 'model: ""'])
@pytest.mark.parametrize("key,flag", [("permission", "has_permission"), ("tools", "has_tools")])
def test_an_empty_model_does_not_hide_a_later_watched_key(model_line, key, flag):
    got = _scan("---\n%s\n%s: {bash: allow}\n---\n" % (model_line, key))
    assert got[flag] is True, got


def test_a_multiline_quoted_value_is_unknown_so_an_inner_model_line_hides_nothing():
    assert _scan('---\ndescription: "a\nmodel:\n"\npermission: {bash: allow}\n---\n')["unknown"] is True
    assert _scan("---\ndescription: 'a\nmodel:\n'\n---\n")["unknown"] is True


@pytest.mark.parametrize("key,flag", [("permission", "has_permission"), ("tools", "has_tools")])
def test_a_bare_carriage_return_is_a_line_break(key, flag):
    got = _scan("---\ndescription: x\r%s: {edit: allow}\n---\n" % key)
    assert got[flag] is True or got["unknown"] is True, got


def test_a_bare_carriage_return_before_a_model_is_read():
    got = _scan("---\ndescription: x\rmodel: evil/model\n---\n")
    assert got["model"] == "evil/model" or got["unknown"] is True, got


def test_a_bare_carriage_return_on_a_line_with_a_second_colon_is_unknown():
    # OpenCode's retry sanitizer does not split on a bare CR.
    assert _scan("---\nmodel:\rdescription: a: b\n---\n")["unknown"] is True


@pytest.mark.parametrize("key", ["permission", "tools", "model"])
def test_a_tab_led_line_is_unknown(key):
    assert _scan("---\ndescription: x\n\t%s: {edit: allow}\n---\n" % key)["unknown"] is True
    assert _scan("---\ndescription: x\n \t%s: a/b\n---\n" % key)["unknown"] is True


def test_a_plain_model_folded_onto_an_indented_line_is_unknown():
    assert _scan("---\nmodel: a/b\n  -evil\n---\n")["unknown"] is True
    assert _scan("---\nmodel: a/b\nname: x\n---\n") == {
        "model": "a/b", "has_permission": False, "has_tools": False, "unknown": False}


def test_the_walk_counts_directories_toward_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_MAX_WALK", 5)
    for i in range(20):
        (tmp_path / ("d%02d" % i)).mkdir()
    problems = []
    list(inventory._walk_markdown(tmp_path, problems))
    assert problems == ["truncated"]


# ------------------------------------- empty model, document end, strict ASCII

NESTED_VALUES = ["  evil/x", "  - evil/x", "  a: b"]
GAPS = ["\n", "\n\n", "\n# c\n", "\n\n# c\n\n", "\r", "\r\r", "\r# c\r", "\r\n\r\n", "\n  # c\n"]


@pytest.mark.parametrize("value", NESTED_VALUES)
@pytest.mark.parametrize("gap", GAPS)
@pytest.mark.parametrize("model_line", ["model:", "model: ''", "model: # c"])
def test_an_empty_model_with_an_indented_value_after_blank_or_comment_lines_is_unknown(model_line, gap, value):
    assert _scan("---\n%s%s%s\n---\n" % (model_line, gap, value))["unknown"] is True


@pytest.mark.parametrize("gap", GAPS)
def test_an_empty_model_followed_by_another_key_stays_known(gap):
    got = _scan("---\nmodel:%sname: x\n---\n" % gap)
    if gap in ("\r", "\r\r", "\r# c\r"):  # a bare CR plus a second colon: the retry path may fold the lines
        assert got["unknown"] is True
        return
    assert got == {"model": None, "has_permission": False, "has_tools": False, "unknown": False}


@pytest.mark.parametrize("space", [" ", "\u0085", " ", "　"])
@pytest.mark.parametrize("flow", [("[a,", "]"), ("{a: 1,", "}")])
def test_a_document_end_followed_by_a_unicode_space_inside_a_flow_is_unknown(space, flow):
    opening, closing = flow
    text = "---\nname: %s\n...%s\n%s\npermission: {bash: allow}\ntools: {bash: true}\nmodel: evil/x\n---\n" % (
        opening, space, closing)
    assert _scan(text)["unknown"] is True


@pytest.mark.parametrize("char", [" ", "\u0085", " ", "　", "é", "\x0b", "\x0c", "\x00", "\x7f"])
def test_any_non_ascii_or_control_character_in_the_block_is_unknown(char):
    assert _scan("---\ndescription: a%sb\n---\n" % char)["unknown"] is True
    assert _scan("---\ndescription: x\n---\nbody %s\n" % char)["unknown"] is False


@pytest.mark.parametrize("line", ["...", "... ", "...\t", "... # end"])
def test_a_document_end_with_only_blank_or_comment_lines_after_it_ends_the_block(line):
    got = _scan("---\nmodel: a/b\n%s\n\n# trailing\n---\nbody\n" % line)
    assert got == {"model": "a/b", "has_permission": False, "has_tools": False, "unknown": False}


@pytest.mark.parametrize("text", [
    "---\nmodel: a/b\n...\npermission: {bash: allow}\n---\n",
    "---\nname: [a,\n...\n]\npermission: {bash: allow}\n---\n",
    "---\nmodel: a/b\n...x\n---\n",
    "---\nmodel: a/b\n...#c\n---\n",
])
def test_content_after_a_document_end_or_a_malformed_end_is_unknown(text):
    assert _scan(text)["unknown"] is True


@pytest.mark.parametrize("text", [
    "---\ntools: {}model: evil/x\n---\n",
    "---\nname: [a]permission: x\n---\n",
    "---\nname: [a,\nb]\n---\n",
    "---\nname: \"a\"model: evil/x\n---\n",
    "---\nmodel: 'a/b'x\n---\n",
    "---\nmodel:evil/x\n---\n",
    "---\nx:model: evil/x\n---\n",
])
def test_text_glued_after_a_closed_value_or_a_colon_without_a_space_is_unknown(text):
    assert _scan(text)["unknown"] is True


@pytest.mark.parametrize("value", ["~", "null", "Null", "true", "False", "1", "0x1f", "1.5", ".inf", "2001-12-14", "-1"])
def test_a_plain_model_yaml_reads_as_a_non_string_is_unknown(value):
    assert _scan("---\nmodel: %s\n---\n" % value)["unknown"] is True


@pytest.mark.parametrize("opening", ["----\n", "---\rmodel: evil/x\n", "--- x\n", "--- \n"])
def test_an_opening_fence_gray_matter_reads_differently_is_unknown(opening):
    assert _scan(opening + "model: evil/x\n---\n")["unknown"] is True


def test_the_block_closes_at_the_first_line_starting_with_three_dashes():
    got = _scan("---\nmodel: a/b\n----- trailing\npermission: {bash: allow}\n---\n")
    assert got["model"] == "a/b" and got["has_permission"] is False and got["unknown"] is False


@pytest.mark.parametrize("line,model", [
    ("model: a/b\t# c", "a/b"),
    ("model: a/b # c", "a/b"),
    ("model: a/b#c", "a/b#c"),
    ("model: \"a/b\"\t# c", "a/b"),
    ("model: 'a/b' # c", "a/b"),
    ("model: 'x''y'", "x'y"),
    ("model: '''x'''", "'x'"),
])
def test_inline_comments_and_doubled_single_quotes_read_as_yaml_does(line, model):
    assert _scan("---\n%s\n---\n" % line)["model"] == model


def test_a_model_that_is_only_a_comment_is_empty():
    got = _scan("---\nmodel: # c\nname: x\n---\n")
    assert got == {"model": None, "has_permission": False, "has_tools": False, "unknown": False}


@pytest.mark.parametrize("comment", ["  # note", "\t# note", "# note", "    #"])
def test_a_comment_line_after_a_model_keeps_it_known(comment):
    got = _scan("---\nmodel: a/b\n%s\nname: x\n---\n" % comment)
    assert got == {"model": "a/b", "has_permission": False, "has_tools": False, "unknown": False}


def test_a_folded_model_behind_a_comment_line_is_still_unknown():
    assert _scan("---\nmodel: a/b\n  # note\n  -evil\n---\n")["unknown"] is True


ORDINARY = [
    ("---\ndescription: Reviews code\nmodel: anthropic/claude-x\ntemperature: 0.1\n---\nbody\n",
     {"model": "anthropic/claude-x", "has_permission": False, "has_tools": False}),
    ("---\r\ndescription: x\r\nmode: subagent\r\nmodel: openai/gpt-y\r\n---\r\nbody\r\n",
     {"model": "openai/gpt-y", "has_permission": False, "has_tools": False}),
    ("---\n# comment\ndescription: >\n  folded text\n  more\n\nmodel: a/b\npermission:\n  edit: deny\n"
     "  bash:\n    \"git *\": allow\ntools:\n  write: false\n---\nbody\n",
     {"model": "a/b", "has_permission": True, "has_tools": True}),
    ("---\ndescription: |\n  literal\n\nmodel: \"a/b\" # quoted\nname: 'it''s'\n---\n",
     {"model": "a/b", "has_permission": False, "has_tools": False}),
    ("---\nagent: build\nsubtask: true\nmodel: p/m\n...\n---\n",
     {"model": "p/m", "has_permission": False, "has_tools": False}),
    ("﻿---  \ndescription: x\ntools: {write: false}\n---\n",
     {"model": None, "has_permission": False, "has_tools": True}),
]


@pytest.mark.parametrize("text,expected", ORDINARY)
def test_ordinary_agent_files_scan_as_known(text, expected):
    assert _scan(text) == dict(expected, unknown=False)


def test_unknown_frontmatter_is_refused_with_its_own_code_not_a_model_code(tmp_path):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / "agent" / "a.md", "---\nmodel:\n\n  evil/x\n---\n")
    world.write(world.project / ".opencode" / "command" / "c.md", "---\ndescription: café\n---\n")
    inv = world.collect(compiled_doc=COMPILED)
    work = [v for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"]
    assert {v.code for v in work} == {"inventory-agent-unreadable"} and len(work) == 2
    assert not any(v.verdict == "refused" for v in inventory.evaluate(inv, _expected("personal")))


@pytest.mark.parametrize("front", [
    "model:\n\n  evil/x",
    "model:\n# c\n  evil/x",
    "name: [a,\n... \n]\npermission: {bash: allow}\ntools: {bash: true}\nmodel: evil/x",
])
@pytest.mark.parametrize("kind", ["agent", "command"])
def test_the_bypass_probes_are_refused_end_to_end_under_work(tmp_path, front, kind):
    world = World(tmp_path)
    world.write(world.project / ".opencode" / kind / "x.md", "---\n%s\n---\nbody" % front)
    inv = world.collect(compiled_doc=COMPILED)
    verdicts = inventory.evaluate(inv, _expected("work"))
    assert any(v.verdict == "refused" and v.entry_index >= 0 and inv.entries[v.entry_index].kind == kind
               for v in verdicts)


# ------------------------------------------------------------- the walk

def test_an_unlistable_agent_subdirectory_is_an_unreadable_source(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can list anything")
    world = World(tmp_path)
    agents = world.project / ".opencode" / "agent"
    world.write(agents / "a.md", "plain")
    hidden = agents / "nested"
    hidden.mkdir()
    hidden.chmod(0)
    try:
        inv = world.collect(compiled_doc=COMPILED)
    finally:
        hidden.chmod(0o700)
    assert [s.state for s in inv.sources if s.kind == "directory" and s.layer == "dot-opencode"] == ["unreadable"]
    assert any(v.code == "config-layer-unreadable" for v in inventory.evaluate(inv, _expected("work")))


def test_the_walk_reads_a_listing_only_up_to_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_MAX_WALK", 3)
    for i in range(50):
        (tmp_path / ("f%02d.md" % i)).write_text("x")
    pulled = []
    real = os.scandir

    class Counting:
        def __init__(self, path):
            self.inner = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            for item in self.inner:
                pulled.append(item.name)
                yield item

    monkeypatch.setattr(inventory.os, "scandir", Counting)
    problems = []
    assert list(inventory._walk_markdown(tmp_path, problems)) == []
    assert problems == ["truncated"] and len(pulled) == 4


def test_the_walk_yields_files_and_linked_directories_in_sorted_depth_first_order(tmp_path):
    for rel in ("b.md", "a.md", "z/c.md", "m/d.md", "m/n/e.md", "notes.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    os.symlink(str(tmp_path / "m"), str(tmp_path / "link"))
    problems = []
    got = [(p.relative_to(tmp_path).as_posix(), linked) for p, linked in inventory._walk_markdown(tmp_path, problems)]
    assert got == [("link", True), ("a.md", False), ("b.md", False), ("m/d.md", False), ("m/n/e.md", False),
                   ("z/c.md", False)]
    assert problems == []


# ----------------------------------------------------- profile-less records

def _policy_check(monkeypatch, effective_config, accept):
    monkeypatch.setattr(handoff, "_store_or_none", lambda root: pathlib.Path("store"))
    monkeypatch.setattr(handoff.runstore, "load_record",
                        lambda directory, run_id: {"prepared": {"effective_config": effective_config}})
    outcome = {}
    handoff._check_policy(
        pathlib.Path("."), {"completed": [{"run_id": "r1"}]}, {"classification": "work"},
        accept_unverified=accept, resume_hint="quoin run X --continue", outcome=outcome, policy_profile="work",
    )
    return outcome


@pytest.mark.parametrize("effective_config", [{"policy": {"classification": "work"}}, {"policy": {}, "profile": None}])
def test_a_stored_record_with_a_policy_but_no_profile_is_unverifiable_with_its_own_message(monkeypatch, effective_config):
    with pytest.raises(handoff.HandoffRefused) as info:
        _policy_check(monkeypatch, effective_config, None)
    assert info.value.code == "scope-unverifiable"
    assert "without a profile" in info.value.message
    assert "prepared before this version" not in info.value.message
    assert "quoin run X --continue --accept-unverified-policy" in info.value.message


def test_a_stored_record_with_no_policy_keeps_the_older_version_message(monkeypatch):
    with pytest.raises(handoff.HandoffRefused) as info:
        _policy_check(monkeypatch, None, None)
    assert "prepared before this version" in info.value.message


@pytest.mark.parametrize("accept", [True, "r1"])
def test_the_accept_flag_admits_a_profile_less_record_as_unverified(monkeypatch, accept):
    assert _policy_check(monkeypatch, {"policy": {"classification": "work"}}, accept) == {"unverified_accepted": "r1"}


@pytest.mark.parametrize("line", ["   ", "    ", "\t", "  \t", "  # c ", "    # c", " a"])
def test_a_continuation_line_with_whitespace_adds_the_unallowable_reading(line):
    got = _scan("---\nmodel: %s\n%s\ndescription: a: b\n---\n" % (OLLAMA, line))
    readings = got["model_readings"]
    assert OLLAMA in readings and any("\n" in reading for reading in readings)
    assert len(readings) <= 3


def test_scanning_a_megabyte_of_continuation_lines_is_fast():
    import time
    body = "\t#\n" * (256 * 1024)
    text = "---\nmodel: a:b\n%sdescription: a: b\n---\n" % body
    start = time.monotonic()
    got = _scan(text)
    assert time.monotonic() - start < 5
    assert len(got["model_readings"]) <= 3
