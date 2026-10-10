"""Configuration discovery shared by the doctor, the launch checks and the inventory."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import doctor, inventory, launch_env

INVENTORY = Path(inventory.__file__)
LAUNCH_ENV = Path(launch_env.__file__)

ALIASES = (
    ("display_path", "display_path"),
    ("_ConfigUnreadable", "ConfigUnreadable"),
    ("_strip_jsonc", "strip_jsonc"),
    ("_load_config_file", "load_config_file"),
    ("_xdg_config_dir", "xdg_config_dir"),
    ("_worktree_root", "worktree_root"),
    ("_dir_chain", "dir_chain"),
    ("_build_roots", "build_roots"),
    ("_project_config_dirs", "project_config_dirs"),
)


@pytest.mark.parametrize("old,new", ALIASES)
def test_doctor_names_are_the_inventory_objects(old, new):
    assert getattr(doctor, old) is getattr(inventory, new)


def _imported_adapter_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("quoin.opencode_adapter"):
            found.add(node.module.split(".")[-1])
    return found


def test_inventory_imports_no_adapter_module_beyond_jsonio_and_paths():
    assert _imported_adapter_modules(INVENTORY) <= {"jsonio", "paths"}


def test_launch_env_no_longer_imports_the_doctor():
    assert "doctor" not in _imported_adapter_modules(LAUNCH_ENV)
    assert "inventory" in _imported_adapter_modules(LAUNCH_ENV)


def test_inventory_module_hygiene():
    text = INVENTORY.read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 10))
    body = [n for n in ast.parse(text).body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__"
    assert "os.environ" not in text and "os.getenv" not in text


def _world(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    (home / ".config" / "opencode").mkdir(parents=True)
    (project / ".git").mkdir(parents=True)
    return home, project


def test_read_config_files_returns_documents_in_load_order_and_unreadable_paths(tmp_path):
    home, project = _world(tmp_path)
    glob = home / ".config" / "opencode"
    (glob / "opencode.json").write_text(json.dumps({"model": "a/b"}))
    (glob / "config.json").write_text("{not json")
    (project / "opencode.jsonc").write_text('{"share": "disabled", // note\n}')
    docs, unreadable = inventory.read_config_files(project, project, {}, home)
    assert [p.name for p, _ in docs] == ["opencode.json", "opencode.jsonc"]
    assert unreadable == [glob / "config.json"]


def test_the_doctor_wrapper_turns_unreadable_paths_into_findings_in_order(tmp_path):
    home, project = _world(tmp_path)
    glob = home / ".config" / "opencode"
    (glob / "config.json").write_text("[]")
    (project / "opencode.json").write_text("{")
    roots = doctor._build_roots(project, {}, home)
    docs, findings = doctor._read_config_files(project, project, {}, home, roots)
    assert docs == []
    assert [f.id for f in findings] == ["config-unreadable", "config-unreadable"]
    assert [f.path for f in findings] == ["~/.config/opencode/config.json", "./opencode.json"]


def test_project_config_can_be_disabled_by_the_legacy_flag(tmp_path):
    home, project = _world(tmp_path)
    (project / "opencode.json").write_text("{}")
    docs, _ = inventory.read_config_files(project, project, {"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}, home)
    assert docs == []


# ------------------------------------------------------------------ collect

import os
import sqlite3
import threading

SECRET = "sk-live-0123456789abcdef"


class World:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.project = tmp_path / "proj"
        self.data = tmp_path / "data"
        self.global_dir = self.home / ".config" / "opencode"
        self.global_dir.mkdir(parents=True)
        (self.project / ".git").mkdir(parents=True)
        (self.data / "opencode").mkdir(parents=True)
        self.compiled = tmp_path / "state" / "opencode.json"
        self.compiled.parent.mkdir()
        self.compiled.write_text(json.dumps({"provider": {"prov": {}}, "model": "prov/m"}))

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def collect(self, **kw):
        kw.setdefault("scan_env", {"OPENCODE_CONFIG": str(self.compiled)})
        kw.setdefault("compiled_path", self.compiled)
        kw.setdefault("compiled_doc", json.loads(self.compiled.read_text()))
        kw.setdefault("data_dir", self.data)
        return inventory.collect(cwd=self.project, home=self.home, **kw)

    def kinds(self, inv, layer=None):
        return sorted((e.kind, e.name) for e in inv.entries if layer in (None, e.layer))


@pytest.fixture()
def world(tmp_path):
    return World(tmp_path)


KEY_KIND_CASES = [
    ({"plugin": ["x"]}, "plugin"),
    ({"provider": {"p": {}}}, "provider"),
    ({"enabled_providers": ["p"]}, "provider"),
    ({"disabled_providers": ["p"]}, "disabled-providers"),
    ({"mcp": {"s": {"type": "local", "command": ["x"]}}}, "mcp-local"),
    ({"mcp": {"s": {"type": "remote", "url": "https://x"}}}, "mcp-remote"),
    ({"mcp": {"s": {"enabled": False}}}, "mcp-toggle"),
    ({"lsp": {"ts": {"disabled": True}}}, "lsp"),
    ({"formatter": {"x": {"disabled": True}}}, "formatter"),
    ({"tools": {"bash": False}}, "tool"),
    ({"agent": {"a": {"model": "p/m"}}}, "agent"),
    ({"mode": {"a": {}}}, "agent"),
    ({"command": {"c": {"model": "p/m"}}}, "command"),
    ({"permission": {"edit": "allow"}}, "permission"),
    ({"instructions": ["AGENTS.md"]}, "instruction"),
    ({"skills": {"paths": ["s"]}}, "skill"),
    ({"model": "p/m"}, "compiled-key"),
    ({"small_model": "p/m"}, "compiled-key"),
    ({"theme": "x"}, "benign-key"),
    ({"watcher": {"ignore": ["x"]}}, "benign-key"),
    ({"watcher": {"other": 1}}, "unknown-key"),
    ({"surprise": 1}, "unknown-key"),
]


@pytest.mark.parametrize("doc,kind", KEY_KIND_CASES)
def test_each_top_level_key_maps_to_its_kind(world, doc, kind):
    world.write(world.global_dir / "opencode.json", doc)
    inv = world.collect()
    assert {e.kind for e in inv.entries if e.layer == "global" and e.kind != "fetch-watch"} == {kind}


def test_sub_entries_are_one_entry_each_and_url_items_are_flagged(world):
    world.write(world.global_dir / "opencode.json", {
        "mcp": {"a": {"type": "local"}, "b": {"type": "remote"}},
        "instructions": ["https://user:pw@example.com/x", "local.md"],
        "skills": {"urls": ["https://example.com/s"], "paths": ["p"]},
        "provider": {"prov": {}, "other": {}},
    })
    inv = world.collect()
    by_kind = {}
    for e in inv.entries:
        by_kind.setdefault(e.kind, []).append(e)
    assert sorted(e.name for e in by_kind["mcp-local"] + by_kind["mcp-remote"]) == ["a", "b"]
    assert sorted(e.facts["url"] for e in by_kind["instruction"]) == [False, True]
    assert sorted(e.facts["url"] for e in by_kind["skill"]) == [False, True]
    assert {e.name: e.facts["in_compiled_set"] for e in by_kind["provider"]} == {"prov": True, "other": False}


def test_layers_and_load_order(world):
    world.write(world.global_dir / "opencode.json", {"theme": "g"})
    world.write(world.project / "opencode.json", {"theme": "p"})
    world.write(world.project / ".opencode" / "opencode.json", {"theme": "d"})
    world.write(world.home / ".opencode" / "opencode.json", {"theme": "h"})
    inv = world.collect()
    layers = [s.layer for s in inv.sources if s.kind == "config-file"]
    assert layers.index("global") < layers.index("compiled") < layers.index("project")
    assert {"global", "project", "dot-opencode", "home-opencode", "compiled"} <= set(layers)
    compiled = [e for e in inv.entries if e.layer == "compiled"]
    assert [e.kind for e in compiled] == ["compiled-file"] and compiled[0].owned


def test_owned_and_foreign_markdown(world):
    text = "---\nmodel: prov/m\n---\nbody"
    mine = world.write(world.project / ".opencode" / "agent" / "quoin-plan.md", text)
    other = world.write(world.project / ".opencode" / "agent" / "mine.md", text)
    digest = inventory.sha256_hex(text.encode())
    inv = world.collect(owned={".opencode/agent/quoin-plan.md": digest})
    flags = {e.name: e.owned for e in inv.entries if e.kind == "agent"}
    assert flags == {"quoin-plan": True, "mine": False}
    inv = world.collect(owned={".opencode/agent/quoin-plan.md": "0" * 64})
    assert {e.name: e.owned for e in inv.entries if e.kind == "agent"}["quoin-plan"] is False
    assert {p.name for p, _, _ in inv.markdown} == {mine.name, other.name}


@pytest.mark.parametrize("text,expected", [
    ("---\nmodel: a/b\n---\n", {"model": "a/b", "unknown": False}),
    ('---\nmodel: "a/b"\n---\n', {"model": "a/b", "unknown": False}),
    ("---\nmodel: 'a/b' # note\n---\n", {"model": "a/b", "unknown": False}),
    ("---\nmodel: a/b  # note\n---\n", {"model": "a/b", "unknown": False}),
    ("---\nmodel: >-\n  a/b\n---\n", {"unknown": True}),
    ("---\nmodel: !tag x\n---\n", {"unknown": True}),
    ("---\nmodel: &a x\n---\n", {"unknown": True}),
    ("---\nmodel:\n  nested: 1\n---\n", {"unknown": True}),
    ("---\n{model: a/b}\n---\n", {"unknown": True}),
    ("---\nmodel: a/b\n", {"unknown": True}),
    ("---\nmodel: a\nmodel: b\n---\n", {"unknown": True}),
    ("﻿---\nmodel: a/b\n---\n", {"model": "a/b", "unknown": False}),
    ("---\npermission:\n  edit: allow\ntools:\n  x: true\n---\n", {"model": None, "has_permission": True, "has_tools": True, "unknown": False}),
    ("---\ndescription: hi\n---\nbody", {"model": None, "has_permission": False, "has_tools": False, "unknown": False}),
    ("plain prompt text", {"model": None, "has_permission": False, "has_tools": False, "unknown": False}),
    ("\n---\nmodel: a\n---\n", {"unknown": True}),
])
def test_frontmatter_scan(text, expected):
    got = inventory.scan_frontmatter(text)
    for key, value in expected.items():
        assert got[key] == value, (text, got)


def test_markdown_facts_reach_the_entries(world):
    world.write(world.project / ".opencode" / "command" / "c.md", "---\nmodel: x/y\n---\n")
    inv = world.collect()
    entry = next(e for e in inv.entries if e.kind == "command")
    assert entry.facts["model"] == "x/y" and not entry.facts["unknown"]


def test_tool_plugin_and_skill_directories(world):
    base = world.project / ".opencode"
    world.write(base / "tool" / "t.ts", "x")
    world.write(base / "tools" / "notes.txt", "x")
    world.write(base / "plugin" / "p.js", "x")
    (base / "skill" / "s1").mkdir(parents=True)
    inv = world.collect()
    assert [e.name for e in inv.entries if e.kind == "tool"] == ["t.ts"]
    assert [e.name for e in inv.entries if e.kind == "skill"] == ["s1"]
    assert inv.plugin_dirs == [(base / "plugin", "populated")]


def test_auth_json_keeps_only_remote_entries(world):
    world.write(world.data / "opencode" / "auth.json", {
        "openrouter": {"type": "api", "key": SECRET},
        "acct": {"type": "oauth", "access": SECRET, "refresh": SECRET},
        "https://corp.example.com": {"type": "wellknown", "key": "k", "token": SECRET},
        "weird": {"type": "other"},
    })
    inv = world.collect()
    remote = [e for e in inv.entries if e.kind == "remote-config"]
    assert len(remote) == 2 and all(e.name.startswith("h-") for e in remote)
    assert SECRET not in repr(inv) and "corp.example.com" not in repr(inv)


def test_auth_json_garbage_and_absence(world):
    assert [s.state for s in world.collect().sources if s.kind == "auth-file"] == ["absent"]
    world.write(world.data / "opencode" / "auth.json", "{nope")
    inv = world.collect()
    assert [s.state for s in inv.sources if s.kind == "auth-file"] == ["unreadable"]
    assert [e.facts for e in inv.entries if e.kind == "remote-config"] == [{"unknown": True}]


def _db(path, active):
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE account_state (id INTEGER PRIMARY KEY, active_account_id TEXT)")
    connection.execute("INSERT INTO account_state VALUES (1, ?)", (active,))
    connection.commit()
    return connection


def test_session_database_account_cases(world):
    path = world.data / "opencode" / "opencode.db"
    _db(path, "acct-1").close()
    inv = world.collect()
    assert [e.kind for e in inv.entries if e.layer == "account"] == ["account-config"]
    source = next(s for s in inv.sources if s.kind == "database")
    assert source.state == "read" and source.sha256 is None
    _db_empty = sqlite3.connect(str(path))
    _db_empty.execute("UPDATE account_state SET active_account_id = NULL")
    _db_empty.commit()
    _db_empty.close()
    assert not [e for e in world.collect().entries if e.layer == "account"]
    path.unlink()
    sqlite3.connect(str(path)).close()  # no account_state table
    assert not [e for e in world.collect().entries if e.layer == "account"]
    path.write_bytes(b"this is not a database" * 100)
    inv = world.collect()
    assert [dict(e.facts) for e in inv.entries if e.layer == "account"] == [{"unknown": True}]
    assert next(s for s in inv.sources if s.kind == "database").state == "unreadable"


def test_database_path_with_a_space_and_a_hash(tmp_path):
    w = World(tmp_path)
    w.data = tmp_path / "da ta #1"
    (w.data / "opencode").mkdir(parents=True)
    _db(w.data / "opencode" / "opencode.db", "a").close()
    assert [e.kind for e in w.collect().entries if e.layer == "account"] == ["account-config"]


def test_busy_database_is_retried_then_recorded(world):
    path = world.data / "opencode" / "opencode.db"
    _db(path, "a").close()
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    sleeps = []
    try:
        inv = world.collect(sleep=sleeps.append)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert sleeps == [0.1, 0.3, 0.9]
    assert next(s for s in inv.sources if s.kind == "database").state == "busy"
    assert [dict(e.facts) for e in inv.entries if e.layer == "account"] == [{"busy": True}]


def test_database_released_before_the_second_retry_reads_normally(world):
    path = world.data / "opencode" / "opencode.db"
    _db(path, "a").close()
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            holder.execute("ROLLBACK")
            holder.close()

    inv = world.collect(sleep=sleep)
    assert sleeps == [0.1]
    assert next(s for s in inv.sources if s.kind == "database").state == "read"
    assert [dict(e.facts) for e in inv.entries if e.layer == "account"] == [{"account": True}]


def test_large_and_growing_databases_do_not_move_the_sources(world):
    path = world.data / "opencode" / "opencode.db"
    connection = _db(path, "a")
    connection.execute("CREATE TABLE blob (b BLOB)")
    connection.execute("INSERT INTO blob VALUES (?)", (b"x" * (2 * 1024 * 1024),))
    connection.commit()
    before = world.collect()
    assert os.path.getsize(path) > 1024 * 1024
    assert next(s for s in before.sources if s.kind == "database").state == "read"
    connection.execute("INSERT INTO blob VALUES (?)", (b"y" * 4096,))
    connection.commit()
    connection.close()
    after = world.collect()
    assert before.sources == after.sources and before.entries == after.entries


def test_legacy_toml_config_is_presence_only(world):
    world.write(world.global_dir / "config", 'model = "x"\nkey = "%s"\n' % SECRET)
    inv = world.collect()
    assert [e.kind for e in inv.entries if e.kind == "legacy-config"] == ["legacy-config"]
    assert SECRET not in repr(inv)


def test_ambient_config_env_inputs_and_the_inherited_compiled_file(world):
    ambient = {
        "OPENCODE_CONFIG": str(world.compiled), "OPENCODE_CONFIG_DIR": "/x", "OPENCODE_CONFIG_CONTENT": SECRET,
        "OPENCODE_AUTH_CONTENT": SECRET, "OPENCODE_DB": "/db", "OPENCODE_MODELS_URL": "https://m", "OPENCODE_PERMISSION": "{}",
        "PATH": "/bin",
    }
    inv = world.collect(ambient_env=ambient)
    by_name = {e.name: e for e in inv.config_env}
    assert sorted(by_name) == sorted(n for n in ambient if n != "PATH")
    assert by_name["OPENCODE_CONFIG"].facts["inherited"] is True
    assert SECRET not in repr(inv)
    other = world.root / "other.json"
    other.write_text("{}")
    inv = world.collect(ambient_env=dict(ambient, OPENCODE_CONFIG=str(other)))
    assert {e.name: e.facts["inherited"] for e in inv.config_env}["OPENCODE_CONFIG"] is False


def test_managed_files_and_preferences(world):
    managed = world.root / "managed"
    world.write(managed / "opencode.json", {"theme": "m"})
    prefs = world.root / "prefs.plist"
    prefs.write_text("x")
    inv = world.collect(managed_dir=managed, managed_prefs=[prefs], plutil=lambda p: json.dumps({"theme": "q"}))
    assert {s.layer for s in inv.sources} >= {"managed", "managed-prefs"}
    broken = world.collect(managed_dir=managed, managed_prefs=[prefs], plutil=lambda p: "nope")
    assert next(s for s in broken.sources if s.layer == "managed-prefs").state == "unreadable"


def test_claude_compat_entries_carry_the_flag_state(world):
    world.write(world.home / ".claude" / "CLAUDE.md", "x")
    (world.home / ".claude" / "skills").mkdir()
    inv = world.collect(hardening_names=("OPENCODE_DISABLE_CLAUDE_CODE",))
    flags = {e.name: e.facts["flag_set"] for e in inv.entries if e.kind == "claude-compat"}
    assert flags == {"CLAUDE.md": True, "skills": False}


def test_fetch_watch_rows(world):
    opencode = world.project / ".opencode"
    opencode.mkdir()
    rows = lambda: {e.layer: dict(e.facts) for e in world.collect().fetch_watch}  # noqa: E731
    assert rows()["dot-opencode"] == {"writable": True, "node_modules": False, "locked": False}
    (opencode / "node_modules").mkdir()
    world.write(opencode / "package.json", {"dependencies": {"a": "1"}})
    assert rows()["dot-opencode"]["locked"] is False
    world.write(opencode / "package-lock.json", {"packages": {"": {"dependencies": {"a": "1"}}}})
    assert rows()["dot-opencode"]["locked"] is True
    os.chmod(opencode, 0o500)
    try:
        if os.geteuid() != 0:
            assert rows()["dot-opencode"]["writable"] is False
    finally:
        os.chmod(opencode, 0o700)


def test_more_entries_than_the_bound_truncate_the_source(world):
    world.write(world.global_dir / "opencode.json", {"mcp": {"s%d" % i: {"type": "local"} for i in range(20)}})
    inv = world.collect(max_entries=5)
    source = next(s for s in inv.sources if s.layer == "global" and s.kind == "config-file")
    assert source.state == "truncated"
    assert len([e for e in inv.entries if e.source_index == inv.sources.index(source)]) == 5


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions are not enforced for root")
def test_an_unreadable_directory_is_recorded_not_raised(world):
    locked = world.project / ".opencode" / "agent"
    locked.mkdir(parents=True)
    os.chmod(locked, 0o000)
    try:
        inv = world.collect()
    finally:
        os.chmod(locked, 0o700)
    assert inv.entries is not None


def test_an_unreadable_config_file_is_a_source_not_an_exception(world):
    world.write(world.global_dir / "opencode.json", "{broken")
    inv = world.collect()
    assert inv.unreadable == [world.global_dir / "opencode.json"]
    assert [s.state for s in inv.sources if s.kind == "config-file" and s.layer == "global"] == ["unreadable"]


def test_seeded_secrets_never_reach_the_entries_or_sources(world):
    world.write(world.global_dir / "opencode.json", {
        "provider": {"prov": {"options": {"apiKey": SECRET}}},
        "mcp": {"r": {"type": "remote", "url": "https://h.example.com", "headers": {"Authorization": "Bearer " + SECRET}}},
        "instructions": ["https://user:%s@h.example.com/i.md" % SECRET],
    })
    inv = world.collect()
    text = repr((inv.entries, inv.sources))
    assert SECRET not in text and "h.example.com" not in text


# ----------------------------------------------------------------- evaluate

from types import SimpleNamespace

from quoin.opencode_adapter import profile_policy

COMPILED = {
    "provider": {"prov": {"whitelist": ["m", "n"]}},
    "model": "prov/m",
    "agent": {"quoin-planner": {"model": "prov/m"}},
}


def _policy(classification, **explicit):
    values = {
        "policy." + key.replace("__", "."): SimpleNamespace(value=value, origin=("profile",))
        for key, value in explicit.items()
    }
    return profile_policy.view(SimpleNamespace(values=values, classification=classification))


def _expected(classification, **explicit):
    return inventory.Expected.from_compiled(COMPILED, {}, _policy(classification, **explicit))


def _judge_doc(world, doc, classification, layer="global", **explicit):
    target = world.global_dir / "opencode.json" if layer == "global" else world.project / "opencode.json"
    world.write(target, doc)
    inv = world.collect(compiled_doc=COMPILED)
    verdicts = inventory.evaluate(inv, _expected(classification, **explicit))
    return inv, verdicts


VERDICT_TABLE = [
    # doc, (work verdict, code), (personal verdict, code)
    ({"theme": "x"}, ("benign", None), ("benign", None)),
    ({"model": "prov/m"}, ("overridden", None), ("overridden", None)),
    ({"plugin": ["x"]}, ("refused", "inventory-plugin"), ("refused", "inventory-plugin")),
    ({"provider": {"prov": {"options": {}}}}, ("refused", "inventory-provider"), ("recorded", None)),
    ({"provider": {"other": {}}}, ("refused", "inventory-provider"), ("refused", "inventory-provider")),
    ({"enabled_providers": ["prov"]}, ("refused", "inventory-provider"), ("recorded", None)),
    ({"enabled_providers": ["other"]}, ("refused", "inventory-provider"), ("refused", "inventory-provider")),
    ({"disabled_providers": ["x"]}, ("refused", "inventory-disabled-providers"), ("refused", "inventory-disabled-providers")),
    ({"mcp": {"s": {"type": "local", "command": ["x"]}}}, ("refused", "inventory-mcp-local"), ("recorded", None)),
    ({"mcp": {"s": {"type": "remote", "url": "https://x"}}}, ("refused", "inventory-mcp-remote"), ("recorded", None)),
    ({"mcp": {"s": {"enabled": False}}}, ("benign", None), ("benign", None)),
    ({"mcp": {"s": {"enabled": True}}}, ("refused", "inventory-mcp-local"), ("recorded", None)),
    ({"lsp": {"ts": {"disabled": True}}}, ("refused", "inventory-lsp"), ("recorded", None)),
    ({"formatter": {"x": {}}}, ("refused", "inventory-formatter"), ("recorded", None)),
    ({"tools": {"bash": False}}, ("refused", "inventory-tool"), ("refused", "inventory-tool")),
    ({"agent": {"a": {"model": "prov/zzz"}}}, ("refused", "agent-model-outside-allowlist"), ("refused", "agent-model-outside-allowlist")),
    ({"agent": {"a": {"model": "prov/n"}}}, ("recorded", None), ("recorded", None)),
    ({"agent": {"a": {"permission": {}}}}, ("refused", "inventory-agent-permission"), ("recorded", None)),
    ({"mode": {"a": {}}}, ("recorded", None), ("recorded", None)),
    ({"command": {"c": {"model": "x/y"}}}, ("refused", "command-model-outside-allowlist"), ("refused", "command-model-outside-allowlist")),
    ({"command": {"c": {"template": "t"}}}, ("recorded", None), ("recorded", None)),
    ({"permission": {"edit": "allow"}}, ("refused", "inventory-config-permission"), ("recorded", None)),
    ({"instructions": ["AGENTS.md"]}, ("recorded", None), ("recorded", None)),
    ({"instructions": ["https://example.invalid/x"]}, ("refused", "inventory-remote-instructions"), ("recorded", None)),
    ({"skills": {"urls": ["https://example.invalid/s"]}}, ("refused", "inventory-remote-instructions"), ("recorded", None)),
    ({"surprise": 1}, ("refused", "inventory-unknown-key"), ("recorded", None)),
]


@pytest.mark.parametrize("doc,work,personal", VERDICT_TABLE)
def test_verdict_table(tmp_path, doc, work, personal):
    for classification, want in (("work", work), ("personal", personal)):
        world = World(tmp_path / classification)
        inv, verdicts = _judge_doc(world, doc, classification)
        picked = [v for v in verdicts if inv.entries[v.entry_index].layer == "global" and v.entry_index >= 0]
        assert picked, classification
        first = next((v for v in picked if v.verdict == "refused"), picked[0])
        assert (first.verdict, first.code) == want, (classification, doc)


def test_every_document_entry_gets_exactly_one_known_verdict(tmp_path):
    emitted = set()
    for i, (doc, _, _) in enumerate(VERDICT_TABLE):
        for classification in ("work", "personal"):
            inv, verdicts = _judge_doc(World(tmp_path / ("w%d%s" % (i, classification))), doc, classification)
            positions = [v.entry_index for v in verdicts if v.entry_index >= 0]
            assert len(positions) == len(set(positions))
            assert all(v.verdict in inventory.VERDICTS for v in verdicts)
            emitted.update(e.kind for e in inv.entries if e.layer == "global")
    assert {"plugin", "provider", "mcp-local", "mcp-remote", "mcp-toggle", "lsp", "formatter", "tool", "agent",
            "command", "permission", "instruction", "skill", "compiled-key", "benign-key", "unknown-key"} <= emitted


def test_personal_explicit_denial_is_honoured_and_defaults_are_not(tmp_path):
    doc = {"mcp": {"s": {"type": "local", "command": ["x"]}}, "lsp": {"ts": {}}}
    inv, verdicts = _judge_doc(World(tmp_path / "a"), doc, "personal")
    assert {v.verdict for v in verdicts if v.layer == "global"} == {"recorded"}
    inv, verdicts = _judge_doc(World(tmp_path / "b"), doc, "personal", mcp__local="deny", lsp__mode="deny")
    assert {v.code for v in verdicts if v.verdict == "refused"} == {"inventory-mcp-local", "inventory-lsp"}


def test_work_mcp_allow_list_admits_a_named_server(tmp_path):
    doc = {"mcp": {"s": {"type": "local", "command": ["x"]}}}
    _, verdicts = _judge_doc(World(tmp_path), doc, "work", mcp__local="allow-listed", mcp__allowed=("s",))
    assert [v.verdict for v in verdicts if v.layer == "global" and v.code is None and v.verdict != "prevented-offline"] == ["ok"]


def test_config_env_verdicts(tmp_path):
    world = World(tmp_path)
    ambient = {"OPENCODE_CONFIG": "/elsewhere.json", "OPENCODE_CONFIG_DIR": "/d", "OPENCODE_CONFIG_CONTENT": "{}", "OPENCODE_DB": "x"}
    inv = world.collect(ambient_env=ambient)
    work = {inv.entries[v.entry_index].name: (v.verdict, v.code) for v in inventory.evaluate(inv, _expected("work")) if inv.entries[v.entry_index].kind == "config-env"}
    assert work["OPENCODE_CONFIG"] == ("refused", "config-env-opencode-config")
    assert work["OPENCODE_CONFIG_DIR"] == ("refused", "config-env-opencode-config-dir")
    assert work["OPENCODE_CONFIG_CONTENT"] == ("refused", "config-env-opencode-config-content")
    assert work["OPENCODE_DB"] == ("stripped", None)
    personal = {v.verdict for v in inventory.evaluate(inv, _expected("personal")) if inv.entries[v.entry_index].kind == "config-env"}
    assert personal == {"stripped"}
    inherited = world.collect(ambient_env={"OPENCODE_CONFIG": str(world.compiled)})
    assert {v.verdict for v in inventory.evaluate(inherited, _expected("work")) if inherited.entries[v.entry_index].kind == "config-env"} == {"stripped"}


def test_remote_account_and_legacy_entries(tmp_path):
    world = World(tmp_path)
    world.write(world.data / "opencode" / "auth.json", {"https://x": {"type": "wellknown", "token": SECRET}})
    db = world.data / "opencode" / "opencode.db"
    conn = sqlite3.connect(str(db))
    conn.execute("create table account_state (id integer primary key, active_account_id text)")
    conn.execute("insert into account_state values (1, 'acct')")
    conn.commit()
    conn.close()
    world.write(world.global_dir / "config", "x = 1")
    inv = world.collect()
    for classification, want in (("work", "refused"), ("personal", "recorded")):
        got = {inv.entries[v.entry_index].kind: (v.verdict, v.code) for v in inventory.evaluate(inv, _expected(classification))}
        assert got["remote-config"] == (want, "inventory-remote-config" if want == "refused" else None)
        assert got["account-config"][0] == want
        assert got["legacy-config"] == ("refused", "inventory-legacy-config")


def test_a_busy_database_has_its_own_code_in_work(tmp_path):
    world = World(tmp_path)
    db = world.data / "opencode" / "opencode.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("create table account_state (id integer primary key, active_account_id text)")
    conn.execute("begin exclusive")
    try:
        inv = world.collect(sleep=lambda _s: None)
    finally:
        conn.execute("rollback")
        conn.close()
    codes = {v.code for v in inventory.evaluate(inv, _expected("work")) if v.verdict == "refused"}
    assert codes == {"inventory-account-busy"}
    assert {v.verdict for v in inventory.evaluate(inv, _expected("personal")) if v.layer == "account"} == {"recorded"}


def test_fetch_watch_verdicts_follow_the_offline_flag(tmp_path, monkeypatch):
    world = World(tmp_path)
    (world.project / ".opencode").mkdir()
    inv = world.collect()
    watch = lambda exp: [v for v in inventory.evaluate(inv, exp) if inv.entries[v.entry_index].kind == "fetch-watch"]
    assert {v.verdict for v in watch(_expected("work"))} == {"prevented-offline"}
    assert {v.verdict for v in watch(_expected("personal"))} == {"recorded"}
    monkeypatch.setattr(inventory, "NPM_OFFLINE_VERIFIED", False)
    assert {(v.verdict, v.code) for v in watch(_expected("work"))} == {("refused", "executable-fetch-possible")}


def test_an_unreadable_config_source_is_refused_in_work_only(tmp_path):
    world = World(tmp_path)
    world.write(world.project / "opencode.json", "{")
    inv = world.collect()
    work = inventory.evaluate(inv, _expected("work"))
    assert [(v.verdict, v.code) for v in work if v.entry_index == -1] == [("refused", "config-layer-unreadable")]
    assert all(v.verdict != "refused" for v in inventory.evaluate(inv, _expected("personal")))


def test_ordering_is_deterministic_and_first_refusal_is_the_first_in_load_order(tmp_path):
    world = World(tmp_path)
    world.write(world.global_dir / "opencode.json", {"permission": {}, "lsp": {"a": {}}})
    inv = world.collect()
    expected = _expected("work")
    first = inventory.evaluate(inv, expected)
    assert first == inventory.evaluate(inv, expected)
    assert inventory.first_refusal(first).code == "inventory-config-permission"
    assert inventory.first_refusal([]) is None


def test_refusal_text_is_closed_and_never_carries_a_path_or_secret(tmp_path):
    for code, text in inventory.REFUSAL_TEXT.items():
        assert "/" not in text and "http" not in text, code
    message = inventory.refusal_message("inventory-mcp-local", "global", inventory.entry_token(SECRET))
    assert SECRET not in message and message.startswith(inventory.REFUSAL_TEXT["inventory-mcp-local"])
    assert inventory.entry_token("plain-name") == "plain-name"
    assert inventory.entry_token("a/b").startswith("h-")
    assert inventory.entry_token("h-abc").startswith("h-") and inventory.entry_token("h-abc") != "h-abc"


def test_npm_offline_flag_is_tied_to_the_verified_rows():
    text = (Path(inventory.__file__).resolve().parents[3] / "quoin" / "adapters" / "opencode" / "compatibility.md").read_text(encoding="utf-8")
    for key in ("npm-config-env", "npm-install-skip-conditions", "npm-config-precedence"):
        assert key in text
    assert inventory.NPM_OFFLINE_VERIFIED is True
