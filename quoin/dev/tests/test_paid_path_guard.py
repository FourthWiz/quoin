"""The paid-path guard reports a planted marker and passes an untouched shim dir."""
from __future__ import annotations

import subprocess

from conftest import make_paid_path_guard_dir, paid_path_violations


def test_untouched_guard_reports_nothing(tmp_path):
    guard = make_paid_path_guard_dir(tmp_path)
    assert paid_path_violations(guard) == []


def test_planted_marker_is_reported(tmp_path):
    guard = make_paid_path_guard_dir(tmp_path)
    (guard / "docker.invoked").write_text("")
    assert paid_path_violations(guard) == ["docker"]


def test_shim_leaves_marker_when_run(tmp_path):
    guard = make_paid_path_guard_dir(tmp_path)
    proc = subprocess.run([str(guard / "claude"), "--version"], capture_output=True)
    assert proc.returncode == 99
    assert paid_path_violations(guard) == ["claude"]
