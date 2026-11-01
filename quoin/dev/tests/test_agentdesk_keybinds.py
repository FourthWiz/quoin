"""Tests for manage_zellij_keybinds() in setup-agentdesk.sh (IVG-86, Path B).

Test suite covers:
  1. Source-text assertions: function defined, sentinel string present, Alt s bind present.
  2. Behavioral merge test: apply merge to a minimal config.kdl fixture and assert results.
  3. Idempotency: re-running merge on already-merged config produces identical output.
  4. Scope test: Alt s bind appears inside shared_except "locked" block.

All subprocess tests skip gracefully when bash is not available.
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "setup-agentdesk.sh"

BASH_AVAILABLE = shutil.which("bash") is not None
pytestmark = pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="bash not available on this system",
)

# Minimal config.kdl fixture containing the three conflicting binds.
FIXTURE_KDL = """\
keybinds clear-defaults=true {
    locked {
        bind "Ctrl g" { SwitchToMode "normal"; }
    }
    shared_except "locked" {
        bind "Alt left" { MoveFocusOrTab "left"; }
        bind "Alt down" { MoveFocus "down"; }
        bind "Alt up" { MoveFocus "up"; }
        bind "Alt right" { MoveFocusOrTab "right"; }
        bind "Alt f" { ToggleFloatingPanes; }
        bind "Ctrl g" { SwitchToMode "locked"; }
        bind "Alt h" { MoveFocusOrTab "left"; }
        bind "Alt n" { NewPane; }
        bind "Ctrl q" { Quit; }
    }
    shared_except "locked" "move" {
        bind "Ctrl h" { SwitchToMode "move"; }
    }
    shared_except "locked" "session" {
        bind "Ctrl o" { SwitchToMode "session"; }
    }
    shared_except "locked" "scroll" "search" {
        bind "Ctrl s" { SwitchToMode "scroll"; }
    }
}
"""


def _run_merge(config_path: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Source setup-agentdesk.sh (functions only) and run manage_zellij_keybinds on config_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    script = textwrap.dedent(f"""
        source "{SETUP_SH}"
        set +euo pipefail
        manage_zellij_keybinds "{config_path}"
    """).strip()

    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=60,
    )


# ── Source-text assertions ─────────────────────────────────────────────────────

def test_setup_sh_exists() -> None:
    """Sanity: setup-agentdesk.sh exists at expected path."""
    assert SETUP_SH.exists(), f"setup-agentdesk.sh not found at {SETUP_SH}"


def test_manage_zellij_keybinds_defined() -> None:
    """manage_zellij_keybinds function is defined in setup-agentdesk.sh."""
    src = SETUP_SH.read_text(encoding="utf-8")
    assert "manage_zellij_keybinds()" in src, (
        "manage_zellij_keybinds() function not found in setup-agentdesk.sh"
    )


def test_sentinel_string_in_awk_script() -> None:
    """The sentinel comment '// [agentdesk: freed]' appears in the awk script in setup-agentdesk.sh."""
    src = SETUP_SH.read_text(encoding="utf-8")
    assert "// [agentdesk: freed]" in src, (
        "Sentinel '// [agentdesk: freed]' not found in setup-agentdesk.sh"
    )


def test_alt_s_bind_in_awk_script() -> None:
    """The Alt s session-mode bind appears in the awk script in setup-agentdesk.sh."""
    src = SETUP_SH.read_text(encoding="utf-8")
    assert 'bind "Alt s" { SwitchToMode "session"; }' in src, (
        "Alt s session bind not found in setup-agentdesk.sh"
    )


def test_manage_fn_has_idempotency_guard() -> None:
    """manage_zellij_keybinds() contains an idempotency guard checking for the sentinel."""
    src = SETUP_SH.read_text(encoding="utf-8")
    # Find the function body
    fn_start = src.find("manage_zellij_keybinds()")
    assert fn_start != -1, "manage_zellij_keybinds() not found"
    # The sentinel check must appear after the function definition
    sentinel_check_pos = src.find("agentdesk: freed", fn_start)
    assert sentinel_check_pos != -1, (
        "Idempotency guard (check for sentinel) not found inside manage_zellij_keybinds()"
    )


def test_manage_fn_uses_write_if_changed() -> None:
    """manage_zellij_keybinds() calls write_if_changed for atomic update."""
    src = SETUP_SH.read_text(encoding="utf-8")
    fn_start = src.find("manage_zellij_keybinds()")
    assert fn_start != -1
    # Find function end (next top-level function or end of file)
    fn_end = src.find("\nmanage_", fn_start + 1)
    if fn_end == -1:
        fn_end = len(src)
    fn_body = src[fn_start:fn_end]
    assert "write_if_changed" in fn_body, (
        "manage_zellij_keybinds() must call write_if_changed"
    )


# ── Behavioral merge tests ─────────────────────────────────────────────────────

def test_merge_frees_alt_left(tmp_path: Path) -> None:
    """After merge, 'Alt left' bind is commented out with the sentinel."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    result = _run_merge(str(config), tmp_path)
    assert result.returncode == 0, f"merge failed (rc={result.returncode}):\n{result.stderr}"
    merged = config.read_text()
    # The original bind must be commented out
    assert '// [agentdesk: freed]' in merged, "Sentinel not found in merged config"
    # The uncommented Alt left bind must be gone
    uncommented_lines = [
        ln for ln in merged.splitlines()
        if 'bind "Alt left"' in ln and not ln.strip().startswith("//")
    ]
    assert not uncommented_lines, (
        f"Alt left bind is still uncommented after merge:\n"
        + "\n".join(uncommented_lines)
    )


def test_merge_frees_alt_right(tmp_path: Path) -> None:
    """After merge, 'Alt right' bind is commented out with the sentinel."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()
    uncommented_lines = [
        ln for ln in merged.splitlines()
        if 'bind "Alt right"' in ln and not ln.strip().startswith("//")
    ]
    assert not uncommented_lines, (
        f"Alt right bind is still uncommented after merge:\n"
        + "\n".join(uncommented_lines)
    )


def test_merge_frees_ctrl_o_session(tmp_path: Path) -> None:
    """After merge, 'Ctrl o' → SwitchToMode session bind is commented out."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()
    uncommented_lines = [
        ln for ln in merged.splitlines()
        if 'bind "Ctrl o"' in ln
        and 'SwitchToMode "session"' in ln
        and not ln.strip().startswith("//")
    ]
    assert not uncommented_lines, (
        f"Ctrl o → session bind is still uncommented after merge:\n"
        + "\n".join(uncommented_lines)
    )


def test_merge_adds_alt_s_bind(tmp_path: Path) -> None:
    """After merge, 'bind \"Alt s\" { SwitchToMode \"session\"; }' appears in config."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()
    assert 'bind "Alt s" { SwitchToMode "session"; }' in merged, (
        f"Alt s session bind not found in merged config:\n{merged}"
    )


def test_merge_preserves_other_binds(tmp_path: Path) -> None:
    """Merge does not remove unrelated binds (Alt f, Alt n, Ctrl q, Ctrl s, etc.)."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()
    for bind_str in [
        'bind "Alt f"',
        'bind "Alt n"',
        'bind "Ctrl q"',
        'bind "Ctrl s"',
        'bind "Ctrl h"',
        'bind "Alt h"',
        'bind "Alt down"',
        'bind "Alt up"',
    ]:
        uncommented = [
            ln for ln in merged.splitlines()
            if bind_str in ln and not ln.strip().startswith("//")
        ]
        assert uncommented, (
            f"Unrelated bind '{bind_str}' was incorrectly removed by merge"
        )


def test_merge_idempotent(tmp_path: Path) -> None:
    """Running merge twice produces identical output (idempotency guard fires on second run)."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)

    # First run
    r1 = _run_merge(str(config), tmp_path)
    assert r1.returncode == 0, f"First merge failed:\n{r1.stderr}"
    content_after_first = config.read_text()

    # Second run — idempotency guard should fire
    r2 = _run_merge(str(config), tmp_path)
    assert r2.returncode == 0, f"Second merge failed:\n{r2.stderr}"
    content_after_second = config.read_text()

    assert content_after_first == content_after_second, (
        "Merge is NOT idempotent: second run changed the config.\n"
        f"--- after first ---\n{content_after_first}\n"
        f"--- after second ---\n{content_after_second}"
    )
    # Also assert the sentinel message appears in second-run output
    combined = r2.stdout + r2.stderr
    assert "already applied" in combined or "Unchanged" in combined, (
        f"Second run should report idempotency skip; got:\n{combined}"
    )


# ── Scope test: Alt s must be inside shared_except "locked" block ─────────────

def test_alt_s_inside_shared_except_locked_block(tmp_path: Path) -> None:
    """Alt s bind appears INSIDE the shared_except \"locked\" block, not at the top level."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()

    # Parse: find shared_except "locked" { block (NOT with extra quoted args)
    lines = merged.splitlines()
    in_shared_locked = False
    depth = 0
    alt_s_found_in_block = False

    for ln in lines:
        stripped = ln.strip()
        # Header lines may carry a trailing comment; judge the code part only.
        code = stripped.split("//")[0].rstrip()

        # Detect entry into shared_except "locked" { (no extra quoted args)
        if (
            'shared_except "locked"' in code
            and code.endswith("{")
            and '"locked" "' not in code  # exclude shared_except "locked" "session" etc.
        ):
            in_shared_locked = True
            depth = 1
            continue

        if in_shared_locked:
            depth += stripped.count("{") - stripped.count("}")
            if 'bind "Alt s"' in stripped and 'SwitchToMode "session"' in stripped:
                alt_s_found_in_block = True
            if depth <= 0:
                in_shared_locked = False

    assert alt_s_found_in_block, (
        "bind \"Alt s\" { SwitchToMode \"session\"; } was NOT found inside "
        "the shared_except \"locked\" block.\n"
        f"Merged config:\n{merged}"
    )


def test_alt_s_not_at_top_level(tmp_path: Path) -> None:
    """Alt s bind does NOT appear at the top level of the keybinds block (must be nested)."""
    config = tmp_path / "config.kdl"
    config.write_text(FIXTURE_KDL)
    _run_merge(str(config), tmp_path)
    merged = config.read_text()

    # Lines at depth=1 (directly inside keybinds { }) should NOT contain Alt s
    lines = merged.splitlines()
    depth = 0
    top_level_alt_s = []

    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        # Count brace depth BEFORE processing this line for Alt s check
        opens = stripped.count("{")
        closes = stripped.count("}")
        depth += opens - closes
        # A line at depth == 1 is directly inside `keybinds { }` — too shallow for a bind
        if depth == 1 and 'bind "Alt s"' in stripped:
            top_level_alt_s.append(ln)

    assert not top_level_alt_s, (
        "Alt s bind found at top level of keybinds block (should be inside shared_except):\n"
        + "\n".join(top_level_alt_s)
    )


def test_missing_config_returns_zero(tmp_path: Path) -> None:
    """manage_zellij_keybinds with a non-existent config path returns 0 with a warning."""
    nonexistent = str(tmp_path / "does_not_exist.kdl")
    result = _run_merge(nonexistent, tmp_path)
    assert result.returncode == 0, (
        f"Missing config must return 0 (fail-open); rc={result.returncode}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Warning" in combined or "warning" in combined or "not found" in combined, (
        f"Missing config should emit a warning; output:\n{combined}"
    )
