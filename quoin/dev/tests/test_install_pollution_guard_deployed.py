"""install.sh deploys pollution_guard.py to ~/.claude/scripts/.

Verifies the script is copied byte-identical, is executable, and that a second
install leaves it unchanged.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "quoin" / "install.sh"
SCRIPT_SRC = REPO_ROOT / "quoin" / "scripts" / "pollution_guard.py"


pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("npx") is None,
    reason=(
        "install.sh requires `claude` (hard) and `npx` (soft); test is dev-machine only. "
        "install.sh aborts on missing claude, so test cannot run on CI."
    ),
)


def _run_install(tmp_home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(tmp_home)}
    return subprocess.run(
        ["bash", str(INSTALL_SH)], env=env, capture_output=True, text=True,
        cwd=str(REPO_ROOT), timeout=180,
    )


def test_install_deploys_pollution_guard(tmp_path):
    result = _run_install(tmp_path)
    assert result.returncode == 0, (
        f"install.sh failed: rc={result.returncode}\n"
        f"stdout: {result.stdout[:1500]}\nstderr: {result.stderr[:1500]}"
    )
    deployed = tmp_path / ".claude" / "scripts" / "pollution_guard.py"
    assert deployed.exists(), f"install.sh did not deploy pollution_guard.py at {deployed}"
    assert deployed.read_bytes() == SCRIPT_SRC.read_bytes()
    assert os.access(str(deployed), os.X_OK)


def test_install_is_deterministic(tmp_path):
    _run_install(tmp_path)
    deployed = tmp_path / ".claude" / "scripts" / "pollution_guard.py"
    assert deployed.exists()
    first = deployed.read_bytes()
    _run_install(tmp_path)
    assert deployed.read_bytes() == first
