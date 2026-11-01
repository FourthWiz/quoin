"""affected_tests separates an unusable test environment from a change that broke collection.

When every selected test fails to collect, the helper re-collects the same tests
at the baseline revision. Collection failing there too means the interpreter or
runner cannot load the project (exit 3, test-environment-unusable); collection
working there means the change broke it (exit 1, affected-red, unchanged).
These tests run real git and a real pytest subprocess.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_CORE_PATH = Path(__file__).resolve().parents[2] / "core" / "scripts" / "affected_tests.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("_quoin_core_affected_tests_env", _CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_at = _load_core()


@pytest.fixture(autouse=True)
def _interp(monkeypatch):
    monkeypatch.delenv("QUOIN_PYTHON", raising=False)
    monkeypatch.setenv("QUOIN_DISABLE_VENV_PROBE", "1")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo(tmp_path, test_body, source_after):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "foo.py").write_text("def foo():\n    return 1\n")
    (repo / "test_foo.py").write_text(test_body)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    _git("switch", "-c", "feat", cwd=repo)
    (repo / "foo.py").write_text(source_after)
    _git("commit", "-am", "change", cwd=repo)
    return repo


def _run(repo):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _at.main(["--repo-root", str(repo), "--files", "foo.py", "--format", "json"])
    return rc, json.loads(buf.getvalue())


def test_collection_failing_at_baseline_too_is_undeterminable(tmp_path):
    repo = _repo(
        tmp_path,
        "import quoin_missing_module_for_env_test\nimport foo\n\ndef test_foo():\n    assert foo.foo()\n",
        "def foo():\n    return 2\n",
    )
    rc, d = _run(repo)
    assert rc == 3
    assert d["exit_reason"] == "test-environment-unusable"
    assert d["ran_pytest"] is True
    assert "cannot collect these tests even without the change" in d["remedy"]


def test_collection_broken_by_the_change_stays_red(tmp_path):
    repo = _repo(
        tmp_path,
        "import foo\n\ndef test_foo():\n    assert foo.foo()\n",
        "import quoin_missing_module_for_env_test\n\ndef foo():\n    return 2\n",
    )
    rc, d = _run(repo)
    assert rc == 1
    assert d["exit_reason"] == "affected-red"


def test_a_failing_test_that_ran_stays_red(tmp_path):
    repo = _repo(
        tmp_path,
        "import foo\n\ndef test_foo():\n    assert foo.foo() == 1\n",
        "def foo():\n    return 2\n",
    )
    rc, d = _run(repo)
    assert rc == 1
    assert d["exit_reason"] == "affected-red"


def test_baseline_worktree_is_removed(tmp_path):
    repo = _repo(
        tmp_path,
        "import quoin_missing_module_for_env_test\n\ndef test_foo():\n    pass\n",
        "def foo():\n    return 2\n",
    )
    _run(repo)
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True, check=True).stdout
    assert len(listed.strip().splitlines()) == 1


def test_junit_check_needs_no_test_that_ran(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="" name="t">'
        '<error message="collection failure">x</error></testcase>'
        '<testcase classname="m" name="test_ok"/></testsuite></testsuites>'
    )
    assert _at._junit_only_collection_errors(report) is False
    report.write_text(
        '<testsuites><testsuite><testcase classname="" name="t">'
        '<error message="collection failure">x</error></testcase></testsuite></testsuites>'
    )
    assert _at._junit_only_collection_errors(report) is True
