"""One fail-closed-not-crash test per always-on probe. A probe failure is a
verdict or a refusal, never a traceback, and whether a guard refuses or lets
the launch continue is decided by what the profile requires, not by its
classification. Each docstring names the guard it pins."""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

import _opencode_isolation_helpers as iso
from quoin.opencode_adapter import driver, isolation, netpolicy
from test_opencode_driver_prepare import Setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _personal(env, **policy):
    """A default personal world: convenience isolation, no repository roots, identity off."""
    import shutil

    from quoin.opencode_adapter import profile_dirs

    # A refused launch may already have stamped the profile directory with its old classification.
    shutil.rmtree(profile_dirs.adapter_root(env.world.env, env.world.home) / "work", ignore_errors=True)
    env.world.profile["classification"] = "personal"
    env.world.project["classification"] = "personal"
    env.world.profile["policy"] = dict(policy)
    env.world.write()
    env.world.write_records()


def _record(env, prepared):
    from quoin.opencode_adapter import runstore

    return runstore.load_record(runstore.store_dir(env.root), prepared.run_id)["prepared"]


def _raising(*a, **k):
    raise RuntimeError("probe")


def _refuses(env, code, detail=None, drv=None, profile="work"):
    with pytest.raises(driver.PrepareRefused) as info:
        (drv or env.driver()).prepare(env.request(profile=profile))
    assert info.value.code == code, info.value.message
    if detail is not None:
        assert info.value.detail == detail
    return info.value


# ---------------------------------------------------------- rows 2-9 (units)


def test_row_2_a_passwd_less_uid_is_missing_evidence_on_the_launch_path(env):
    """Pins _passwd_name's KeyError catch through the real lookup (mutation r)."""
    import pwd

    roots = iso.fixture_roots(env.tmp / "r", "managed-os-user", corroborate=True,
                              user_name=isolation._passwd_name)
    original = pwd.getpwuid
    pwd.getpwuid = lambda uid: (_ for _ in ()).throw(KeyError(uid))
    try:
        _refuses(env, "isolation-required-unverified", "corroboration-missing", env.driver(isolation_roots=roots))
    finally:
        pwd.getpwuid = original


def test_row_7_darwin_managed_os_user_with_a_raising_lookup_is_corroboration_missing(env):
    """Pins corroborate's last-resort guard and evaluate's per-check guard together."""
    roots = iso.fixture_roots(env.tmp / "r", "managed-os-user", user_name=_raising)
    _refuses(env, "isolation-required-unverified", "corroboration-missing", env.driver(isolation_roots=roots))


def test_row_7_a_linux_probe_root_without_proc_or_sys(env):
    """Pins _read_bounded: an absent /proc and /sys is missing evidence, never an error."""
    roots = iso.fixture_roots(env.tmp / "r", "vm", corroborate=False)
    _refuses(env, "isolation-required-unverified", "corroboration-missing", env.driver(isolation_roots=roots))


def test_row_8_a_scandir_that_raises_permission_error(env):
    """Pins the opposite-profile probe's listing handler."""
    def deny(path):
        raise PermissionError(13, "denied")

    roots = iso.fixture_roots(env.tmp / "r", scandir=deny)
    _refuses(env, "isolation-required-unverified", "opposite-profile-present", env.driver(isolation_roots=roots))


@pytest.mark.parametrize("which", ["passwd", "listing", "probe_root"])
def test_a_convenience_personal_world_launches_through_prepare_and_the_tui(env, which):
    """The same probe failures do not stop a launch that requires nothing."""
    _personal(env)
    def deny(path):
        raise PermissionError(13, "denied")

    kw = {
        "passwd": dict(platform="darwin", user_name=_raising),
        "listing": dict(scandir=deny),
        "probe_root": dict(),
    }[which]
    environment = "managed-os-user" if which == "passwd" else "container"
    roots = iso.fixture_roots(env.tmp / "r", environment, **kw)
    if which == "probe_root":
        roots = dataclasses.replace(roots, probe_root=env.tmp / "no-such-root")
    drv = env.driver(isolation_roots=roots)
    prepared = drv.prepare(env.request(profile="work"))
    assert _record(env, prepared)["effective_config"]["isolation"]["status"] == "unverified"
    drv.prepare_interactive("work")


# ------------------------------------------------------ rows 13-15 (driver)


def test_row_13_a_raising_evaluate_is_attestation_invalid(env, monkeypatch):
    """Pins the driver's isolation guard: managed refuses, a convenience personal world launches."""
    monkeypatch.setattr(isolation, "evaluate", _raising)
    _refuses(env, "isolation-required-unverified", "attestation-invalid")
    with pytest.raises(driver.PrepareRefused) as info:
        env.driver().prepare_interactive("work")
    assert info.value.code == "isolation-required-unverified"
    _personal(env)
    prepared = env.driver().prepare(env.request(profile="work"))
    section = _record(env, prepared)["effective_config"]["isolation"]
    assert section == {
        "status": "unverified", "environment": None, "evidence": [], "missing": ["attestation-invalid"],
        "boundary": None, "issuer": None, "approved_egress_hosts": 0,
    }


def test_row_13_a_personal_world_that_requires_managed_isolation_refuses_too(env, monkeypatch):
    monkeypatch.setattr(isolation, "evaluate", _raising)
    _personal(env, isolation="managed", repositories={"allowed_roots": [str(env.root)]})
    _refuses(env, "isolation-required-unverified", "attestation-invalid")


def test_row_15_a_raising_census_refuses_work_and_continues_for_default_personal(env, monkeypatch):
    """Pins _network_policy at site B (rows 15 and 15b)."""
    monkeypatch.setattr(netpolicy, "census", _raising)
    _refuses(env, "destination-not-approved", netpolicy.POLICY_EVALUATION_FAILED)
    _personal(env, network={"identity_check": "required"})
    _refuses(env, "destination-not-approved", netpolicy.POLICY_EVALUATION_FAILED)
    _personal(env, isolation="managed", repositories={"allowed_roots": [str(env.root)]})
    _refuses(env, "destination-not-approved", netpolicy.POLICY_EVALUATION_FAILED)
    _personal(env)
    prepared = env.driver().prepare(env.request(profile="work"))
    record = _record(env, prepared)
    assert record["effective_config"]["network"] == {
        "status": "unavailable", "identity_check": "off", "destinations": [], "proxies": [],
    }
    table = {r["control"]: r["status"] for r in record["effective_config"]["enforcement"]}
    for control in netpolicy.NETWORK_STEP_CONTROLS:
        assert table[control] == "not-enforced", control
    assert record["identity_check"] == []


def test_row_15_a_refusal_raised_by_approve_keeps_its_own_code(env, monkeypatch):
    """Pins the pass-through: PrepareRefused is re-raised before the broad handler (mutation x)."""
    def refuses(census, policy, classification):
        raise driver.PrepareRefused("policy-denial", "proxy-route-not-approved", "proxy", detail="pin")

    monkeypatch.setattr(netpolicy, "approve", refuses)
    _refuses(env, "proxy-route-not-approved", "pin")


def test_row_15_approve_returning_a_specific_refusal_is_not_rewritten(env, monkeypatch):
    monkeypatch.setattr(netpolicy, "approve", lambda c, p, k: netpolicy.NetRefusal("proxy-route-not-approved"))
    _refuses(env, "proxy-route-not-approved")


def test_row_10_a_raising_evaluation_refusal_refuses_work_and_continues_for_default_personal(env, monkeypatch):
    """Pins _network_policy at site A."""
    monkeypatch.setattr(netpolicy, "evaluation_refusal", _raising)
    _refuses(env, "destination-not-approved", netpolicy.POLICY_EVALUATION_FAILED)
    _personal(env)
    env.driver().prepare(env.request(profile="work"))


def test_row_14b_the_repositories_step_guard(env, monkeypatch):
    """Pins the repositories guard at the named seam _check_repositories: a world with roots
    refuses, a personal world without roots launches (the step returned before any call)."""
    monkeypatch.setattr(driver.OpenCodeDriver, "_check_repositories", _raising)
    _refuses(env, "repository-not-approved", netpolicy.POLICY_EVALUATION_FAILED)
    _personal(env)
    env.driver().prepare(env.request(profile="work"))


def test_row_14_the_snapshot_predicate_fails_to_not_exempt(env, monkeypatch):
    """A realpath failure inside the exemption predicate means the cwd is checked as a user workspace."""
    monkeypatch.setattr(os.path, "realpath", lambda p, **k: (_ for _ in ()).throw(OSError("boom")))
    assert env.driver()._is_launcher_snapshot(env.root, env.root, "work") is False


def test_the_guards_removed_would_crash_a_convenience_launch(env, monkeypatch):
    """Pins mutation u: both guards matter, so each raising probe leaves a launch that works today working."""
    _personal(env)
    monkeypatch.setattr(isolation, "evaluate", _raising)
    monkeypatch.setattr(netpolicy, "census", _raising)
    env.driver().prepare(env.request(profile="work"))


def test_a_requirement_that_cannot_be_computed_counts_as_required():
    assert netpolicy.network_controls_required(object(), "convenience") is True
