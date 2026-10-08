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


# -- sequencing, large-selection rule and knob documentation ----------------

SEQUENCING = "Never start one test suite while the other still runs."
DISPATCH_GUIDE = REPO_ROOT / "quoin" / "memory" / "dispatch-guide.md"
AT_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "affected_tests.py"


def _row(start: str, end: str = "\n- [ ] ") -> str:
    text = _gate()
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def test_sequencing_rule_at_each_site():
    for region in (_fg_region(), _hl_slice(), _am_slice()):
        assert region.count(SEQUENCING) == 1


def test_foreground_large_selection_rule():
    row = _row("- [ ] Affected-area test suite (BLOCKING hard precondition for APPROVED)")
    assert "45" in row and "600000" in row and "wait_for.py" in row
    post = _row("- [ ] Affected-area test suite (re-run")
    assert "(same 45-file rule)" in post


def test_gate_names_pytest_timeout_knob():
    assert "QUOIN_PYTEST_TIMEOUT" in _gate()


def test_docstring_documents_pytest_knob():
    doc = AT_PATH.read_text(encoding="utf-8").split('"""', 2)[1]
    assert "QUOIN_PYTEST_TIMEOUT" in doc
    assert "max(600, QUOIN_SUBPROCESS_TIMEOUT)" not in doc


def test_dispatch_guide_documents_pytest_knob_and_keeps_ci_mirror_bound():
    text = DISPATCH_GUIDE.read_text(encoding="utf-8")
    assert "QUOIN_PYTEST_TIMEOUT" in text
    i = text.index("`ci_mirror.py` still bounds")
    assert "max(600, QUOIN_SUBPROCESS_TIMEOUT)" in text[i : i + 200]


def test_recipe_notes_moved_text_present_in_amode():
    text = " ".join(_amode().split())
    assert "**Recipe notes.**" in text
    for phrase in ("outer project root", "silently re-enable", "fresh shell"):
        assert phrase in text
