"""The isolation doctor finding ids are declared and categorised ahead of the
check that will emit them."""
from __future__ import annotations

import re

import _opencode_helpers as helpers
from quoin.opencode_adapter import categories, doctor

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
IDS = (
    "isolation-verified",
    "isolation-unverified",
    "isolation-required-unverified",
    "egress-shell-not-enforced",
    "enforcement-layers",
)


def test_the_five_ids_are_declared_with_text_and_remediation():
    for finding_id in IDS:
        assert finding_id in doctor.MESSAGES
        assert doctor.MESSAGES[finding_id].strip()
        finding = doctor.make_finding(finding_id, "info")
        assert finding.message and finding.remediation


def test_the_five_ids_are_policy_denials():
    for finding_id in IDS:
        assert categories.category_for(finding_id) == "policy-denial"
    assert set(doctor.MESSAGES) == set(categories.FINDING_CATEGORIES)


def test_texts_make_no_claim_about_this_machine():
    for finding_id in IDS:
        text = (doctor.MESSAGES[finding_id] + " " + doctor.make_finding(finding_id, "info").remediation).lower()
        assert "supported" not in text
        assert "isolated" not in text and "secure" not in text


# ------------------------------------------------------------ emission

import os  # noqa: E402

import pytest  # noqa: E402

import _opencode_isolation_helpers as iso  # noqa: E402
from _opencode_merge_helpers import NOW  # noqa: E402
from quoin.opencode_adapter import driver  # noqa: E402
from test_opencode_driver_prepare import Setup  # noqa: E402

GW_B = "gateway-b.example.invalid"


@pytest.fixture
def denv(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _ambient(denv):
    return dict(denv.driver()._env)  # noqa: SLF001


def _findings(denv, roots=None, ambient=None):
    env = ambient or _ambient(denv)
    cfg = {k: env[k] for k in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY") if k in env}
    return doctor._profile_findings(  # noqa: SLF001
        denv.root, "work", cfg, denv.world.home, NOW, ambient_env=env,
        isolation_roots=roots if roots is not None else denv.kw["isolation_roots"],
    )


def _ids(findings):
    return sorted(f.id for f in findings)


def _by(findings, finding_id):
    return [f for f in findings if f.id == finding_id]


def test_a_verified_world_reports_verified_with_evidence_and_the_layers(denv):
    found = _findings(denv)
    (verified,) = _by(found, "isolation-verified")
    assert verified.severity == "ok" and "attestation-current" in verified.message
    assert not _by(found, "launch-refused") and not _by(found, "isolation-unverified")
    assert _by(found, "enforcement-layers") and "provider-destinations=enforced" in _by(found, "enforcement-layers")[0].message
    assert not _by(found, "egress-shell-not-enforced")
    assert doctor.report_status(found) == "healthy"


def test_a_managed_work_world_with_unverified_roots_reports_the_refusal_once(denv):
    found = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    (refused,) = _by(found, "launch-refused")
    assert refused.severity == "error" and "isolation-required-unverified" in refused.message
    (unverified,) = _by(found, "isolation-unverified")
    assert unverified.severity == "warn" and "attestation-missing" in unverified.message
    assert not _by(found, "isolation-required-unverified")
    (launchable,) = _by(found, "runtime-profile-launchable")
    assert launchable.severity == "ok" and "compiles" in launchable.message and "refusal" in launchable.message
    assert len(_by(found, "isolation-unverified")) + len(_by(found, "isolation-verified")) == 1
    assert _by(found, "egress-shell-not-enforced")[0].severity == "warn"
    assert _ids(found) == sorted([
        "launch-refused", "isolation-unverified", "runtime-profile-launchable", "enforcement-layers",
        "egress-shell-not-enforced", "inventory-clean",
    ])
    assert doctor.report_status(found) == "errors"


def test_a_convenience_world_reports_unverified_without_failing_the_run(denv):
    denv.world.profile["policy"]["isolation"] = "convenience"
    denv.world.profile["classification"] = "personal"
    denv.world.project["classification"] = "personal"
    denv.world.profile["policy"].pop("repositories", None)
    denv.world.write()
    denv.world.write_records()
    found = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    assert _by(found, "isolation-unverified")[0].severity == "info"
    assert not _by(found, "launch-refused") and not _by(found, "approved-repositories-unset")
    assert doctor.report_status(found) == "healthy"
    assert _by(found, "egress-shell-not-enforced")[0].severity == "info"


def test_a_requirement_only_in_the_project_file_reports_the_required_finding(denv):
    del denv.world.profile["policy"]["isolation"]
    denv.world.profile["policy"].pop("repositories", None)
    denv.world.project["policy"] = {"isolation": "managed"}
    denv.world.write()
    found = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    assert _by(found, "isolation-unverified") and (
        _by(found, "isolation-required-unverified") or _by(found, "launch-refused"))


def test_the_destination_world_reports_compile_blocked_and_the_destination_refusal(denv):
    denv.world.profile["policy"]["allowed_hosts"] = [GW_B]
    denv.world.write()
    found = _findings(denv)
    assert _by(found, "runtime-compile-blocked")
    (refused,) = _by(found, "launch-refused")
    assert "destination-not-approved" in refused.message


def test_a_work_profile_with_unapproved_repositories_warns_when_not_managed(denv):
    denv.world.profile["policy"]["isolation"] = "convenience"
    denv.world.profile["policy"].pop("repositories", None)
    denv.world.write()
    found = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    assert _by(found, "approved-repositories-unset")[0].severity == "warn"


def test_every_early_return_path_still_carries_exactly_one_isolation_finding(denv):
    denv.world.profile["unknown_key"] = 1
    denv.world.write()
    found = _findings(denv)
    assert len(_by(found, "isolation-verified")) + len(_by(found, "isolation-unverified")) == 1
    assert _by(found, "runtime-config-invalid") or _by(found, "runtime-config-policy-denied")
    unverified = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    assert _by(unverified, "isolation-unverified")[0].severity == "info"


def test_a_failing_offline_check_is_reported_and_the_other_findings_stand(denv, monkeypatch):
    monkeypatch.setattr(driver.OpenCodeDriver, "offline_launch_refusal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    found = _findings(denv)
    assert _by(found, "launch-check-unavailable")[0].severity == "warn"
    assert _by(found, "runtime-profile-launchable") and _by(found, "inventory-clean")


def test_host_doctor_with_raising_probes_is_info_and_exits_clean(tmp_path):
    def deny(path):
        raise PermissionError(13, "denied")

    def nobody(uid):
        raise KeyError(uid)

    roots = iso.unverified_roots(tmp_path, scandir=deny, user_name=nobody)
    verdict = doctor._isolation_verdict_for(None, {}, {}, tmp_path, roots)  # noqa: SLF001
    finding = doctor._isolation_finding(verdict, "info")  # noqa: SLF001
    assert finding.id == "isolation-unverified" and finding.severity == "info"
    assert doctor.report_status([finding]) == "healthy" and doctor.exit_code("healthy") == 0


def test_host_level_finding_on_this_machine_is_unverified_info(tmp_path):
    from quoin.opencode_adapter import paths

    if paths.attestation_path().exists():
        pytest.skip("this machine carries a system attestation")
    verdict = doctor._isolation_verdict_for(None, {"HOME": str(tmp_path)}, {}, tmp_path, None)  # noqa: SLF001
    finding = doctor._isolation_finding(verdict, "info")  # noqa: SLF001
    assert finding.severity == "info" and "attestation-missing" in finding.message


def test_new_finding_texts_make_no_claim(denv):
    found = _findings(denv, iso.unverified_roots(denv.tmp / "empty"))
    for finding in found:
        text = (finding.message + " " + (finding.remediation or "")).lower()
        if finding.id.startswith(("isolation", "launch", "enforcement", "egress", "approved")):
            assert "supported" not in text and "isolated" not in text and "secure" not in text, finding.id
