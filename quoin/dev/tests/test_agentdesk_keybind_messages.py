"""Outcome text printed by run_keybind_steps must describe the resulting
Zellij config, not the tier that was requested."""
import shutil
import subprocess
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import (
    SETUP_SH,
    fixture_text,
    make_bin,
    make_env,
    place_config,
    read_exact,
    run_fn,
)

pytestmark = pytest.mark.skipif(
    shutil.which("python3") is None, reason="python3 required for the key map merge"
)

STOCK = "zellij-0.44.3-stock.kdl"
V1 = "user-v1-merged.kdl"
CTRL_O_NOTE = "Ctrl o (session mode) was already freed"
TIER1_HEAD = "only Ctrl q was removed"
TIER2_HEAD = "Ctrl t/b/g/s/p/n/h are free for your programs"
NOT_APPLIED = "Keybindings: not applied"


def steps(tmp_path: Path, text: str, assume_alt: bool, env=None):
    cfg = place_config(tmp_path, text)
    body = "ASSUME_ALT=1\nrun_keybind_steps" if assume_alt else "run_keybind_steps"
    result = run_fn(tmp_path, body, env=env)
    assert result.returncode == 0, result.stderr
    return cfg, result.stdout


def test_normal_tier1_message(tmp_path: Path) -> None:
    _, out = steps(tmp_path, fixture_text(STOCK), assume_alt=False)
    assert TIER1_HEAD in out
    assert TIER2_HEAD not in out
    assert NOT_APPLIED not in out
    assert CTRL_O_NOTE not in out


def test_normal_tier2_message(tmp_path: Path) -> None:
    _, out = steps(tmp_path, fixture_text(STOCK), assume_alt=True)
    assert TIER2_HEAD in out
    assert TIER1_HEAD not in out
    assert NOT_APPLIED not in out
    assert "Quit: Alt s, then q." in out


def test_kept_tier2_config_without_alt_says_ctrl_keys_stay_freed(tmp_path: Path) -> None:
    cfg, _ = steps(tmp_path, fixture_text(STOCK), assume_alt=True)
    after_first = read_exact(cfg)
    result = run_fn(tmp_path, "run_keybind_steps")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert read_exact(cfg) == after_first
    assert TIER1_HEAD not in out
    assert "stay bound to Zellij modes" not in out
    assert "remain freed" in out
    assert "--revert-keybinds" in out


@pytest.mark.parametrize(
    "text",
    [
        'keybinds {\n    normal {\n        bind "F12" { Quit; }\n    }\n}\nkeybinds {\n}\n',
        'keybinds {\n    normal {\n}\n',
    ],
    ids=["two-keybinds-nodes", "unbalanced-braces"],
)
@pytest.mark.parametrize("assume_alt", [False, True])
def test_refused_merge_reports_not_applied(tmp_path: Path, text: str, assume_alt: bool) -> None:
    cfg, out = steps(tmp_path, text, assume_alt=assume_alt)
    assert read_exact(cfg) == text
    assert NOT_APPLIED in out
    assert "unchanged" in out
    assert TIER2_HEAD not in out
    assert TIER1_HEAD not in out


def test_failed_merge_with_failing_python_reports_not_applied(tmp_path: Path) -> None:
    bindir = make_bin(tmp_path, with_zellij=False, with_python=False)
    stub = bindir / "python3"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    env = make_env(tmp_path, bindir)
    text = fixture_text(STOCK)
    cfg, out = steps(tmp_path, text, assume_alt=True, env=env)
    assert read_exact(cfg) == text
    assert NOT_APPLIED in out
    assert TIER2_HEAD not in out


def test_missing_config_reports_not_applied(tmp_path: Path) -> None:
    result = run_fn(tmp_path, "ASSUME_ALT=1\nrun_keybind_steps")
    assert result.returncode == 0, result.stderr
    assert NOT_APPLIED in result.stdout
    assert TIER2_HEAD not in result.stdout


def test_tier2_request_on_kept_tier1_refusal_is_not_reported_as_confirmed(tmp_path: Path) -> None:
    # A tier 1 region sits in a file that now cannot be merged any further.
    cfg, _ = steps(tmp_path, fixture_text(STOCK), assume_alt=False)
    broken = read_exact(cfg) + 'keybinds {\n}\n'
    cfg.write_text(broken, encoding="utf-8", newline="")
    result = run_fn(tmp_path, "ASSUME_ALT=1\nrun_keybind_steps")
    assert result.returncode == 0, result.stderr
    assert read_exact(cfg) == broken
    assert NOT_APPLIED in result.stdout
    assert TIER2_HEAD not in result.stdout


def test_clear_defaults_tier1_mentions_earlier_ctrl_o_and_option_arrows(tmp_path: Path) -> None:
    _, out = steps(tmp_path, fixture_text(V1), assume_alt=False)
    assert TIER1_HEAD in out
    assert CTRL_O_NOTE in out
    assert "Alt Left and Alt Right" in out


def test_ctrl_o_note_absent_when_no_earlier_comment_out(tmp_path: Path) -> None:
    _, out = steps(tmp_path, fixture_text("custom-no-clear-defaults.kdl"), assume_alt=False)
    assert TIER1_HEAD in out
    assert CTRL_O_NOTE not in out


def test_keybind_verify_has_ctrl_o_sentence() -> None:
    doc = (SETUP_SH.parent / "KEYBIND-VERIFY.md").read_text(encoding="utf-8")
    section = doc.split("## Without working Alt keys", 1)[1].split("\n## ", 1)[0]
    assert "`Ctrl o`" in section and "Alt s" in section


def test_stdin_execution_survives_unset_bash_source(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", "-s", "--", "--help"],
        input=SETUP_SH.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=60,
    )
    assert "unbound variable" not in result.stderr
    assert result.returncode == 0, result.stderr
