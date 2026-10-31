"""Managed isolation, destination approval and repositories through the real
prepare, TUI and preview paths. Every refusal happens before a process is
spawned, a profile directory is created or a credential is resolved."""
from __future__ import annotations

import dataclasses
import json
import os
import re

import pytest

import _opencode_isolation_helpers as iso
from _opencode_merge_helpers import NOW
from quoin import cli
from quoin.opencode_adapter import driver, explain, isolation, launch_codes, netpolicy, paths, profile_dirs, profile_policy
from test_opencode_driver_prepare import Setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

GW = "gateway.example.invalid"
GW_B = "gateway-b.example.invalid"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _policy(env, **fields):
    policy = env.world.profile.setdefault("policy", {})
    policy.update(fields)
    env.world.write()


def _refused(env, code, drv=None, detail=None):
    info = env.refused(code, drv=drv)
    if detail is not None:
        assert info.detail == detail
    assert info.category == "policy-denial"
    return info


def _drv(setup, **kw):
    return setup.driver(**kw)


def _prepared_record(env, prepared):
    from quoin.opencode_adapter import runstore

    return runstore.load_record(runstore.store_dir(env.root), prepared.run_id)["prepared"]


def _no_side_effects(env):
    adapter = profile_dirs.adapter_root(env.world.env, env.world.home)
    assert not (adapter / "work").exists(), "a refused launch must not create the profile directory"


# ------------------------------------------------------------------- AC-5


def test_managed_work_with_no_attestation_refuses_before_anything_is_created(env):
    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    info = _refused(env, "isolation-required-unverified", drv, "attestation-missing")
    _no_side_effects(env)
    from quoin.opencode_adapter import runstore

    record = runstore.load_record(runstore.store_dir(env.root), info.run_id)
    assert record["refusal"]["code"] == "isolation-required-unverified" and record["state"] == "prepared"


def test_a_verified_fixture_launches_and_records_the_verdict(env):
    prepared = _drv(env).prepare(env.request())
    effective = _prepared_record(env, prepared)["effective_config"]
    assert effective["isolation"]["status"] == "verified" and effective["isolation"]["environment"] == "container"
    assert effective["pending_sections"] == []


def _spoof_cases():
    uid = os.getuid()
    return {
        "user-owned-file": ({"owner_uid": uid + 7}, "attestation-not-root-owned"),
        "root-session": ({"invoking_uid": uid}, "invoking-user-is-root"),
        "expired": ({"attestation": {"not_after": "2000-01-01"}}, "attestation-expired"),
        "egress-none": (
            {"attestation": {"boundary": {"filesystem": True, "secrets": True, "process_env": True, "egress": "none"}}},
            "attestation-incomplete",
        ),
        "no-corroboration": ({"corroborate": False}, "corroboration-missing"),
    }


@pytest.mark.parametrize("name", sorted(_spoof_cases()))
def test_each_spoof_refuses_with_the_matching_detail(env, name):
    kwargs, token = _spoof_cases()[name]
    sub = env.tmp / ("spoof-" + name)
    drv = _drv(env, isolation_roots=iso.fixture_roots(sub, **kwargs))
    _refused(env, "isolation-required-unverified", drv, token)


def test_a_group_writable_file_a_writable_chain_and_a_symlink_refuse(env):
    for name, mutate, token in (
        ("file", lambda r: os.chmod(r.attestation_path, 0o664), "attestation-not-root-owned"),
        ("chain", lambda r: os.chmod(r.attestation_path.parent, 0o775), "attestation-chain-writable"),
        ("link", lambda r: (r.attestation_path.rename(r.attestation_path.with_name("real.json")),
                            r.attestation_path.symlink_to(r.attestation_path.with_name("real.json"))), "attestation-invalid"),
    ):
        roots = iso.fixture_roots(env.tmp / ("spoof-" + name))
        mutate(roots)
        _refused(env, "isolation-required-unverified", _drv(env, isolation_roots=roots), token)


def test_an_opposite_profile_on_the_account_refuses(env):
    other = paths.profiles_dir(env.world.env, env.world.home) / "other.json"
    other.write_text("{}")
    _refused(env, "isolation-required-unverified", detail="opposite-profile-present")


def test_a_convenience_world_launches_with_the_unverified_verdict_recorded(env):
    _policy(env, isolation="convenience")
    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    section = _prepared_record(env, drv.prepare(env.request()))["effective_config"]["isolation"]
    assert section["status"] == "unverified" and "attestation-missing" in section["missing"]


# ------------------------------------------------------------------- AC-7


def test_a_label_approved_provider_with_an_unapproved_host_refuses(env):
    _policy(env, allowed_hosts=[GW_B])
    info = _refused(env, "destination-not-approved", detail="host-not-allowed")
    assert GW not in info.message


def test_a_proxy_route_that_is_not_approved_refuses(env):
    drv = _drv(env, env=dict(env.world.env, PATH=os.environ.get("PATH", ""), HTTPS_PROXY="http://proxy.invalid:8080"))
    _refused(env, "proxy-route-not-approved", drv)
    _policy(env, allowed_hosts=[GW, GW_B, "proxy.invalid"])
    drv = _drv(env, env=dict(env.world.env, PATH=os.environ.get("PATH", ""), HTTPS_PROXY="http://proxy.invalid:8080"))
    drv.prepare(env.request())


def test_identity_mismatch_and_unverifiable_come_from_the_checker(env):
    mismatch = iso.FakeIdentity({GW: netpolicy.IdentityResult("mismatch", "tls", "name-mismatch")})
    _refused(env, "destination-identity-mismatch", _drv(env, identity_checker=mismatch), "name-mismatch")
    unverifiable = iso.FakeIdentity({GW_B: netpolicy.IdentityResult("unverifiable", "tls", "dns-failed")})
    _refused(env, "destination-unverifiable", _drv(env, identity_checker=unverifiable), "dns-failed")


def test_a_loopback_provider_records_loopback_and_never_reaches_the_checker(env):
    for provider in env.world.profile["providers"].values():
        provider["base_url"] = "http://127.0.0.1:9/v1"
    _policy(env, allowed_hosts=["127.0.0.1"])
    env.world.write_records()
    fake = iso.FakeIdentity()
    prepared = _prepared_record(env, _drv(env, identity_checker=fake).prepare(env.request()))
    assert fake.checked == []
    hosts = {d["host"] for d in prepared["effective_config"]["network"]["destinations"] if d["role"] == "provider"}
    assert hosts == {"loopback"}
    assert {o["kind"] for o in prepared["identity_check"]} == {"loopback"}


def test_a_non_loopback_plain_http_provider_never_launches(env):
    """Configuration validation refuses plain http beyond loopback first; the
    plaintext destination refusal behind it is pinned in the identity-check tests."""
    env.world.profile["providers"]["corp-gw"]["base_url"] = "http://%s/v1" % GW
    env.world.write()
    info = env.refused("insecure-http")
    assert info.run_id is not None or info.code


def test_identity_observations_sit_beside_the_record_not_in_its_digest(env):
    prepared = _prepared_record(env, _drv(env).prepare(env.request()))
    assert prepared["identity_check"] and "identity_check" not in prepared["effective_config"]
    assert all(o["host"].startswith("h-") for o in prepared["identity_check"])


def test_a_remote_mcp_server_on_an_unapproved_host_refuses(env):
    _policy(env, mcp={"remote": "allow-listed", "allowed": ["docs"]})
    glob = env.world.tmp / "xdg" / "opencode"
    glob.mkdir(parents=True, exist_ok=True)
    (glob / "opencode.json").write_text(json.dumps({"mcp": {"docs": {"type": "remote", "url": "https://docs.unapproved.invalid/m"}}}))
    _refused(env, "mcp-remote-denied")


# ------------------------------------------------------------ repositories


def test_a_project_outside_every_approved_root_refuses(env):
    _policy(env, repositories={"allowed_roots": [str(env.tmp / "elsewhere")]})
    _refused(env, "repository-not-approved")
    _no_side_effects(env)


def test_a_symlinked_project_root_is_compared_by_its_real_path(env):
    approved = env.tmp / "approved"
    approved.mkdir()
    link = approved / "link"
    link.symlink_to(env.root)
    _policy(env, repositories={"allowed_roots": [str(approved)]})
    env.root = link
    _refused(env, "repository-not-approved")


def test_project_only_roots_never_count_as_approval_under_managed_isolation(env):
    del env.world.profile["policy"]["repositories"]
    env.world.project["policy"] = {"repositories": {"allowed_roots": [str(env.root)]}}
    env.world.write()
    _refused(env, "approved-repositories-unset")


def test_unset_roots_under_convenience_launch(env):
    del env.world.profile["policy"]["repositories"]
    _policy(env, isolation="convenience")
    _drv(env).prepare(env.request())


# --------------------------------------------------------------------- TUI


@pytest.mark.parametrize("case", ["isolation", "repository", "host"])
def test_the_interactive_start_refuses_with_the_same_code_as_prepare(env, case):
    expected = {
        "isolation": "isolation-required-unverified", "repository": "repository-not-approved",
        "host": "destination-not-approved",
    }[case]
    kw = {}
    if case == "isolation":
        kw["isolation_roots"] = iso.unverified_roots(env.tmp / "empty")
    elif case == "repository":
        _policy(env, repositories={"allowed_roots": [str(env.tmp / "elsewhere")]})
    else:
        _policy(env, allowed_hosts=[GW_B])
    with pytest.raises(driver.PrepareRefused) as info:
        _drv(env, **kw).prepare_interactive("work")
    assert info.value.code == expected
    with pytest.raises(driver.PrepareRefused) as again:
        _drv(env, **kw).prepare(env.request())
    assert again.value.code == expected


# ------------------------------------------------------------------ parity


def _offline(env, drv=None):
    return (drv or _drv(env)).offline_launch_refusal("work", ambient_env=dict(env.world.env, PATH=os.environ.get("PATH", "")))


PARITY = {
    "isolation-required-unverified": lambda e: dict(isolation_roots=iso.unverified_roots(e.tmp / "empty")),
    "repository-not-approved": lambda e: _policy(e, repositories={"allowed_roots": [str(e.tmp / "elsewhere")]}) or {},
    "destination-not-approved": lambda e: _policy(e, allowed_hosts=[GW_B]) or {},
}


@pytest.mark.parametrize("code", sorted(PARITY))
def test_prepare_and_the_offline_helper_agree_on_the_code(env, code):
    """Workspace runs are out of scope: the preview has no workspace."""
    kw = PARITY[code](env)
    drv = _drv(env, **kw)
    with pytest.raises(driver.PrepareRefused) as info:
        drv.prepare(env.request())
    assert info.value.code == code
    refusal = _offline(env, drv)
    assert refusal is not None and refusal.code == code and refusal.layer == "launch-policy"
    assert refusal.category == launch_codes.category(code)


def test_the_headline_destination_world_is_not_launchable_yet_still_refuses_with_the_destination_code(env):
    """A label-approved provider whose host is excluded and whose model a role uses."""
    _policy(env, allowed_hosts=[GW_B])
    drv = _drv(env)
    assert drv.effective_preview("work", ambient_env=dict(env.world.env)) is None
    refusal = _offline(env, drv)
    assert refusal.code == "destination-not-approved"


def test_a_compile_blocked_managed_world_has_no_launch_policy_refusal(env):
    """Prepare refuses compile-blocked here, which doctor and explain already report."""
    env.world.profile["roles"]["gate"]["model"] = "no-such-model"
    env.world.write()
    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    assert _offline(env, drv) is None


def test_the_preview_never_raises_a_refusal_and_returns_a_complete_record(env):
    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    pair = drv.effective_preview("work", ambient_env=dict(env.world.env, PATH=os.environ.get("PATH", "")))
    record, layer_check = pair
    assert layer_check.launch_refusal.code == "isolation-required-unverified"
    assert record["isolation"]["status"] == "unverified" and record["pending_sections"] == []
    assert any(r["control"] == "shell-egress" and r["status"] == "delegated-unverified" for r in record["enforcement"])


def test_the_preview_keeps_the_first_refusal_and_still_fills_every_section(env):
    _policy(env, repositories={"allowed_roots": [str(env.tmp / "elsewhere")]})
    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    record, layer_check = drv.effective_preview("work", ambient_env=dict(env.world.env, PATH=os.environ.get("PATH", "")))
    assert layer_check.launch_refusal.code == "repository-not-approved"
    assert record["isolation"]["status"] == "unverified"  # the verdict is computed even after a repositories refusal
    assert record["network"]["status"] == "planned" and record["network"]["destinations"]


def test_the_preview_records_the_unavailable_shape_when_the_census_fails(env, monkeypatch):
    def boom(**kw):
        raise RuntimeError("census")

    monkeypatch.setattr(netpolicy, "census", boom)
    record, layer_check = _drv(env).effective_preview("work", ambient_env=dict(env.world.env, PATH=os.environ.get("PATH", "")))
    assert layer_check.launch_refusal.code == "destination-not-approved"
    assert layer_check.launch_refusal.token == netpolicy.POLICY_EVALUATION_FAILED
    assert record["network"] == {"status": "unavailable", "identity_check": "required", "destinations": [], "proxies": []}
    assert {r["control"]: r["status"] for r in record["enforcement"]}["provider-destinations"] == "not-enforced"


def test_a_clean_world_preview_digest_equals_prepare(env):
    ambient = dict(env.world.env, PATH=os.environ.get("PATH", ""), UNRELATED_SECRET="leak-1234567890")
    drv = _drv(env)
    prepared = drv.prepare(env.request())
    record, layer_check = drv.effective_preview("work", ambient_env=ambient)
    assert layer_check.launch_refusal is None
    assert prepared.effective_config_digest == record["digest"]


def test_the_prepared_record_carries_filled_sections_and_the_enforcement_table(env):
    prepared = _prepared_record(env, _drv(env).prepare(env.request()))
    effective = prepared["effective_config"]
    assert effective["pending_sections"] == []
    table = {r["control"]: r["status"] for r in effective["enforcement"]}
    assert table["provider-destinations"] == "enforced" and table["shell-egress"] == "delegated-verified"
    assert prepared["static_key_in_use"] is True or prepared["static_key_in_use"] is False
    assert "credentials" in prepared


def test_no_refusal_path_resolves_a_credential(env):
    class Boom:
        def resolve(self, ref):
            raise AssertionError("a refused launch must not resolve a credential")

    drv = _drv(env, isolation_roots=iso.unverified_roots(env.tmp / "empty"), resolver_factory=lambda e, platform: Boom())
    _refused(env, "isolation-required-unverified", drv)
    _policy(env, allowed_hosts=[GW_B])
    drv = _drv(env, resolver_factory=lambda e, platform: Boom())
    _refused(env, "destination-not-approved", drv)


# ----------------------------------------------------- snapshot exemptions


def _workspace_world(tmp_path, monkeypatch):
    from test_opencode_driver_workspace import Setup as WorkspaceSetup

    return WorkspaceSetup(tmp_path, monkeypatch)


def test_a_workspace_outside_every_approved_root_refuses(tmp_path, monkeypatch):
    ws_env = _workspace_world(tmp_path, monkeypatch)
    iso.launch_ready(ws_env.world)  # only the project root, not the workspace parent
    ws = ws_env.make_workspace()
    info = ws_env.refused("repository-not-approved", ws_env.request(workspace=ws))
    assert info.category == "policy-denial"


def test_a_workspace_inside_an_approved_parent_launches(tmp_path, monkeypatch):
    ws_env = _workspace_world(tmp_path, monkeypatch)
    ws = ws_env.make_workspace()
    ws_env.driver().prepare(ws_env.request(workspace=ws))


def _under_state(ws_env, *parts):
    state = profile_dirs.adapter_root(ws_env.world.env, ws_env.world.home) / "work"
    target = state.joinpath(*parts)
    target.mkdir(parents=True)
    return target


def test_only_a_directory_directly_under_this_projects_snapshot_key_is_exempt(tmp_path, monkeypatch):
    """Negative exemption cases: the project root is still checked, and any other path under the
    profile state directory is checked as a user workspace."""
    ws_env = _workspace_world(tmp_path, monkeypatch)
    iso.launch_ready(ws_env.world)
    own_key = paths.project_key(ws_env.root.resolve())
    for parts in (("snapshots", "other-key", "snap"), ("somewhere", "else"), ("snapshots", own_key, "a", "b")):
        target = _under_state(ws_env, *parts)
        (target / ".git").mkdir()
        ws_env.refused("repository-not-approved", ws_env.request(workspace=target))
    # a symlink into the snapshots directory is not a directory the launcher made
    real = _under_state(ws_env, "snapshots", own_key, "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    ws_env.refused("workspace-invalid", ws_env.request(workspace=link))


# ------------------------------------------------------------ explain, scan


def _activate(env, monkeypatch, **kw):
    ambient = dict(env.driver()._env)  # noqa: SLF001
    monkeypatch.setattr(os, "environ", ambient)
    monkeypatch.setattr(cli, "datetime", type("F", (__import__("datetime").datetime,), {"now": classmethod(lambda c, tz=None: NOW)}))
    monkeypatch.setattr(cli, "_make_opencode_driver", lambda root: env.driver(env=ambient, **kw))
    return ambient


def _explain(capsys, env, *extra):
    code = cli.main(["opencode", "config", "explain", "--profile", "work", "--project-root", str(env.root), *extra])
    out = capsys.readouterr()
    return code, out.out


def test_explain_shows_the_launch_refusal_and_keeps_its_exit_code(env, monkeypatch, capsys):
    """A launchable managed work world under unverified roots: exit 0 plus the refusal line."""
    _activate(env, monkeypatch, isolation_roots=iso.unverified_roots(env.tmp / "empty"))
    code, text = _explain(capsys, env)
    assert code == 0
    assert "launch policy refusal: isolation-required-unverified" in text
    assert "isolation: unverified (evidence: " in text and "missing: attestation-missing" in text
    assert re.search(r"^enforcement shell-egress managed-boundary delegated-unverified$", text, flags=re.M)
    code, blob = _explain(capsys, env, "--json")
    section = json.loads(blob)["effective_config"]
    assert code == 0 and section["launch_refusal"] == {"code": "isolation-required-unverified", "detail": "attestation-missing"}


def test_explain_shows_none_for_a_clean_world_and_the_destination_code_for_the_headline_world(env, monkeypatch, capsys):
    _activate(env, monkeypatch)
    assert "launch policy refusal: none" in _explain(capsys, env)[1]
    _policy(env, allowed_hosts=[GW_B])
    _activate(env, monkeypatch)
    code, text = _explain(capsys, env)
    assert "launch policy refusal: destination-not-approved" in text and code == 1  # compile blocker, as before


def test_a_failing_offline_check_is_shown_as_unavailable(env, monkeypatch, capsys):
    _activate(env, monkeypatch)
    monkeypatch.setattr(driver.OpenCodeDriver, "offline_launch_refusal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    code, text = _explain(capsys, env)
    assert code == 0 and "launch policy refusal: unavailable" in text
    assert json.loads(_explain(capsys, env, "--json")[1])["effective_config"]["launch_refusal"] == {"code": "unavailable"}


def test_explain_never_tracebacks_when_the_preview_fails(env, monkeypatch, capsys):
    _activate(env, monkeypatch)
    monkeypatch.setattr(driver.OpenCodeDriver, "effective_preview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    code, text = _explain(capsys, env)
    assert "Traceback" not in text and code in (0, 1)


def test_a_work_world_whose_census_fails_shows_the_unavailable_network_line(env, monkeypatch, capsys):
    _activate(env, monkeypatch)
    monkeypatch.setattr(netpolicy, "census", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    code, text = _explain(capsys, env)
    assert "network: unavailable (destination checks could not be evaluated; no destination control enforced)" in text
    assert "launch policy refusal: destination-not-approved" in text


# --------------------------------------------------------------- secret scan

SEED = "SEEDEDGATEWAYSECRET99"


def test_seeded_secrets_never_reach_the_new_surfaces(env, monkeypatch, capsys, tmp_path):
    roots = iso.fixture_roots(env.tmp / "seeded", attestation={"issuer": "issuer-" + SEED})
    env.world.profile["policy"]["allowed_hosts"] = [GW, GW_B, "proxy.invalid"]
    env.world.write()
    proxy = "http://user:%s@proxy.invalid:8080" % SEED
    ambient = dict(env.world.env, PATH=os.environ.get("PATH", ""), HTTPS_PROXY=proxy)
    drv = _drv(env, env=ambient, isolation_roots=roots)
    prepared = drv.prepare(env.request())
    blob = json.dumps(_prepared_record(env, prepared))
    refusal_blobs = []
    for kw in (
        dict(isolation_roots=iso.unverified_roots(env.tmp / "empty")),
        dict(identity_checker=iso.FakeIdentity({GW: netpolicy.IdentityResult("mismatch", "tls", "name-mismatch")})),
    ):
        with pytest.raises(driver.PrepareRefused) as info:
            _drv(env, env=ambient, **{"isolation_roots": roots, **kw}).prepare(env.request())
        refusal_blobs.append(info.value.message + str(info.value.detail))
    _activate(env, monkeypatch, isolation_roots=roots)
    os.environ["HTTPS_PROXY"] = proxy
    outputs = [_explain(capsys, env, *extra)[1] for extra in ((), ("--json",), ("--redact",))]
    findings = doctor_text(env, ambient, roots)
    for text in [blob, *refusal_blobs, *outputs, findings]:
        assert SEED not in text


def doctor_text(env, ambient, roots):
    from quoin.opencode_adapter import doctor

    cfg = {k: ambient[k] for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME") if k in ambient}
    found = doctor._profile_findings(env.root, "work", cfg, env.world.home, NOW, ambient_env=ambient, isolation_roots=roots)  # noqa: SLF001
    return " ".join(f.message + (f.remediation or "") for f in found)


# ------------------------------------------------------------- static pins


def test_no_module_builds_trusted_roots_or_a_checker_outside_the_allow_list():
    import ast
    from pathlib import Path

    src = Path(driver.__file__).resolve().parents[1]
    allowed_kw = {("doctor.py", "isolation_roots"), ("driver.py", "isolation_roots"), ("driver.py", "identity_checker")}
    offenders = []
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "TrustedRoots" and (node.args or node.keywords) and path.name != "isolation.py":
                offenders.append((path.name, "TrustedRoots("))
            for kw in node.keywords:
                if kw.arg in ("isolation_roots", "identity_checker") and (path.name, kw.arg) not in allowed_kw:
                    offenders.append((path.name, kw.arg))
    assert not offenders, offenders
