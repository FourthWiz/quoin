"""Tests for injected XDG resolution and the non-exiting data-dir locator."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quoin import cli
from quoin.opencode_adapter import paths

SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_PKG = SOURCE_ROOT.parent / "src" / "quoin"
HOME = Path("/nonexistent-home")


def test_config_home_variants(tmp_path):
    assert paths.config_home({}, HOME) == HOME / ".config"
    assert paths.config_home({"XDG_CONFIG_HOME": ""}, HOME) == HOME / ".config"
    assert paths.config_home({"XDG_CONFIG_HOME": "relative/dir"}, HOME) == HOME / ".config"
    assert paths.config_home({"XDG_CONFIG_HOME": str(tmp_path)}, HOME) == tmp_path


def test_state_home_variants(tmp_path):
    assert paths.state_home({}, HOME) == HOME / ".local" / "state"
    assert paths.state_home({"XDG_STATE_HOME": "rel"}, HOME) == HOME / ".local" / "state"
    assert paths.state_home({"XDG_STATE_HOME": str(tmp_path)}, HOME) == tmp_path


def test_derived_paths(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "c"), "XDG_STATE_HOME": str(tmp_path / "s")}
    base = tmp_path / "c" / "quoin" / "opencode"
    assert paths.opencode_config_dir(env, HOME) == base
    assert paths.profiles_dir(env, HOME) == base / "profiles"
    assert paths.profile_path("work", env, HOME) == base / "profiles" / "work.json"
    assert paths.qualification_path("q1", env, HOME) == base / "qualifications" / "q1.json"
    assert paths.state_dir(env, HOME) == tmp_path / "s" / "quoin" / "opencode"
    with pytest.raises(ValueError):
        paths.profile_path("Bad Name", env, HOME)


def test_managed_policy_path(tmp_path, monkeypatch):
    assert paths.managed_policy_path({}) is None
    assert paths.managed_policy_path({"QUOIN_OPENCODE_MANAGED_POLICY": ""}) is None
    monkeypatch.chdir(tmp_path)
    got = paths.managed_policy_path({"QUOIN_OPENCODE_MANAGED_POLICY": "rel/policy.json"})
    assert got.is_absolute()
    assert got == Path(os.path.abspath("rel/policy.json"))


def test_project_paths_and_key(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    other = tmp_path / "other"
    other.mkdir()
    assert paths.project_runtime_path(real) == real / ".quoin" / "runtime.json"
    assert paths.project_key(real) == paths.project_key(alias)
    assert paths.project_key(real) != paths.project_key(other)
    assert len(paths.project_key(real)) == 16


def test_locator_parity_editable():
    expected = cli._resolve_source_dir(None) / "adapters" / "opencode"
    assert paths.adapter_data_dir() == expected
    assert paths.runtime_config_schema_path().name == "runtime-config.schema.json"


_PROBE = (
    "import sys\n"
    "from pathlib import Path\n"
    "from quoin.opencode_adapter import paths\n"
    "print(paths.adapter_data_dir())\n"
)
_CLI_PROBE = (
    "import sys\n"
    "from quoin import cli\n"
    "print(cli._resolve_source_dir(None) / 'adapters' / 'opencode')\n"
)


def _run(code, site):
    env = dict(os.environ, PYTHONPATH=str(site))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )


def _copy_package(site):
    dest = site / "quoin"
    shutil.copytree(SRC_PKG, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def test_locator_parity_wheel_layout(tmp_path):
    site = tmp_path / "site"
    pkg = _copy_package(site)
    (pkg / "data" / "skills").mkdir(parents=True)
    (pkg / "data" / "adapters" / "opencode").mkdir(parents=True)
    mine = _run(_PROBE, site)
    theirs = _run(_CLI_PROBE, site)
    assert mine.returncode == 0, mine.stderr
    assert theirs.returncode == 0, theirs.stderr
    assert mine.stdout.strip() == theirs.stdout.strip()
    assert mine.stdout.strip().endswith("data/adapters/opencode")


def test_locator_neither_layout(tmp_path):
    site = tmp_path / "site"
    _copy_package(site)
    mine = _run(_PROBE, site)
    theirs = _run(_CLI_PROBE, site)
    assert mine.returncode == 0, mine.stderr
    assert mine.stdout.strip() == "None"
    assert theirs.returncode == 2


def test_missing_data_raises(monkeypatch):
    monkeypatch.setattr(paths, "adapter_data_dir", lambda: None)
    with pytest.raises(paths.AdapterDataMissing):
        paths.runtime_config_schema_path()


def test_module_never_exits():
    text = (SRC_PKG / "opencode_adapter" / "paths.py").read_text(encoding="utf-8")
    assert "sys.exit" not in text
