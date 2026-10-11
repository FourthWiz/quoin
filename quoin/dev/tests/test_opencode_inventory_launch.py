"""The launch configuration check with the inventory allowlist, and its capture mode."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from quoin.opencode_adapter import inventory, launch_env, profile_policy
from quoin.opencode_adapter.launch_env import LaunchRefused

SECRET_NAME = "sk-live-SEEDED-0123456789"
COMPILED = {
    "provider": {"prov": {"whitelist": ["m"]}},
    "model": "prov/m",
    "agent": {"quoin-plan": {"model": "prov/m"}},
}


class Project:
    def __init__(self, tmp_path):
        self.root = tmp_path / "project"
        (self.root / ".git").mkdir(parents=True)
        self.home = tmp_path / "home"
        self.xdg = tmp_path / "xdg"
        (self.xdg / "opencode").mkdir(parents=True)
        self.home.mkdir()
        self.compiled = tmp_path / "out" / "opencode.json"
        self.compiled.parent.mkdir()
        self.compiled.write_text("{}", encoding="utf-8")
        self.env = {"XDG_CONFIG_HOME": str(self.xdg), "OPENCODE_CONFIG": str(self.compiled)}
        self.managed = tmp_path / "managed"
        self.data = tmp_path / "data"

    def write(self, rel, data, base=None):
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def expected(self, classification="work"):
        policy = profile_policy.view(SimpleNamespace(values={}, classification=classification))
        return inventory.Expected.from_compiled(COMPILED, {}, policy)

    def check(self, *, expected=None, capture=False, **kw):
        kw.setdefault("owned_agents", {})
        kw.setdefault("owned_commands", {})
        kw.setdefault("managed_dir", self.managed)
        kw.setdefault("managed_prefs", [])
        return launch_env.check_config_layers(
            cwd=self.root, env=self.env, home=self.home, compiled_doc=COMPILED,
            expected=expected, capture=capture, **kw,
        )


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


def _legacy_worlds(p):
    """(label, setup callable, expected legacy code)"""
    return [
        ("plugin file", lambda: p.write(".opencode/plugin/x.js", "x"), "plugin-directory-present"),
        ("plugin key", lambda: p.write("opencode.json", {"plugin": ["x"]}), "protected-key-overridden"),
        ("small_model", lambda: p.write("opencode.json", {"small_model": "x/y"}), "protected-key-overridden"),
        ("unreadable layer", lambda: p.write("opencode.json", "{"), "config-layer-unreadable"),
        ("quoin agent shadow", lambda: p.write(".opencode/agent/quoin-x.md", "---\n---\n"), "agent-file-overridden"),
    ]


def test_without_expected_the_check_returns_a_clean_layer_check(project):
    project.write("opencode.json", {"mcp": {"s": {"type": "local", "command": ["x"]}}})
    result = project.check()
    assert result.refusal is None and result.verdicts == []
    assert result.inventory.entries


@pytest.mark.parametrize("index", range(5))
def test_capture_mode_returns_the_code_the_raising_call_raises(tmp_path, index):
    p = Project(tmp_path)
    label, setup, code = _legacy_worlds(p)[index]
    setup()
    with pytest.raises(LaunchRefused) as info:
        p.check(expected=p.expected())
    assert info.value.code == code, label
    captured = p.check(expected=p.expected(), capture=True)
    assert captured.refusal is not None and captured.refusal.code == code
    assert captured.refusal.category == info.value.category
    assert captured.refusal.message == info.value.message
    assert captured.verdicts, "evaluate still runs so explain can list every entry"


def test_legacy_rules_win_over_the_allowlist(project):
    project.write("opencode.json", {"plugin": ["x"], "lsp": {"a": {}}})
    captured = project.check(expected=project.expected(), capture=True)
    assert captured.refusal.code == "protected-key-overridden"
    assert inventory.first_refusal(captured.verdicts).code == "inventory-plugin"


def test_an_inventory_refusal_is_raised_with_its_entry_token_and_no_path(project):
    project.write(project.xdg / "opencode" / "opencode.json", {"lsp": {SECRET_NAME: {}}}, base=project.root)
    with pytest.raises(LaunchRefused) as info:
        project.check(expected=project.expected())
    assert info.value.code == "inventory-lsp"
    assert info.value.category == "policy-denial"
    assert info.value.detail and info.value.detail.startswith("h-")
    for text in (info.value.message, info.value.detail):
        assert SECRET_NAME not in text and str(project.root) not in text and str(project.xdg) not in text
    assert "layer global" in info.value.message


def test_personal_records_what_work_refuses(tmp_path):
    p = Project(tmp_path)
    p.write(p.xdg / "opencode" / "opencode.json", {"mcp": {"s": {"type": "local", "command": ["x"]}}}, base=p.root)
    assert p.check(expected=p.expected("personal")).refusal is None
    assert p.check(expected=p.expected("work"), capture=True).refusal.code == "inventory-mcp-local"


def test_inventory_plugin_is_a_backstop_for_an_entry_the_legacy_rules_accepted(project):
    inv = inventory.Inventory(
        [inventory.Source("config-file", "project", "./x", None, "read")],
        [inventory.Entry("plugin", "project", 0, "plugin", "p", None, False, {})],
    )
    verdicts = inventory.evaluate(inv, project.expected())
    assert [(v.verdict, v.code) for v in verdicts] == [("refused", "inventory-plugin")]


def test_launch_refused_carries_an_optional_detail():
    assert LaunchRefused("c", "x", "m").detail is None
    assert LaunchRefused("c", "x", "m", detail="tok").detail == "tok"


# ------------------------------------------------- through the real driver

import os

import _opencode_helpers as _helpers
from quoin.opencode_adapter import driver as _driver
from quoin.opencode_adapter import effective_record, runstore
from test_opencode_driver_prepare import SECRET as DRIVER_SECRET
from test_opencode_driver_prepare import Setup

pytestmark_driver = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def denv(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _global_dir(denv):
    return denv.world.tmp / "xdg" / "opencode"


def _stored(denv, run_id):
    return runstore.load_record(runstore.store_dir(denv.root), run_id)


@pytestmark_driver
def test_a_clean_work_world_prepares_and_stores_a_self_consistent_record(denv):
    prepared = denv.driver().prepare(denv.request())
    stored = _stored(denv, prepared.run_id)["prepared"]["effective_config"]
    assert stored["digest"] == prepared.effective_config_digest == effective_record.digest(stored)
    assert stored["profile"] == "work" and stored["classification"] == "work"
    assert "OPENCODE_CONFIG" in stored["env"]["launcher_set"] and "UNRELATED_SECRET" not in stored["env"]["names"]
    assert DRIVER_SECRET not in json.dumps(stored) and str(denv.root) not in json.dumps(stored)
    again = denv.driver().prepare(denv.request())
    assert again.effective_config_digest == prepared.effective_config_digest and again.run_id != prepared.run_id


@pytestmark_driver
def test_a_refused_world_stores_the_code_and_entry_token_but_no_record(denv):
    gdir = _global_dir(denv)
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "opencode.json").write_text(json.dumps({"lsp": {"pyright": {}}}), encoding="utf-8")
    error = denv.refused("inventory-lsp", "policy-denial")
    stored = _stored(denv, error.run_id)
    assert stored["refusal"]["code"] == "inventory-lsp" and stored["refusal"]["detail"] == "pyright"
    assert "effective_config" not in (stored.get("prepared") or {})
    assert str(denv.root) not in error.message and "layer global" in error.message


@pytestmark_driver
def test_the_inherited_compiled_file_is_inert_and_another_one_is_refused(denv):
    first = denv.driver().prepare(denv.request())
    inherited = dict(denv.world.env, PATH=os.environ.get("PATH", ""), OPENCODE_CONFIG=str(first.config_path))
    second = denv.driver(env=inherited).prepare(denv.request())
    assert second.effective_config_digest
    other = denv.tmp / "other.json"
    other.write_text("{}", encoding="utf-8")
    foreign = dict(inherited, OPENCODE_CONFIG=str(other), OPENCODE_CONFIG_DIR=str(denv.tmp))
    denv.refused("config-env-opencode-config", drv=denv.driver(env=foreign))


@pytestmark_driver
def test_a_database_with_an_active_account_in_the_profile_data_is_refused_under_work(denv):
    import sqlite3

    prepared = denv.driver().prepare(denv.request())
    profile_data = prepared.launch_env.materialize()["XDG_DATA_HOME"]
    target = os.path.join(profile_data, "opencode")
    os.makedirs(target, exist_ok=True)
    conn = sqlite3.connect(os.path.join(target, "opencode.db"))
    conn.execute("create table account_state (id integer primary key, active_account_id text)")
    conn.execute("insert into account_state values (1, 'acct')")
    conn.commit()
    conn.close()
    denv.refused("inventory-remote-config")


@pytestmark_driver
def test_an_ambient_config_file_with_the_compiled_files_name_elsewhere_is_refused(denv):
    first = denv.driver().prepare(denv.request())
    elsewhere = denv.tmp / "elsewhere"
    elsewhere.mkdir()
    twin = elsewhere / first.config_path.name
    twin.write_text("{}", encoding="utf-8")
    env = dict(denv.world.env, PATH=os.environ.get("PATH", ""), OPENCODE_CONFIG=str(twin))
    denv.refused("config-env-opencode-config", drv=denv.driver(env=env))
