"""T-03: install.sh deploys session_age_guard.py to ~/.claude/scripts/.

Verifies:
  1. install.sh copies session_age_guard.py to ~/.claude/scripts/session_age_guard.py.
  2. The deployed file is executable (chmod +x applied by install.sh).
  3. Running install.sh a second time produces no dirty state (deterministic output).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
SCRIPT_SRC = REPO_ROOT / "quoin" / "scripts" / "session_age_guard.py"


pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("npx") is None,
    reason=(
        "install.sh requires `claude` (hard) and `npx` (soft); test is dev-machine only. "
        "install.sh aborts on missing claude (lines 46-48), so test cannot run on CI."
    ),
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


def test_install_deploys_session_age_guard(tmp_path):
    """install.sh must deploy session_age_guard.py to ~/.claude/scripts/ and make it executable."""
    assert INSTALL_SH.exists(), f"quoin/install.sh not found at {INSTALL_SH}"
    assert SCRIPT_SRC.exists(), f"session_age_guard.py not found at {SCRIPT_SRC}"

    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )

    deployed = tmp_path / ".claude" / "scripts" / "session_age_guard.py"
    assert deployed.exists(), (
        f"install.sh did not deploy session_age_guard.py — expected at {deployed}"
    )

    assert deployed.read_bytes() == SCRIPT_SRC.read_bytes(), (
        "Deployed session_age_guard.py is not byte-identical to quoin/scripts/session_age_guard.py"
    )

    assert os.access(str(deployed), os.X_OK), (
        f"Deployed session_age_guard.py is not executable at {deployed}"
    )


def test_install_is_deterministic(tmp_path):
    """Running install.sh twice produces no new diff in ~/.claude/scripts/session_age_guard.py."""
    _run_install(tmp_path)
    deployed = tmp_path / ".claude" / "scripts" / "session_age_guard.py"
    assert deployed.exists(), "First install did not create session_age_guard.py"

    first_content = deployed.read_bytes()

    _run_install(tmp_path)
    second_content = deployed.read_bytes()

    assert first_content == second_content, (
        "install.sh is non-deterministic: second run produced a different session_age_guard.py"
    )
