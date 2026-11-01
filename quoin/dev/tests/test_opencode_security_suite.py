"""Attempt matrix for the work-profile isolation: every way the specification
names to reach the personal provider, read the opposite profile or change the
approved configuration, driven through the real prepare and start paths with
the capture listeners, the egress guard and the fake executable's own
allowlist watching.

Outcome kinds: a refusal happens before any process is spawned and before any
credential is resolved; a spawned run reaches only the work gateway; a
record-only row asserts what the effective-config record and the doctor say
without running the attempt (the fake really egresses, so an attempt is never
spawned in a world without a real boundary).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import _opencode_capture_helpers as ch
from _opencode_capture_helpers import egress_guard  # noqa: F401  (pytest fixture)
import _opencode_driver_helpers as dh
import _opencode_isolation_helpers as iso
import test_opencode_driver_prepare as dp
from _opencode_run_helpers import InstalledProject
from test_opencode_acceptance_matrix import MD, gdir, personal_setup, write_global, write_project
from test_opencode_fetch_watch import _run as fetch_run
from quoin.opencode_adapter import config, doctor, driver, launch_env, paths, profile_dirs, runstore
from quoin.opencode_adapter import secrets as credential_refs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

# Every attempt the specification lists. A row below must exist for each.
FR82_ATTEMPT_IDS = (
    "prov-project", "prov-dotdir", "prov-global", "prov-env", "cli-model", "child-model", "cmd-model",
    "aux-models", "plugin-key", "plugin-file", "mcp-local", "mcp-remote", "lsp", "formatter", "web-tools",
    "disabled-providers", "label-host", "loopback-trust", "provider-call", "shell-net", "shell-net-proxy",
    "read-opposite", "plugin-load",
)

PERSONAL_HOST = "personal.example.invalid"
PROVIDER_BLOCK = {"provider": {"anthropic": {"options": {}}}}


class CountingResolver:
    """A resolver that counts every credential it is asked for."""

    def __init__(self) -> None:
        self.calls = []

    def resolve(self, ref):
        self.calls.append(ref)
        return credential_refs.SecretValue(dp.SECRET)


def _env_row(name, value_of, code):
    def setup(denv, tmp_path):
        other = tmp_path / "other.json"
        other.write_text("{}", encoding="utf-8")
        return {name: value_of(str(other), str(tmp_path))}

    return setup, code


def _write(rel, text):
    return lambda d, tmp: d.write(rel, text)


# (attempt id, row label, setup(denv, tmp_path) -> optional environment additions, refusal code)
REFUSAL_ROWS = [
    ("prov-project", "project opencode.json", lambda d, t: write_project(d, PROVIDER_BLOCK), "inventory-provider"),
    ("prov-dotdir", "project .opencode", lambda d, t: d.write(".opencode/opencode.json", json.dumps(PROVIDER_BLOCK)) and None, "inventory-provider"),
    ("prov-global", "global config", lambda d, t: write_global(d, PROVIDER_BLOCK), "inventory-provider"),
    ("prov-env", "OPENCODE_CONFIG", *_env_row("OPENCODE_CONFIG", lambda other, tmp: other, "config-env-opencode-config")),
    ("prov-env", "OPENCODE_CONFIG_DIR", *_env_row("OPENCODE_CONFIG_DIR", lambda other, tmp: tmp, "config-env-opencode-config-dir")),
    ("prov-env", "OPENCODE_CONFIG_CONTENT", *_env_row("OPENCODE_CONFIG_CONTENT", lambda other, tmp: "{}", "config-env-opencode-config-content")),
    ("child-model", "agent markdown model", _write(".opencode/agent/helper.md", MD % "model: openrouter/not-allowed"), "agent-model-outside-allowlist"),
    ("cmd-model", "command model", _write(".opencode/command/c.md", MD % "model: openrouter/x"), "command-model-outside-allowlist"),
    ("aux-models", "small_model", lambda d, t: write_project(d, {"small_model": "x/y"}), "protected-key-overridden"),
    ("aux-models", "title model", lambda d, t: write_project(d, {"agent": {"title": {"model": "x/y"}}}), "protected-key-overridden"),
    ("aux-models", "compaction model", lambda d, t: write_project(d, {"agent": {"compaction": {"model": "x/y"}}}), "protected-key-overridden"),
    ("plugin-key", "plugin key", lambda d, t: write_project(d, {"plugin": ["x"]}), "protected-key-overridden"),
    ("plugin-file", "plugin file", _write(".opencode/plugin/x.js", "x"), "plugin-directory-present"),
    ("mcp-local", "local server", lambda d, t: write_project(d, {"mcp": {"s": {"type": "local", "command": ["x"]}}}), "inventory-mcp-local"),
    ("mcp-remote", "remote server", lambda d, t: write_project(d, {"mcp": {"s": {"type": "remote", "url": "https://%s/" % PERSONAL_HOST}}}), "inventory-mcp-remote"),
    ("lsp", "lsp command", lambda d, t: write_project(d, {"lsp": {"pyright": {}}}), "inventory-lsp"),
    ("formatter", "formatter command", lambda d, t: write_project(d, {"formatter": {"black": {}}}), "inventory-formatter"),
    ("web-tools", "agent webfetch allow", _write(".opencode/agent/helper.md", MD % "permission:\n  webfetch: allow"), "inventory-agent-permission"),
    ("disabled-providers", "disabled_providers", lambda d, t: write_project(d, {"disabled_providers": ["anthropic"]}), "inventory-disabled-providers"),
]


@pytest.fixture
def denv(tmp_path, monkeypatch):
    return dp.Setup(tmp_path, monkeypatch)


def _ids():
    return ["%s:%s" % (row[0], row[1]) for row in REFUSAL_ROWS]


def _spawned_nothing(denv):
    assert not (denv.tmp / "fake-state" / "invocations.jsonl").exists()


def _silent(guard):
    assert guard.work.captures() == [] and guard.personal.captures() == [] and guard.proxy.records() == []


@pytest.mark.parametrize("index", range(len(REFUSAL_ROWS)), ids=_ids())
def test_each_refusal_row_refuses_before_spawn_credentials_and_traffic(denv, tmp_path, egress_guard, index):
    _attempt, _label, setup, code = REFUSAL_ROWS[index]
    result = setup(denv, tmp_path)
    extra = result if isinstance(result, dict) else {}
    resolver = CountingResolver()
    base = dict(denv.driver()._env)  # noqa: SLF001
    drv = denv.driver(env=dict(base, **extra), resolver_factory=lambda environ, platform: resolver)
    error = denv.refused(code, drv=drv)
    assert error.code == code
    _spawned_nothing(denv)
    assert resolver.calls == []
    _silent(egress_guard)


def test_a_launch_that_passes_resolves_a_credential(denv):
    """Positive twin of the zero-credential assertion on refused rows."""
    resolver = CountingResolver()
    denv.driver(resolver_factory=lambda environ, platform: resolver).prepare(denv.request())
    assert len(resolver.calls) >= 1


def test_label_host_refuses_a_label_approved_provider_on_an_unapproved_host(denv, egress_guard):
    providers = denv.world.profile["providers"]
    providers["corp-gw"]["base_url"] = "https://%s/v1" % PERSONAL_HOST
    denv.world.write()
    denv.world.write_records()
    identity = iso.FakeIdentity()
    resolver = CountingResolver()
    drv = denv.driver(identity_checker=identity, resolver_factory=lambda environ, platform: resolver)
    error = denv.refused("destination-not-approved", drv=drv)
    assert error.detail == "host-not-allowed"
    assert resolver.calls == [] and PERSONAL_HOST not in error.message
    _spawned_nothing(denv)
    _silent(egress_guard)


# ---------------------------------------------------------------- spawned rows


def _request(project):
    return driver.RunRequest(project_root=project.root, task="demo", stage=None, phase="plan", profile="work")


def _run_through_driver(project):
    drv = project.driver_factory()(project.root)
    prepared = drv.prepare(_request(project))
    handle = drv.start(prepared)
    for _event in drv.observe(handle):
        pass
    return prepared, handle


def _world(tmp_path, monkeypatch, guard, scenario, *, allowed=True, secret=None):
    doc = ch.with_allowed_targets(scenario, guard.allowed_targets()) if allowed else dh.scenario_dict(scenario)
    project = InstalledProject(tmp_path, monkeypatch, doc, secret=secret or ch.make_seed("wseed"))
    ch.bind_work_providers(project.world, guard.work)
    guard.watch_network_log(dh.state_of(tmp_path) / "network.jsonl", required=True)
    return project


def _calls_scenario(*roles):
    return dh.fake._scenario(  # noqa: SLF001
        *[{"do": "provider_call", "role": role} for role in roles], *dh.fake._clean_finish())  # noqa: SLF001


@pytest.fixture
def cleanup(tmp_path):
    yield
    dh.cleanup_fakes(tmp_path)
    assert dh.stray_pids(tmp_path) == []


def test_an_allowlisted_role_call_reaches_the_work_gateway_only(tmp_path, monkeypatch, egress_guard, cleanup):
    project = _world(tmp_path, monkeypatch, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"))
    _prepared, handle = _run_through_driver(project)
    assert handle.outcome.state == "completed"
    assert len(egress_guard.work.captures()) == 1
    assert egress_guard.personal.captures() == [] and egress_guard.proxy.records() == []
    assert len(project.resolver_calls) >= 1
    argv = project.invocations()[0]["argv"]
    assert "--model" not in argv and not any(a.startswith("--model") for a in argv)


def test_auxiliary_and_title_calls_stay_on_the_approved_gateway(tmp_path, monkeypatch, egress_guard, cleanup):
    """The allowlist reaches the fake through the scenario file, not the environment."""
    roles = ("implementer", "title", "compaction", "small")
    project = _world(tmp_path, monkeypatch, egress_guard, _calls_scenario(*roles))
    prepared, handle = _run_through_driver(project)
    assert handle.outcome.state == "completed", handle.outcome
    doc = json.loads(Path(prepared.launch_env.materialize()["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    expected = []
    for role in roles:
        provider_id, _, model_id = dh.fake._document_model(doc, role).partition("/")  # noqa: SLF001
        assert model_id in doc["provider"][provider_id]["whitelist"]
        expected.append(model_id)
    assert [c["model"] for c in egress_guard.work.captures()] == expected
    network = [json.loads(line) for line in (dh.state_of(tmp_path) / "network.jsonl").read_text().splitlines()]
    assert len(network) == len(roles) and all(r["ok"] and not r["blocked"] for r in network)
    assert egress_guard.personal.captures() == []
    assert not any(name.startswith("FAKE_") for name in project.invocations()[0]["env_names"])


def test_the_work_seed_is_delivered_to_the_child_and_the_gateway(tmp_path, monkeypatch, egress_guard, cleanup):
    seed = ch.make_seed("wseed")
    scenario = dh.fake._scenario(  # noqa: SLF001
        {"do": "hash_env", "name": config.provider_env_name(ch.WORK_PROVIDER_ID)}, {"do": "provider_call", "role": "implementer"},
        *dh.fake._clean_finish())  # noqa: SLF001
    project = _world(tmp_path, monkeypatch, egress_guard, scenario, secret=seed)
    _run_through_driver(project)
    digest = hashlib.sha256(("Bearer " + seed).encode()).hexdigest()
    assert egress_guard.work.captures()[0]["auth_sha256"] == digest


def test_without_an_allowlist_the_fake_blocks_and_the_guard_fails(tmp_path, monkeypatch, egress_guard, cleanup):
    """Negative twin of the launcher control: no list, no egress."""
    project = _world(tmp_path, monkeypatch, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"),
                     allowed=False)
    _prepared, handle = _run_through_driver(project)
    assert handle.outcome.state != "completed"
    log = (dh.state_of(tmp_path) / "network.jsonl").read_text()
    assert json.loads(log.splitlines()[0])["blocked"] is True
    assert egress_guard.work.captures() == []
    with pytest.raises(AssertionError, match="blocked egress"):
        egress_guard.check()
    egress_guard.skip_teardown_check = True


def test_production_launch_environment_names_hold_no_fake_variable():
    assert not any(str(name).startswith("FAKE_") for name in launch_env._ALLOWED_ENV)  # noqa: SLF001


def test_a_loopback_gateway_is_recorded_as_loopback_never_checked(tmp_path, monkeypatch, egress_guard, cleanup):
    project = _world(tmp_path, monkeypatch, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"))
    prepared, _handle = _run_through_driver(project)
    record = runstore.load_record(runstore.store_dir(project.root), prepared.run_id)["prepared"]
    providers = [d for d in record["effective_config"]["network"]["destinations"] if d["role"] == "provider"]
    assert providers and {d["host"] for d in providers} == {"loopback"}
    assert {o["kind"] for o in record["identity_check"]} == {"loopback"}


# -------------------------------------------------- shell egress, opposite profile


def _rows(record):
    return {r["control"]: r for r in record["enforcement"]}


def _prepared_record(denv, prepared):
    return runstore.load_record(runstore.store_dir(denv.root), prepared.run_id)["prepared"]["effective_config"]


BOUNDARY_CONTROLS = ("shell-egress", "redirects", "dns-after-launch", "opposite-profile-files")


def test_unverified_managed_work_refuses_before_any_spawn(denv, egress_guard):
    """shell-net, shell-net-proxy and read-opposite, managed with no attestation."""
    resolver = CountingResolver()
    drv = denv.driver(isolation_roots=iso.unverified_roots(denv.tmp / "empty"),
                      resolver_factory=lambda environ, platform: resolver)
    denv.refused("isolation-required-unverified", drv=drv)
    assert resolver.calls == [] and not (profile_dirs.adapter_root(denv.world.env, denv.world.home) / "work").exists()
    _spawned_nothing(denv)
    _silent(egress_guard)


def test_an_opposite_profile_on_the_account_also_refuses_a_verified_looking_world(denv):
    (paths.profiles_dir(denv.world.env, denv.world.home) / "personal.json").write_text("{}")
    error = denv.refused("isolation-required-unverified")
    assert error.detail == "opposite-profile-present"


def test_verified_roots_record_the_boundary_rows_and_point_nowhere_near_the_opposite_root(denv):
    prepared = denv.driver().prepare(denv.request())
    rows = _rows(_prepared_record(denv, prepared))
    for control in BOUNDARY_CONTROLS:
        assert (rows[control]["layer"], rows[control]["status"]) == ("managed-boundary", "delegated-verified")
    opposite = str(profile_dirs.adapter_root(denv.world.env, denv.world.home) / "personal")
    assert not any(opposite in str(v) for v in prepared.launch_env.materialize().values())
    assert not any(opposite in str(a) for a in prepared.argv)
    assert opposite not in json.dumps(_prepared_record(denv, prepared))


def test_a_convenience_world_says_the_boundary_rows_are_not_enforced(denv):
    policy = denv.world.profile.setdefault("policy", {})
    policy["isolation"] = "convenience"
    denv.world.write()
    drv = denv.driver(isolation_roots=iso.unverified_roots(denv.tmp / "empty"))
    prepared = drv.prepare(denv.request())
    rows = _rows(_prepared_record(denv, prepared))
    for control in BOUNDARY_CONTROLS:
        assert rows[control]["status"] == "not-enforced", control
    found = doctor._inventory_findings(drv, "work", None, dict(drv._env), strict=False)  # noqa: SLF001
    assert "egress-shell-not-enforced" in {f.id for f in found}


def test_a_personal_convenience_profile_records_no_boundary(tmp_path, monkeypatch):
    penv = personal_setup(tmp_path, monkeypatch)
    drv = penv.driver(isolation_roots=iso.unverified_roots(penv.tmp / "empty"))
    prepared = drv.prepare(penv.request(profile="personal"))
    rows = _rows(_prepared_record(penv, prepared))
    assert rows["shell-egress"]["status"] == "not-enforced"


# ---------------------------------------------------------------- plugin load


def test_an_install_into_a_scanned_directory_is_detected_unless_offline(tmp_path):
    try:
        _prepared, handle = fetch_run(tmp_path, "npm_install")
        assert (handle.outcome.state, handle.outcome.reason) == ("failed", "executable-fetch-detected")
    finally:
        dh.cleanup_fakes(tmp_path)
    sub = tmp_path / "offline"
    sub.mkdir()
    try:
        _prepared, handle = fetch_run(sub, "npm_install", offline=True)
        assert handle.outcome.state == "completed"
        assert not (dh.project_of(sub) / ".opencode" / "node_modules").exists()
    finally:
        dh.cleanup_fakes(sub)


# ---------------------------------------------------------------- completeness

# Attempt ids covered by a dedicated test above rather than a table row.
DEDICATED = {
    "label-host": "test_label_host_refuses_a_label_approved_provider_on_an_unapproved_host",
    "loopback-trust": "test_a_loopback_gateway_is_recorded_as_loopback_never_checked",
    "provider-call": "test_an_allowlisted_role_call_reaches_the_work_gateway_only",
    "cli-model": "test_an_allowlisted_role_call_reaches_the_work_gateway_only",
    "shell-net": "test_verified_roots_record_the_boundary_rows_and_point_nowhere_near_the_opposite_root",
    "shell-net-proxy": "test_unverified_managed_work_refuses_before_any_spawn",
    "read-opposite": "test_an_opposite_profile_on_the_account_also_refuses_a_verified_looking_world",
    "plugin-load": "test_an_install_into_a_scanned_directory_is_detected_unless_offline",
}


def test_every_specified_attempt_has_a_row():
    covered = {row[0] for row in REFUSAL_ROWS} | set(DEDICATED)
    assert covered == set(FR82_ATTEMPT_IDS)
    for name in set(DEDICATED.values()):
        assert callable(globals().get(name)), name
