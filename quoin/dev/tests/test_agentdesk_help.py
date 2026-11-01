"""T-09: Unit tests for -h|--help on the 14 agentdesk.zsh functions that gained it.

Uses a HERMETIC mock PATH (mock_bin:/usr/bin:/bin — never the full
os.environ['PATH']) so guard-order regressions are actually caught: a
non-hermetic PATH would let a real Homebrew-installed fzf/zellij stay
reachable even when "dropped" from the mock bin, silently passing tests
that should fail (round-1 critic MAJ-1 / plan D-07).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_ZSH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"

ZSH_AVAILABLE = shutil.which("zsh") is not None
pytestmark = pytest.mark.skipif(
    not ZSH_AVAILABLE,
    reason="zsh not available on this system",
)

SUBPROCESS_TIMEOUT = 30


def _mock_env(tmp_path: Path, drop_tools: tuple = ()) -> dict:
    """Hermetic PATH: mock_bin + bare-minimum system dirs only.

    Never includes the real os.environ['PATH'] — that would let an
    installed fzf/zellij/codex/claude stay reachable even when omitted
    from mock_bin, defeating the guard-order regression tests below.
    """
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir(exist_ok=True)
    for name in ("zellij", "claude", "codex", "fzf", "lazygit"):
        if name in drop_tools:
            continue
        stub = mock_bin / name
        stub.write_text("#!/bin/zsh\nexit 0\n")
        stub.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "PROJECT_ROOT": str(tmp_path),
    }
    return env


def _run_help(fn: str, tmp_path: Path, drop_tools: tuple = ()) -> subprocess.CompletedProcess:
    script = f'source "{AGENTDESK_ZSH}"\n{fn} --help'
    return subprocess.run(
        ["zsh", "-c", script],
        env=_mock_env(tmp_path, drop_tools),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def _assert_tool_absent(tool: str, tmp_path: Path, drop_tools: tuple) -> None:
    """Self-check: proves `tool` is genuinely unreachable in the hermetic PATH
    before trusting a --help-works-without-it assertion (round-1 critic D-07)."""
    result = subprocess.run(
        ["zsh", "-c", f"command -v {tool}"],
        env=_mock_env(tmp_path, drop_tools),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode != 0, (
        f"expected {tool!r} to be absent from hermetic PATH, but found: {result.stdout!r}"
    )


CASES = [
    ("repos", "repos"),
    ("crepo", "crepo"),
    ("grepos", "grepos"),
    ("gitreview", "gitreview"),
    ("gitrootreview", "gitrootreview"),
    ("croot", "croot"),
    ("agentdesk-sessions", "agentdesk-sessions"),
    ("agentdesk-attach", "agentdesk-attach"),
    ("codexpane", "codexpane"),
    ("codexright", "codexright"),
    ("codexcritic", "codexcritic"),
    ("claudepane", "claudepane"),
    ("agentprompt", "agentprompt"),
    ("codexcriticprompt", "codexcriticprompt"),
    ("agentdesk-kill", "agentdesk-delete"),  # alias — help text is agentdesk-delete's
]


@pytest.mark.parametrize("fn,token", CASES)
def test_help_flag(fn, token, tmp_path):
    result = _run_help(fn, tmp_path)
    assert result.returncode == 0, f"{fn} --help rc={result.returncode}\n{result.stderr}"
    assert "Usage:" in result.stdout, f"{fn} --help missing 'Usage:': {result.stdout!r}"
    assert token in result.stdout, f"{fn} --help missing {token!r}: {result.stdout!r}"


def test_crepo_help_without_fzf(tmp_path):
    """crepo --help works even when fzf is absent from PATH (guard-order proof)."""
    _assert_tool_absent("fzf", tmp_path, drop_tools=("fzf",))
    result = _run_help("crepo", tmp_path, drop_tools=("fzf",))
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_gitreview_help_without_fzf(tmp_path):
    """gitreview --help works even when fzf is absent from PATH (guard-order proof)."""
    _assert_tool_absent("fzf", tmp_path, drop_tools=("fzf",))
    result = _run_help("gitreview", tmp_path, drop_tools=("fzf",))
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_codexpane_help_without_zellij_or_codex(tmp_path):
    """codexpane --help works even when zellij AND codex are absent (guard-order proof)."""
    _assert_tool_absent("zellij", tmp_path, drop_tools=("zellij", "codex"))
    _assert_tool_absent("codex", tmp_path, drop_tools=("zellij", "codex"))
    result = _run_help("codexpane", tmp_path, drop_tools=("zellij", "codex"))
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_agentdesk_sessions_help_without_zellij(tmp_path):
    """agentdesk-sessions --help works even when zellij is absent (guard-order proof)."""
    _assert_tool_absent("zellij", tmp_path, drop_tools=("zellij",))
    result = _run_help("agentdesk-sessions", tmp_path, drop_tools=("zellij",))
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_agentprompt_help_does_not_leak_prompt_body(tmp_path):
    """agentprompt --help must NOT contain the prompt body (proves help intercepted it)."""
    result = _run_help("agentprompt", tmp_path)
    assert result.returncode == 0, result.stderr
    assert ".workflow_artifacts/repos.md" not in result.stdout, (
        f"--help leaked the prompt body: {result.stdout!r}"
    )


def test_agentprompt_bare_call_still_dumps_prompt(tmp_path):
    """Bare agentprompt (no --help) still prints the full prompt (regression check)."""
    script = f'source "{AGENTDESK_ZSH}"\nagentprompt'
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_mock_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert ".workflow_artifacts/repos.md" in result.stdout
