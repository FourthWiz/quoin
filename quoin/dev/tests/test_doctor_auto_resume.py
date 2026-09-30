"""Tests for the `quoin doctor` "Auto-resume CLI" block: it must report the
same result as the hooks do, by shelling out to the deployed resolver's
read-only `cli-check` in a scrubbed env, rather than re-implementing the
resolver's own logic."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
QUOIN_SRC = REPO / "quoin"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import quoin  # noqa: E402
import quoin.cli as cli  # noqa: E402

RUNNING_VERSION = quoin.__version__


def _build_mini_deploy(dest_root: Path) -> None:
    """Copies just enough of the deploy tree for the deployed
    auto_resume.py to run standalone (mirrors the e2e helper in
    test_auto_resume_record_e2e.py)."""
    core_scripts = dest_root / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    for fname in ("auto_resume.py", "run_state.py"):
        (core_scripts / fname).write_text(
            (QUOIN_SRC / "core" / "scripts" / fname).read_text(encoding="utf-8"), encoding="utf-8"
        )


def _write_interpreter(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_record(dest_root: Path, *, python: str, version: str, pythonpath=None, source_version=None) -> None:
    record = {
        "schema": 1,
        "python": python,
        "version": version,
        "pythonpath": pythonpath,
        "quoin_file": "x",
        "source_dir": "x",
        "source_version": source_version,
        "installed_at": "2026-09-29T00:00:00Z",
    }
    (dest_root / "quoin-runtime.json").write_text(json.dumps(record), encoding="utf-8")


def _deploy(tmp_path: Path) -> Path:
    dest_root = tmp_path / ".claude"
    _build_mini_deploy(dest_root)
    return dest_root


def test_missing_record_is_an_error_and_skips_subprocess(tmp_path):
    dest_root = _deploy(tmp_path)
    errors: list = []
    warnings: list = []

    def _fail_run(*a, **k):
        raise AssertionError("run() must not be called when no record exists")

    cli._doctor_auto_resume_cli(
        dest_root, tmp_path, errors, warnings, run=_fail_run, which=lambda *a, **k: "/usr/bin/claude"
    )
    assert len(errors) == 1
    assert "no install record" in errors[0]
    assert warnings == []


def test_usable_record_reports_no_errors_or_warnings(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-ok"
    _write_interpreter(interp, f"echo QUOIN_VERSION={RUNNING_VERSION}\nexit 0")
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION)

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert errors == []
    assert warnings == []


def test_interpreter_missing_names_the_path(tmp_path):
    dest_root = _deploy(tmp_path)
    gone = tmp_path / "no-such-python"
    _write_record(dest_root, python=str(gone), version=RUNNING_VERSION)

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "interpreter-missing" in errors[0]
    assert gone.name in errors[0]


def test_interpreter_exit_nonzero_names_import_failed(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-boom"
    _write_interpreter(interp, "echo boom >&2\nexit 1")
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION)

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "import-failed" in errors[0]


def test_probed_version_mismatch_names_both_versions(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-stale"
    _write_interpreter(interp, "echo QUOIN_VERSION=0.0.1\nexit 0")
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION)

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "version-mismatch" in errors[0]
    assert "0.0.1" in errors[0] and RUNNING_VERSION in errors[0]


def test_usable_but_older_than_running_cli_names_both(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-0.0.1"
    _write_interpreter(interp, "echo QUOIN_VERSION=0.0.1\nexit 0")
    _write_record(dest_root, python=str(interp), version="0.0.1")

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "but this CLI is" in errors[0]
    assert "0.0.1" in errors[0]


def test_source_version_mismatch_names_version_mismatch(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-ok"
    _write_interpreter(interp, f"echo QUOIN_VERSION={RUNNING_VERSION}\nexit 0")
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION, source_version="9.9.9")

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "version-mismatch" in errors[0]


def test_deployed_resolver_broken_gives_predates_error(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-ok"
    _write_interpreter(interp, f"echo QUOIN_VERSION={RUNNING_VERSION}\nexit 0")
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION)
    (dest_root / "core" / "scripts" / "auto_resume.py").write_text(
        "print('not json')\n", encoding="utf-8"
    )

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert len(errors) == 1
    assert "predates the install record" in errors[0]


def test_pythonpath_fallback_is_a_single_warning(tmp_path):
    dest_root = _deploy(tmp_path)
    interp = tmp_path / "python-ok"
    _write_interpreter(interp, f"echo QUOIN_VERSION={RUNNING_VERSION}\nexit 0")
    pp_dir = tmp_path / "src-fallback"
    pp_dir.mkdir()
    _write_record(dest_root, python=str(interp), version=RUNNING_VERSION, pythonpath=str(pp_dir))

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, tmp_path, errors, warnings, which=lambda *a, **k: "/usr/bin/claude")
    assert errors == []
    assert len(warnings) == 1
    assert f"PYTHONPATH={pp_dir}" in warnings[0]


def test_claude_missing_in_project_scope_is_a_warning(tmp_path):
    dest_root = tmp_path / "proj" / ".claude"
    _build_mini_deploy(dest_root)
    project_root = tmp_path / "proj"

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, project_root, errors, warnings, which=lambda *a, **k: None)
    assert any("claude not found on PATH" in w for w in warnings)


def test_claude_found_only_outside_minimal_path_is_informational_only(tmp_path, capsys):
    dest_root = tmp_path / "proj" / ".claude"
    _build_mini_deploy(dest_root)
    project_root = tmp_path / "proj"

    def _which(name, path=None):
        if path is None:
            return "/opt/homebrew/bin/claude"
        return None

    errors: list = []
    warnings: list = []
    cli._doctor_auto_resume_cli(dest_root, project_root, errors, warnings, which=_which)
    assert warnings == []
    out = capsys.readouterr().out
    assert "claude resolves only outside" in out


def test_cmd_doctor_project_scope_missing_record_exits_nonzero(tmp_path, monkeypatch, capsys):
    import argparse

    project_dir = tmp_path / "proj"
    dest_root = project_dir / ".claude"
    _build_mini_deploy(dest_root)
    # Minimum install shape _cmd_doctor needs before reaching the auto-resume block.
    import quoin.installer as inst
    (dest_root / "memory").mkdir(exist_ok=True)
    for fname in inst.TIER1_MEMORY_FILES:
        (dest_root / "memory" / fname).write_text("x", encoding="utf-8")
    (dest_root / "scripts").mkdir(exist_ok=True)
    for fname in inst.DEPLOYED_SCRIPTS:
        (dest_root / "scripts" / fname).write_text("x", encoding="utf-8")
    for fname in inst.CORE_SCRIPTS:
        (dest_root / "core" / "scripts" / fname).write_text("x", encoding="utf-8")
    (dest_root / "core" / "workflow").mkdir(exist_ok=True)
    for fname in inst.CORE_WORKFLOW_FILES:
        (dest_root / "core" / "workflow" / fname).write_text("x", encoding="utf-8")
    assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
    assets_dir.mkdir(exist_ok=True)
    for fname in inst._DASHBOARD_ASSETS:
        (assets_dir / fname).write_text("x", encoding="utf-8")
    (dest_root / "skills").mkdir(exist_ok=True)
    for skill in inst.CANONICAL_SKILLS:
        (dest_root / "skills" / skill).mkdir(exist_ok=True)
    (project_dir / "CLAUDE.md").write_text(
        "# === DEV WORKFLOW START ===\nx\n# === DEV WORKFLOW END ===\n", encoding="utf-8"
    )

    monkeypatch.chdir(project_dir)
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    args = argparse.Namespace(runtime="claude", scope="project", json=False)
    rc = cli._cmd_doctor(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no install record" in out
