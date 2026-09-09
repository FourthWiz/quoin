"""IVG-255 T-10: install.sh deploys comment_cleanup.py to both targets, plus
the criteria memory file.

Three-tier test (mirrors test_install_plan_path_lint_deployed.py, plus a
third manifest assertion for TIER1_MEMORY_FILES):

1. Non-skipped unit-level assertions (run on CI without `claude`):
   Import installer.py and assert "comment_cleanup.py" is in BOTH
   DEPLOYED_SCRIPTS and CORE_SCRIPTS (wrapped portable-core), and
   "comment-cleanup-criteria.md" is in TIER1_MEMORY_FILES.

2. Deployment test (dev-machine only — skipif claude/npx absent):
   Run install.sh and assert the wrapper, the core twin, and the deployed
   criteria file all land, and that the deployed wrapper imports its core
   (--help exit 0 via the parents[1] loader).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "comment_cleanup.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "comment_cleanup.py"
CRITERIA_SRC = REPO_ROOT / "quoin" / "memory" / "comment-cleanup-criteria.md"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"


def _load_installer():
    import importlib.util

    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_deployed_scripts_contains_comment_cleanup():
    mod = _load_installer()
    assert "comment_cleanup.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'comment_cleanup.py'."
    )


def test_installer_core_scripts_contains_comment_cleanup():
    mod = _load_installer()
    assert "comment_cleanup.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'comment_cleanup.py' — the wrapper's "
        "parents[1] loader fails at runtime otherwise."
    )


def test_installer_tier1_memory_files_contains_criteria_doc():
    mod = _load_installer()
    assert "comment-cleanup-criteria.md" in mod.TIER1_MEMORY_FILES, (
        "installer.py TIER1_MEMORY_FILES must contain 'comment-cleanup-criteria.md'."
    )


def test_source_files_exist():
    assert WRAPPER_SRC.is_file(), f"Wrapper source not found at {WRAPPER_SRC}."
    assert CORE_IMPL_SRC.is_file(), f"Core impl source not found at {CORE_IMPL_SRC}."
    assert CRITERIA_SRC.is_file(), f"Criteria doc not found at {CRITERIA_SRC}."


_SKIP_REASON = (
    "install.sh requires `claude` (hard) and `npx` (soft); dev-machine only."
)
_dev_machine_only = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("npx") is None,
    reason=_SKIP_REASON,
)


def _run_install(tmp_home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(tmp_home)}
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )


@_dev_machine_only
def test_install_deploys_wrapper_core_and_criteria(tmp_path):
    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )
    wrapper = tmp_path / ".claude" / "scripts" / "comment_cleanup.py"
    core = tmp_path / ".claude" / "core" / "scripts" / "comment_cleanup.py"
    criteria = tmp_path / ".claude" / "memory" / "comment-cleanup-criteria.md"
    assert wrapper.exists(), f"wrapper not deployed — expected at {wrapper}"
    assert core.exists(), f"core impl not deployed — expected at {core}"
    assert criteria.exists(), f"criteria doc not deployed — expected at {criteria}"


@_dev_machine_only
def test_deployed_wrapper_imports_core(tmp_path):
    result = _run_install(tmp_path)
    assert result.returncode == 0
    wrapper = tmp_path / ".claude" / "scripts" / "comment_cleanup.py"
    assert wrapper.exists(), "install.sh did not deploy wrapper"
    run = subprocess.run(
        ["python3", str(wrapper), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        f"Deployed wrapper --help failed (rc={run.returncode}): {run.stderr[:500]}"
    )
