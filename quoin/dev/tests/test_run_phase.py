"""The /run phase marker script: one appended line per call, and it never fails a step."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "run_phase.py"
LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (\S+)( skipped)?$")


def _run(root, *args, profile=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("QUOIN_RUN_PROFILE", None)
    if profile is not None:
        env["QUOIN_RUN_PROFILE"] = profile
    return subprocess.run(
        [sys.executable, str(WRAPPER_PATH), *args, "--root", str(root)],
        capture_output=True, text=True, env=env,
    )


def _lines(root, task="fix-thing"):
    return (root / ".workflow_artifacts" / task / "phases.log").read_text().splitlines()


def test_appends_one_line_per_marker_with_skipped_and_stage(tmp_path):
    root = tmp_path / "My Drive"
    assert _run(root, "fix-thing", "setup").returncode == 0
    assert _run(root, "fix-thing", "discover", "--skipped").returncode == 0
    assert _run(root, "fix-thing", "implement", "--stage", "2").returncode == 0
    parsed = [LINE_RE.match(line).groups() for line in _lines(root)]
    assert parsed == [("setup", None), ("discover", " skipped"), ("2:implement", None)]


def test_prints_the_run_profile(tmp_path):
    assert _run(tmp_path, "fix-thing", "setup", profile="bench-lean").stdout == "profile=bench-lean\n"
    assert _run(tmp_path, "fix-thing", "frame").stdout == "profile=\n"


def test_bad_input_warns_and_still_exits_zero(tmp_path):
    for args in (("fix-thing", "plan"), ("../escape", "setup"), ("fix-thing",),
                 ("fix-thing", "verify", "--stage", "0")):
        result = _run(tmp_path, *args)
        assert result.returncode == 0
    assert not (tmp_path / ".workflow_artifacts").exists() or not any(
        (tmp_path / ".workflow_artifacts").rglob("phases.log"))
    blocked = tmp_path / "blocked"
    (blocked / ".workflow_artifacts").mkdir(parents=True)
    (blocked / ".workflow_artifacts" / "fix-thing").write_text("not a dir")
    result = _run(blocked, "fix-thing", "setup")
    assert result.returncode == 0 and "WARN" in result.stderr
