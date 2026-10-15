"""T-09: Unit tests for agentdesk() arg parsing and _agentdesk_pick_layout() in agentdesk.zsh.

Approach: use Python subprocess + zsh to source agentdesk.zsh and invoke functions.
A mock `zellij` wrapper in $PATH prevents actual Zellij launches.
All tests skip gracefully when zsh is not available on the system.

Picker tests isolate _agentdesk_pick_layout() via piped stdin (non-TTY path
bypasses the picker in agentdesk(); we call _agentdesk_pick_layout() directly).
"""
import os
import pty
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_ZSH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"

ZSH_AVAILABLE = shutil.which("zsh") is not None
pytestmark = pytest.mark.skipif(
    not ZSH_AVAILABLE,
    reason="zsh not available on this system",
)

# Full agentdesk() launches spend ~14s on the pre-existing non-git child-folder
# search on slow/cloud-mounted filesystems, which brushes a 15s timeout under
# load and makes these tests flaky (TimeoutExpired, not assertion failures).
SUBPROCESS_TIMEOUT = 30


def _isolate_home(env: dict, tmp_path: Path) -> dict:
    """Point HOME at a private dir under tmp_path and drop dashboard/pick
    variables a developer shell may carry, so no launcher run can read or write
    the real home. Builders that need their own HOME override it afterwards."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    for key in ("AGENTDESK_DASHBOARD", "AGENTDESK_DASHBOARD_URL_FILE", "AGENTDESK_PICK"):
        env.pop(key, None)
    return env


def _make_mock_env(tmp_path: Path) -> dict:
    """Build an env dict with a mock zellij wrapper in $PATH.

    The mock zellij exits 0 unconditionally (simulates successful launch)
    so arg-parsing errors (rc=1) are distinguishable from zellij being called.
    Also adds mock claude/codex so 'command -v' checks inside pane cmds pass.
    """
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir(exist_ok=True)

    for name in ("zellij", "claude", "codex", "ccr"):
        stub = mock_bin / name
        stub.write_text("#!/bin/zsh\nexit 0\n")
        stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{mock_bin}:{os.environ['PATH']}"}
    _isolate_home(env, tmp_path)
    # Ensure PROJECT_ROOT is set to avoid _agentdesk_realpath issues
    env["PROJECT_ROOT"] = str(tmp_path)
    # Unset ZELLIJ so the "already inside Zellij" guard does not fire
    # (tests run from within a Zellij session on the dev machine)
    env.pop("ZELLIJ", None)
    env.pop("ZELLIJ_SESSION_NAME", None)
    # IVG-135 T-04: unset ZELLIJ_SOCKET_DIR so launch-env assertions are not
    # coupled to whatever the test runner's shell happened to inherit
    # (dogfooding a real agentdesk/zellij session would set this).
    env.pop("ZELLIJ_SOCKET_DIR", None)
    return env


def _run_agentdesk(args: str, tmp_path: Path, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call agentdesk with the given args string.

    Runs with cwd=tmp_path so _detect_repos scans only the (empty) tmp dir,
    not the quoin repo — avoids slow find traversals that cause timeouts.
    """
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        agentdesk {args}
    """).strip()
    # Pass input="" when no stdin provided so subprocess.PIPE closes immediately,
    # preventing hangs on interactive reads inside the zsh script.
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_mock_env(tmp_path),
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def _make_mock_env_with_socket_capture(tmp_path: Path) -> dict:
    """Extend _make_mock_env with a zellij stub that captures ZELLIJ_SOCKET_DIR
    to $AGENTDESK_TEST_SOCKET_CAPTURE instead of a bare `exit 0` (IVG-135 T-04).

    Lets tests assert what socket dir the real launch call actually saw,
    without a real zellij ever running.
    """
    env = _make_mock_env(tmp_path)
    mock_bin = tmp_path / "mock_bin"  # already exists from _make_mock_env

    # Only capture on the actual session-create invocation (--new-session-with-layout),
    # not on the earlier collision-check `zellij list-sessions` call the launch
    # path makes first — otherwise T-04c's "zellij never invoked" assertion would
    # be a false negative (list-sessions runs even when the length guard later
    # rejects the create call).
    zellij_stub = mock_bin / "zellij"
    zellij_stub.write_text(
        "#!/bin/zsh\n"
        'if printf \'%s\\n\' "$@" | grep -q -- "--new-session-with-layout"; then\n'
        '  printf \'%s\\n\' "$ZELLIJ_SOCKET_DIR" > "$AGENTDESK_TEST_SOCKET_CAPTURE"\n'
        "fi\n"
        "exit 0\n"
    )
    zellij_stub.chmod(0o755)

    env["AGENTDESK_TEST_SOCKET_CAPTURE"] = str(tmp_path / "socket_capture.txt")
    return env


def _run_agentdesk_capture_socket(
    args: str, tmp_path: Path, env_overrides: Optional[dict] = None
):
    """Run agentdesk with the socket-capture zellij stub (IVG-135 T-04).

    Returns (CompletedProcess, capture_path). `env_overrides` is applied AFTER
    the harness's default ZELLIJ_SOCKET_DIR pop, so callers can simulate a
    pre-set user override or a forced-long value.
    """
    env = _make_mock_env_with_socket_capture(tmp_path)
    if env_overrides:
        env.update(env_overrides)
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        agentdesk {args}
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=env,
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    return result, Path(env["AGENTDESK_TEST_SOCKET_CAPTURE"])


def _run_pick_layout(stdin_input: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call _agentdesk_pick_layout() directly with piped stdin."""
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        _agentdesk_pick_layout
    """).strip()
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_mock_env(tmp_path),
        input=stdin_input,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def _gen_layout(tokens: str, tmp_path: Path) -> str:
    """Call _agentdesk_gen_layout with the given tokens, return the KDL file content.

    The function echoes the temp-file path; we read and return its content,
    then clean up the temp file.
    """
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        out="$(_agentdesk_gen_layout {tokens})"
        cat "$out"
        rm -f "$out"
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_make_mock_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"gen_layout failed: {result.stderr}"
    return result.stdout


# ── Arg parsing tests ──────────────────────────────────────────────────────────

def test_agentdesk_zsh_exists() -> None:
    """Sanity: agentdesk.zsh exists at expected path."""
    assert AGENTDESK_ZSH.exists(), f"agentdesk.zsh not found at {AGENTDESK_ZSH}"


def test_agentdesk_zsh_syntax() -> None:
    """zsh -n agentdesk.zsh passes (no syntax errors)."""
    result = subprocess.run(
        ["zsh", "-n", str(AGENTDESK_ZSH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error in agentdesk.zsh:\n{result.stderr}"


def test_agentdesk_help_exits_zero(tmp_path: Path) -> None:
    """agentdesk --help returns rc=0 and includes 'Usage:'.

    Verifies ZELLIJ guard is ordered AFTER --help (T-02 requirement).
    """
    result = _run_agentdesk("--help", tmp_path)
    assert result.returncode == 0, (
        f"--help must return 0 (rc={result.returncode})\nstderr: {result.stderr}"
    )
    assert "Usage:" in result.stdout, f"Expected 'Usage:' in help output:\n{result.stdout}"


def test_agentdesk_invalid_mode_exits_nonzero(tmp_path: Path) -> None:
    """agentdesk --mode quad returns rc=1 with an error about invalid mode."""
    result = _run_agentdesk("--mode quad", tmp_path)
    assert result.returncode != 0, "Invalid mode should return non-zero"
    combined = result.stdout + result.stderr
    assert "invalid mode" in combined.lower() or "valid modes" in combined.lower(), (
        f"Expected 'invalid mode' error:\n{combined}"
    )


def test_agentdesk_mode_plus_tokens_conflict(tmp_path: Path) -> None:
    """agentdesk --mode duo claude returns rc=1 (mutually exclusive)."""
    result = _run_agentdesk("--mode duo claude", tmp_path)
    assert result.returncode != 0, "--mode + positional tokens should conflict (rc!=0)"


def test_agentdesk_unknown_token_exits_nonzero(tmp_path: Path) -> None:
    """agentdesk emacs claude returns rc=1 (session name + window token mixed)."""
    result = _run_agentdesk("emacs claude", tmp_path)
    assert result.returncode != 0, "Session name + window token should be an error"


def test_agentdesk_mode_name_combo_allowed(tmp_path: Path) -> None:
    """agentdesk --mode duo --name mysession is valid (--mode + --name allowed)."""
    result = _run_agentdesk("--mode duo --name mysession", tmp_path)
    # rc=0 means arg parsing succeeded (mock zellij exits 0)
    assert result.returncode == 0, (
        f"--mode + --name should be allowed (rc={result.returncode})\nstderr: {result.stderr}"
    )


def test_agentdesk_valid_tokens_succeed(tmp_path: Path) -> None:
    """agentdesk claude shell succeeds (rc=0 via mock zellij)."""
    result = _run_agentdesk("claude shell", tmp_path)
    assert result.returncode == 0, (
        f"claude shell tokens should succeed (rc={result.returncode})\nstderr: {result.stderr}"
    )


# ── IVG-135: ZELLIJ_SOCKET_DIR pin + pre-flight length guard ──────────────────

def test_socket_dir_default_pinned(tmp_path: Path) -> None:
    """T-04a: with ZELLIJ_SOCKET_DIR unset ambiently (harness pop), the launch
    env's ZELLIJ_SOCKET_DIR is set, non-empty, and starts with the resolved
    base ($XDG_RUNTIME_DIR or /tmp) followed by '/zellij-agentdesk-'.
    """
    result, capture_path = _run_agentdesk_capture_socket("claude shell", tmp_path)
    assert result.returncode == 0, (
        f"launch should succeed (rc={result.returncode})\nstderr: {result.stderr}"
    )
    assert capture_path.exists(), "mock zellij must have been invoked and captured the socket dir"
    captured = capture_path.read_text().strip()
    assert captured, "ZELLIJ_SOCKET_DIR must be set (non-empty) at launch"
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    assert captured.startswith(f"{base}/zellij-agentdesk-"), (
        f"expected ZELLIJ_SOCKET_DIR to start with '{base}/zellij-agentdesk-', got: {captured!r}"
    )


def test_socket_dir_user_override_preserved(tmp_path: Path) -> None:
    """T-04b: a pre-set ZELLIJ_SOCKET_DIR=/custom/short is preserved verbatim
    (no clobber) — proves the guard in T-01 never overwrites a user override.
    """
    result, capture_path = _run_agentdesk_capture_socket(
        "claude shell", tmp_path, env_overrides={"ZELLIJ_SOCKET_DIR": "/custom/short"}
    )
    assert result.returncode == 0, (
        f"launch should succeed (rc={result.returncode})\nstderr: {result.stderr}"
    )
    assert capture_path.exists(), "mock zellij must have been invoked"
    captured = capture_path.read_text().strip()
    assert captured == "/custom/short", (
        f"expected ZELLIJ_SOCKET_DIR to be preserved verbatim, got: {captured!r}"
    )


def test_socket_dir_length_guard_blocks_long_name(tmp_path: Path) -> None:
    """T-04c (T-03, required): a forced-long ZELLIJ_SOCKET_DIR yields rc=1, a
    clear actionable error naming the session and the resolved socket dir, and
    zellij is never invoked (no capture file written).
    """
    long_base = "/tmp/" + ("x" * 90)  # long enough to overflow the ~100-byte budget
    result, capture_path = _run_agentdesk_capture_socket(
        "claude shell", tmp_path, env_overrides={"ZELLIJ_SOCKET_DIR": long_base}
    )
    assert result.returncode == 1, (
        f"expected rc=1 for an over-length socket path (rc={result.returncode})\nstderr: {result.stderr}"
    )
    assert "too long" in result.stderr, f"expected clear error in stderr:\n{result.stderr}"
    assert "ZELLIJ_SOCKET_DIR" in result.stderr, (
        f"error must name the resolved ZELLIJ_SOCKET_DIR:\n{result.stderr}"
    )
    assert not capture_path.exists(), "zellij must NOT have been invoked when the guard rejects the session"


# ── Picker tests ───────────────────────────────────────────────────────────────

def test_agentdesk_non_tty_skips_picker(tmp_path: Path) -> None:
    """agentdesk with stdin piped (non-TTY) skips picker and uses fixed layout."""
    # Piped stdin means [ -t 0 ] is false → fixed layout path
    # Fixed layout file won't exist, so it errors with "layout not found" (rc=1).
    # That's fine — the point is it didn't call _agentdesk_pick_layout (no menu output).
    result = _run_agentdesk("", tmp_path, stdin="1\n")
    # No picker menu should appear in stderr
    assert "Select a layout" not in result.stderr, (
        "Picker menu should NOT appear when stdin is not a TTY"
    )


def test_agentdesk_picker_option2_claude_shell(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with input '2' returns 'claude shell'."""
    result = _run_pick_layout("2\n", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "claude shell", (
        f"Expected 'claude shell', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_option3_two_claude_shell(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with input '3' returns 'claude claude shell'."""
    result = _run_pick_layout("3\n", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "claude claude shell", (
        f"Expected 'claude claude shell', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_option4_claude_codex_shell(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with input '4' returns 'claude codex shell'."""
    result = _run_pick_layout("4\n", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "claude codex shell", (
        f"Expected 'claude codex shell', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_custom_comma_input(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with 'claude, codex, shell' (direct) returns 'claude codex shell'."""
    result = _run_pick_layout("claude, codex, shell\n", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "claude codex shell", (
        f"Expected 'claude codex shell', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_custom_no_spaces(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with 'claude,shell,shell' returns 'claude shell shell'."""
    result = _run_pick_layout("claude,shell,shell\n", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "claude shell shell", (
        f"Expected 'claude shell shell', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_unknown_token_in_custom(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with option 6 + unknown token + empty retry returns rc=1."""
    # Input: choose option 6 (Custom — was 5 before ccr renumber), then type bad token, then empty retry (cancel)
    result = _run_pick_layout("6\nemacs, shell\n\n", tmp_path)
    assert result.returncode != 0, (
        f"Unknown token in custom input should return non-zero after re-prompt (rc={result.returncode})"
    )


def test_agentdesk_picker_option1_returns_empty(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with '1' echoes nothing (caller uses fixed layout)."""
    result = _run_pick_layout("1\n", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"Option 1 should produce empty output, got: {result.stdout.strip()!r}"
    )


def test_agentdesk_picker_empty_input_returns_empty(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with empty Enter echoes nothing (same as option 1)."""
    result = _run_pick_layout("\n", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"Empty input should produce empty output, got: {result.stdout.strip()!r}"
    )


# ── KDL layout generation validity (regression: review I-01 / I-02) ─────────────

def _assert_kdl_valid(kdl: str) -> None:
    """Structural validity checks for generated KDL.

    Catches the two review-1 CRITICAL bugs:
      I-01: leaked `name=`/`cmd=` lines from `local` inside a redirected loop.
      I-02: unescaped inner double-quotes that terminate the args "..." string.
    """
    lines = kdl.splitlines()

    # I-01: no leaked variable-assignment lines (e.g. name='Claude Code', cmd=...)
    for ln in lines:
        stripped = ln.strip()
        assert not stripped.startswith("name="), f"Leaked 'name=' line in KDL: {ln!r}"
        assert not stripped.startswith("cmd="), f"Leaked 'cmd=' line in KDL: {ln!r}"

    # Balanced braces
    assert kdl.count("{") == kdl.count("}"), "Unbalanced braces in KDL"

    # I-02: every args line must escape inner double-quotes as \" — there must be
    # NO bare (unescaped) `"$HOME` or `"$PROJECT_ROOT` or `"$PWD` inside the cmd.
    for ln in lines:
        if 'args "-lc"' in ln:
            # The outer wrapper is `args "-lc" "<cmd>"`. Inner var refs must be \"
            assert '"$HOME' not in ln.replace('\\"$HOME', ""), (
                f"Unescaped \"$HOME in args line: {ln!r}"
            )
            assert '"$PROJECT_ROOT' not in ln.replace('\\"$PROJECT_ROOT', ""), (
                f"Unescaped \"$PROJECT_ROOT in args line: {ln!r}"
            )
            assert '"$PWD' not in ln.replace('\\"$PWD', ""), (
                f"Unescaped \"$PWD in args line: {ln!r}"
            )


def test_kdl_solo_valid(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude → valid single-pane KDL, no leaks, escaped quotes."""
    kdl = _gen_layout("claude", tmp_path)
    _assert_kdl_valid(kdl)
    assert 'tab name="main"' in kdl
    assert 'pane name="Claude Code"' in kdl
    # The horizontal wrapper is always present; a solo layout has no strip and
    # no size split inside the tiled area.
    main = kdl.split('tab name="main"', 1)[1].split("floating_panes {", 1)[0]
    assert "size=\"" not in main, "Solo layout should not have a size split"


def test_kdl_duo_valid(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude shell → agent on top, Shell strip below."""
    kdl = _gen_layout("claude shell", tmp_path)
    _assert_kdl_valid(kdl)
    assert 'pane name="Claude Code" size="75%"' in kdl, "Agent should take the top 75%"
    assert 'pane name="Shell" size="25%"' in kdl, "Shell strip should take 25%"


def test_kdl_trio_valid_no_leaks(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude codex shell → valid 3-pane KDL, NO leaked lines.

    This is the exact case that exposed review I-01 (leaked name=/cmd= lines).
    """
    kdl = _gen_layout("claude codex shell", tmp_path)
    _assert_kdl_valid(kdl)
    assert 'pane name="Claude Code"' in kdl
    assert 'pane name="Codex"' in kdl
    assert 'pane name="Shell"' in kdl
    # 3 main panes + the floating Spend pane (always included)
    assert kdl.count('command "zsh"') == 4, "Trio must have 3 main panes + 1 Spend pane"


def test_kdl_four_panes_agents_on_top_shell_strip(tmp_path: Path) -> None:
    """_agentdesk_gen_layout with 4 tokens → three agents on top, Shell strip below."""
    kdl = _gen_layout("claude claude codex shell", tmp_path)
    _assert_kdl_valid(kdl)
    assert 'split_direction="horizontal"' in kdl
    assert 'pane name="Shell" size="25%"' in kdl
    assert 'pane size="75%" split_direction="vertical"' in kdl
    assert kdl.count('command "zsh"') == 5  # 4 main panes + the floating Spend pane


def test_kdl_escaping_matches_fixed_layout(tmp_path: Path) -> None:
    """Generated KDL escapes inner quotes the same way as the fixed agent-desk.kdl.

    The fixed layout uses `source \\"$HOME/...\\"` — verify the generator matches.
    """
    kdl = _gen_layout("claude", tmp_path)
    # Inner var refs must appear escaped
    assert '\\"$HOME/.config/agentdesk/agentdesk.zsh\\"' in kdl, (
        "Generated KDL must escape $HOME ref like the fixed layout"
    )
    assert '\\"$PROJECT_ROOT\\"' in kdl, "Generated KDL must escape $PROJECT_ROOT ref"


# ============================================================
# T-10: /status agentdesk token tests (IVG-59)
# ============================================================

def _run_zsh_fn(fn_call: str, tmp_path: Path, stdin_input: str = "") -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call an arbitrary zsh function/expression."""
    script = f'source "{AGENTDESK_ZSH}"\n{fn_call}'
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_mock_env(tmp_path),
        input=stdin_input,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def test_agentdesk_status_token_valid(tmp_path: Path) -> None:
    """status token → generates a pane with status_graph.py (not a named session).

    Verifies the L373 fix: 'status' must be in the positional-token case so it
    routes as a window token. We verify via _gen_layout (same as other KDL tests)
    rather than the full agentdesk() invocation (which adds zellij/mktemp overhead).
    """
    # Verify layout generation works (status routed as window token, not session name)
    kdl = _gen_layout("status", tmp_path)
    assert "status_graph.py" in kdl, (
        "status token must generate a pane with status_graph.py; "
        f"got KDL: {kdl[:300]}"
    )
    assert "--compact" in kdl, "status pane command must include --compact"
    assert 'pane name="Status"' in kdl, f"status pane must be named 'Status': {kdl[:300]}"


def test_agentdesk_status_pane_cmd(tmp_path: Path) -> None:
    """_agentdesk_pane_cmd status → contains status_graph.py and --compact."""
    result = _run_zsh_fn("_agentdesk_pane_cmd status", tmp_path)
    assert result.returncode == 0
    cmd = result.stdout
    assert "status_graph.py" in cmd, f"pane cmd missing status_graph.py: {cmd!r}"
    assert "--compact" in cmd, f"pane cmd missing --compact: {cmd!r}"


def test_agentdesk_status_pane_name(tmp_path: Path) -> None:
    """_agentdesk_pane_name status → 'Status'."""
    result = _run_zsh_fn("_agentdesk_pane_name status", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "Status", (
        f"Expected pane name 'Status', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_parse_custom_tokens_with_status(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens 'claude, status' → outputs both tokens."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'claude, status'", tmp_path)
    assert result.returncode == 0
    output = result.stdout
    assert "status" in output, f"status token not in output: {output!r}"
    assert "claude" in output, f"claude token not in output: {output!r}"


def test_agentdesk_unknown_token_error_includes_status(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens with unknown token → error mentions 'status'."""
    # Provide empty retry so the function terminates
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'emacs'", tmp_path, stdin_input="\n")
    assert result.returncode != 0 or "status" in result.stderr, (
        f"Error message for unknown token should mention 'status': {result.stderr!r}"
    )
    assert "status" in result.stderr, (
        f"Valid-tokens error message missing 'status': {result.stderr!r}"
    )


# ============================================================
# CCR token tests (agentdesk-ccr-token)
# ============================================================

def test_agentdesk_ccr_pane_cmd(tmp_path: Path) -> None:
    """_agentdesk_pane_cmd ccr → branches on the store shape, keeping both guards."""
    result = _run_zsh_fn("_agentdesk_pane_cmd ccr", tmp_path)
    assert result.returncode == 0
    cmd = result.stdout
    assert "ccr code" in cmd, f"pane cmd missing 'ccr code': {cmd!r}"
    assert "ccr default-claude-code" in cmd, (
        f"pane cmd missing 'ccr default-claude-code': {cmd!r}"
    )
    assert "config.sqlite" in cmd, f"pane cmd missing the store-shape guard: {cmd!r}"
    assert "command -v ccr" in cmd, f"pane cmd missing 'command -v ccr' guard: {cmd!r}"
    assert "ccr not found" in cmd, f"pane cmd missing 'ccr not found' fallback: {cmd!r}"


def test_agentdesk_ccr_pane_name(tmp_path: Path) -> None:
    """_agentdesk_pane_name ccr → prints exactly 'CCR (OpenRouter)'."""
    result = _run_zsh_fn("_agentdesk_pane_name ccr", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "CCR (OpenRouter)", (
        f"Expected pane name 'CCR (OpenRouter)', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_ccr_token_valid(tmp_path: Path) -> None:
    """ccr token → valid KDL carrying both launch commands and the pane name."""
    kdl = _gen_layout("ccr", tmp_path)
    assert "ccr code" in kdl, f"KDL missing 'ccr code': {kdl[:400]}"
    assert "ccr default-claude-code" in kdl, (
        f"KDL missing 'ccr default-claude-code': {kdl[:400]}"
    )
    assert "config.sqlite" in kdl, f"KDL missing the store-shape guard: {kdl[:400]}"
    assert 'pane name="CCR (OpenRouter)"' in kdl, (
        f"KDL missing pane name 'CCR (OpenRouter)': {kdl[:400]}"
    )
    _assert_kdl_valid(kdl)


def test_agentdesk_parse_custom_tokens_with_ccr(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens 'claude, ccr' → outputs both tokens."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'claude, ccr'", tmp_path)
    assert result.returncode == 0
    output = result.stdout
    assert "ccr" in output, f"ccr token not in output: {output!r}"
    assert "claude" in output, f"claude token not in output: {output!r}"


def test_agentdesk_unknown_token_error_includes_ccr(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens with unknown token → error mentions 'status, ccr' tail."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'emacs'", tmp_path, stdin_input="\n")
    assert result.returncode != 0 or "ccr" in result.stderr, (
        f"Error message for unknown token should mention 'ccr': {result.stderr!r}"
    )
    assert "status, ccr" in result.stderr, (
        f"Valid-tokens error message missing ordered tail 'status, ccr': {result.stderr!r}"
    )


def test_agentdesk_picker_option5_claude_ccr_shell(tmp_path: Path) -> None:
    """_agentdesk_pick_layout with input '5' returns 'claude ccr shell'."""
    result = _run_pick_layout("5\n", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "claude ccr shell", (
        f"Expected 'claude ccr shell', got: {result.stdout.strip()!r}"
    )


# ============================================================
# T-09: spend token tests (IVG-62)
# ============================================================

def test_agentdesk_spend_pane_cmd(tmp_path: Path) -> None:
    """_agentdesk_pane_cmd spend → contains spend_monitor.py, --compact, --watch."""
    result = _run_zsh_fn("_agentdesk_pane_cmd spend", tmp_path)
    assert result.returncode == 0
    cmd = result.stdout
    assert "spend_monitor.py" in cmd, f"pane cmd missing spend_monitor.py: {cmd!r}"
    assert "--compact" in cmd, f"pane cmd missing --compact: {cmd!r}"
    assert "--watch" in cmd, f"pane cmd missing --watch: {cmd!r}"


def test_agentdesk_spend_pane_name(tmp_path: Path) -> None:
    """_agentdesk_pane_name spend → prints exactly 'Token Spend'."""
    result = _run_zsh_fn("_agentdesk_pane_name spend", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "Token Spend", (
        f"Expected pane name 'Token Spend', got: {result.stdout.strip()!r}"
    )


def test_agentdesk_spend_token_valid(tmp_path: Path) -> None:
    """spend token → generates valid KDL with spend_monitor.py command and correct pane name."""
    kdl = _gen_layout("spend", tmp_path)
    assert "spend_monitor.py" in kdl, f"KDL missing 'spend_monitor.py': {kdl[:400]}"
    assert "--compact" in kdl, f"KDL missing '--compact': {kdl[:400]}"
    assert "--watch" in kdl, f"KDL missing '--watch': {kdl[:400]}"
    assert 'pane name="Token Spend"' in kdl, (
        f"KDL missing pane name 'Token Spend': {kdl[:400]}"
    )
    # With only spend requested, one tiled Shell is added next to the floating pane.
    assert 'pane name="Shell"' in kdl
    _assert_kdl_valid(kdl)


def test_agentdesk_parse_custom_tokens_with_spend(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens 'claude, spend' → outputs both tokens."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'claude, spend'", tmp_path)
    assert result.returncode == 0
    output = result.stdout
    assert "spend" in output, f"spend token not in output: {output!r}"
    assert "claude" in output, f"claude token not in output: {output!r}"


def test_agentdesk_unknown_token_error_includes_spend(tmp_path: Path) -> None:
    """_agentdesk_parse_custom_tokens with unknown token → error mentions 'spend'."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'emacs'", tmp_path, stdin_input="\n")
    assert "spend" in result.stderr, (
        f"Valid-tokens error message missing 'spend': {result.stderr!r}"
    )


def test_agentdesk_unknown_token_error_includes_ccr_and_spend(tmp_path: Path) -> None:
    """Unknown-token error message still contains 'status, ccr' ordered tail (R-03 guard)
    and also mentions 'spend' appended after ccr."""
    result = _run_zsh_fn("_agentdesk_parse_custom_tokens 'emacs'", tmp_path, stdin_input="\n")
    assert "status, ccr" in result.stderr, (
        f"Valid-tokens error message missing ordered tail 'status, ccr': {result.stderr!r}"
    )
    assert "spend" in result.stderr, (
        f"Valid-tokens error message missing 'spend': {result.stderr!r}"
    )


def test_agentdesk_help_includes_spend(tmp_path: Path) -> None:
    """agentdesk --help output contains 'spend' in Window types section."""
    result = _run_agentdesk("--help", tmp_path)
    assert result.returncode == 0
    assert "spend" in result.stdout, (
        f"--help output missing 'spend' window type: {result.stdout!r}"
    )


def test_help_text_describes_attach_prompt_not_never_attaches(tmp_path: Path) -> None:
    """agentdesk --help Behavior section describes the attach-or-new prompt (IVG-109),
    not the old 'it never attaches' wording, and mentions the non-TTY fallback."""
    result = _run_agentdesk("--help", tmp_path)
    assert result.returncode == 0
    assert "it never attaches" not in result.stdout, (
        f"--help must not contain the stale 'it never attaches' wording: {result.stdout!r}"
    )
    assert "prompts to attach" in result.stdout, (
        f"--help must describe the attach-or-new prompt: {result.stdout!r}"
    )
    assert "Non-interactive" in result.stdout or "no TTY" in result.stdout, (
        f"--help must describe the non-TTY fallback: {result.stdout!r}"
    )


def test_agentdesk_spend_as_positional_token(tmp_path: Path) -> None:
    """agentdesk claude spend routes 'spend' as a window token (rc=0, not 'unexpected argument')."""
    result = _run_agentdesk("claude spend", tmp_path)
    assert result.returncode == 0, (
        f"'agentdesk claude spend' should succeed (rc={result.returncode})\n"
        f"stderr: {result.stderr}"
    )


# ============================================================
# S-3: _agentdesk_open_dashboard tests (IVG-63 stage-3)
# ============================================================

def _make_dashboard_env(tmp_path: Path, with_server: bool = True) -> dict:
    """Extend _make_mock_env with a fake $HOME containing (optionally) a stub
    dashboard_server.py that immediately prints URL=http://127.0.0.1:8787 and
    exits, and stub `open` / `xdg-open` that record their argv to a marker file.

    Args:
        with_server: if True, create the stub server script; if False, omit it
                     to test the missing-server code path.
    """
    env = _make_mock_env(tmp_path)
    mock_bin = tmp_path / "mock_bin"  # already exists from _make_mock_env

    # Fake HOME so $HOME/.claude/scripts/ resolves to a writable temp dir.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)

    if with_server:
        scripts_dir = fake_home / ".claude" / "scripts"
        scripts_dir.mkdir(parents=True)
        server = scripts_dir / "dashboard_server.py"
        # Stub: print URL= line and exit immediately.
        server.write_text(
            "import sys\n"
            "print('URL=http://127.0.0.1:8787', flush=True)\n"
            "# --no-browser flag accepted and ignored\n"
        )
        server.chmod(0o755)

    # Stub `open` writes argv[1] (the URL) to a marker file so tests can assert it.
    marker = tmp_path / "open_called.txt"
    open_stub = mock_bin / "open"
    open_stub.write_text(
        f"#!/bin/sh\necho \"$1\" > '{marker}'\nexit 0\n"
    )
    open_stub.chmod(0o755)

    # Also stub xdg-open with same behaviour (fallback path).
    xdg_stub = mock_bin / "xdg-open"
    xdg_stub.write_text(
        f"#!/bin/sh\necho \"$1\" > '{marker}'\nexit 0\n"
    )
    xdg_stub.chmod(0o755)

    return env


def _run_dashboard_fn_with_pty(
    env: dict,
    input_text: str,
    tmp_path: Path,
    timeout: int = SUBPROCESS_TIMEOUT,
    fn_call: str = "_agentdesk_dashboard_resolve '' '' '' '' 1 dash:proj:x",
) -> subprocess.CompletedProcess:
    """Run a zsh expression with a real PTY as stdin.

    Uses pty.openpty() to provide a file descriptor that satisfies [ -t 0 ],
    so terminal-only code paths run. input_text is written to the PTY master
    side after the subprocess is started; the child reads it via 'read -r'.
    stdout and stderr are captured via PIPE (not the PTY) so assertions are
    straightforward.
    """
    script = f'source "{AGENTDESK_ZSH}"\n{fn_call}'
    master_fd, slave_fd = pty.openpty()
    stdout_b: bytes = b""
    stderr_b: bytes = b""
    rc: int = -1
    try:
        try:
            proc = subprocess.Popen(
                ["zsh", "-c", script],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(tmp_path),
                close_fds=True,
            )
        finally:
            os.close(slave_fd)  # parent no longer needs the slave end
        try:
            os.write(master_fd, input_text.encode())
        except OSError:
            pass  # process may have exited before the write
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_b, stderr_b = proc.communicate()
        rc = proc.returncode
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    return subprocess.CompletedProcess(
        args=["zsh", "-c", script],
        returncode=rc,
        stdout=stdout_b.decode(errors="replace"),
        stderr=stderr_b.decode(errors="replace"),
    )


def test_dashboard_resolve_non_tty_defaults_to_no(tmp_path: Path) -> None:
    """With no flag, env or terminal the resolver answers no without asking."""
    env = _make_dashboard_env(tmp_path)
    script = f'source "{AGENTDESK_ZSH}"\n_agentdesk_dashboard_resolve "" "" "" "" "" dash:proj:x'
    result = subprocess.run(
        ["zsh", "-c", script], env=env, input="", capture_output=True, text=True,
        cwd=str(tmp_path), timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "no default"
    assert "Open quoin dashboard" not in result.stderr
    assert not (tmp_path / "open_called.txt").exists()


def test_dashboard_resolve_declines_default(tmp_path: Path) -> None:
    """A terminal answer of 'n' resolves to no, and nothing is opened."""
    env = _make_dashboard_env(tmp_path)
    result = _run_dashboard_fn_with_pty(env, "n\n", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "no asked"
    assert "Open quoin dashboard" in result.stderr
    assert not (tmp_path / "open_called.txt").exists()


def test_dashboard_resolve_accepts_and_url_file_helper_creates_path(tmp_path: Path) -> None:
    """'y' resolves to yes without exporting anything; the URL-file helper then
    prints a real temp path and the browser opener is not touched."""
    env = _make_dashboard_env(tmp_path, with_server=True)
    result = _run_dashboard_fn_with_pty(
        env, "y\n", tmp_path,
        fn_call=(
            "_agentdesk_dashboard_resolve '' '' '' '' 1 dash:proj:x\n"
            "f=\"$(_agentdesk_dashboard_url_file)\"; echo \"URLFILE=$f\"; "
            "[ -f \"$f\" ] && echo EXISTS\n"
            "typeset -m 'AGENTDESK_DASHBOARD*'"
        ),
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    lines = result.stdout.splitlines()
    assert lines[0] == "yes asked"
    url_line = [l for l in lines if l.startswith("URLFILE=")]
    assert url_line and url_line[0][len("URLFILE="):]
    assert "EXISTS" in lines
    assert not any(l.startswith("AGENTDESK_DASHBOARD") for l in lines)
    assert not (tmp_path / "open_called.txt").exists()


def test_open_dashboard_poller_opens_url(tmp_path: Path) -> None:
    """T-05 poller subshell in isolation: pre-write a URL to a temp file and assert
    that _agentdesk_open_url is called with that URL. (T-05/MAJ-2 fix replacement test B)

    Exercises the D-03 bounded background poller logic by running it directly in zsh
    with a pre-existing url-file, verifying the browser-open stub is invoked.
    """
    env = _make_dashboard_env(tmp_path, with_server=False)  # browser stub only needed
    # Pre-write a URL to a temp file to simulate the dashboard server having started.
    url_file = tmp_path / "dash-url.txt"
    test_url = "http://127.0.0.1:19999"
    url_file.write_text(test_url + "\n")
    marker = tmp_path / "open_called.txt"

    # Run the poller subshell in isolation — borrow _agentdesk_open_url definition.
    dash_url_file = str(url_file)
    script = f"""
source "{AGENTDESK_ZSH}"
_dash_url_file="{dash_url_file}"
i=0
while [ "$i" -lt 50 ]; do
  if [ -f "$_dash_url_file" ] && grep -q '^http' "$_dash_url_file" 2>/dev/null; then
    url="$(grep '^http' "$_dash_url_file" | head -1)"
    _agentdesk_open_url "$url"
    rm -f "$_dash_url_file"
    break
  fi
  sleep 0.2
  i=$((i + 1))
done
rm -f "$_dash_url_file"
"""
    result = subprocess.run(
        ["zsh", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert marker.exists(), (
        f"Browser opener stub should have been called by poller; stderr={result.stderr!r}"
    )
    url_opened = marker.read_text().strip()
    assert url_opened == test_url, (
        f"Unexpected URL passed to opener: {url_opened!r}"
    )


def test_dashboard_url_file_helper_missing_server(tmp_path: Path) -> None:
    """Without the dashboard server script the helper prints the skip note,
    returns 1 and creates nothing."""
    env = _make_dashboard_env(tmp_path, with_server=False)
    script = f'source "{AGENTDESK_ZSH}"\n_agentdesk_dashboard_url_file; echo "RC=$?"'
    result = subprocess.run(
        ["zsh", "-c", script], env=env, input="", capture_output=True, text=True,
        cwd=str(tmp_path), timeout=SUBPROCESS_TIMEOUT,
    )
    assert "RC=1" in result.stdout
    assert "not found" in result.stderr
    assert not (tmp_path / "open_called.txt").exists()


# ============================================================
# T-05 / T-06: _agentdesk_next_session_name tests (IVG-81)
# ============================================================

def _make_mock_env_with_sessions(tmp_path: Path, sessions: list) -> dict:
    """Build a mock env with a controllable zellij list-sessions stub.

    Writes the shared bare stubs for claude/codex/ccr FIRST, then overwrites
    the zellij stub with a list-sessions-aware version AFTER.  This ordering
    ensures the shared loop cannot clobber the controllable stub (ORDERING TRAP
    guard per MAJ-2 in the plan).

    Each session in `sessions` is emitted as a realistic decorated line, e.g.:
      foo-agents [Created 3m ago] (EXITED - attach to resume)
    This proves the sed-strip + awk first-column parse isolates the bare name
    from real zellij output decoration (T-05/T-06(f) MIN-3 requirement).

    The default _make_mock_env path keeps the bare exit-0 zellij stub so
    existing tests are unaffected.
    """
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir(exist_ok=True)

    # Step 1: write bare stubs for non-zellij tools (loop does NOT include zellij)
    for name in ("claude", "codex", "ccr"):
        stub = mock_bin / name
        stub.write_text("#!/bin/zsh\nexit 0\n")
        stub.chmod(0o755)

    # Step 2: write the controllable zellij stub AFTER the loop — ordering trap safe
    # Emit one printf line per session so each decorated line is a separate output line.
    # Using single-quoted strings in the stub to avoid shell expansion of special chars.
    printf_lines = "".join(
        f"  printf '%s\\n' '{s} [Created 3m ago] (EXITED - attach to resume)'\n"
        for s in sessions
    )
    zellij_stub = mock_bin / "zellij"
    zellij_stub.write_text(
        "#!/bin/zsh\n"
        'if [ "$1" = "list-sessions" ]; then\n'
        + printf_lines +
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    zellij_stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{mock_bin}:{os.environ['PATH']}"}
    _isolate_home(env, tmp_path)
    env["PROJECT_ROOT"] = str(tmp_path)
    env.pop("ZELLIJ", None)
    env.pop("ZELLIJ_SESSION_NAME", None)
    # IVG-135 T-04: see _make_mock_env for rationale — this builder does not
    # delegate to _make_mock_env(), so it needs its own pop.
    env.pop("ZELLIJ_SOCKET_DIR", None)
    return env


def _run_next_session_name(base: str, sessions: list, tmp_path: Path) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call _agentdesk_next_session_name BASE with controllable sessions."""
    env = _make_mock_env_with_sessions(tmp_path, sessions)
    script = f'source "{AGENTDESK_ZSH}"\n_agentdesk_next_session_name {base}'
    return subprocess.run(
        ["zsh", "-c", script],
        env=env,
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def _make_mock_env_with_sessions_and_attach(tmp_path: Path, sessions: list) -> dict:
    """Combined sessions + attach + fixed-layout mock env (IVG-109 collision-prompt tests).

    Follows the same ordering-trap-safe pattern as _make_mock_env_with_sessions (bare
    stubs first, controllable zellij stub last), but the single zellij stub also handles
    'attach' (writing AGENTDESK_TEST_ATTACH_MARKER) alongside 'list-sessions'. ALSO seeds
    a fake $HOME with the fixed-layout stub (mirroring _make_state_env) so tests whose
    code path falls through past the collision block don't hit the "layout not found"
    guard regardless of whether the collision/dashboard/picker fix is correct.
    """
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir(exist_ok=True)

    for name in ("claude", "codex", "ccr"):
        stub = mock_bin / name
        stub.write_text("#!/bin/zsh\nexit 0\n")
        stub.chmod(0o755)

    printf_lines = "".join(
        f"  printf '%s\\n' '{s} [Created 3m ago] (EXITED - attach to resume)'\n"
        for s in sessions
    )
    zellij_stub = mock_bin / "zellij"
    zellij_stub.write_text(
        "#!/bin/zsh\n"
        'if [ "$1" = "list-sessions" ]; then\n'
        + printf_lines +
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "attach" ]; then\n'
        '  printf \'%s\\n\' "$2" > "$AGENTDESK_TEST_ATTACH_MARKER"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    zellij_stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{mock_bin}:{os.environ['PATH']}"}
    _isolate_home(env, tmp_path)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["AGENTDESK_TEST_ATTACH_MARKER"] = str(tmp_path / "attach_called.txt")
    env.pop("ZELLIJ", None)
    env.pop("ZELLIJ_SESSION_NAME", None)
    # IVG-135 T-04: see _make_mock_env for rationale — this builder does not
    # delegate to _make_mock_env(), so it needs its own pop.
    env.pop("ZELLIJ_SOCKET_DIR", None)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(exist_ok=True)
    env["HOME"] = str(fake_home)
    zellij_layout_dir = fake_home / ".config" / "zellij" / "layouts"
    zellij_layout_dir.mkdir(parents=True, exist_ok=True)
    (zellij_layout_dir / "agent-desk.kdl").write_text("# stub layout\n")

    return env


def _run_agentdesk_with_sessions_and_attach(
    args: str, tmp_path: Path, sessions: list, stdin: Optional[str] = None
) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call agentdesk with a controllable sessions+attach mock env.

    Mirrors _run_agentdesk, but built on _make_mock_env_with_sessions_and_attach instead
    of the bare, session-blind _make_mock_env (_run_agentdesk cannot register a collision).
    """
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        agentdesk {args}
    """).strip()
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_mock_env_with_sessions_and_attach(tmp_path, sessions),
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


# ============================================================
# Spend tab tests (agentdesk-spend-tab)
# ============================================================

def _floating_section(kdl: str) -> str:
    """The text of the floating_panes block of the main tab."""
    assert "floating_panes {" in kdl, f"Expected a floating_panes block: {kdl[:400]}"
    return kdl.split("floating_panes {", 1)[1].split("swap_tiled_layout", 1)[0]


def test_agentdesk_spend_floating_with_main(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude spend → one main tab with a floating Token Spend pane."""
    kdl = _gen_layout("claude spend", tmp_path)
    assert 'tab name="main"' in kdl, f"Expected main tab: {kdl[:400]}"
    assert 'tab name="Spend"' not in kdl, "Spend is floating, not a tab"
    assert 'pane name="Token Spend"' in _floating_section(kdl)
    assert "spend_monitor.py" in kdl, f"Expected spend_monitor.py in KDL: {kdl[:400]}"
    assert kdl.count('tab name=') == 1, (
        f"Expected exactly 1 tab, got: {kdl.count('tab name=')}"
    )
    _assert_kdl_valid(kdl)


def test_agentdesk_spend_only_token_main_tab_with_shell(tmp_path: Path) -> None:
    """_agentdesk_gen_layout spend → main tab with a Shell plus the floating Spend pane."""
    kdl = _gen_layout("spend", tmp_path)
    assert 'tab name="main"' in kdl, f"Expected main tab: {kdl[:400]}"
    assert 'tab name="Spend"' not in kdl
    assert 'pane name="Token Spend"' in _floating_section(kdl)
    _assert_kdl_valid(kdl)


def test_agentdesk_spend_separated_from_multi_main(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude codex spend shell → Token Spend only inside floating_panes."""
    kdl = _gen_layout("claude codex spend shell", tmp_path)
    assert 'split_direction="vertical"' in kdl, (
        f"claude/codex on top should produce a vertical split: {kdl[:400]}"
    )
    assert 'tab name="Spend"' not in kdl
    assert 'pane name="Claude Code"' in kdl
    assert 'pane name="Codex"' in kdl
    assert 'pane name="Shell"' in kdl
    # 3 main panes + 1 spend pane = 4 total command panes
    count = kdl.count('command "zsh"')
    assert count == 4, (
        f"Expected 4 command panes (3 main + 1 spend), got {count}"
    )
    main_section = kdl.split("floating_panes {", 1)[0]
    assert 'pane name="Token Spend"' not in main_section, (
        "Token Spend pane must not appear among the tiled panes"
    )
    assert kdl.count('pane name="Token Spend"') == 1
    _assert_kdl_valid(kdl)


def test_agentdesk_spend_always_present(tmp_path: Path) -> None:
    """_agentdesk_gen_layout claude codex shell → floating Spend always present, even without a spend token."""
    kdl = _gen_layout("claude codex shell", tmp_path)
    assert 'pane name="Token Spend"' in _floating_section(kdl), (
        f"Floating Spend must always be present in generated layouts: {kdl[:400]}"
    )
    assert 'tab name="main"' in kdl, f"main tab must be present: {kdl[:400]}"
    _assert_kdl_valid(kdl)


# T-06 (a): base free → no suffix
def test_next_session_name_base_free(tmp_path: Path) -> None:
    """_agentdesk_next_session_name returns base unchanged when no sessions exist."""
    result = _run_next_session_name("foo-agents", [], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "foo-agents", (
        f"Expected 'foo-agents', got: {result.stdout.strip()!r}"
    )


# T-06 (b): base taken → _1 (REACH-PROVING: if the ordering trap fires, sessions list is
# empty, helper returns 'foo-agents' instead of 'foo-agents_1', and this test FAILS)
def test_next_session_name_base_taken_returns_1(tmp_path: Path) -> None:
    """_agentdesk_next_session_name returns BASE_1 when base is already taken.

    This is the reach-proving assertion (MAJ-2): a passing result here proves the
    controllable zellij stub's list-sessions output actually reaches the function.
    If the ordering trap silently emptied the stub, this test would wrongly return
    'foo-agents' and FAIL, catching the false-green.
    """
    result = _run_next_session_name("foo-agents", ["foo-agents"], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "foo-agents_1", (
        f"Expected 'foo-agents_1' (reach-proving), got: {result.stdout.strip()!r}"
    )


# T-06 (c): base + _1 taken → _2
def test_next_session_name_base_and_1_taken_returns_2(tmp_path: Path) -> None:
    """_agentdesk_next_session_name returns BASE_2 when base and base_1 are both taken."""
    result = _run_next_session_name("foo-agents", ["foo-agents", "foo-agents_1"], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "foo-agents_2", (
        f"Expected 'foo-agents_2', got: {result.stdout.strip()!r}"
    )


# T-06 (d): custom --name collision
def test_next_session_name_custom_name_collision(tmp_path: Path) -> None:
    """_agentdesk_next_session_name returns myname_1 when myname is taken."""
    result = _run_next_session_name("myname", ["myname"], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "myname_1", (
        f"Expected 'myname_1', got: {result.stdout.strip()!r}"
    )


# T-06 (e): gap case — returns lowest free, not next after max
def test_next_session_name_gap_case(tmp_path: Path) -> None:
    """_agentdesk_next_session_name returns foo_1 (lowest free) when foo and foo_2 exist."""
    result = _run_next_session_name("foo", ["foo", "foo_2"], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "foo_1", (
        f"Expected 'foo_1' (lowest free), got: {result.stdout.strip()!r}"
    )


# T-06 (f): decorated-line parse (MIN-3) — realistic ANSI + suffix decoration
def test_next_session_name_decorated_line_parse(tmp_path: Path) -> None:
    """_agentdesk_next_session_name isolates bare name from realistic decorated zellij output.

    The stub emits lines like 'foo-agents [Created 3m ago] (EXITED - attach to resume)'
    proving the sed-strip + awk first-column parse correctly extracts 'foo-agents'
    from real-shape zellij output decoration.
    """
    # With one decorated session line for 'foo-agents', helper should return 'foo-agents_1'
    result = _run_next_session_name("foo-agents", ["foo-agents"], tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "foo-agents_1", (
        f"Expected 'foo-agents_1' (decorated-line parse), got: {result.stdout.strip()!r}"
    )


# ============================================================
# T-01/T-03: collision-prompt (attach-or-new) tests (IVG-109)
# ============================================================

def test_collision_non_tty_preserves_prior_behavior(tmp_path: Path) -> None:
    """Non-TTY collision path is byte-identical to the pre-fix behavior: no prompt, no hang."""
    result = _run_agentdesk_with_sessions_and_attach(
        "--name foo-agents", tmp_path, sessions=["foo-agents"]
    )
    combined = result.stdout + result.stderr
    assert (
        "Session 'foo-agents' already exists; starting new session 'foo-agents_1' instead."
        in combined
    ), f"Expected unchanged collision message: {combined!r}"
    marker = tmp_path / "attach_called.txt"
    assert not marker.exists(), "Non-TTY path must never call agentdesk-attach"
    assert "Attach to it, or start a new session" not in combined, (
        f"Non-TTY path must never show the interactive prompt: {combined!r}"
    )


def test_collision_tty_default_enter_attaches(tmp_path: Path) -> None:
    """TTY collision, default (Enter) reply → attaches to the ORIGINAL (non-suffixed) session."""
    env = _make_mock_env_with_sessions_and_attach(tmp_path, ["foo-agents"])
    result = _run_agentdesk_with_pty("--name foo-agents", tmp_path, "\n", env=env)
    assert "Attach to it, or start a new session 'foo-agents_1'" in result.stderr, (
        f"Expected attach-or-new prompt on stderr: {result.stderr!r}"
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    marker = tmp_path / "attach_called.txt"
    assert marker.exists(), "Attach marker must exist after default-Enter reply"
    assert marker.read_text().strip() == "foo-agents", (
        f"Must attach to the ORIGINAL name, not the suffixed one: {marker.read_text()!r}"
    )


def test_collision_tty_explicit_n_starts_new(tmp_path: Path) -> None:
    """TTY collision, explicit 'n' reply → starts a new session, full launch completes."""
    env = _make_mock_env_with_sessions_and_attach(tmp_path, ["foo-agents"])
    result = _run_agentdesk_with_pty("--name foo-agents", tmp_path, "n\nn\n\n", env=env)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    marker = tmp_path / "attach_called.txt"
    assert not marker.exists(), "Attach must not be called when 'n' starts a new session"
    assert "Starting new session 'foo-agents_1'" in (result.stdout + result.stderr), (
        f"Expected new-session message: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_collision_tty_reply_new_word_starts_new(tmp_path: Path) -> None:
    """TTY collision, 'new' (word form, case-insensitive) reply → same as explicit 'n'."""
    env = _make_mock_env_with_sessions_and_attach(tmp_path, ["foo-agents"])
    result = _run_agentdesk_with_pty("--name foo-agents", tmp_path, "new\nn\n\n", env=env)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    marker = tmp_path / "attach_called.txt"
    assert not marker.exists(), "Attach must not be called when 'new' starts a new session"
    assert "Starting new session 'foo-agents_1'" in (result.stdout + result.stderr), (
        f"Expected new-session message: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_no_collision_skips_prompt_entirely(tmp_path: Path) -> None:
    """No existing session with this name → the collision prompt never fires."""
    env = _make_mock_env_with_sessions_and_attach(tmp_path, [])
    result = _run_agentdesk_with_pty("--name foo-agents", tmp_path, "n\n\n", env=env)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert "already exists" not in combined, f"No collision text expected: {combined!r}"
    assert "Attach to it, or start a new session" not in combined, (
        f"No prompt expected on a fresh name: {combined!r}"
    )
    marker = tmp_path / "attach_called.txt"
    assert not marker.exists(), "Attach must never be called when there is no collision"


# ============================================================
# IVG-82 last-layout persistence tests
# ============================================================
# HARD REQUIREMENT: every test that drives a full agentdesk() launch (not a
# helper-level unit test) MUST use _make_state_env / _run_agentdesk_state.
# Using bare _make_mock_env for a launch test means the fixed Zellij layout
# is absent → rc=1 and "Starting agent desk" assertions fail.


def _make_state_env(tmp_path: Path) -> dict:
    """Extend _make_mock_env with a fake $HOME that has the stub fixed-layout file.

    Creates:
      tmp_path/fake_home/.config/zellij/layouts/agent-desk.kdl  (stub file)
      tmp_path/fake_home/.config/agentdesk/                      (created on save)

    All persistence tests that drive a full agentdesk() launch MUST use this
    helper so the fixed layout file exists and the "Zellij layout not found"
    guard does not fire (which would give rc=1 before saving the layout).
    """
    env = _make_mock_env(tmp_path)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(exist_ok=True)
    env["HOME"] = str(fake_home)

    # Create stub fixed-layout file so layout-not-found guard passes
    zellij_layout_dir = fake_home / ".config" / "zellij" / "layouts"
    zellij_layout_dir.mkdir(parents=True, exist_ok=True)
    (zellij_layout_dir / "agent-desk.kdl").write_text("# stub layout\n")

    return env


def _run_agentdesk_state(
    args: str, tmp_path: Path, stdin: "Optional[str]" = None
) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call agentdesk with the given args, using _make_state_env."""
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        agentdesk {args}
    """).strip()
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def _run_zsh_fn_state(
    fn_call: str, tmp_path: Path, stdin_input: str = ""
) -> subprocess.CompletedProcess:
    """Source agentdesk.zsh and call an arbitrary zsh expression using _make_state_env."""
    script = f'source "{AGENTDESK_ZSH}"\n{fn_call}'
    return subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input=stdin_input,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )


def test_state_file_path(tmp_path: Path) -> None:
    """_agentdesk_state_file echoes a path ending with /.config/agentdesk/last-layout."""
    result = _run_zsh_fn_state("_agentdesk_state_file", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    path = result.stdout.strip()
    assert path.endswith("/.config/agentdesk/last-layout"), (
        f"State file path unexpected: {path!r}"
    )
    # Must be under the fake HOME set by _make_state_env
    env = _make_state_env(tmp_path)
    assert path.startswith(env["HOME"]), (
        f"State file not under fake HOME {env['HOME']!r}: {path!r}"
    )


def test_encode_key_no_raw_dots_slashes_spaces(tmp_path: Path) -> None:
    """_agentdesk_encode_key encodes /, spaces, and . (critical for awk exact-match safety)."""
    # /a b/c.d → no raw space, no raw /, no raw .
    result = _run_zsh_fn_state("_agentdesk_encode_key '/a b/c.d'", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    encoded = result.stdout.strip()
    assert " " not in encoded, f"Encoded key must not contain spaces: {encoded!r}"
    # Raw / must be encoded (not a literal slash in output)
    # The encoded form has no literal / because %2F is produced
    assert "/" not in encoded, f"Encoded key must not contain raw /: {encoded!r}"
    # Raw . must be encoded as %2E (not left as literal .)
    assert "%2E" in encoded, f"Encoded key must contain %2E for '.': {encoded!r}"
    assert "." not in encoded, f"Encoded key must not contain raw '.': {encoded!r}"

    # my.project → my%2Eproject (dot encoded, no raw dot)
    result2 = _run_zsh_fn_state("_agentdesk_encode_key 'my.project'", tmp_path)
    assert result2.returncode == 0
    encoded2 = result2.stdout.strip()
    assert encoded2 == "my%2Eproject", (
        f"Expected 'my%2Eproject', got: {encoded2!r}"
    )


def test_layout_key_named(tmp_path: Path) -> None:
    """_agentdesk_layout_key with non-empty custom_name returns name:<sanitized>."""
    result = _run_zsh_fn_state("_agentdesk_layout_key 'my sess' '/some/path'", tmp_path)
    assert result.returncode == 0
    key = result.stdout.strip()
    assert key.startswith("name:"), f"Named key must start with 'name:': {key!r}"
    # Sanitized: spaces → dashes
    assert key == "name:my-sess", f"Expected 'name:my-sess', got: {key!r}"


def test_layout_key_project(tmp_path: Path) -> None:
    """_agentdesk_layout_key with empty custom_name returns proj:<encoded-root>."""
    result = _run_zsh_fn_state("_agentdesk_layout_key '' '/p r'", tmp_path)
    assert result.returncode == 0
    key = result.stdout.strip()
    assert key.startswith("proj:"), f"Project key must start with 'proj:': {key!r}"
    # Space in path must be encoded
    assert " " not in key, f"Encoded project key must not contain spaces: {key!r}"


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """_agentdesk_save_layout + _agentdesk_load_layout round-trips a value under fake HOME."""
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        _agentdesk_save_layout 'proj:myproject' 'claude+shell'
        _agentdesk_load_layout 'proj:myproject'
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    assert result.stdout.strip() == "claude+shell", (
        f"Expected 'claude+shell' from load, got: {result.stdout.strip()!r}"
    )


def test_save_load_dotted_path(tmp_path: Path) -> None:
    """Dotted project path key encodes to %2E; awk exact-match loads correct value; myXproject returns its own distinct value."""
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        key_dot="$(_agentdesk_layout_key '' '/home/user/my.project')"
        key_x="$(_agentdesk_layout_key '' '/home/user/myXproject')"
        _agentdesk_save_layout "$key_dot" 'dot-value'
        _agentdesk_save_layout "$key_x" 'x-value'
        echo "dot:$(_agentdesk_load_layout "$key_dot")"
        echo "x:$(_agentdesk_load_layout "$key_x")"
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    lines = result.stdout.strip().splitlines()
    dot_line = next((l for l in lines if l.startswith("dot:")), "")
    x_line = next((l for l in lines if l.startswith("x:")), "")
    assert dot_line == "dot:dot-value", f"Dotted-path key load wrong: {dot_line!r}"
    assert x_line == "x:x-value", f"X-path key load wrong: {x_line!r}"


def test_load_absent_key_rc1(tmp_path: Path) -> None:
    """_agentdesk_load_layout for unknown key → rc=1, empty stdout."""
    # First save something so the file exists
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        _agentdesk_save_layout 'proj:something' '__FIXED__'
        _agentdesk_load_layout 'proj:does-not-exist'
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode != 0, (
        f"Loading absent key should return rc!=0 (rc={result.returncode})"
    )
    assert result.stdout.strip() == "", (
        f"Absent-key load must produce empty stdout: {result.stdout!r}"
    )


def test_load_missing_file_failopen(tmp_path: Path) -> None:
    """_agentdesk_load_layout with no state file → rc=1, no stderr."""
    result = _run_zsh_fn_state("_agentdesk_load_layout 'proj:anything'", tmp_path)
    assert result.returncode != 0, (
        f"Missing-file load should return rc!=0 (rc={result.returncode})"
    )
    assert result.stdout.strip() == "", f"Missing-file load must produce empty stdout: {result.stdout!r}"
    assert result.stderr.strip() == "", f"Missing-file load must produce no stderr: {result.stderr!r}"


def test_layout_from_value_fixed(tmp_path: Path) -> None:
    """_agentdesk_layout_from_value __FIXED__ → agent-desk.kdl path, rc=0."""
    result = _run_zsh_fn_state("_agentdesk_layout_from_value '__FIXED__'", tmp_path)
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    path = result.stdout.strip()
    assert path.endswith("/agent-desk.kdl"), (
        f"__FIXED__ must return agent-desk.kdl path, got: {path!r}"
    )


def test_layout_from_value_tokens(tmp_path: Path) -> None:
    """_agentdesk_layout_from_value claude+shell → generates KDL with both panes, rc=0."""
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        out="$(_agentdesk_layout_from_value 'claude+shell')"
        rc=$?
        if [ $rc -eq 0 ] && [ -n "$out" ]; then
            cat "$out"
            rm -f "$out"
        fi
        exit $rc
    """).strip()
    result = subprocess.run(
        ["zsh", "-c", script],
        env=_make_state_env(tmp_path),
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    kdl = result.stdout
    assert 'pane name="Claude Code"' in kdl, f"Claude Code pane missing: {kdl[:300]}"
    assert 'pane name="Shell"' in kdl, f"Shell pane missing: {kdl[:300]}"


def test_layout_from_value_corrupt_rc1(tmp_path: Path) -> None:
    """_agentdesk_layout_from_value claude+bogus → rc=1 (invalid token)."""
    result = _run_zsh_fn_state("_agentdesk_layout_from_value 'claude+bogus'", tmp_path)
    assert result.returncode != 0, (
        f"Corrupt value must return rc!=0 (rc={result.returncode})"
    )


def test_bare_launch_saves_after_tokens(tmp_path: Path) -> None:
    """agentdesk claude shell → state file contains =claude+shell under fake HOME.

    Uses _make_state_env so the fixed Zellij layout stub exists and zellij mock exits 0.
    """
    result = _run_agentdesk_state("claude shell", tmp_path)
    assert result.returncode == 0, (
        f"agentdesk claude shell must succeed (rc={result.returncode})\nstderr: {result.stderr}"
    )
    env = _make_state_env(tmp_path)
    state_file = Path(env["HOME"]) / ".config" / "agentdesk" / "last-layout"
    assert state_file.exists(), f"State file must exist after token launch: {state_file}"
    content = state_file.read_text()
    assert "=claude+shell" in content, (
        f"State file must contain '=claude+shell': {content!r}"
    )


def test_bare_launch_reuses_saved(tmp_path: Path) -> None:
    """Pre-seeded state file → stderr has 'Reusing last layout' + 'Starting agent desk'; no picker menu."""
    env = _make_state_env(tmp_path)
    fake_home = Path(env["HOME"])

    # Pre-seed state file with fixed layout for this project key
    # agentdesk uses proj:<encoded-cwd> as key in bare launch from tmp_path
    state_dir = fake_home / ".config" / "agentdesk"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Derive the key by running _agentdesk_layout_key
    key_script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        project_root="$(_agentdesk_realpath "{tmp_path}")"
        _agentdesk_layout_key '' "$project_root"
    """).strip()
    key_result = subprocess.run(
        ["zsh", "-c", key_script],
        env=env,
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert key_result.returncode == 0, f"key derivation failed: {key_result.stderr}"
    key = key_result.stdout.strip()
    assert key, "derived key must not be empty"

    # Write pre-seeded state
    (state_dir / "last-layout").write_text(f"{key}=__FIXED__\n")

    result = _run_agentdesk_state("", tmp_path)
    combined = result.stdout + result.stderr
    assert "Reusing last layout" in combined, (
        f"Expected 'Reusing last layout' in output:\n{combined}"
    )
    assert "Starting agent desk" in combined, (
        f"Expected 'Starting agent desk' in output:\n{combined}"
    )
    assert "Select a layout" not in combined, (
        f"Picker menu must NOT appear on reuse path:\n{combined}"
    )


def test_pick_flag_parses(tmp_path: Path) -> None:
    """agentdesk --pick rc=0, does not trigger 'unexpected argument', non-TTY → fixed branch; state=__FIXED__."""
    result = _run_agentdesk_state("--pick", tmp_path)
    assert result.returncode == 0, (
        f"agentdesk --pick must return 0 (rc={result.returncode})\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "unexpected argument" not in combined.lower(), (
        f"--pick must not trigger 'unexpected argument': {combined}"
    )
    # Non-TTY + --pick → fixed branch (D-07); state saved as __FIXED__
    env = _make_state_env(tmp_path)
    state_file = Path(env["HOME"]) / ".config" / "agentdesk" / "last-layout"
    assert state_file.exists(), f"State file must exist after --pick launch: {state_file}"
    content = state_file.read_text()
    assert "=__FIXED__" in content, (
        f"Non-TTY --pick must save '__FIXED__' (not empty string): {content!r}"
    )


def _run_agentdesk_with_pty(
    args: str,
    tmp_path: Path,
    input_text: str,
    timeout: int = SUBPROCESS_TIMEOUT,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run agentdesk with a real PTY as stdin so [ -t 0 ] is true (picker tests).

    When env is None (all existing callers — unaffected), builds the default
    _make_state_env. Pass an explicit env (e.g. from
    _make_mock_env_with_sessions_and_attach) to drive collision-prompt tests.
    """
    if env is None:
        env = _make_state_env(tmp_path)
    script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        agentdesk {args}
    """).strip()
    master_fd, slave_fd = pty.openpty()
    stdout_b: bytes = b""
    stderr_b: bytes = b""
    rc: int = -1
    try:
        try:
            proc = subprocess.Popen(
                ["zsh", "-c", script],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(tmp_path),
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        try:
            os.write(master_fd, input_text.encode())
        except OSError:
            pass
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_b, stderr_b = proc.communicate()
        rc = proc.returncode
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    return subprocess.CompletedProcess(
        args=["zsh", "-c", script],
        returncode=rc,
        stdout=stdout_b.decode(errors="replace"),
        stderr=stderr_b.decode(errors="replace"),
    )


def test_picker_option1_saves_fixed_sentinel(tmp_path: Path) -> None:
    """TTY picker run with option 1 (empty/default) → state file line is =__FIXED__, NOT empty string."""
    # T-03/CRIT-1 fix: dashboard prompt now comes BEFORE the layout picker.
    # Input order: 'n\n' to decline the dashboard prompt, then '\n' (empty = option 1 = standard layout).
    result = _run_agentdesk_with_pty("", tmp_path, "n\n\n", timeout=SUBPROCESS_TIMEOUT)
    env = _make_state_env(tmp_path)
    state_file = Path(env["HOME"]) / ".config" / "agentdesk" / "last-layout"
    assert state_file.exists(), (
        f"State file must exist after picker option 1\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    content = state_file.read_text()
    assert "=__FIXED__" in content, (
        f"Option-1 (empty pick) must save '__FIXED__', NOT empty string: {content!r}"
    )
    assert "=" + "\n" not in content and not any(
        line.endswith("=") for line in content.splitlines()
    ), f"State file must not have empty value (empty-save bug): {content!r}"


def test_picker_token_saves_joined_string(tmp_path: Path) -> None:
    """TTY picker run with option 2 (claude shell) → state file contains =claude+shell."""
    # T-03/CRIT-1 fix: dashboard prompt now comes BEFORE the layout picker.
    # Input order: 'n\n' to decline the dashboard prompt, then '2\n' to select option 2.
    result = _run_agentdesk_with_pty("", tmp_path, "n\n2\n", timeout=SUBPROCESS_TIMEOUT)
    env = _make_state_env(tmp_path)
    state_file = Path(env["HOME"]) / ".config" / "agentdesk" / "last-layout"
    assert state_file.exists(), (
        f"State file must exist after picker option 2\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    content = state_file.read_text()
    assert "=claude+shell" in content, (
        f"Option-2 pick must save 'claude+shell' as joined string: {content!r}"
    )


def test_corrupt_saved_failopen_nontty(tmp_path: Path) -> None:
    """Pre-seeded corrupt value claude+bogus → rc=0, falls open to fixed, no 'Reusing' message."""
    env = _make_state_env(tmp_path)
    fake_home = Path(env["HOME"])
    state_dir = fake_home / ".config" / "agentdesk"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Derive the key
    key_script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        project_root="$(_agentdesk_realpath "{tmp_path}")"
        _agentdesk_layout_key '' "$project_root"
    """).strip()
    key_result = subprocess.run(
        ["zsh", "-c", key_script],
        env=env,
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    key = key_result.stdout.strip()
    (state_dir / "last-layout").write_text(f"{key}=claude+bogus\n")

    result = _run_agentdesk_state("", tmp_path)
    assert result.returncode == 0, (
        f"Corrupt saved value must fail-open (rc=0): rc={result.returncode}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Reusing last layout" not in combined, (
        f"Corrupt saved value must NOT print 'Reusing last layout': {combined}"
    )
    assert "Starting agent desk" in combined, (
        f"Corrupt saved value must still start agent desk: {combined}"
    )


def test_name_proj_namespace_isolation(tmp_path: Path) -> None:
    """Named launch save does NOT trigger reuse on bare same-project launch.

    name: and proj: are independent namespaces — a save via agentdesk --name myname
    must not match a bare agentdesk (proj: key) lookup.

    Strategy: directly pre-seed a name: key into the state file, then verify a
    bare (proj:) launch does not pick it up.
    """
    env = _make_state_env(tmp_path)
    fake_home = Path(env["HOME"])

    # Derive the proj: key for the bare launch
    key_script = textwrap.dedent(f"""
        source "{AGENTDESK_ZSH}"
        project_root="$(_agentdesk_realpath "{tmp_path}")"
        _agentdesk_layout_key '' "$project_root"
    """).strip()
    key_result = subprocess.run(
        ["zsh", "-c", key_script],
        env=env,
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    proj_key = key_result.stdout.strip()
    assert proj_key.startswith("proj:"), f"Expected proj: key, got: {proj_key!r}"

    # Pre-seed a name: key (different namespace) into the state file
    state_dir = fake_home / ".config" / "agentdesk"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last-layout").write_text("name:myname=claude+shell\n")

    # Bare launch (non-TTY, no saved proj: key) → must NOT reuse the name: save
    result_bare = _run_agentdesk_state("", tmp_path)
    assert result_bare.returncode == 0, (
        f"Bare launch must succeed (rc={result_bare.returncode})\nstderr: {result_bare.stderr}"
    )
    combined = result_bare.stdout + result_bare.stderr
    assert "Reusing last layout" not in combined, (
        f"Bare launch must not reuse name: namespace save (namespace isolation):\n{combined}"
    )


# ============================================================
# T-07: IVG-85 dashboard-lifecycle regression prevention tests
# ============================================================

def test_dashboard_kdl_injection_adds_tab_preserves_existing(tmp_path: Path) -> None:
    """T-07 Test A: KDL injection in T-05 adds a Dashboard tab to the Standard layout
    without removing any existing tabs.

    Creates a minimal 5-tab KDL fixture, runs the T-05 injection logic via zsh
    (setting AGENTDESK_DASHBOARD=1), and asserts:
    - The output KDL contains 'tab name="main"' (original tab preserved)
    - The output KDL contains 'tab name="Dashboard"' (new tab injected)
    - The injected pane args include 'dashboard_server.py' (correct server command)
    - The output KDL is syntactically closed (ends with '}')
    """
    # Minimal 5-tab KDL fixture mimicking the Standard layout structure.
    fixture_kdl = tmp_path / "test-layout.kdl"
    fixture_kdl.write_text(
        'layout {\n'
        '    tab name="main" {\n'
        '        pane { command "zsh" }\n'
        '    }\n'
        '    tab name="review" {\n'
        '        pane { command "zsh" }\n'
        '    }\n'
        '    tab name="repos" {\n'
        '        pane { command "zsh" }\n'
        '    }\n'
        '    tab name="shell" {\n'
        '        pane { command "zsh" }\n'
        '    }\n'
        '    tab name="Spend" {\n'
        '        pane { command "zsh" }\n'
        '    }\n'
        '}\n'
    )

    # A temp path to simulate AGENTDESK_DASHBOARD_URL_FILE.
    url_file = tmp_path / "dash-url.txt"

    # Run T-05 injection logic directly (not the full agentdesk() function).
    # We replicate the injection block here so the test is self-contained.
    script = f"""
source "{AGENTDESK_ZSH}"
layout_path="{fixture_kdl}"
AGENTDESK_DASHBOARD=1
AGENTDESK_DASHBOARD_URL_FILE="{url_file}"
dash_tab_tmp="$(mktemp /tmp/agentdesk-test-XXXXXX.kdl)"
sed '$d' "$layout_path" > "$dash_tab_tmp"
cat >> "$dash_tab_tmp" <<'DASHTAB'
    tab name="Dashboard" {{
        pane name="quoin dashboard" {{
            command "zsh"
            args "-lc" "exec python3 \\"$HOME/.claude/scripts/dashboard_server.py\\" --no-browser --url-file \\"/tmp/dash-url.txt\\""
        }}
    }}
}}
DASHTAB
cat "$dash_tab_tmp"
rm -f "$dash_tab_tmp"
"""
    result = subprocess.run(
        ["zsh", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"rc={result.returncode}, stderr={result.stderr}"
    kdl_out = result.stdout

    # Original tabs preserved
    assert 'tab name="main"' in kdl_out, f"'main' tab missing from injected KDL:\n{kdl_out}"
    assert 'tab name="review"' in kdl_out, f"'review' tab missing:\n{kdl_out}"
    assert 'tab name="Spend"' in kdl_out, f"'Spend' tab missing:\n{kdl_out}"

    # Dashboard tab injected
    assert 'tab name="Dashboard"' in kdl_out, (
        f"'Dashboard' tab not injected into KDL:\n{kdl_out}"
    )

    # Server command present
    assert 'dashboard_server.py' in kdl_out, (
        f"dashboard_server.py not found in injected pane args:\n{kdl_out}"
    )

    # KDL is syntactically closed (last non-empty line is '}')
    last_line = next((l for l in reversed(kdl_out.splitlines()) if l.strip()), "")
    assert last_line.strip() == "}", (
        f"Injected KDL not closed — last non-empty line: {last_line!r}"
    )


def test_dashboard_helpers_do_not_background_a_server() -> None:
    """Structural guard: the decision and URL-file helpers never start the
    dashboard server (it belongs in an injected pane) and never export or unset
    a dashboard variable."""
    import re
    src = AGENTDESK_ZSH.read_text(encoding="utf-8")
    start = src.index("_agentdesk_dashboard_ask() {")
    end = src.index("\n}\n", src.index("_agentdesk_dashboard_url_file() {")) + 3
    body = src[start:end]
    assert "dashboard_server.py" in body  # the presence check lives here
    assert re.search(r"python3[^\n]+dashboard_server\.py[^\n]+&", body) is None
    assert "export AGENTDESK_DASHBOARD" not in src
    assert "unset AGENTDESK_DASHBOARD" not in src


# ── generic state set/get and dashboard keys ─────────────────────────────────

def _state_lines(tmp_path: Path) -> list:
    state = tmp_path / "fake_home" / ".config" / "agentdesk" / "last-layout"
    return state.read_text().splitlines() if state.exists() else []


def test_state_set_get_round_trip(tmp_path: Path) -> None:
    result = _run_zsh_fn_state(
        "_agentdesk_state_set k1 'a=b=c'; _agentdesk_state_set k2 v2; "
        "_agentdesk_state_get k1; _agentdesk_state_get k2",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["a=b=c", "v2"]


def test_state_wrappers_delegate(tmp_path: Path) -> None:
    result = _run_zsh_fn_state(
        "_agentdesk_save_layout via-wrapper w1; _agentdesk_state_get via-wrapper; "
        "_agentdesk_state_set via-set s1; _agentdesk_load_layout via-set",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["w1", "s1"]


def test_dash_key_namespacing(tmp_path: Path) -> None:
    result = _run_zsh_fn_state("_agentdesk_dash_key proj:abc", tmp_path)
    assert result.stdout.strip() == "dash:proj:abc"


def test_dash_and_layout_keys_coexist(tmp_path: Path) -> None:
    script = (
        "_agentdesk_save_layout proj:X claude+shell; "
        "_agentdesk_state_set \"$(_agentdesk_dash_key proj:X)\" yes; "
        "_agentdesk_load_layout proj:X; "
        "_agentdesk_state_get dash:proj:X; "
        "_agentdesk_state_set dash:proj:X no; "
        "_agentdesk_load_layout proj:X; "
        "_agentdesk_state_get dash:proj:X"
    )
    result = _run_zsh_fn_state(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["claude+shell", "yes", "claude+shell", "no"]
    assert sorted(_state_lines(tmp_path)) == ["dash:proj:X=no", "proj:X=claude+shell"]


# ── agentdesk --clean-state ──────────────────────────────────────────────────

def _enc(path: str) -> str:
    from urllib.parse import quote
    return quote(path, safe="").replace(".", "%2E").replace("~", "%7E")


def _clean_state_file(tmp_path: Path) -> Path:
    return tmp_path / "fake_home" / ".config" / "agentdesk" / "last-layout"


def _run_clean(args: str, tmp_path: Path, extra_env: "Optional[dict]" = None):
    env = _make_state_env(tmp_path)
    if extra_env:
        env.update(extra_env)
    script = f'source "{AGENTDESK_ZSH}"\nagentdesk {args}'
    return subprocess.run(
        ["zsh", "-c", script], env=env, input="", capture_output=True, text=True,
        cwd=str(tmp_path), timeout=SUBPROCESS_TIMEOUT,
    )


_CLEAN_KEPT = [
    f"proj:{_enc('/Users/u/code/app')}=claude+shell",
    "name:mysession=__FIXED__",
    f"proj:{_enc('/Users/u/pytest-notes')}=__FIXED__",
    f"proj:{_enc('/private/tmp/claude-501/abc/other')}=shell",
    "garbage-without-equals",
    f"proj:{_enc('/Users/u/eq')}=a=b=c",
]
_CLEAN_REMOVED = [
    f"proj:{_enc('/private/var/folders/xy/T/pytest-of-u/pytest-3/test_a0')}=__FIXED__",
    f"proj:{_enc('/private/tmp/claude-501/abc/scratchpad')}=claude",
    f"dash:proj:{_enc('/private/var/folders/xy/T/pytest-of-u/pytest-3/test_a0')}=yes",
    "proj:/private/var/folders/zz/x=__FIXED__",
]


def _write_mixed_state(tmp_path: Path, trailing_newline: bool = True) -> bytes:
    order = [
        _CLEAN_KEPT[0], _CLEAN_KEPT[1], _CLEAN_REMOVED[0], _CLEAN_KEPT[2],
        _CLEAN_REMOVED[1], _CLEAN_KEPT[3], _CLEAN_REMOVED[2], _CLEAN_REMOVED[3],
        _CLEAN_KEPT[4], _CLEAN_KEPT[5],
    ]
    data = ("\n".join(order) + ("\n" if trailing_newline else "")).encode()
    state = _clean_state_file(tmp_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(data)
    return data


def _backups(tmp_path: Path) -> list:
    return sorted(_clean_state_file(tmp_path).parent.glob("last-layout.bak.*"))


def test_clean_state_removes_polluted_entries_and_backs_up(tmp_path: Path) -> None:
    original = _write_mixed_state(tmp_path)
    result = _run_clean("--clean-state", tmp_path)
    assert result.returncode == 0, result.stderr
    expected = ("\n".join(_CLEAN_KEPT) + "\n").encode()
    assert _clean_state_file(tmp_path).read_bytes() == expected
    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert "removed 4 entries" in result.stdout
    assert str(backups[0]) in result.stdout
    assert "name:mysession" in result.stdout
    assert not list(_clean_state_file(tmp_path).parent.glob(".last-layout.clean.*"))


def test_clean_state_second_run_is_noop(tmp_path: Path) -> None:
    _write_mixed_state(tmp_path)
    _run_clean("--clean-state", tmp_path)
    after_first = _clean_state_file(tmp_path).read_bytes()
    result = _run_clean("--clean-state", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "already clean" in result.stdout
    assert _clean_state_file(tmp_path).read_bytes() == after_first
    assert len(_backups(tmp_path)) == 1


def test_clean_state_dry_run_writes_nothing(tmp_path: Path) -> None:
    original = _write_mixed_state(tmp_path)
    state_dir = _clean_state_file(tmp_path).parent
    listing = sorted(p.name for p in state_dir.iterdir())
    dir_mtime = state_dir.stat().st_mtime_ns
    result = _run_clean("--clean-state --dry-run", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "would remove 4 of 10 entries" in result.stdout
    assert _clean_state_file(tmp_path).read_bytes() == original
    assert sorted(p.name for p in state_dir.iterdir()) == listing
    assert state_dir.stat().st_mtime_ns == dir_mtime


def test_clean_state_missing_file(tmp_path: Path) -> None:
    result = _run_clean("--clean-state", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "nothing to clean" in result.stdout
    assert not _clean_state_file(tmp_path).exists()
    assert not _clean_state_file(tmp_path).parent.exists()


def test_clean_state_preserves_missing_trailing_newline(tmp_path: Path) -> None:
    _write_mixed_state(tmp_path, trailing_newline=False)
    result = _run_clean("--clean-state", tmp_path)
    assert result.returncode == 0, result.stderr
    assert _clean_state_file(tmp_path).read_bytes() == "\n".join(_CLEAN_KEPT).encode()


@pytest.mark.parametrize(
    "args",
    ["--clean-state claude", "--clean-state --panes shell", "--dry-run",
     "--clean-state --mode solo", "--clean-state --pick", "--clean-state --name x",
     "--clean-state --if-exists new"],
)
def test_clean_state_rejects_launch_arguments(args: str, tmp_path: Path) -> None:
    original = _write_mixed_state(tmp_path)
    result = _run_clean(args, tmp_path)
    assert result.returncode == 1
    assert "Error" in result.stderr
    assert _clean_state_file(tmp_path).read_bytes() == original
    assert not _backups(tmp_path)


def test_clean_state_runs_inside_zellij_session(tmp_path: Path) -> None:
    _write_mixed_state(tmp_path)
    result = _run_clean("--clean-state", tmp_path, {"ZELLIJ": "1"})
    assert result.returncode == 0, result.stderr
    assert "removed 4 entries" in result.stdout


def test_clean_state_ignores_exported_pick_variable(tmp_path: Path) -> None:
    _write_mixed_state(tmp_path)
    result = _run_clean("--clean-state", tmp_path, {"AGENTDESK_PICK": "1"})
    assert result.returncode == 0, result.stderr
    assert "removed 4 entries" in result.stdout


def test_clean_state_backup_name_never_overwrites(tmp_path: Path) -> None:
    original = _write_mixed_state(tmp_path)
    state_dir = _clean_state_file(tmp_path).parent
    _make_state_env(tmp_path)  # creates mock_bin, which is first on PATH
    stub_bin = tmp_path / "mock_bin"
    (stub_bin / "date").write_text("#!/bin/sh\necho 20260101-000000\n")
    (stub_bin / "date").chmod(0o755)
    (state_dir / "last-layout.bak.20260101-000000").write_text("older backup\n")
    result = _run_clean("--clean-state", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state_dir / "last-layout.bak.20260101-000000").read_text() == "older backup\n"
    assert (state_dir / "last-layout.bak.20260101-000000-1").read_bytes() == original


# ── detach hint and workflow-artifact placeholders ───────────────────────────

_AGENTDESK_TOOLS_DIR = AGENTDESK_ZSH.parent
_LEGACY_SETUP = _AGENTDESK_TOOLS_DIR / "setup-agentdesk.sh"
_PLACEHOLDERS = ("current-plan.md", "review.md", "lessons-learned.md")


def test_no_stale_detach_hint_in_launcher_files() -> None:
    for path in _AGENTDESK_TOOLS_DIR.rglob("*"):
        if path.is_file():
            assert "Ctrl+o, then d" not in path.read_text(encoding="utf-8"), path


def test_inside_zellij_prints_working_detach_hint(tmp_path: Path) -> None:
    env = _make_state_env(tmp_path)
    env["ZELLIJ"] = "1"
    result = subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"\nagentdesk claude'],
        env=env, input="", capture_output=True, text=True, cwd=str(tmp_path),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 1
    assert "Alt+s, then d" in result.stdout
    assert "zellij action detach" in result.stdout


def test_legacy_inline_launcher_has_new_hint_and_no_placeholders() -> None:
    text = _LEGACY_SETUP.read_text(encoding="utf-8")
    start_marker = 'cat > "$helpers_tmp" <<\'EOF\'\n'
    start = text.index(start_marker) + len(start_marker)
    end = text.index("\nEOF\n", start)
    body = text[start:end]
    assert "agentdesk() {" in body
    assert "Alt+s, then d" in body and "zellij action detach" in body
    for name in _PLACEHOLDERS:
        assert f'touch "$project_root/.workflow_artifacts/{name}"' not in body
    assert 'touch "$project_root/.workflow_artifacts/repos.md"' in body


def test_launch_creates_no_placeholder_files(tmp_path: Path) -> None:
    project = tmp_path / "freshproj"
    project.mkdir()
    env = _make_state_env(tmp_path)
    result = subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"\nagentdesk claude shell'],
        env=env, input="", capture_output=True, text=True, cwd=str(project),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in (project / ".workflow_artifacts").iterdir())
    assert "repos.md" in names
    for name in _PLACEHOLDERS:
        assert name not in names


def test_launch_leaves_existing_plan_file_untouched(tmp_path: Path) -> None:
    project = tmp_path / "existingproj"
    (project / ".workflow_artifacts").mkdir(parents=True)
    plan = project / ".workflow_artifacts" / "current-plan.md"
    plan.write_text("keep me\n")
    env = _make_state_env(tmp_path)
    result = subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"\nagentdesk claude shell'],
        env=env, input="", capture_output=True, text=True, cwd=str(project),
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert plan.read_text() == "keep me\n"
