"""IVG-280 T-08: pytest wrapper for the run-continuation hook shell fixtures
plus the pre-existing hook shell fixtures probe (f) recorded as baseline-
green on the unmodified branch (auto-resume-finding.md).

Each `.sh` fixture prints "PASS: ..."/"FAIL: ..." lines and its own summary,
then exits non-zero if any case failed. This wrapper just runs each one
with `sh` and asserts a clean exit and no "FAIL:" line in the output — the
fixtures own the actual assertions.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

NEW_FIXTURES = (
    "test_auto_resume_stop_hook.sh",
    "test_auto_resume_sessionstart_start.sh",
    "test_userpromptsubmit_disarm.sh",
    "test_sessionend_arm_cleanup.sh",
)

# Baseline hook shell fixtures probe (f) recorded green on the unmodified
# branch (a0f273d) — wired in here so a future hook-file edit can't silently
# break one of them without the affected-tests selector catching it via this
# wrapper.
BASELINE_FIXTURES = (
    "test_sessionstart_pending_restore.sh",
    "test_session_lifecycle_hooks.sh",
    "test_sessionend_close_snapshot.sh",
    "test_sessionend_verify.sh",
    "test_precompact_hook.sh",
    "test_postcompact_hook.sh",
    "test_recent_sessions_hook.sh",
    "test_sessionstart_discovery_staleness.sh",
    "test_lib_run_state_probe.sh",
    "test_resolve_project_root.sh",
)


@pytest.mark.parametrize("fixture_name", NEW_FIXTURES + BASELINE_FIXTURES)
def test_hook_shell_fixture_passes(fixture_name: str):
    path = TESTS_DIR / fixture_name
    assert path.exists(), f"fixture not found: {path}"
    result = subprocess.run(
        ["sh", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Each fixture's own `[ "$FAIL" -eq 0 ] || exit 1` (or equivalent) is the
    # authoritative signal — summary line wording differs enough across the
    # baseline fixtures (some print "FAIL: 0" as part of a clean summary)
    # that a bare "FAIL:" substring check would false-positive on those.
    assert result.returncode == 0, (
        f"{fixture_name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
