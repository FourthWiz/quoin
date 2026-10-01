"""IVG-280 T-08: Python 3.8 compatibility checks for auto_resume.py.

Mirrors test_probe_gateway_py38.py's approach (AST-level static checks plus
execution under a located real 3.8 interpreter). Consolidated — a handful of
representative subcommand invocations under 3.8, not the architecture's full
per-subcommand exhaustive matrix — see current-plan.md T-08's recorded
deviation.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"

_SAFE_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}

# Banned runtime (non-annotation) constructs for a Python 3.8 target,
# matching I-09: no runtime PEP-604 union, no str.removeprefix/removesuffix,
# no dict |, no functools.cache, no zoneinfo, no runtime list[str].
_DENYLIST_SUBSTRINGS = (
    ".removeprefix(",
    ".removesuffix(",
    "functools.cache(",
    "import zoneinfo",
)


def _read() -> str:
    return CORE_PATH.read_text(encoding="utf-8")


def test_ast_parses_under_feature_version_38():
    ast.parse(_read(), feature_version=(3, 8))


def test_future_annotations_present():
    tree = ast.parse(_read())
    found = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    assert found, "auto_resume.py is missing 'from __future__ import annotations'"


def test_no_denylisted_runtime_constructs():
    src = _read()
    hits = [tok for tok in _DENYLIST_SUBSTRINGS if tok in src]
    assert not hits, f"auto_resume.py uses Python 3.8-incompatible construct(s): {hits}"


def _find_real_python38():
    candidates = []
    which38 = shutil.which("python3.8")
    if which38:
        candidates.append(which38)
    candidates.append("/usr/bin/python3")
    which3 = shutil.which("python3")
    if which3:
        candidates.append(which3)
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip() == "3 8":
            return candidate
    return None


@pytest.fixture(scope="module")
def real_python38():
    interp = _find_real_python38()
    if interp is None:
        pytest.skip("no real Python 3.8 interpreter found (checked python3.8, /usr/bin/python3, python3)")
    return interp


def test_real_38_imports_module(real_python38):
    program = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('quoin_auto_resume_py38test', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('ok')\n"
    ) % (str(CORE_PATH),)
    result = subprocess.run(
        [real_python38, "-B", "-c", program],
        capture_output=True, text=True, timeout=15, env=_SAFE_ENV,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_real_38_status_subcommand_exits_0(real_python38, tmp_path):
    project = tmp_path / "project"
    (project / ".workflow_artifacts" / "memory").mkdir(parents=True)
    result = subprocess.run(
        [real_python38, "-B", str(CORE_PATH), "status", "--project-root", str(project), "--task", "demo"],
        capture_output=True, text=True, timeout=15, env=_SAFE_ENV,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_real_38_stop_no_markers_is_silent(real_python38, tmp_path):
    project = tmp_path / "project"
    (project / ".workflow_artifacts" / "memory").mkdir(parents=True)
    result = subprocess.run(
        [real_python38, "-B", str(CORE_PATH), "stop", "--project-root", str(project)],
        input='{"session_id": "sid-1", "cwd": "%s"}' % str(project),
        capture_output=True, text=True, timeout=15, env=_SAFE_ENV,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_real_38_garbage_argv_exits_0(real_python38, tmp_path):
    """Fail-OPEN (I-01): even a malformed invocation never raises past main()."""
    result = subprocess.run(
        [real_python38, "-B", str(CORE_PATH), "not-a-real-subcommand"],
        capture_output=True, text=True, timeout=15, env=_SAFE_ENV,
    )
    assert result.returncode == 0, result.stderr


def test_real_38_progress_rule_runs(real_python38):
    program = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('quoin_auto_resume_py38rule', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(mod.next_streak(False, ('implement',), 1, 0, 2))\n"
    ) % (str(CORE_PATH),)
    result = subprocess.run(
        [real_python38, "-B", "-c", program],
        capture_output=True, text=True, timeout=15, env=_SAFE_ENV,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "(1, 1, 'repair')"
