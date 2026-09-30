"""Install-tier coverage for the runtime record: a real install, run against
a real venv interpreter, must produce a record the deployed resolver reports
as usable — for the three shapes install.sh (or a wrapping tool) actually
produces.

Tier 1 / Tier 3 (quoin importable unaided by the target interpreter: an
editable or non-editable install) and Tier 2 (`PYTHONPATH=<repo>/src python
-m quoin install`, no unaided import) are both install.sh fast-path outcomes.
The isolated-tool shape (`uv tool install` / `pipx install`, then that tool's
own `quoin install`) is not something install.sh can see at all — it probes
a single `$PYTHON` on `PATH`, and an isolated tool venv is invisible to that
probe. These tests exercise the writer + deployed resolver directly (real
install via `_cmd_claude_install`, real venv, real subprocess `cli-check`),
not install.sh itself; skip cleanly if venv creation is not possible here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unittest.mock
from pathlib import Path

import pytest

import quoin
import quoin.cli as cli
import quoin.installer as inst

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"

_MINIMAL_ENV = {"PATH": "/usr/bin:/bin"}


def _make_venv(dest: Path, *, pth_target: Path | None) -> Path:
    """Creates a venv at `dest` and, when `pth_target` is given, drops a
    `.pth` file in its site-packages pointing at it (making quoin unaided-
    importable from that venv). Returns the venv's python. Skips the test
    cleanly when venv creation isn't possible on this machine."""
    import venv

    try:
        venv.create(dest, with_pip=False, symlinks=True)
    except Exception as exc:  # noqa: BLE001 — environment-dependent
        pytest.skip(f"venv creation failed on this machine: {exc}")

    venv_python = dest / "bin" / "python"
    if not venv_python.exists():
        pytest.skip("venv did not produce a bin/python (unsupported platform layout)")

    if pth_target is not None:
        site_packages = next(dest.glob("lib/python*/site-packages"), None)
        if site_packages is None:
            pytest.skip("venv did not produce a site-packages directory")
        (site_packages / "quoin_wt.pth").write_text(str(pth_target) + "\n", encoding="utf-8")

    return venv_python


def _install(tmp_path: Path, monkeypatch, venv_python: Path) -> Path:
    """Runs a real project-scope `_cmd_claude_install` with `sys.executable`
    swapped to `venv_python`, so the writer records that interpreter. Returns
    the deploy dest_root (`<project>/.claude`)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(inst, "check_prerequisites", lambda: [])
    monkeypatch.setattr(sys, "executable", str(venv_python))
    monkeypatch.chdir(project_dir)
    args = argparse.Namespace(
        scope="project", allow_hook_merge=True, runtime="claude", check=False,
        source_dir=str(REPO / "quoin"), force_merge=False, dev=False, use_pip=False,
    )
    with unittest.mock.patch.object(Path, "home", return_value=fake_home), \
         unittest.mock.patch("time.sleep"):
        result = cli._cmd_claude_install(args)
    assert result == 0
    return project_dir / ".claude"


def _cli_check(dest_root: Path, project_root: Path) -> dict:
    auto_resume_path = dest_root / "core" / "scripts" / "auto_resume.py"
    proc = subprocess.run(
        [sys.executable, str(auto_resume_path), "cli-check", "--project-root", str(project_root)],
        env=dict(_MINIMAL_ENV),
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


# ── Tier 1 / Tier 3: quoin importable unaided ───────────────────────────────


def test_tier1_shape_unaided_importable_records_null_pythonpath(tmp_path, monkeypatch):
    venv_python = _make_venv(tmp_path / "venv", pth_target=SRC)

    dest_root = _install(tmp_path, monkeypatch, venv_python)
    record = json.loads((dest_root / "quoin-runtime.json").read_text(encoding="utf-8"))
    assert record["python"] == str(venv_python)
    assert record["pythonpath"] is None

    out = _cli_check(dest_root, tmp_path / "proj")
    assert out["source"] == "record"
    assert out["status"] == "usable"
    assert out["pythonpath"] is None


# ── Tier 2: PYTHONPATH-dependent install, no unaided import ────────────────


def test_tier2_shape_bare_venv_records_pythonpath(tmp_path, monkeypatch):
    venv_python = _make_venv(tmp_path / "venv", pth_target=None)

    dest_root = _install(tmp_path, monkeypatch, venv_python)
    record = json.loads((dest_root / "quoin-runtime.json").read_text(encoding="utf-8"))
    assert record["python"] == str(venv_python)
    assert record["pythonpath"] == str(SRC)

    out = _cli_check(dest_root, tmp_path / "proj")
    assert out["source"] == "record"
    assert out["status"] == "usable"
    assert out["pythonpath"] == str(SRC)


# ── isolated tool install (uv tool / pipx): host PATH scrubbed ─────────────


def test_isolated_tool_venv_records_tool_interpreter_with_scrubbed_path(tmp_path, monkeypatch):
    tool_venv_dir = tmp_path / "tools" / "quoin" / "venv"
    tool_venv_dir.parent.mkdir(parents=True)
    venv_python = _make_venv(tool_venv_dir, pth_target=SRC)

    # The host PATH cannot see this tool venv at all — the record must not
    # depend on PATH to find the interpreter again.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    dest_root = _install(tmp_path, monkeypatch, venv_python)
    record = json.loads((dest_root / "quoin-runtime.json").read_text(encoding="utf-8"))
    assert record["python"] == str(venv_python)
    assert record["pythonpath"] is None

    out = _cli_check(dest_root, tmp_path / "proj")
    assert out["source"] == "record"
    assert out["status"] == "usable"
