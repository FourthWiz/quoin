"""Ghostty Option-as-Alt detection, opt-in write and Alt-delivery gating.

Every test runs with HOME and XDG_CONFIG_HOME under a temp dir, a pinned PATH and
AGENTDESK_GHOSTTY_BIN set, so the real ghostty binary is never executed (it reads
Application Support from the real home regardless of HOME).
"""
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import (
    BEGIN_PREFIX,
    SETUP_SH,
    fixture_text,
    make_bin,
    make_env,
    run_fn,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are required",
)

KEY_LINE = "macos-option-as-alt = left"
MARKER_LINE = "# [agentdesk: added] treat the left Option key as Alt for Zellij keybindings"


def dirs(tmp_path):
    home = Path(tmp_path) / "home"
    xdg = home / ".config" / "ghostty"
    app = home / "Library" / "Application Support" / "com.mitchellh.ghostty"
    return home, xdg, app


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def env_for(tmp_path, ghostty_bin="none", with_zellij=True):
    bindir = make_bin(tmp_path, with_zellij=with_zellij)
    env = make_env(tmp_path, bindir, {"AGENTDESK_GHOSTTY_BIN": ghostty_bin})
    return env


def stub_ghostty(tmp_path, output, log=None):
    path = Path(tmp_path) / "ghostty-stub"
    record = f'echo "$@" >> "{log}"\n' if log else ""
    path.write_text(f"#!/bin/sh\n{record}cat <<'OUT'\n{output}\nOUT\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def state(tmp_path, env=None):
    result = run_fn(tmp_path, "ghostty_option_as_alt_state", env=env or env_for(tmp_path))
    assert result.returncode == 0, result.stderr
    out = {"set_in": []}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "set_in":
            out["set_in"].append(value)
        else:
            out[key] = value
    return out


def run_script(tmp_path, *args, env=None):
    return subprocess.run(
        ["bash", str(SETUP_SH), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env or env_for(tmp_path),
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def zellij_config(tmp_path, text=None):
    cfg = Path(tmp_path) / "home" / ".config" / "zellij" / "config.kdl"
    put(cfg, text if text is not None else fixture_text("zellij-0.44.3-stock.kdl"))
    return cfg


def region_tier(cfg):
    text = cfg.read_text(encoding="utf-8")
    match = re.search(re.escape(BEGIN_PREFIX) + r"(\d)", text)
    return int(match.group(1)) if match else None


def tree(root):
    return sorted(str(p) for p in Path(root).rglob("*")) if Path(root).exists() else []


def test_env_helper_always_pins_the_ghostty_binary(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    assert env["AGENTDESK_GHOSTTY_BIN"] == "none"
    assert env["HOME"].startswith(str(tmp_path))
    assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    assert make_env(tmp_path)["AGENTDESK_GHOSTTY_BIN"] == "none"


# ── detection ─────────────────────────────────────────────────────────────────

def test_state_with_no_files(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    s = state(tmp_path)
    assert (s["value"], s["source"], s["set_in"]) == ("unset", "none", [])
    assert s["target"] == str(xdg / "config.ghostty")


@pytest.mark.parametrize("value", ["true", "false", "left", "right"])
def test_state_reads_each_value(tmp_path: Path, value: str) -> None:
    _, xdg, _ = dirs(tmp_path)
    cfg = put(xdg / "config", f"font-size = 14\nmacos-option-as-alt = {value}\n")
    s = state(tmp_path)
    assert s["value"] == value and s["source"] == "files"
    assert s["set_in"] == [f"{cfg}:2"]
    assert s["target"] == str(cfg)


def test_state_handles_quotes_and_case(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", 'macos-option-as-alt="Left"\n')
    assert state(tmp_path)["value"] == "left"


def test_state_unknown_value_is_ambiguous(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = maybe\n")
    assert state(tmp_path)["value"] == "ambiguous"


def test_state_empty_value_resets_to_unset(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = left\nmacos-option-as-alt =\n")
    s = state(tmp_path)
    assert s["value"] == "unset" and len(s["set_in"]) == 2


def test_application_support_true_beats_xdg_false(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = false\n")
    put(app / "config", "macos-option-as-alt = true\n")
    assert state(tmp_path)["value"] == "true"


def test_xdg_true_loses_to_application_support_false(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = true\n")
    put(app / "config", "macos-option-as-alt = false\n")
    assert state(tmp_path)["value"] == "false"


def test_xdg_config_ghostty_overrides_xdg_config(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = left\n")
    put(xdg / "config.ghostty", "macos-option-as-alt = right\n")
    assert state(tmp_path)["value"] == "right"


def test_value_only_in_xdg_config_ghostty(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    cfg = put(xdg / "config.ghostty", "macos-option-as-alt = left\n")
    s = state(tmp_path)
    assert s["value"] == "left" and s["set_in"] == [f"{cfg}:1"]


def test_value_only_in_application_support_config_ghostty(tmp_path: Path) -> None:
    _, _, app = dirs(tmp_path)
    cfg = put(app / "config.ghostty", "macos-option-as-alt = true\n")
    s = state(tmp_path)
    assert s["value"] == "true" and s["target"] == str(cfg)


def test_value_only_in_included_file(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    inc = put(xdg / "extra.conf", "macos-option-as-alt = left\n")
    put(xdg / "config", "config-file = extra.conf\n")
    s = state(tmp_path)
    assert s["value"] == "left" and s["set_in"] == [f"{inc}:1"]


def test_include_wins_over_assignment_in_the_including_file(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "extra.conf", "macos-option-as-alt = true\n")
    put(xdg / "config", "macos-option-as-alt = false\nconfig-file = extra.conf\n")
    assert state(tmp_path)["value"] == "true"


def test_xdg_include_loads_after_application_support_files(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    put(xdg / "extra.conf", "macos-option-as-alt = left\n")
    put(xdg / "config", "config-file = extra.conf\n")
    put(app / "config", "macos-option-as-alt = false\n")
    assert state(tmp_path)["value"] == "left"


def test_optional_and_missing_includes_are_skipped(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", 'macos-option-as-alt = right\nconfig-file = "?missing.conf"\n')
    assert state(tmp_path)["value"] == "right"


def test_nested_include_is_processed_after_sibling_includes(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "a.conf", "config-file = c.conf\n")
    put(xdg / "b.conf", "macos-option-as-alt = left\n")
    put(xdg / "c.conf", "macos-option-as-alt = false\n")
    put(xdg / "config", "config-file = a.conf\nconfig-file = b.conf\n")
    assert state(tmp_path)["value"] == "false"


def test_include_cycles_terminate(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "a.conf", "config-file = config\nmacos-option-as-alt = left\n")
    put(xdg / "config", "config-file = a.conf\n")
    assert state(tmp_path)["value"] == "left"


def test_append_target_is_the_last_existing_default_file(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    put(xdg / "config", "font-size = 14\n")
    empty = put(app / "config.ghostty", "")
    s = state(tmp_path)
    assert s["target"] == str(empty) and s["value"] == "unset"


def test_binary_value_wins_when_a_config_exists(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "font-size = 14\n")
    stub = stub_ghostty(tmp_path, "font-size = 14\nmacos-option-as-alt = left")
    s = state(tmp_path, env_for(tmp_path, stub))
    assert (s["value"], s["source"], s["set_in"]) == ("left", "binary", [])


def test_binary_is_not_run_when_no_config_file_exists(tmp_path: Path) -> None:
    home, xdg, app = dirs(tmp_path)
    log = Path(tmp_path) / "calls.log"
    stub = stub_ghostty(tmp_path, "macos-option-as-alt = left", log)
    env = env_for(tmp_path, stub)
    s = state(tmp_path, env)
    assert s["value"] == "unset" and s["source"] == "none"
    result = run_script(tmp_path, "--keybinds-only", env=env)
    assert result.returncode == 0, result.stderr
    assert not log.exists(), "ghostty binary must not run without a config file"
    assert tree(xdg) == [] and tree(app) == []


def test_failing_binary_falls_back_to_the_files(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = right\n")
    path = Path(tmp_path) / "bad-ghostty"
    path.write_text("#!/bin/sh\nexit 3\n")
    path.chmod(0o755)
    s = state(tmp_path, env_for(tmp_path, str(path)))
    assert (s["value"], s["source"]) == ("right", "files")


# ── end to end through --keybinds-only ────────────────────────────────────────

def test_nothing_set_non_interactive_changes_no_ghostty_file_and_stays_tier1(tmp_path: Path) -> None:
    home, xdg, app = dirs(tmp_path)
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only")
    assert result.returncode == 0, result.stderr
    assert tree(xdg) == [] and tree(app) == []
    assert "macos-option-as-alt" in result.stdout and "--ghostty-option-as-alt" in result.stdout
    assert region_tier(cfg) == 1
    for fallback in ("zellij action detach", "zellij kill-session NAME", "agentdesk-delete NAME"):
        assert fallback in result.stdout
    assert "--keybinds-only" in result.stdout


def test_opt_in_flag_appends_the_line_and_stays_tier1_until_rerun(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    original = "theme = dark\nfont-size = 15"
    cfg_g = put(xdg / "config", original)
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert result.returncode == 0, result.stderr
    text = cfg_g.read_text(encoding="utf-8")
    assert text == original + "\n" + MARKER_LINE + "\n" + KEY_LINE + "\n"
    backups = list(xdg.glob("config.agentdesk.*.bak"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    assert region_tier(cfg) == 1
    assert "cmd+shift+," in result.stdout
    assert "setup-agentdesk.sh" in result.stdout and "--keybinds-only" in result.stdout
    # After Ghostty reloads, the rerun sees the value and frees the Ctrl keys.
    again = run_script(tmp_path, "--keybinds-only")
    assert again.returncode == 0, again.stderr
    assert region_tier(cfg) == 2
    assert cfg_g.read_text(encoding="utf-8") == text


def test_opt_in_with_no_candidate_file_creates_xdg_config_ghostty(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert result.returncode == 0, result.stderr
    created = xdg / "config.ghostty"
    assert created.read_text(encoding="utf-8") == MARKER_LINE + "\n" + KEY_LINE + "\n"
    assert tree(app) == []


def test_opt_in_appends_to_an_empty_application_support_config_ghostty(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    target = put(app / "config.ghostty", "")
    zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == MARKER_LINE + "\n" + KEY_LINE + "\n"
    assert tree(xdg) == []


def test_existing_left_is_left_alone_and_gives_tier2(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    cfg_g = put(xdg / "config", "macos-option-as-alt = left\n")
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert result.returncode == 0, result.stderr
    assert cfg_g.read_text(encoding="utf-8") == "macos-option-as-alt = left\n"
    assert list(xdg.glob("*.bak")) == []
    assert region_tier(cfg) == 2


def test_explicit_false_keeps_tier1_and_explains(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = false\n")
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert result.returncode == 0, result.stderr
    assert region_tier(cfg) == 1
    assert "Alt keybindings will not reach Zellij" in result.stdout
    assert (xdg / "config").read_text(encoding="utf-8") == "macos-option-as-alt = false\n"


def test_right_gives_tier2_with_a_note(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = right\n")
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only")
    assert result.returncode == 0, result.stderr
    assert region_tier(cfg) == 2
    assert "only the right Option key sends Alt" in result.stdout


def test_binary_reporting_left_gives_tier2(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "font-size = 14\n")
    stub = stub_ghostty(tmp_path, "macos-option-as-alt = left")
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", env=env_for(tmp_path, stub))
    assert result.returncode == 0, result.stderr
    assert region_tier(cfg) == 2
    assert (xdg / "config").read_text(encoding="utf-8") == "font-size = 14\n"


def test_assume_alt_with_nothing_set_gives_tier2_without_touching_ghostty(tmp_path: Path) -> None:
    _, xdg, app = dirs(tmp_path)
    cfg = zellij_config(tmp_path)
    result = run_script(tmp_path, "--keybinds-only", "--assume-alt")
    assert result.returncode == 0, result.stderr
    assert region_tier(cfg) == 2
    assert tree(xdg) == [] and tree(app) == []


def test_missing_zellij_config_warns_and_exits_zero(tmp_path: Path) -> None:
    result = run_script(tmp_path, "--keybinds-only")
    assert result.returncode == 0
    assert "Zellij config not found" in result.stdout


# ── flags ─────────────────────────────────────────────────────────────────────

def test_unknown_flag_exits_2(tmp_path: Path) -> None:
    result = run_script(tmp_path, "--nope")
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_revert_flag_cannot_be_combined(tmp_path: Path) -> None:
    result = run_script(tmp_path, "--revert-keybinds", "--assume-alt")
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr


def test_help_lists_every_flag(tmp_path: Path) -> None:
    result = run_script(tmp_path, "--help")
    assert result.returncode == 0
    for flag in ("--keybinds-only", "--revert-keybinds", "--assume-alt", "--ghostty-option-as-alt"):
        assert flag in result.stdout


def test_sourcing_defines_functions_and_does_nothing_else(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    result = subprocess.run(
        ["bash", "-c", f'source "{SETUP_SH}"; declare -F manage_zellij_keybinds revert_zellij_keybinds'],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "manage_zellij_keybinds" in result.stdout
    assert result.stderr == ""
    assert not (Path(tmp_path) / "home" / ".zshrc").exists()
    assert not (Path(tmp_path) / "home" / ".config" / "agentdesk").exists()


def test_revert_flag_restores_the_config_and_names_the_ghostty_edit(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    original = fixture_text("zellij-0.44.3-stock.kdl")
    cfg = zellij_config(tmp_path, original)
    put(xdg / "config", "font-size = 15\n")
    first = run_script(tmp_path, "--keybinds-only", "--ghostty-option-as-alt")
    assert first.returncode == 0, first.stderr
    assert cfg.read_text(encoding="utf-8") != original
    result = run_script(tmp_path, "--revert-keybinds")
    assert result.returncode == 0, result.stderr
    assert cfg.read_text(encoding="utf-8") == original
    assert str(xdg / "config") in result.stdout
    assert "backup" in result.stdout


# ── interactive paths (pty) ───────────────────────────────────────────────────

def run_pty(tmp_path, env, args, answers):
    """Run the script on a pty; send each answer when its prompt text appears."""
    home = Path(env["HOME"]).resolve()
    assert str(home).startswith(str(Path(tmp_path).resolve()))
    assert str(Path(env["XDG_CONFIG_HOME"]).resolve()).startswith(str(Path(tmp_path).resolve()))
    assert env.get("AGENTDESK_GHOSTTY_BIN")
    assert env["PATH"].split(":")[0].startswith(str(Path(tmp_path)))
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(tmp_path))
        os.execvpe("bash", ["bash", str(SETUP_SH), *args], env)
    buf = ""
    pending = list(answers)
    deadline = time.time() + 120
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            if pending and pending[0][0] in buf:
                os.write(fd, pending.pop(0)[1].encode())
        else:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                pid = 0
                break
    if pid:
        _, status = os.waitpid(pid, 0)
    else:
        status = 0
    os.close(fd)
    assert not pending, f"prompts not seen: {[p[0] for p in pending]}\n{buf}"
    return buf


def test_interactive_yes_writes_ghostty_and_asks_nothing_about_ctrl(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    cfg = zellij_config(tmp_path)
    out = run_pty(tmp_path, env_for(tmp_path), ["--keybinds-only"], [("[y/N]:", "y\n")])
    written = (xdg / "config.ghostty").read_text(encoding="utf-8")
    assert written == MARKER_LINE + "\n" + KEY_LINE + "\n"
    assert "Free Ctrl t/b/g/s/p/n/h anyway?" not in out
    assert region_tier(cfg) == 1
    assert "cmd+shift+," in out


def test_interactive_ctrl_override_prompt_frees_the_keys(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = false\n")
    cfg = zellij_config(tmp_path)
    out = run_pty(tmp_path, env_for(tmp_path), ["--keybinds-only"], [("anyway?", "y\n")])
    assert region_tier(cfg) == 2
    assert "Ctrl t/b/g/s/p/n/h are free" in out


def test_interactive_no_keeps_tier1(tmp_path: Path) -> None:
    _, xdg, _ = dirs(tmp_path)
    put(xdg / "config", "macos-option-as-alt = false\n")
    cfg = zellij_config(tmp_path)
    run_pty(tmp_path, env_for(tmp_path), ["--keybinds-only"], [("anyway?", "n\n")])
    assert region_tier(cfg) == 1
