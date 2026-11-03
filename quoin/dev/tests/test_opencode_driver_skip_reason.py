"""The pure skip decision of the binary contract module.

The contract module's own `pytestmark` skips everything in it on a machine
without the pinned binary, so its decision function is checked here, where
the tests always run.
"""
from __future__ import annotations

from test_opencode_binary_contract import skip_reason


def test_absent_binary():
    assert skip_reason(None, "1.18.32", None) == "no 'opencode' binary on PATH"


def test_version_mismatch_names_both_versions():
    reason = skip_reason("/usr/bin/opencode", "1.18.32", "1.0.0")
    assert reason is not None
    assert "did not report a qualified release" in reason
    assert "'1.18.32'" in reason and "'1.0.0'" in reason


def test_unreadable_version_or_pin_is_a_mismatch():
    assert skip_reason("/usr/bin/opencode", "1.18.32", None) is not None
    assert skip_reason("/usr/bin/opencode", None, "1.18.32") is not None


def test_matching_version_runs():
    assert skip_reason("/usr/bin/opencode", "1.18.32", "1.18.32") is None
    assert skip_reason("/usr/bin/opencode", "1.18.32", "opencode 1.18.32\n") is None


def test_any_qualified_release_runs_and_others_skip():
    qualified = ["1.18.32", "1.19.0"]
    assert skip_reason("/usr/bin/opencode", qualified, "opencode 1.19.0") is None
    assert skip_reason("/usr/bin/opencode", qualified, "1.18.320") is not None
    assert skip_reason("/usr/bin/opencode", qualified, "1.20.0") is not None
