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


def test_nothing_emits_the_ids_until_the_isolation_check_is_wired():
    """Until the isolation check is wired, no doctor code path may build one of
    these findings; delete this assertion when the check lands."""
    source = (SRC_DIR / "doctor.py").read_text(encoding="utf-8")
    assert not re.search(r"make_finding\(\s*[\"'](?:isolation-|egress-shell|enforcement-layers)", source)
