"""Drift guards for the full-suite recipe: all three copies resolve the repo's
interpreter through the same guarded line instead of a bare `python3 -m pytest`."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
AMODE = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"
PACKAGE_DIR = REPO_ROOT / "quoin"

GUARDED_LINE = (
    'PY="$(python3 __QUOIN_HOME__/scripts/affected_tests.py --print-interpreter '
    '--interpreter-anchor repo --interpreter-only --project-root "$PROJECT_ROOT")" '
    '&& [ -x "$PY" ] || { echo "interpreter_reason: helper-unavailable"; PY=python3; }'
)

FG_START = '- [ ] Run full test suite with the known-red manifest consult (IVG-144'
HL_START = "**Headless variant (autonomous):**"
HL_END = "\n  `RC=$?`"
AM_START = "**Headless full-suite recipe.**"


def _gate() -> str:
    return GATE.read_text(encoding="utf-8")


def _amode() -> str:
    return AMODE.read_text(encoding="utf-8")


def _fg_slice() -> str:
    text = _gate()
    i = text.index('JUNIT="$PROJECT_ROOT/.workflow_artifacts/cache/gate-fullsuite-')
    return text[i : text.index("known_red.py", i)]


def _hl_slice() -> str:
    text = _gate()
    i = text.index(HL_START)
    return text[i : text.index(HL_END, i)]


def _am_slice() -> str:
    text = _amode()
    i = text.index(AM_START)
    return text[i : text.index("\n\n", i)]


def _fg_region() -> str:
    text = _gate()
    i = text.index(FG_START)
    return text[i : text.index(HL_START, i)]


SLICES = {"foreground": _fg_slice, "headless": _hl_slice, "autonomous": _am_slice}


@pytest.mark.parametrize("site", sorted(SLICES))
def test_each_site_has_guarded_line(site):
    assert GUARDED_LINE in SLICES[site]()


@pytest.mark.parametrize("site", sorted(SLICES))
def test_each_site_runs_resolved_interpreter(site):
    assert '"$PY" -m pytest' in SLICES[site]()


@pytest.mark.parametrize("site", sorted(SLICES))
def test_no_site_runs_bare_python3_pytest(site):
    assert "python3 -m pytest" not in SLICES[site]()


def test_slices_are_disjoint():
    gate = _gate()
    amode = _amode()
    fg_off = gate.index(_fg_slice())
    hl_off = gate.index(_hl_slice())
    assert fg_off != hl_off
    assert gate.index(GUARDED_LINE) < hl_off or gate.index(GUARDED_LINE) != hl_off
    for slc in (_fg_slice(), _hl_slice(), _am_slice()):
        assert slc.count(GUARDED_LINE) == 1
    assert gate.count(GUARDED_LINE) == 2
    assert amode.count(GUARDED_LINE) == 1


def test_repo_has_no_other_full_suite_bare_copy():
    skip_parts = {"tests", ".venv", "node_modules"}
    stub = REPO_ROOT / "quoin" / "skills" / "gate" / "SKILL.md"
    offenders = []
    for path in (REPO_ROOT / "quoin").rglob("*.md"):
        rel = path.relative_to(REPO_ROOT / "quoin")
        if skip_parts & set(rel.parts) or path == stub:
            continue
        if "python3 -m pytest -rA" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(rel))
    assert offenders == []


def test_foreground_records_interpreter_lines():
    fg = _fg_slice()
    assert (
        'affected_tests.py --print-interpreter --interpreter-anchor repo '
        '--project-root "$PROJECT_ROOT"'
    ) in fg


def test_audit_row_sentence_present():
    assert (
        "Record the second call's `interpreter*` lines (or `helper-unavailable`) "
        "in the audit row."
    ) in _gate()


def _old_python():
    for name in ("python3.8", "python3.9"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture
def stub_project(tmp_path):
    proj = tmp_path / "proj"
    bindir = proj / "repo" / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (proj / "repo" / ".git").mkdir()
    stub = bindir / "python"
    stub.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return proj, str(proj / "repo" / ".venv" / "bin" / "python")


@pytest.mark.skipif(_old_python() is None, reason="no python3.8/3.9 on PATH")
def test_helper_runs_under_oldest_python(stub_project):
    proj, expected = stub_project
    old = _old_python()
    # the stub is a plain executable (not a symlink): the helper's import probe
    # succeeds because the stub execs the test interpreter, which has pytest
    r = subprocess.run(
        [old, "quoin/scripts/affected_tests.py", "--print-interpreter",
         "--interpreter-anchor", "repo", "--interpreter-only",
         "--project-root", str(proj)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [expected]

    # the deployed home path has no spaces; the checkout path may, so go through a link
    home_link = proj.parent / "quoin-home"
    home_link.symlink_to(PACKAGE_DIR)
    line = GUARDED_LINE.replace("__QUOIN_HOME__", str(home_link))
    bin_dir = Path(old).parent
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PROJECT_ROOT"] = str(proj)
    env.pop("QUOIN_DISABLE_AFFECTED_TESTS", None)
    r2 = subprocess.run(
        ["/bin/sh", "-c", line + '; echo "$PY"'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.splitlines()[-1] == expected
    assert "helper-unavailable" not in r2.stdout
