"""The isolation attestation reader, corroboration, opposite-profile probe and
verdict. Fixture roots live under tmp_path; the trusted uid is the test user."""
from __future__ import annotations

import dataclasses
import datetime
import os
import re
import stat
from pathlib import Path

import pytest

import _opencode_helpers as helpers
import _opencode_isolation_helpers as iso_helpers
from quoin.opencode_adapter import isolation, paths

SRC = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
NOW = 1_800_000_000.0


def _read(roots):
    return isolation.read_attestation(roots)


def _tokens(roots, tmp_path, profile="work"):
    return iso_helpers.verdict_for(roots, tmp_path, profile)


# ----------------------------------------------------------------- location


def test_the_attestation_sits_beside_the_trusted_policy():
    assert paths.attestation_path("darwin") == Path("/Library/Application Support/quoin/opencode/isolation-attestation.json")
    assert paths.attestation_path("linux") == Path("/etc/quoin/opencode/isolation-attestation.json")
    assert paths.attestation_path("linux").parent == paths.trusted_policy_path("linux").parent


def test_system_roots_do_no_lookup_and_use_real_scandir(monkeypatch):
    import pwd

    def boom(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", boom)
    roots = isolation.TrustedRoots.system()
    assert roots.scandir is os.scandir
    assert roots.owner_uid == 0
    assert isolation._passwd_name(1234) is None


def test_system_roots_run_in_a_uid_with_no_password_entry(monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    assert isolation.TrustedRoots.system().user_name(os.getuid()) is None


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".", "..", "x" * 256, "a\x00b"])
def test_passwd_name_refuses_unsafe_names(monkeypatch, name):
    import pwd

    class Entry:
        pw_name = name

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: Entry)
    assert isolation._passwd_name(5) is None


def test_sysctl_failures_give_none(monkeypatch):
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("sysctl", 2)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert isolation._real_sysctl("kern.hv_vmm_present") is None

    def missing(*a, **k):
        raise FileNotFoundError("/usr/sbin/sysctl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert isolation._real_sysctl("kern.hv_vmm_present") is None


# ------------------------------------------------------------------- reader


def test_a_valid_file_reads_with_root_evidence(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path)
    got = _read(roots)
    assert got.state == "read" and got.tokens == ()
    assert set(got.evidence) == {"attestation-root-owned", "attestation-chain-root-owned", "invoking-user-not-root"}


def test_an_absent_file_is_missing(tmp_path):
    got = _read(iso_helpers.unverified_roots(tmp_path))
    assert got.state == "absent" and got.tokens == ("attestation-missing",)


def test_a_symlinked_file_is_invalid(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path)
    real = roots.attestation_path.with_name("real.json")
    roots.attestation_path.rename(real)
    roots.attestation_path.symlink_to(real)
    assert _read(roots).tokens == ("attestation-invalid",)


def test_a_fifo_is_invalid(tmp_path):
    roots = iso_helpers.unverified_roots(tmp_path)
    roots.attestation_path.parent.mkdir(parents=True)
    os.chmod(roots.attestation_path.parent, 0o755)
    os.mkfifo(roots.attestation_path)
    assert _read(roots).tokens == ("attestation-invalid",)


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_a_writable_file_is_not_root_owned(tmp_path, mode):
    roots = iso_helpers.fixture_roots(tmp_path)
    os.chmod(roots.attestation_path, mode)
    assert "attestation-not-root-owned" in _read(roots).tokens


def test_a_file_owned_by_another_uid_is_not_root_owned(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, owner_uid=os.getuid() + 7, invoking_uid=os.getuid() + 1)
    got = _read(roots)
    assert "attestation-not-root-owned" in got.tokens
    assert "attestation-chain-writable" in got.tokens


def test_a_group_writable_chain_directory_fails(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path)
    os.chmod(roots.attestation_path.parent, 0o775)
    got = _read(roots)
    assert got.tokens == ("attestation-chain-writable",)
    assert "attestation-chain-root-owned" not in got.evidence


def test_a_symlinked_chain_directory_fails(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path)
    parent = roots.attestation_path.parent
    moved = parent.with_name("opencode-real")
    parent.rename(moved)
    parent.symlink_to(moved)
    assert "attestation-chain-writable" in _read(roots).tokens


def test_an_invoking_root_session_fails(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, invoking_uid=os.getuid())
    assert _read(roots).tokens == ("invoking-user-is-root",)


def test_a_path_outside_the_chain_top_is_mapped_not_raised(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, chain_top=tmp_path / "elsewhere")
    assert "attestation-chain-writable" in _read(roots).tokens


def test_an_oversized_file_is_invalid(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path)
    roots.attestation_path.write_text(" " * (70 * 1024))
    assert _read(roots).tokens == ("attestation-invalid",)


@pytest.mark.parametrize("text", ['{"a": 1, "a": 2}', '{"x": NaN}', "not json", "﻿{}", "[1]"])
def test_malformed_text_is_invalid(tmp_path, text):
    roots = iso_helpers.fixture_roots(tmp_path)
    roots.attestation_path.write_text(text, encoding="utf-8")
    got = _read(roots)
    if got.state == "read":
        fields, tokens = isolation.validate_attestation(got.data, NOW)
        assert tokens == ("attestation-invalid",)
    else:
        assert got.tokens == ("attestation-invalid",)


# ---------------------------------------------------------------- validator


def _good():
    import copy

    return copy.deepcopy(iso_helpers.GOOD_ATTESTATION)


def test_a_good_declaration_validates():
    fields, tokens = isolation.validate_attestation(_good(), NOW)
    assert tokens == () and fields["environment"] == "container"


def test_unknown_key_is_invalid():
    data = _good()
    data["extra"] = 1
    assert isolation.validate_attestation(data, NOW)[1] == ("attestation-invalid",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("environment"),
        lambda d: d.update(environment="laptop"),
        lambda d: d.update(attestation_format=2),
        lambda d: d.update(attestation_format=True),
        lambda d: d.update(attestation_format="1"),
        lambda d: d.pop("issuer"),
        lambda d: d.update(issuer=""),
        lambda d: d.update(issuer="x" * 257),
        lambda d: d.update(issuer=5),
        lambda d: d.pop("boundary"),
        lambda d: d.update(boundary="all"),
        lambda d: d["boundary"].pop("secrets"),
        lambda d: d["boundary"].update(secrets="yes"),
        lambda d: d["boundary"].update(extra=True),
        lambda d: d["boundary"].update(egress="open"),
        lambda d: d.update(approved_egress_hosts="a.example"),
        lambda d: d.update(approved_egress_hosts=["bad host"]),
        lambda d: d.update(approved_egress_hosts=["a.example"] * 257),
        lambda d: d.update(not_after=5),
        lambda d: d.update(not_after="9999-99-99"),
        lambda d: d.update(not_after="10000-01-01"),
        lambda d: d.update(not_after="soon"),
    ],
)
def test_each_malformed_field_is_invalid_without_raising(mutate):
    data = _good()
    mutate(data)
    assert isolation.validate_attestation(data, NOW)[1] == ("attestation-invalid",)


def test_not_after_is_clock_relative():
    when = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
    past = (when - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    future = (when + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    data = _good()
    data["not_after"] = past
    assert isolation.validate_attestation(data, NOW)[1] == ("attestation-expired",)
    data["not_after"] = future
    assert isolation.validate_attestation(data, NOW)[1] == ()
    data["not_after"] = future + "T00:00:00Z"
    assert isolation.validate_attestation(data, NOW)[1] == ()
    data["not_after"] = "9999-12-31"
    assert isolation.validate_attestation(data, NOW)[1] == ()


def test_egress_none_and_each_false_flag_are_incomplete():
    data = _good()
    data["boundary"]["egress"] = "none"
    assert isolation.validate_attestation(data, NOW)[1] == ("attestation-incomplete",)
    for flag in ("filesystem", "secrets", "process_env"):
        data = _good()
        data["boundary"][flag] = False
        assert isolation.validate_attestation(data, NOW)[1] == ("attestation-incomplete",)


# ---------------------------------------------------------- corroboration


@pytest.mark.parametrize("environment", ["container", "vm", "managed-os-user"])
def test_each_environment_verifies_with_its_marker(tmp_path, environment):
    roots = iso_helpers.fixture_roots(tmp_path, environment)
    verdict = _tokens(roots, tmp_path)
    assert verdict.status == "verified", verdict.missing
    assert verdict.environment == environment
    assert "boundary-declared" in verdict.evidence


def test_container_via_cgroup_text(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, corroborate=False)
    (roots.probe_root / "proc/1").mkdir(parents=True)
    (roots.probe_root / "proc/1/cgroup").write_text("12:cpu:/kubepods/burstable/pod1\n")
    assert isolation.corroborate("container", roots) == "corroboration-container"


def test_vm_via_dmi_and_via_darwin_sysctl(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, "vm")
    assert isolation.corroborate("vm", roots) == "corroboration-vm"
    darwin = iso_helpers.fixture_roots(tmp_path / "d", "vm", platform="darwin", corroborate=False)
    assert isolation.corroborate("vm", darwin) == "corroboration-vm"
    off = dataclasses.replace(darwin, sysctl=lambda name: "0")
    assert isolation.corroborate("vm", off) is None


def test_each_corroboration_absent_is_missing_evidence(tmp_path):
    for env in ("container", "vm", "managed-os-user"):
        roots = iso_helpers.fixture_roots(tmp_path / env, env, corroborate=False)
        verdict = _tokens(roots, tmp_path / env)
        assert verdict.status == "unverified" and "corroboration-missing" in verdict.missing


def test_a_marker_owned_by_another_uid_does_not_count(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, owner_uid=os.getuid() + 7)
    assert isolation.corroborate("container", roots) is None


def test_a_marker_that_is_a_symlink_does_not_count(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, corroborate=False)
    target = tmp_path / "real-marker"
    target.write_text("")
    (roots.probe_root / ".dockerenv").symlink_to(target)
    assert isolation.corroborate("container", roots) is None


def test_managed_os_user_on_linux_is_missing(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, "managed-os-user", platform="linux")
    assert "corroboration-missing" in _tokens(roots, tmp_path).missing


def test_corroborate_survives_a_raising_user_lookup(tmp_path):
    """Pins corroborate's own last-resort guard."""
    def boom(uid):
        raise KeyError(uid)

    roots = iso_helpers.fixture_roots(tmp_path, "managed-os-user", user_name=boom)
    assert isolation.corroborate("managed-os-user", roots) is None
    assert "corroboration-missing" in _tokens(roots, tmp_path).missing


def test_a_missing_proc_and_sys_is_missing_evidence_not_an_error(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, "vm", corroborate=False)
    assert isolation.corroborate("vm", roots) is None
    assert isolation.corroborate("container", roots) is None


# ---------------------------------------------------------- opposite probe


def _stamp(adapter_root: Path, profile: str) -> None:
    directory = adapter_root / profile
    directory.mkdir(parents=True)
    (directory / "profile.json").write_text("{}")


def test_another_profile_root_is_present(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    _stamp(state, "work")
    assert not isolation.opposite_profiles("work", state, profiles)
    _stamp(state, "personal")
    assert isolation.opposite_profiles("work", state, profiles)


def test_another_profile_file_is_present(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    (profiles / "work.json").write_text("{}")
    assert not isolation.opposite_profiles("work", state, profiles)
    (profiles / "home.json").write_text("{}")
    assert isolation.opposite_profiles("work", state, profiles)


def test_host_level_probe_counts_more_than_one(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    (profiles / "work.json").write_text("{}")
    assert not isolation.opposite_profiles(None, state, profiles)
    (profiles / "home.json").write_text("{}")
    assert isolation.opposite_profiles(None, state, profiles)


def test_three_hundred_entries_overflow_to_present(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    for index in range(300):
        (state / ("d%03d" % index)).mkdir()
    assert isolation.opposite_profiles("work", state, profiles)


def test_absent_directories_hold_nothing(tmp_path):
    """A fresh install has neither directory and must not read as another profile."""
    assert not isolation.opposite_profiles("work", tmp_path / "no-state", tmp_path / "no-profiles")


@pytest.mark.skipif(os.getuid() == 0, reason="mode 0o000 does not stop root")
def test_an_unreadable_adapter_root_counts_as_present(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    os.chmod(state, 0)
    try:
        assert isolation.opposite_profiles("work", state, profiles)
    finally:
        os.chmod(state, 0o755)


def test_an_injected_scandir_failure_counts_as_present(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)

    def deny(path):
        raise PermissionError(13, "denied")

    assert isolation.opposite_profiles("work", state, profiles, scandir=deny)


def test_a_symlinked_stamp_entry_does_not_hide_a_profile(tmp_path):
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "profile.json").write_text("{}")
    (state / "other").symlink_to(real)
    # a link is never followed, so it cannot name a profile either way
    assert not isolation.opposite_profiles("work", state, profiles)


# ----------------------------------------------------------------- verdict


def test_a_requirement_alone_is_unverified_with_attestation_missing(tmp_path):
    roots = iso_helpers.unverified_roots(tmp_path)
    verdict = _tokens(roots, tmp_path)
    assert verdict.status == "unverified" and "attestation-missing" in verdict.missing


def test_a_file_at_the_env_pointed_managed_policy_path_contributes_nothing(tmp_path, monkeypatch):
    other = tmp_path / "managed.json"
    other.write_text('{"attestation_format": 1}')
    monkeypatch.setenv(paths.ENV_MANAGED_POLICY, str(other))
    roots = iso_helpers.unverified_roots(tmp_path)
    assert "attestation-missing" in _tokens(roots, tmp_path).missing


def test_multiple_failures_are_listed_together(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, corroborate=False, invoking_uid=os.getuid())
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    _stamp(state, "personal")
    verdict = isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles)
    assert {"invoking-user-is-root", "corroboration-missing", "opposite-profile-present"} <= set(verdict.missing)


def test_the_issuer_is_only_a_digest_and_hosts_are_keys(tmp_path):
    roots = iso_helpers.fixture_roots(tmp_path, attestation={"approved_egress_hosts": ["Gateway.Example.COM."]})
    verdict = _tokens(roots, tmp_path)
    assert verdict.status == "verified"
    assert verdict.issuer_digest.startswith("h-") and "corp-it-fixture" not in repr(verdict)
    assert verdict.egress_hosts == frozenset({"gateway.example.com"})
    section = isolation.record_section(verdict)
    assert section["issuer"] == verdict.issuer_digest and section["approved_egress_hosts"] == 1
    assert "gateway" not in repr(section)


def test_evaluate_never_raises_when_a_check_does(tmp_path, monkeypatch):
    """Pins evaluate's per-check and last-resort guards."""
    roots = iso_helpers.fixture_roots(tmp_path)
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("probe")

    monkeypatch.setattr(isolation, "corroborate", boom)
    verdict = isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles)
    assert verdict.status == "unverified" and "corroboration-missing" in verdict.missing
    monkeypatch.setattr(isolation, "opposite_profiles", boom)
    verdict = isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles)
    assert "opposite-profile-present" in verdict.missing
    monkeypatch.setattr(isolation, "read_attestation", boom)
    verdict = isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles)
    assert "attestation-invalid" in verdict.missing
    monkeypatch.setattr(isolation, "_evaluate", boom)
    assert isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles).missing == (
        "attestation-invalid",
    )


def test_every_token_in_the_closed_set_is_produced(tmp_path):
    seen_missing, seen_evidence = set(), set()
    cases = [
        iso_helpers.unverified_roots(tmp_path / "a"),
        iso_helpers.fixture_roots(tmp_path / "b"),
        iso_helpers.fixture_roots(tmp_path / "c", invoking_uid=os.getuid(), corroborate=False),
        iso_helpers.fixture_roots(tmp_path / "d", attestation={"not_after": "2000-01-01", "boundary": {
            "filesystem": False, "secrets": True, "process_env": True, "egress": "none"}}),
        iso_helpers.fixture_roots(tmp_path / "e", "vm"),
        iso_helpers.fixture_roots(tmp_path / "f", "managed-os-user"),
    ]
    odd = iso_helpers.fixture_roots(tmp_path / "g")
    os.chmod(odd.attestation_path, 0o666)
    os.chmod(odd.attestation_path.parent, 0o777)
    cases.append(odd)
    bad = iso_helpers.fixture_roots(tmp_path / "h")
    bad.attestation_path.write_text("nope")
    cases.append(bad)
    for index, roots in enumerate(cases):
        root = tmp_path / ("run%d" % index)
        state, profiles = iso_helpers.empty_adapter_dirs(root)
        if index == 2:
            _stamp(state, "other")
        verdict = isolation.evaluate(roots, profile="work", adapter_root=state, profiles_dir=profiles)
        seen_missing |= set(verdict.missing)
        seen_evidence |= set(verdict.evidence)
    assert seen_missing == set(isolation.MISSING_TOKENS)
    assert seen_evidence == set(isolation.EVIDENCE_TOKENS)


def test_this_machine_is_unverified_without_a_system_attestation(tmp_path):
    if paths.attestation_path().exists():
        pytest.skip("this machine carries a system attestation")
    state, profiles = iso_helpers.empty_adapter_dirs(tmp_path)
    verdict = isolation.evaluate(isolation.TrustedRoots.system(), profile="work", adapter_root=state, profiles_dir=profiles)
    assert verdict.status == "unverified" and "attestation-missing" in verdict.missing


# ----------------------------------------------------------------- static


def test_isolation_source_reads_no_environment():
    text = (SRC / "isolation.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", text, flags=re.S)
    for word in ("environ", "getenv", "argv"):
        assert not re.search(r"\b%s\b" % word, code), word
