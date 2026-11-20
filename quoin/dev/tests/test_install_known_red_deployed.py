"""IVG-144 T-10: install.sh deploys known_red.py to both targets.

Two-tier test (modelled verbatim on test_install_affected_tests_deployed.py):

1. Non-skipped unit-level assertion (runs on CI without `claude`):
   Import installer.py and assert "known_red.py" is in BOTH
   DEPLOYED_SCRIPTS and CORE_SCRIPTS.  This is the real regression guard
   for the silent-skip failure mode documented in lessons-learned 2026-05-31
   and 2026-06-08 (a wrapped script missing from either list fails at runtime).

2. Deployment test (dev-machine only — skipif claude/npx absent):
   Run install.sh and assert BOTH deployment targets are present:
     - wrapper at ~/.claude/scripts/known_red.py
     - core impl at ~/.claude/core/scripts/known_red.py
   Also asserts that the deployed wrapper can be invoked (python3 --help
   exit 0, exercises the parents[1] loader).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "known_red.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "known_red.py"

INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"


# ---------------------------------------------------------------------------
# Tier 1: non-skipped unit-level assertion (runs on CI)
# ---------------------------------------------------------------------------


def test_installer_deployed_scripts_contains_known_red():
    """DEPLOYED_SCRIPTS in installer.py must contain 'known_red.py'.

    This is the regression guard for the silent-skip failure:
    if DEPLOYED_SCRIPTS lacks the entry, install.sh never deploys the wrapper
    to ~/.claude/scripts/known_red.py (IVG-144 R-04).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "known_red.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'known_red.py'. "
        "Missing entry means install.sh won't deploy the adapter wrapper to "
        "~/.claude/scripts/known_red.py."
    )


def test_installer_core_scripts_contains_known_red():
    """CORE_SCRIPTS in installer.py must contain 'known_red.py'.

    This is the regression guard for the silent-import-failure mode:
    if CORE_SCRIPTS lacks the entry, the wrapper's parents[1] loader
    fails at runtime with a FileNotFoundError (IVG-144 R-04).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "known_red.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'known_red.py'. "
        "Missing entry means install.sh won't deploy the core impl to "
        "~/.claude/core/scripts/known_red.py, causing the wrapper's "
        "parents[1] loader to fail at runtime."
    )


def test_source_files_exist():
    """Source files for both targets must exist in the repo."""
    assert WRAPPER_SRC.is_file(), (
        f"Wrapper source not found at {WRAPPER_SRC}. "
        "This file must exist for install.sh to deploy it."
    )
    assert CORE_IMPL_SRC.is_file(), (
        f"Core impl source not found at {CORE_IMPL_SRC}. "
        "This file must exist for install.sh to deploy it."
    )


# ---------------------------------------------------------------------------
# Tier 2: deployment test (dev-machine only)
# ---------------------------------------------------------------------------

_SKIP_REASON = (
    "install.sh requires `claude` (hard) and `npx` (soft); test is dev-machine only. "
    "install.sh aborts on missing claude (lines 46-48), so test cannot run on CI."
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
def test_install_deploys_known_red_wrapper(tmp_path):
    """install.sh must deploy known_red.py to ~/.claude/scripts/."""
    assert INSTALL_SH.is_file(), f"install.sh not found at {INSTALL_SH}"

    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )

    deployed_wrapper = tmp_path / ".claude" / "scripts" / "known_red.py"
    assert deployed_wrapper.exists(), (
        f"install.sh did not deploy wrapper — expected at {deployed_wrapper}"
    )


@_dev_machine_only
def test_install_deploys_known_red_core_impl(tmp_path):
    """install.sh must deploy known_red.py to ~/.claude/core/scripts/."""
    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )

    deployed_core = tmp_path / ".claude" / "core" / "scripts" / "known_red.py"
    assert deployed_core.exists(), (
        f"install.sh did not deploy core impl — expected at {deployed_core}"
    )


@_dev_machine_only
def test_deployed_wrapper_imports_core(tmp_path):
    """Deployed wrapper must import the core impl successfully (python3 --help exit 0)."""
    result = _run_install(tmp_path)
    assert result.returncode == 0

    deployed_wrapper = tmp_path / ".claude" / "scripts" / "known_red.py"
    assert deployed_wrapper.exists(), "install.sh did not deploy wrapper"

    # Run with --help (exits 0 after printing help, exercises the parents[1] loader)
    run = subprocess.run(
        ["python3", str(deployed_wrapper), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        f"Deployed wrapper --help failed (rc={run.returncode}): {run.stderr[:500]}"
    )
