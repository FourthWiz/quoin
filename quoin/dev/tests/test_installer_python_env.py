"""Interpreter hygiene in the Python installer: pip runs under the running
interpreter, --user is dropped inside a venv, and old Pythons get a clear
message instead of a syntax error."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin import installer as inst  # noqa: E402

MAIN_PATH = REPO_ROOT / "src" / "quoin" / "__main__.py"


def _capture_runs(monkeypatch, pip_version_rc=0, install_rc=0):
    calls = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        rc = pip_version_rc if "--version" in argv else install_rc
        return subprocess.CompletedProcess(argv, rc)

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("in_venv", [True, False])
def test_dev_deps_use_running_interpreter_and_user_flag_rule(monkeypatch, in_venv):
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setattr(sys, "prefix", "/venv" if in_venv else "/base")
    inst.install_dev_deps()
    install = [c for c in calls if "install" in c][0]
    assert install[:3] == [sys.executable, "-m", "pip"]
    assert ("--user" in install) is (not in_venv)


def test_dev_deps_pip_missing_warns_and_skips_install(monkeypatch, capsys):
    calls = _capture_runs(monkeypatch, pip_version_rc=1)
    inst.install_dev_deps()
    assert not [c for c in calls if "install" in c]
    assert "pip not found" in capsys.readouterr().err


def test_no_python3_on_path_is_a_warning_not_a_missing_tool(monkeypatch, capsys):
    real_which = inst.shutil.which

    def which(name, *a, **kw):
        return None if name == "python3" else (real_which(name, *a, **kw) or "/bin/true")

    monkeypatch.setattr(inst.shutil, "which", which)
    missing = inst.check_prerequisites()
    assert not any("python3" in m for m in missing)
    err = capsys.readouterr().err
    assert "no python3 on PATH" in err and sys.executable in err


def test_old_bare_python3_on_path_warns(monkeypatch, capsys):
    real_which = inst.shutil.which

    def which(name, *a, **kw):
        return "/fake/python3" if name == "python3" else (real_which(name, *a, **kw) or "/bin/true")

    monkeypatch.setattr(inst.shutil, "which", which)
    monkeypatch.setattr(inst, "_bare_python3_version", lambda path: (3, 8))
    inst.check_prerequisites()
    err = capsys.readouterr().err
    assert "python3 on PATH is 3.8" in err and sys.executable in err


def test_sufficient_bare_python3_on_path_is_silent(monkeypatch, capsys):
    real_which = inst.shutil.which
    monkeypatch.setattr(
        inst.shutil, "which",
        lambda name, *a, **kw: real_which(name, *a, **kw) or "/bin/true",
    )
    monkeypatch.setattr(inst, "_bare_python3_version", lambda path: inst.MIN_PYTHON)
    inst.check_prerequisites()
    assert "python3 on PATH is" not in capsys.readouterr().err


def test_installer_min_python_matches_pyproject():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    major, minor = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)', text).groups()
    assert inst.MIN_PYTHON == (int(major), int(minor))


def test_main_min_python_matches_pyproject():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    major, minor = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)', text).groups()
    ns = {}
    src = MAIN_PATH.read_text(encoding="utf-8").split("from quoin.cli")[0]
    exec(compile(src.replace("raise SystemExit(1)", "pass"), "m", "exec"), ns)
    assert ns["MIN_PYTHON"] == (int(major), int(minor))


def test_old_python_exits_before_importing_the_cli(monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "quoin.cli", raising=False)
    monkeypatch.setattr(sys, "version_info", (3, 8, 18, "final", 0))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MAIN_PATH), run_name="__main__")
    assert exc.value.code == 1
    assert "3.10" in capsys.readouterr().err
    assert "quoin.cli" not in sys.modules
