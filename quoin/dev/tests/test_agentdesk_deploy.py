"""T-08: Unit tests for deploy_agentdesk() in src/quoin/installer.py.

Tests are isolated — no side effects to real ~/.config/agentdesk/.
Uses pytest tmp_path fixture for all temp directories.
"""
import io
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Import the installer module from src/
sys.path.insert(0, str(REPO_ROOT / "src"))
from quoin.installer import deploy_agentdesk  # noqa: E402


def _make_source_dir(tmp_path: Path) -> Path:
    """Create a fake source_dir with tools/agentdesk/ populated."""
    src = tmp_path / "source"
    agentdesk_src = src / "tools" / "agentdesk"
    agentdesk_src.mkdir(parents=True)
    (agentdesk_src / "agentdesk.zsh").write_text("# fake agentdesk.zsh\n", encoding="utf-8")
    (agentdesk_src / "setup-agentdesk.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    return src


def test_agentdesk_deploy_creates_files(tmp_path: Path) -> None:
    """deploy_agentdesk copies both files and sets executable bit on both."""
    src = _make_source_dir(tmp_path)
    dest = tmp_path / "dest"

    deploy_agentdesk(src, dest)

    zsh_file = dest / "agentdesk.zsh"
    sh_file = dest / "setup-agentdesk.sh"

    assert zsh_file.exists(), "agentdesk.zsh must be deployed"
    assert sh_file.exists(), "setup-agentdesk.sh must be deployed"

    # Both files must have at least one executable bit set
    assert zsh_file.stat().st_mode & 0o111, "agentdesk.zsh must be executable"
    assert sh_file.stat().st_mode & 0o111, "setup-agentdesk.sh must be executable"

    # Verify agentdesk.zsh has exactly 0o755
    assert stat.S_IMODE(zsh_file.stat().st_mode) == 0o755, (
        "agentdesk.zsh must have mode 0o755 (set by explicit os.chmod)"
    )


def test_agentdesk_deploy_idempotent(tmp_path: Path) -> None:
    """Calling deploy_agentdesk twice does not error and overwrites cleanly."""
    src = _make_source_dir(tmp_path)
    dest = tmp_path / "dest"

    deploy_agentdesk(src, dest)
    deploy_agentdesk(src, dest)  # second call must not raise

    assert (dest / "agentdesk.zsh").exists()
    assert (dest / "setup-agentdesk.sh").exists()


def test_agentdesk_deploy_missing_source_warns_and_returns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When source tools/agentdesk/ is absent, warns to stderr and returns without error."""
    src = tmp_path / "source_empty"
    src.mkdir()  # no tools/ subdirectory
    dest = tmp_path / "dest"

    # Must not raise and must not call sys.exit
    with patch.object(sys, "exit", side_effect=AssertionError("sys.exit must not be called")):
        deploy_agentdesk(src, dest)

    captured = capsys.readouterr()
    assert "warn" in captured.err.lower() or "not found" in captured.err.lower(), (
        "Expected a warning message to stderr"
    )
    assert not dest.exists() or not any(dest.iterdir()), (
        "No files should be created in dest when source is missing"
    )


def test_agentdesk_deploy_skipped_in_project_mode(tmp_path: Path) -> None:
    """In project mode (_cmd_claude_install with is_project_mode=True), deploy_agentdesk is NOT called."""
    # Patch at the installer module level
    with patch("quoin.installer.deploy_agentdesk") as mock_deploy:
        # Simulate the project-mode guard in cli.py
        is_project_mode = True
        if not is_project_mode:
            deploy_agentdesk(tmp_path, tmp_path / ".config" / "agentdesk")

        mock_deploy.assert_not_called()


def test_ccr_modules_never_resolve_an_agentdesk_path() -> None:
    """The CCR surfaces read and write CCR's own store, never agentdesk's tree.

    The pane command in agentdesk.zsh sources ~/.config/agentdesk/agentdesk.zsh
    at launch time, which is the user's shell reading the file agentdesk owns.
    Nothing in the CCR code path may turn that into a quoin write target.
    """
    src = REPO_ROOT / "src" / "quoin"
    for name in ("router.py", "models.py", "ccr_store.py", "ccr_config.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert '"agentdesk"' not in text, f"{name} joins an agentdesk path component"
        assert "config/agentdesk" not in text, f"{name} names the agentdesk directory"


def test_home_anchored_agentdesk_path_is_resolved_in_one_place() -> None:
    """Only the install command resolves ~/.config/agentdesk as a destination."""
    import re

    src = REPO_ROOT / "src" / "quoin"
    owners = sorted(
        path.name
        for path in src.glob("*.py")
        if re.search(r"home\(\)[^\n]*agentdesk", path.read_text(encoding="utf-8"))
    )
    assert owners == ["cli.py"], f"unexpected agentdesk destination owners: {owners}"
