"""install.sh deploys task_bookkeeping.py to both targets, plus
the cleanup-task-bookkeeping.md reference doc.

Two-tier test (mirrors test_install_nested_root_check_deployed.py):

1. Non-skipped unit-level assertion (runs on CI without `claude`):
   Import installer.py and assert "task_bookkeeping.py" is in BOTH
   DEPLOYED_SCRIPTS and CORE_SCRIPTS (wrapped portable-core), and that
   "cleanup-task-bookkeeping.md" is in TIER1_MEMORY_FILES.

2. Deployment test (dev-machine only — skipif claude/npx absent):
   Run install.sh and assert the wrapper, the core twin, and the memory
   doc all land, and that the deployed wrapper imports its core.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "task_bookkeeping.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "task_bookkeeping.py"
MEMORY_DOC_SRC = REPO_ROOT / "quoin" / "memory" / "cleanup-task-bookkeeping.md"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"


def _load_installer():
    import importlib.util

    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_deployed_scripts_contains_task_bookkeeping():
    mod = _load_installer()
    assert "task_bookkeeping.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'task_bookkeeping.py'."
    )


def test_installer_core_scripts_contains_task_bookkeeping():
    mod = _load_installer()
    assert "task_bookkeeping.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'task_bookkeeping.py' — the wrapper's "
        "parents[1] loader (and its sibling status_graph/path_resolve imports) fails "
        "at runtime otherwise."
    )


def test_installer_tier1_memory_files_contains_cleanup_task_bookkeeping_doc():
    mod = _load_installer()
    assert "cleanup-task-bookkeeping.md" in mod.TIER1_MEMORY_FILES, (
        "installer.py TIER1_MEMORY_FILES must contain 'cleanup-task-bookkeeping.md'."
    )


def test_source_files_exist():
    assert WRAPPER_SRC.is_file(), f"Wrapper source not found at {WRAPPER_SRC}."
    assert CORE_IMPL_SRC.is_file(), f"Core impl source not found at {CORE_IMPL_SRC}."
    assert MEMORY_DOC_SRC.is_file(), f"Memory doc not found at {MEMORY_DOC_SRC}."


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
def test_install_deploys_wrapper_core_and_doc(tmp_path):
    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )
    wrapper = tmp_path / ".claude" / "scripts" / "task_bookkeeping.py"
    core = tmp_path / ".claude" / "core" / "scripts" / "task_bookkeeping.py"
    doc = tmp_path / ".claude" / "memory" / "cleanup-task-bookkeeping.md"
    assert wrapper.exists(), f"wrapper not deployed — expected at {wrapper}"
    assert core.exists(), f"core impl not deployed — expected at {core}"
    assert doc.exists(), f"memory doc not deployed — expected at {doc}"


@_dev_machine_only
def test_deployed_wrapper_imports_core(tmp_path):
    result = _run_install(tmp_path)
    assert result.returncode == 0
    wrapper = tmp_path / ".claude" / "scripts" / "task_bookkeeping.py"
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
