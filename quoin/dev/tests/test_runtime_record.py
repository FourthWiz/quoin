"""Tests for the install-record writer (`quoin.runtime_record`) and its
`_cmd_claude_install` wiring (IVG-281).

Covers the writer's field derivation, its never-raises contract, and the
parity that keeps the writer and the shared resolver (`auto_resume.py`)
agreeing on the record's filename and schema.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
QUOIN_SRC = REPO / "quoin"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quoin import runtime_record  # noqa: E402


def _load_auto_resume():
    core_path = QUOIN_SRC / "core" / "scripts" / "auto_resume.py"
    spec = importlib.util.spec_from_file_location("auto_resume_parity_check", core_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constants_parity_with_resolver():
    ar = _load_auto_resume()
    assert runtime_record.RUNTIME_RECORD_FILENAME == ar.RUNTIME_RECORD_FILENAME
    assert runtime_record.RUNTIME_RECORD_SCHEMA == ar.RUNTIME_RECORD_SCHEMA


def test_constants_parity_is_mutation_sensitive(monkeypatch):
    ar = _load_auto_resume()
    monkeypatch.setattr(ar, "RUNTIME_RECORD_FILENAME", "something-else.json")
    assert runtime_record.RUNTIME_RECORD_FILENAME != ar.RUNTIME_RECORD_FILENAME


def test_writer_happy_path_fields(tmp_path, monkeypatch):
    fake_python = tmp_path / "fake-venv-bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: None)
    monkeypatch.setattr(runtime_record, "_detect_source_version", lambda source_dir: None)

    dest_root = tmp_path / "dest" / ".claude"
    source_dir = tmp_path / "source" / "quoin"
    path = runtime_record.write_runtime_record(dest_root, source_dir)

    assert path == dest_root / runtime_record.RUNTIME_RECORD_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == runtime_record.RUNTIME_RECORD_SCHEMA
    assert data["python"] == str(fake_python)  # verbatim, never resolved
    assert data["pythonpath"] is None
    assert data["source_version"] is None
    assert data["installed_at"].endswith("Z")
    import quoin
    assert data["version"] == quoin.__version__


def test_writer_relative_interpreter_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "executable", "python3")  # relative
    dest_root = tmp_path / "dest"
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert not (dest_root / runtime_record.RUNTIME_RECORD_FILENAME).exists()
    assert "could not write install record" in capsys.readouterr().err


def test_writer_empty_interpreter_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "executable", "")
    dest_root = tmp_path / "dest"
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert not (dest_root / runtime_record.RUNTIME_RECORD_FILENAME).exists()


def test_writer_pythonpath_set_when_unaided_import_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", sys.executable)
    monkeypatch.setattr(runtime_record, "_unaided_quoin_file", lambda python, timeout=10.0: None)
    dest_root = tmp_path / "dest"
    source_dir = tmp_path / "source"
    path = runtime_record.write_runtime_record(dest_root, source_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pythonpath"] is not None
    import quoin
    assert data["pythonpath"] == str(Path(quoin.__file__).resolve().parents[1])


def test_writer_pythonpath_none_when_unaided_import_matches(tmp_path, monkeypatch):
    import quoin
    own_file = str(Path(quoin.__file__).resolve())
    monkeypatch.setattr(runtime_record, "_unaided_quoin_file", lambda python, timeout=10.0: own_file)
    dest_root = tmp_path / "dest"
    path = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pythonpath"] is None


def test_writer_source_version_parsed_and_mismatch_warns(tmp_path, monkeypatch, capsys):
    source_dir = tmp_path / "repo" / "quoin"
    source_dir.mkdir(parents=True)
    about = tmp_path / "repo" / "src" / "quoin" / "__about__.py"
    about.parent.mkdir(parents=True)
    about.write_text('__version__ = "999.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: None)
    dest_root = tmp_path / "dest"
    path = runtime_record.write_runtime_record(dest_root, source_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source_version"] == "999.0.0"
    err = capsys.readouterr().err
    assert "999.0.0" in err
    import quoin
    assert quoin.__version__ in err


def test_writer_no_about_file_source_version_none(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: None)
    dest_root = tmp_path / "dest"
    path = runtime_record.write_runtime_record(dest_root, tmp_path / "source-no-about")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source_version"] is None


def test_writer_replace_failure_leaves_no_record_or_tmp(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: None)
    monkeypatch.setattr(runtime_record, "_detect_source_version", lambda source_dir: None)

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    dest_root = tmp_path / "dest"
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert not (dest_root / runtime_record.RUNTIME_RECORD_FILENAME).exists()
    assert list(dest_root.glob(".quoin-runtime.*.tmp")) == []
    assert "could not write install record" in capsys.readouterr().err


def test_writer_removes_stale_record_on_skip(tmp_path, monkeypatch, capsys):
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    stale = dest_root / runtime_record.RUNTIME_RECORD_FILENAME
    stale.write_text('{"schema": 1}', encoding="utf-8")
    monkeypatch.setattr(sys, "executable", "")  # forces the skip branch
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert not stale.exists()
    assert "previous install record removed" in capsys.readouterr().err


def test_writer_removes_stale_record_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: None)
    monkeypatch.setattr(runtime_record, "_detect_source_version", lambda source_dir: None)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    stale = dest_root / runtime_record.RUNTIME_RECORD_FILENAME
    stale.write_text('{"schema": 1}', encoding="utf-8")

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert not stale.exists()
    assert "previous install record removed" in capsys.readouterr().err


def test_writer_never_raises_on_unexpected_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime_record, "_detect_pythonpath", lambda python: (_ for _ in ()).throw(RuntimeError("boom")))
    dest_root = tmp_path / "dest"
    result = runtime_record.write_runtime_record(dest_root, tmp_path / "source")
    assert result is None
    assert "could not write install record" in capsys.readouterr().err


# ── _cmd_claude_install wiring (T-06) ───────────────────────────────────────


def _stub_install_operations(monkeypatch):
    import quoin.installer as inst

    monkeypatch.setattr(inst, "check_prerequisites", lambda: [])
    monkeypatch.setattr(inst, "deploy_memory", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_quickstart", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_skills", lambda *a, **kw: 0)
    monkeypatch.setattr(inst, "deploy_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_core_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_core_workflow", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_dashboard_assets", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "cleanup_obsolete_scripts", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "deploy_hooks", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "regenerate_pollution_dispatch", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "regenerate_verification_step", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "merge_workflow_rules", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "regenerate_preambles", lambda *a, **kw: None)
    monkeypatch.setattr(inst, "assert_no_placeholders", lambda *a, **kw: [])


def _install_args(**overrides):
    import argparse

    defaults = dict(
        scope="project",
        allow_hook_merge=True,
        runtime="claude",
        check=False,
        source_dir=str(QUOIN_SRC),
        force_merge=False,
        dev=False,
        use_pip=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_install_writes_record_project_scope(tmp_path, monkeypatch):
    import unittest.mock
    import quoin.cli as cli

    _stub_install_operations(monkeypatch)
    monkeypatch.chdir(tmp_path)
    args = _install_args()
    with unittest.mock.patch("time.sleep"):
        result = cli._cmd_claude_install(args)
    assert result == 0
    dest_root = tmp_path / ".claude"
    record_path = dest_root / "quoin-runtime.json"
    assert record_path.exists()
    data = json.loads(record_path.read_text(encoding="utf-8"))
    assert data["python"] == sys.executable


def test_install_writes_record_user_scope(tmp_path, monkeypatch):
    import unittest.mock
    import pathlib as _pathlib
    import quoin.cli as cli
    import quoin.installer as inst

    _stub_install_operations(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(inst, "deploy_agentdesk", lambda *a, **kw: None)
    args = _install_args(scope="user")
    with unittest.mock.patch.object(_pathlib.Path, "home", return_value=fake_home):
        result = cli._cmd_claude_install(args)
    assert result == 0
    record_path = fake_home / ".claude" / "quoin-runtime.json"
    assert record_path.exists()


def test_install_placeholder_violation_writes_no_record(tmp_path, monkeypatch):
    import unittest.mock
    import quoin.cli as cli
    import quoin.installer as inst

    _stub_install_operations(monkeypatch)
    monkeypatch.setattr(inst, "assert_no_placeholders", lambda *a, **kw: ["some/file.md: __QUOIN_HOME__"])
    monkeypatch.chdir(tmp_path)
    args = _install_args()
    with unittest.mock.patch("time.sleep"):
        result = cli._cmd_claude_install(args)
    assert result == 1
    assert not (tmp_path / ".claude" / "quoin-runtime.json").exists()


def test_install_survives_writer_internal_error(tmp_path, monkeypatch):
    """The writer's own never-raises contract (test_writer_never_raises_on_
    unexpected_error) is what keeps a bad write from failing the install —
    this pins the same guarantee end to end through _cmd_claude_install."""
    import unittest.mock
    import quoin.cli as cli

    _stub_install_operations(monkeypatch)
    monkeypatch.setattr(
        runtime_record, "_detect_pythonpath",
        lambda python: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.chdir(tmp_path)
    args = _install_args()
    with unittest.mock.patch("time.sleep"):
        result = cli._cmd_claude_install(args)
    assert result == 0
    assert not (tmp_path / ".claude" / "quoin-runtime.json").exists()
