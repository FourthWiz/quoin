"""The configuration allowlist through the real `prepare` path: what a work
profile refuses (each with its own code), what stays allowed, and what the
record keeps. No fake process is ever started by a refused world."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

import test_opencode_driver_prepare as dp
from _opencode_merge_helpers import PROFILE_PERSONAL, World
from quoin import cli
from quoin.opencode_adapter import driver, runstore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SEED_NAME = "sk-live-SEEDED-0123456789"
SEED_HOST = "seeded-host.example.invalid"


@pytest.fixture
def denv(tmp_path, monkeypatch):
    return dp.Setup(tmp_path, monkeypatch)


def personal_setup(tmp_path, monkeypatch, **policy):
    profile = json.loads(json.dumps(__import__("_opencode_merge_helpers").fixture(PROFILE_PERSONAL)))
    if policy:
        profile["policy"] = policy
    monkeypatch.setattr(dp, "World", lambda tmp, agents=False, root=None: World(
        tmp, profile=profile, agents=agents, root=root))
    return dp.Setup(tmp_path, monkeypatch)


def gdir(denv):
    return denv.world.tmp / "xdg" / "opencode"


def write_global(denv, doc):
    gdir(denv).mkdir(parents=True, exist_ok=True)
    (gdir(denv) / "opencode.json").write_text(json.dumps(doc), encoding="utf-8")


def write_project(denv, doc):
    denv.write("opencode.json", json.dumps(doc))


def profile_data(denv, name="work"):
    prepared = denv.driver().prepare(denv.request(profile=name))
    return os.path.join(prepared.launch_env.materialize()["XDG_DATA_HOME"], "opencode")


def no_spawn(denv):
    assert not (denv.tmp / "fake-state" / "invocations.jsonl").exists()


# ----------------------------------------------------------------------------
# AC-3: work refusals, each with its own code
# ----------------------------------------------------------------------------

MD = "---\n%s\n---\nbody\n"

WORK_REFUSALS = [
    ("plugin file", lambda d: d.write(".opencode/plugin/x.js", "x"), "plugin-directory-present"),
    ("global provider", lambda d: write_global(d, {"provider": {"anthropic": {"options": {}}}}), "inventory-provider"),
    ("disabled providers", lambda d: write_project(d, {"disabled_providers": ["anthropic"]}), "inventory-disabled-providers"),
    ("local mcp", lambda d: write_project(d, {"mcp": {"s": {"type": "local", "command": ["x"]}}}), "inventory-mcp-local"),
    ("remote mcp", lambda d: write_project(d, {"mcp": {"s": {"type": "remote", "url": "https://x.invalid"}}}), "inventory-mcp-remote"),
    ("tool file", lambda d: d.write(".opencode/tool/x.ts", "export default {}"), "inventory-tool"),
    ("lsp", lambda d: write_project(d, {"lsp": {"pyright": {}}}), "inventory-lsp"),
    ("formatter", lambda d: write_project(d, {"formatter": {"black": {}}}), "inventory-formatter"),
    ("agent permission", lambda d: d.write(".opencode/agent/helper.md", MD % "permission:\n  webfetch: allow"), "inventory-agent-permission"),
    ("global permission", lambda d: write_global(d, {"permission": {"bash": "allow"}}), "inventory-config-permission"),
    ("command model", lambda d: d.write(".opencode/command/c.md", MD % "model: openrouter/x"), "command-model-outside-allowlist"),
    ("instructions url", lambda d: write_project(d, {"instructions": ["https://x.invalid/a.md"]}), "inventory-remote-instructions"),
    ("unknown key", lambda d: write_project(d, {"zzz": 1}), "inventory-unknown-key"),
    ("legacy toml", lambda d: (gdir(d).mkdir(parents=True, exist_ok=True), (gdir(d) / "config").write_text("x = 1\n", encoding="utf-8")), "inventory-legacy-config"),
]


@pytest.mark.parametrize("index", range(len(WORK_REFUSALS)))
def test_each_work_refusal_has_its_own_code_and_spawns_nothing(denv, index):
    label, setup, code = WORK_REFUSALS[index]
    setup(denv)
    error = denv.refused(code)
    assert error.code == code, label
    no_spawn(denv)


def test_a_well_known_entry_in_the_profile_auth_file_is_refused(denv):
    target = profile_data(denv)
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "auth.json"), "w", encoding="utf-8") as handle:
        json.dump({"https://corp.invalid": {"type": "wellknown", "token": "t"}}, handle)
    denv.refused("inventory-remote-config")


def test_the_three_ambient_config_variables_are_refused_with_their_own_codes(denv, tmp_path):
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    base = dict(denv.driver()._env)  # noqa: SLF001
    for name, value, code in (
        ("OPENCODE_CONFIG", str(other), "config-env-opencode-config"),
        ("OPENCODE_CONFIG_DIR", str(tmp_path), "config-env-opencode-config-dir"),
        ("OPENCODE_CONFIG_CONTENT", "{}", "config-env-opencode-config-content"),
    ):
        denv.refused(code, drv=denv.driver(env=dict(base, **{name: value})))


def test_legacy_toml_is_also_refused_for_a_personal_profile(tmp_path, monkeypatch):
    denv = personal_setup(tmp_path, monkeypatch)
    gdir(denv).mkdir(parents=True, exist_ok=True)
    (gdir(denv) / "config").write_text("x = 1\n", encoding="utf-8")
    denv.refused("inventory-legacy-config", request=denv.request(profile="personal"))


# ----------------------------------------------------------------------------
# AC-2: the paths Quoin can see
# ----------------------------------------------------------------------------


def test_provider_changes_through_each_visible_layer_are_refused(denv, tmp_path):
    denv.write("opencode.json", json.dumps({"provider": {"anthropic": {"options": {}}}}))
    denv.refused("inventory-provider")
    (denv.root / "opencode.json").unlink()
    denv.write(".opencode/opencode.json", json.dumps({"provider": {"anthropic": {"options": {}}}}))
    denv.refused("inventory-provider")
    (denv.root / ".opencode" / "opencode.json").unlink()
    write_global(denv, {"provider": {"anthropic": {"options": {}}}})
    denv.refused("inventory-provider")
    (gdir(denv) / "opencode.json").unlink()
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"provider": {"anthropic": {}}}), encoding="utf-8")
    env = dict(denv.driver()._env, OPENCODE_CONFIG=str(other))  # noqa: SLF001
    denv.refused("config-env-opencode-config", drv=denv.driver(env=env))


@pytest.mark.parametrize("personal", [False, True])
def test_a_child_agent_model_outside_the_allowlist_is_refused(tmp_path, monkeypatch, personal):
    denv = personal_setup(tmp_path, monkeypatch) if personal else dp.Setup(tmp_path, monkeypatch)
    denv.write(".opencode/agent/helper.md", MD % "model: openrouter/not-allowed")
    denv.refused("agent-model-outside-allowlist", request=denv.request(profile="personal" if personal else "work"))


@pytest.mark.parametrize("doc", [
    {"small_model": "x/y"}, {"agent": {"title": {"model": "x/y"}}}, {"agent": {"compaction": {"model": "x/y"}}},
])
def test_protected_model_keys_in_a_project_layer_are_refused(denv, doc):
    write_project(denv, doc)
    denv.refused("protected-key-overridden")


def test_the_driver_argv_has_no_model_flag_and_start_rejects_one(denv, capsys):
    prepared = denv.driver().prepare(denv.request())
    assert "--model" not in prepared.argv and not any(a.startswith("--model") for a in prepared.argv)
    with pytest.raises(SystemExit) as info:
        cli.main(["opencode", "start", "--profile", "work", "--model", "x", "--project-root", str(denv.root)])
    assert info.value.code == 2
    capsys.readouterr()


# ----------------------------------------------------------------------------
# AC-4: the record
# ----------------------------------------------------------------------------


def stored_record(denv, prepared):
    return runstore.load_record(runstore.store_dir(denv.root), prepared.run_id)["prepared"]["effective_config"]


ADDED_KINDS = [
    ("lsp", lambda d: write_project(d, {"lsp": {"pyright": {}}})),
    ("mcp", lambda d: write_project(d, {"mcp": {"s": {"type": "local", "command": ["x"]}}})),
    ("plugin", lambda d: write_project(d, {"plugin": ["x"]})),
    ("tool", lambda d: d.write(".opencode/tool/x.ts", "export default {}")),
    ("agent", lambda d: d.write(".opencode/agent/helper.md", MD % "description: x")),
    ("command", lambda d: d.write(".opencode/command/c.md", MD % "description: x")),
    ("instruction", lambda d: write_project(d, {"instructions": ["https://x.invalid/a.md"]})),
    ("unknown-key", lambda d: write_project(d, {"zzz": 1})),
    ("benign-key", lambda d: write_project(d, {"theme": "dark"})),
]


def test_the_digest_is_stable_and_changes_with_each_added_entry_kind(denv):
    first = denv.driver().prepare(denv.request())
    second = denv.driver().prepare(denv.request())
    assert first.effective_config_digest == second.effective_config_digest
    base = first.effective_config_digest
    seen = {base}
    for label, add in ADDED_KINDS:
        add(denv)
        digest = denv.driver().effective_preview("work")[0]["digest"]
        assert digest not in seen, label
        seen.add(digest)
        for rel in ("opencode.json", ".opencode/tool/x.ts", ".opencode/agent/helper.md", ".opencode/command/c.md"):
            path = denv.root / rel
            if path.exists():
                path.unlink()
    assert denv.driver().effective_preview("work")[0]["digest"] == base


def test_the_persisted_record_and_every_refusal_text_hold_no_seeded_secret_path_or_host(denv):
    prepared = denv.driver().prepare(denv.request())
    blob = json.dumps(stored_record(denv, prepared))
    for seed in (dp.SECRET, "leak-1234567890", str(denv.root), str(denv.world.home)):
        assert seed not in blob
    write_project(denv, {
        "lsp": {SEED_NAME: {}},
        "mcp": {SEED_HOST: {"type": "remote", "url": "https://%s/x" % SEED_HOST}},
    })
    error = denv.refused("inventory-lsp")
    stored = runstore.load_record(runstore.store_dir(denv.root), error.run_id)
    texts = [error.message, error.detail or "", json.dumps(stored.get("refusal"))]
    for text in texts:
        for seed in (SEED_NAME, SEED_HOST, str(denv.root), str(denv.world.home), dp.SECRET):
            assert seed not in text


# ----------------------------------------------------------------------------
# AC-6: executable-fetch prevention
# ----------------------------------------------------------------------------


def test_a_work_run_is_offline_and_pure_and_its_fetch_watch_rows_say_so(denv):
    prepared = denv.driver().prepare(denv.request())
    names = prepared.launch_env.names()
    assert "OPENCODE_PURE" in names and "npm_config_offline" in names
    record = stored_record(denv, prepared)
    rows = [e for e in record["entries"] if e["kind"] == "fetch-watch"]
    assert rows and all(e["verdict"] == "prevented-offline" for e in rows)
    denv.write(".opencode/plugin/x.js", "x")
    denv.refused("plugin-directory-present")


# ----------------------------------------------------------------------------
# over-refusal guards
# ----------------------------------------------------------------------------


def test_a_realistic_personal_setup_prepares_and_records_what_it_found(tmp_path, monkeypatch):
    denv = personal_setup(tmp_path, monkeypatch)
    write_global(denv, {
        "theme": "dark", "keybinds": {"leader": "ctrl+x"}, "tui": {"scroll_speed": 3}, "username": "me",
        "mcp": {"s": {"type": "local", "command": ["x"]}}, "lsp": {"pyright": {}},
        "provider": {"openrouter": {"options": {"timeout": 1000}}}, "enabled_providers": ["openrouter"],
    })
    (gdir(denv) / "agent").mkdir(parents=True, exist_ok=True)
    (gdir(denv) / "agent" / "helper.md").write_text(MD % "permission:\n  edit: ask", encoding="utf-8")
    denv.write(".opencode/agent/modeled.md", MD % "model: openrouter/example-vendor/example-model  # note")
    claude = denv.world.home / ".claude"
    claude.mkdir(exist_ok=True)
    (claude / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    request = denv.request(profile="personal")
    first = denv.driver().prepare(request)
    target = os.path.join(first.launch_env.materialize()["XDG_DATA_HOME"], "opencode")
    os.makedirs(target, exist_ok=True)
    db = os.path.join(target, "opencode.db")
    conn = sqlite3.connect(db)
    conn.execute("create table account_state (id integer primary key, active_account_id text)")
    conn.execute("insert into account_state values (1, null)")
    conn.commit()
    reader = sqlite3.connect(db)
    reader.execute("begin")
    reader.execute("select * from account_state").fetchall()
    try:
        prepared = denv.driver().prepare(request)
    finally:
        reader.close()
        conn.close()
    record = stored_record(denv, prepared)
    verdicts = {(e["kind"], e["verdict"]) for e in record["entries"]}
    assert not [e for e in record["entries"] if e["verdict"] == "refused"]
    assert ("benign-key", "benign") in verdicts


def test_a_provider_block_outside_the_personal_compiled_set_is_refused(tmp_path, monkeypatch):
    denv = personal_setup(tmp_path, monkeypatch)
    write_global(denv, {"provider": {"anthropic": {"options": {}}}})
    denv.refused("inventory-provider", request=denv.request(profile="personal"))


def test_the_in_set_provider_block_is_refused_under_work(denv):
    write_global(denv, {"provider": {"corp-gw": {"options": {"timeout": 1}}}})
    denv.refused("inventory-provider")


def test_a_work_world_with_only_benign_keys_prepares(denv):
    write_project(denv, {"theme": "dark", "keybinds": {}, "watcher": {"ignore": ["x"]}, "instructions": ["docs/rules.md"]})
    prepared = denv.driver().prepare(denv.request())
    assert prepared.effective_config_digest


def test_an_explicit_personal_mcp_denial_is_honoured(tmp_path, monkeypatch):
    denv = personal_setup(tmp_path, monkeypatch, mcp={"local": "deny"})
    write_project(denv, {"mcp": {"s": {"type": "local", "command": ["x"]}}})
    denv.refused("inventory-mcp-local", request=denv.request(profile="personal"))
