"""End-to-end tests for the auto-resume interpreter resolver: a real
install writes a record that the deployed resolver, invoked as a real
subprocess, reads back correctly — and the deployed hook stays inside its
timing budget even when the recorded interpreter never responds.

These are slower than the unit-level resolver/writer tests and touch a
real subprocess and (where noted) a real venv; skip cleanly if venv
creation fails on this machine rather than failing the suite.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import venv
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
QUOIN_SRC = REPO / "quoin"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import quoin  # noqa: E402

_MINIMAL_ENV = {"PATH": "/usr/bin:/bin"}


def _bare_python3() -> str:
    found = shutil.which("python3", path="/usr/bin:/bin")
    if not found:
        pytest.skip("no bare python3 on /usr/bin:/bin — cannot exercise the minimal-PATH subprocess path")
    return found


def _build_mini_deploy(tmp_path: Path) -> Path:
    """Copies just enough of the deploy tree (core/scripts + the scripts/
    wrapper) for `auto_resume.py` to run standalone, mirroring what
    `deploy_core_scripts`/`deploy_scripts` would produce."""
    deploy = tmp_path / "deploy"
    core_scripts = deploy / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    for fname in ("auto_resume.py", "run_state.py"):
        (core_scripts / fname).write_text(
            (QUOIN_SRC / "core" / "scripts" / fname).read_text(encoding="utf-8"), encoding="utf-8"
        )
    scripts_dir = deploy / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "auto_resume.py").write_text(
        (QUOIN_SRC / "scripts" / "auto_resume.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return deploy


# ── real install → deployed resolver ─────────────────────────────────────────


def test_real_install_then_cli_check_reports_usable(tmp_path, monkeypatch):
    import unittest.mock
    import argparse
    import quoin.cli as cli
    import quoin.installer as inst

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    monkeypatch.setattr(inst, "check_prerequisites", lambda: [])
    monkeypatch.chdir(project_dir)
    args = argparse.Namespace(
        scope="project", allow_hook_merge=True, runtime="claude", check=False,
        source_dir=str(QUOIN_SRC), force_merge=False, dev=False, use_pip=False,
    )
    with unittest.mock.patch.object(Path, "home", return_value=fake_home), \
         unittest.mock.patch("time.sleep"):
        result = cli._cmd_claude_install(args)
    assert result == 0

    dest_root = project_dir / ".claude"
    record_path = dest_root / "quoin-runtime.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text(encoding="utf-8"))

    auto_resume_path = dest_root / "core" / "scripts" / "auto_resume.py"
    proc = subprocess.run(
        [sys.executable, str(auto_resume_path), "cli-check", "--project-root", str(project_dir)],
        env=dict(_MINIMAL_ENV, HOME=str(fake_home)),
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout.strip())
    assert out["source"] == "record"
    assert out["status"] == "usable"
    assert out["probed_version"] == quoin.__version__
    # pythonpath is consistent with whether the record's interpreter can
    # unaided-import quoin (a stale shared venv may shadow it).
    if record["pythonpath"] is None:
        assert out["pythonpath"] is None
    else:
        assert out["pythonpath"] == record["pythonpath"]


# ── pyenv-style install: a bare absolute interpreter, no venv ──────────────


def test_pyenv_style_interpreter_resolves_usable(tmp_path, monkeypatch):
    pyenv_bin = tmp_path / "pyenv" / "versions" / "3.11.0" / "bin"
    pyenv_bin.mkdir(parents=True)
    interp = pyenv_bin / "python"
    interp.write_text("#!/bin/sh\necho QUOIN_VERSION=1.2.3\nexit 0\n", encoding="utf-8")
    interp.chmod(interp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "auto_resume_e2e_pyenv", QUOIN_SRC / "core" / "scripts" / "auto_resume.py"
    )
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)

    record_path = tmp_path / "quoin-runtime.json"
    record_path.write_text(json.dumps({
        "schema": 1, "python": str(interp), "version": "1.2.3", "pythonpath": None,
        "quoin_file": "x", "source_dir": "x", "source_version": None,
        "installed_at": "2026-09-29T00:00:00Z",
    }), encoding="utf-8")
    monkeypatch.setattr(ar, "_runtime_record_path", lambda: record_path)
    res = ar.resolve_cli(tmp_path, "start")
    assert res["status"] == "usable"
    assert res["argv"][0] == str(interp)


# ── spawn bootstrap: hostile package in cwd, real interpreter ──────────────


def test_spawn_bootstrap_ignores_hostile_package_in_cwd(tmp_path):
    """Defense in depth for the detached supervisor spawn (see
    _SPAWN_BOOTSTRAP): even when the spawn's cwd contains a hostile
    `quoin/` package — as it would have under the pre-fix cwd choice — the
    bootstrap's own sys.path[0] strip must stop it from being imported and
    run, while the real installed CLI still runs normally."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "auto_resume_e2e_spawn_bootstrap", QUOIN_SRC / "core" / "scripts" / "auto_resume.py"
    )
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)

    hostile_dir = tmp_path / "hostile-cwd"
    hostile_dir.mkdir()
    marker = tmp_path / "marker.txt"
    (hostile_dir / "quoin").mkdir()
    (hostile_dir / "quoin" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n", encoding="utf-8",
    )
    (hostile_dir / "quoin" / "__main__.py").write_text(
        "print('HOSTILE MAIN RAN')\n", encoding="utf-8",
    )

    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-c", ar._SPAWN_BOOTSTRAP, "--version"],
        cwd=str(hostile_dir), env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert not marker.exists(), "the hostile quoin/ package in cwd was imported"
    assert "HOSTILE MAIN RAN" not in proc.stdout
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"quoin {quoin.__version__}"


def test_snippets_ignore_hostile_cwd_reached_through_empty_pythonpath_element(tmp_path):
    """An empty PYTHONPATH element (`:/x`, the usual result of appending to
    an unset variable) puts the absolute cwd on sys.path a second time,
    after index 0. Both snippets must drop every cwd entry, so a hostile
    `quoin/` package and a planted `runpy.py` in the cwd are never
    imported by either the probe or the spawn bootstrap."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "auto_resume_e2e_empty_pythonpath", QUOIN_SRC / "core" / "scripts" / "auto_resume.py"
    )
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)

    hostile_dir = tmp_path / "hostile-cwd"
    hostile_dir.mkdir()
    marker = tmp_path / "marker.txt"
    (hostile_dir / "quoin").mkdir()
    (hostile_dir / "quoin" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('quoin')\n", encoding="utf-8",
    )
    (hostile_dir / "quoin" / "__main__.py").write_text("print('HOSTILE MAIN RAN')\n", encoding="utf-8")
    (hostile_dir / "runpy.py").write_text(
        f"open({str(marker)!r}, 'w').write('runpy')\n", encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep + str(SRC)}

    probe = subprocess.run(
        [sys.executable, "-c", ar._PROBE_SNIPPET],
        cwd=str(hostile_dir), env=env, capture_output=True, text=True, timeout=10,
    )
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert probe.returncode == 0, probe.stderr
    assert f"QUOIN_VERSION={quoin.__version__}" in probe.stdout

    spawn = subprocess.run(
        [sys.executable, "-c", ar._SPAWN_BOOTSTRAP, "--version"],
        cwd=str(hostile_dir), env=env, capture_output=True, text=True, timeout=10,
    )
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert "HOSTILE MAIN RAN" not in spawn.stdout
    assert spawn.returncode == 0, spawn.stderr
    assert spawn.stdout.strip() == f"quoin {quoin.__version__}"


# ── real venv: pip/uv-venv/uv-tool/pipx-style install ───────────────────────


def test_real_venv_interpreter_resolves_usable(tmp_path):
    venv_dir = tmp_path / "venv"
    try:
        venv.create(venv_dir, with_pip=False, symlinks=True)
    except Exception as exc:  # noqa: BLE001 — environment-dependent, skip rather than fail
        pytest.skip(f"venv creation failed on this machine: {exc}")

    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        pytest.skip("venv did not produce a bin/python (unsupported platform layout)")

    site_packages = next(venv_dir.glob("lib/python*/site-packages"), None)
    if site_packages is None:
        pytest.skip("venv did not produce a site-packages directory")
    (site_packages / "quoin_wt.pth").write_text(str(SRC) + "\n", encoding="utf-8")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "auto_resume_e2e_real_venv", QUOIN_SRC / "core" / "scripts" / "auto_resume.py"
    )
    ar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ar)

    record_path = tmp_path / "quoin-runtime.json"
    record_path.write_text(json.dumps({
        "schema": 1, "python": str(venv_python), "version": quoin.__version__, "pythonpath": None,
        "quoin_file": "x", "source_dir": "x", "source_version": None,
        "installed_at": "2026-09-29T00:00:00Z",
    }), encoding="utf-8")

    start = time.monotonic()
    proc = subprocess.run(
        [str(venv_python), "-c", ar._PROBE_SNIPPET],
        env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    elapsed = time.monotonic() - start
    print(f"real-venv probe wall time: {elapsed:.3f}s")  # printed for timing baselines; only the 5 s ceiling is asserted
    assert elapsed < 5.0
    assert proc.returncode == 0
    match = ar._VERSION_TOKEN_RE.search(proc.stdout)
    assert match is not None
    assert match.group(1) == quoin.__version__


# ── hook-budget: recorded interpreter never responds ────────────────────────


def test_stop_hook_stays_within_budget_when_interpreter_hangs(tmp_path):
    python3 = _bare_python3()
    deploy = _build_mini_deploy(tmp_path)
    project_dir = tmp_path / "proj"
    memory = project_dir / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True)

    slow_interp = tmp_path / "slow" / "python"
    slow_interp.parent.mkdir(parents=True)
    slow_interp.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    slow_interp.chmod(slow_interp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    (deploy / "quoin-runtime.json").write_text(json.dumps({
        "schema": 1, "python": str(slow_interp), "version": "1.0.0", "pythonpath": None,
        "quoin_file": "x", "source_dir": "x", "source_version": None,
        "installed_at": "2026-09-29T00:00:00Z",
    }), encoding="utf-8")

    (memory / "autonomous-run-demo.marker").write_text(
        "task: demo\ntimestamp: 2026-09-29T00:00:00+00:00\nautonomous: true\n", encoding="utf-8"
    )
    (memory / "run-state-demo.json").write_text(json.dumps({
        "schema": 1, "task": "demo", "session_id": "sid-1", "active": True,
        "phase": "implement", "phase_index": 3, "subphase": "", "step": "",
        "at_stage_boundary": False, "route": "", "profile": "", "artifacts": [],
        "next_action": "", "resume_command": "/run --resume demo",
        "notes_path": str(memory / "run-notes-demo.md"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    (memory / "run-continue-arm-sid-1.txt").touch()
    counter = {
        "schema": 1, "task": "demo", "attempts": 0, "consecutive_no_progress": 0,
        "chain_blocks": 5, "last_done_count": 0, "last_phase": ["implement", 3],
        "in_flight": False, "last_reason": "", "last_session_id": "",
        "marker_timestamp": "2026-09-29T00:00:00+00:00",
    }
    (memory / "auto-resume-demo.json").write_text(json.dumps(counter), encoding="utf-8")

    payload = json.dumps({"session_id": "sid-1", "stop_hook_active": True}).encode("utf-8")
    wrapper = deploy / "scripts" / "auto_resume.py"
    start = time.monotonic()
    proc = subprocess.run(
        [python3, str(wrapper), "stop", "--project-root", str(project_dir)],
        input=payload, env=dict(_MINIMAL_ENV), capture_output=True, timeout=10,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    assert elapsed < 7.0  # 3s stop budget + kill-and-reap slack, generous margin
    out = json.loads(proc.stdout.decode("utf-8").strip())
    assert out["decision"] == "block"
    assert not (memory / "autonomous-halt-demo.md").exists()
