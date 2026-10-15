"""Behavioural tests for the Agent Desk Zellij key map merge.

The merge is a marked region appended inside the single ``keybinds`` node, with
two tiers: tier 1 (Alt delivery not confirmed) removes only Ctrl q, tier 2 also
frees the Ctrl mode keys. Tests evaluate the merged file with a small bind
evaluator, check idempotency and byte-exact revert, and cover refusals.
"""
import ast
import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import (
    BEGIN_PREFIX,
    END_MARKER,
    SETUP_SH,
    evaluate,
    fixture_text,
    has_quit,
    make_bin,
    make_env,
    merge,
    norm_key,
    parse_kdl,
    place_config,
    read_exact,
    revert,
    run_fn,
    switch_target,
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are required",
)

STOCK = "zellij-0.44.3-stock.kdl"
PRE_V1 = "user-pre-v1.kdl"
V1 = "user-v1-merged.kdl"
V1_TWICE = "zellij-0.44.3-stock-after-two-v1-runs.kdl"
CUSTOM = "custom-no-clear-defaults.kdl"
MINIMAL = "minimal-no-keybinds.kdl"
ALL_FIXTURES = [STOCK, PRE_V1, V1, V1_TWICE, CUSTOM, MINIMAL]

CTRL_MODE_KEYS = {
    "ctrl t": "tab", "ctrl b": "tmux", "ctrl g": "locked", "ctrl s": "scroll",
    "ctrl p": "pane", "ctrl n": "resize", "ctrl h": "move",
}
ALT_S = 'bind "Alt s" { SwitchToMode "session"; }'


def stock_text():
    return fixture_text(STOCK)


def engine_map(tmp_path):
    result = run_fn(tmp_path, "_agentdesk_keybinds_engine map")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def active_lines(text, needle):
    return [
        ln for ln in text.splitlines()
        if needle in ln and not ln.strip().startswith("//")
    ]


# ── evaluator sanity ───────────────────────────────────────────────────────────

def test_evaluator_reads_stock_defaults() -> None:
    binds = evaluate(stock_text(), stock_text())
    assert switch_target(binds["normal"]["ctrl t"]) == "tab"
    assert has_quit(binds["normal"]["ctrl q"])
    assert "ctrl q" not in binds["locked"]


def test_evaluator_applies_unbind_in_file_order() -> None:
    text = 'keybinds clear-defaults=true {\n  normal { bind "F1" { Quit; } unbind "F1" }\n}\n'
    assert evaluate(text, stock_text())["normal"] == {}


# ── tier 2: everything freed ──────────────────────────────────────────────────

@pytest.mark.parametrize("name", [STOCK, PRE_V1, V1, V1_TWICE, CUSTOM, MINIMAL])
def test_tier2_frees_ctrl_keys_and_adds_alt_modes(tmp_path: Path, name: str) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), 2)
    binds = evaluate(merged, stock_text())
    for key in list(CTRL_MODE_KEYS) + ["ctrl q", "ctrl o"]:
        assert key not in binds["normal"], f"{key} still bound in normal ({name})"
    # Every mode is reachable from normal through some key.
    for mode in ["locked", "pane", "tab", "resize", "move", "scroll", "session", "tmux"]:
        targets = {switch_target(a) for a in binds["normal"].values()}
        assert mode in targets, f"no normal-mode key enters {mode} ({name})"
    assert switch_target(binds["locked"]["ctrl g"]) == "normal"
    assert switch_target(binds["locked"]["alt g"]) == "normal"


@pytest.mark.parametrize("name", [STOCK, PRE_V1, MINIMAL])
def test_tier2_frees_alt_p_and_alt_o_without_cyrillic_twins(tmp_path: Path, name: str) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), 2)
    binds = evaluate(merged, stock_text())
    for key in ("alt p", "alt o", "alt left", "alt right"):
        assert key not in binds["normal"], f"{key} still bound in normal ({name})"
    for key in ("alt щ", "alt з"):
        assert key not in binds["normal"], f"{key} must not be re-bound ({name})"
    # The neighbours stay available.
    assert "alt h" in binds["normal"]


def test_tier2_keeps_ctrl_keys_inside_other_modes(tmp_path: Path) -> None:
    merged, _ = merge(tmp_path, stock_text(), 2)
    binds = evaluate(merged, stock_text())
    assert switch_target(binds["pane"]["ctrl p"]) == "normal"
    assert switch_target(binds["tab"]["ctrl t"]) == "normal"


def test_alt_mode_keys_enter_and_leave_each_mode(tmp_path: Path) -> None:
    merged, _ = merge(tmp_path, stock_text(), 2)
    binds = evaluate(merged, stock_text())
    km = engine_map(tmp_path)
    for mode, latin, cyr in km["modes"]:
        for key in (latin, cyr):
            k = norm_key("Alt " + key)
            assert switch_target(binds["normal"][k]) == mode
            assert switch_target(binds[mode][k]) == "normal"
    _, latin, cyr = km["locked"]
    for key in (latin, cyr):
        k = norm_key("Alt " + key)
        assert switch_target(binds["normal"][k]) == "locked"
        assert switch_target(binds["locked"][k]) == "normal"
    assert switch_target(binds["locked"][norm_key("Ctrl " + km["locked"][2])]) == "normal"


def test_russian_aliases_for_stock_alt_keys(tmp_path: Path) -> None:
    merged, _ = merge(tmp_path, stock_text(), 2)
    binds = evaluate(merged, stock_text())
    km = engine_map(tmp_path)
    for latin, cyr, _action in km["aliases"]:
        assert binds["normal"][norm_key("Alt " + cyr)] == binds["normal"][norm_key("Alt " + latin)], latin
    assert cyr_set(km).isdisjoint({"щ", "з"})


def cyr_set(km):
    out = {m[2] for m in km["modes"]} | {km["locked"][2]} | {a[1] for a in km["aliases"]}
    return out


# ── quit: only Alt s then q ───────────────────────────────────────────────────

@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_only_quit_left_is_in_session_mode(tmp_path: Path, name: str, tier: int) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), tier)
    binds = evaluate(merged, stock_text())
    for mode, table in binds.items():
        for key, action in table.items():
            if has_quit(action):
                assert mode == "session" and key in ("q", "й"), (mode, key, name, tier)
    assert has_quit(binds["session"]["q"]) and has_quit(binds["session"]["й"])


# ── tier 1: Ctrl keys stay ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_tier1_keeps_ctrl_mode_keys_and_adds_alt_keys(tmp_path: Path, name: str) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), 1)
    binds = evaluate(merged, stock_text())
    for key, mode in CTRL_MODE_KEYS.items():
        assert switch_target(binds["normal"][key]) == mode, (key, name)
    assert "ctrl q" not in binds["normal"]
    assert switch_target(binds["normal"]["alt a"]) == "pane"
    assert switch_target(binds["normal"]["alt s"]) == "session"
    assert "alt p" in binds["normal"]


@pytest.mark.parametrize("name", [STOCK, MINIMAL, CUSTOM])
def test_tier1_keeps_ctrl_o_and_alt_arrows_on_unmerged_shapes(tmp_path: Path, name: str) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), 1)
    binds = evaluate(merged, stock_text())
    assert switch_target(binds["normal"]["ctrl o"]) == "session"
    assert "alt left" in binds["normal"] and "alt right" in binds["normal"]
    assert "alt p" in binds["normal"] and "alt o" in binds["normal"]


# ── idempotency, upgrades, markers ─────────────────────────────────────────────

@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_second_apply_is_byte_identical(tmp_path: Path, name: str, tier: int) -> None:
    first, _ = merge(tmp_path, fixture_text(name), tier)
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    backups = sorted(cfg.parent.glob("config.kdl.keybinds.*.bak"))
    assert len(backups) == 1
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" {tier}')
    assert result.returncode == 0
    assert read_exact(cfg) == first
    assert result.stdout == f"Unchanged: {cfg}\n"
    assert sorted(cfg.parent.glob("config.kdl.keybinds.*.bak")) == backups


def test_v1_config_upgrades_to_one_alt_s_bind(tmp_path: Path) -> None:
    original = fixture_text(V1)
    old_freed = [ln for ln in original.splitlines() if ln.strip().startswith("// [agentdesk: freed] ")]
    assert old_freed
    merged, _ = merge(tmp_path, original, 1)
    assert len(active_lines(merged, ALT_S)) == 1
    assert not [ln for ln in merged.splitlines() if ln.strip() == "// [agentdesk: added]"]
    now_freed = [ln for ln in merged.splitlines() if ln.strip().startswith("// [agentdesk: freed] ")]
    assert now_freed == old_freed


def test_stock_after_two_old_runs_upgrades_and_reverts_exactly(tmp_path: Path) -> None:
    original = fixture_text(V1_TWICE)
    assert original.count("// [agentdesk: added]") == 2
    merged, _ = merge(tmp_path, original, 2)
    assert len(active_lines(merged, ALT_S)) == 1
    assert not [ln for ln in merged.splitlines() if ln.strip() == "// [agentdesk: added]"]
    reverted, _ = revert(tmp_path, merged)
    assert reverted == stock_text()


def test_tier1_then_tier2_replaces_region_in_place(tmp_path: Path) -> None:
    first, _ = merge(tmp_path, stock_text(), 1)
    assert first.count(BEGIN_PREFIX) == 1 and BEGIN_PREFIX + "1" in first
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2')
    assert result.returncode == 0
    second = read_exact(cfg)
    assert second.count(BEGIN_PREFIX) == 1 and BEGIN_PREFIX + "2" in second
    assert second.count(END_MARKER) == 1


def test_tier2_then_tier1_request_keeps_the_file(tmp_path: Path) -> None:
    first, _ = merge(tmp_path, stock_text(), 2)
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 1')
    assert result.returncode == 0
    assert read_exact(cfg) == first


def test_tier1_then_tier2_on_config_without_keybinds_node_round_trips(tmp_path: Path) -> None:
    original = fixture_text(MINIMAL)
    first, _ = merge(tmp_path, original, 1)
    assert BEGIN_PREFIX + "1 appended" in first
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2')
    assert result.returncode == 0
    second = read_exact(cfg)
    assert second.count(BEGIN_PREFIX) == 1 and BEGIN_PREFIX + "2 appended" in second
    assert len(re.findall(r"^keybinds\b", second, re.M)) == 1
    reverted, _ = revert(tmp_path, second)
    assert reverted == original


@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_added_or_changed_line_is_marked(tmp_path: Path, name: str, tier: int) -> None:
    original = fixture_text(name)
    merged, _ = merge(tmp_path, original, tier)
    a, b = original.splitlines(), merged.splitlines()
    unmarked = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            unmarked += [ln for ln in b[j1:j2] if "[agentdesk:" not in ln]
    # The blank separator before an appended node is the only unmarked line.
    allowed = [""] if name == MINIMAL else []
    assert unmarked == allowed


def test_a_backup_is_made_before_the_first_change(tmp_path: Path) -> None:
    original = stock_text()
    merge(tmp_path, original, 1)
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    backups = list(cfg.parent.glob("config.kdl.keybinds.*.bak"))
    assert len(backups) == 1 and read_exact(backups[0]) == original


# ── revert ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("name", [STOCK, MINIMAL, CUSTOM, PRE_V1])
def test_revert_restores_the_original_exactly(tmp_path: Path, name: str, tier: int) -> None:
    original = fixture_text(name)
    merged, _ = merge(tmp_path, original, tier)
    assert merged != original
    reverted, _ = revert(tmp_path, merged)
    assert reverted == original


def test_revert_of_v1_config_gives_the_pre_v1_config(tmp_path: Path) -> None:
    reverted, _ = revert(tmp_path, fixture_text(V1))
    assert reverted == fixture_text(PRE_V1)


@pytest.mark.parametrize(
    "original",
    [
        'copy_command "pbcopy"\ncopy_on_select true\n',
        'copy_command "pbcopy"\ncopy_on_select true',
        'copy_command "pbcopy"\n\n',
        "",
    ],
    ids=["newline", "no-trailing-newline", "trailing-blank-line", "empty"],
)
@pytest.mark.parametrize("tier", [1, 2])
def test_revert_round_trip_for_configs_without_keybinds(tmp_path: Path, original: str, tier: int) -> None:
    merged, _ = merge(tmp_path, original, tier)
    assert merged != original
    reverted, _ = revert(tmp_path, merged)
    assert reverted == original


def test_revert_of_an_untouched_file_is_unchanged(tmp_path: Path) -> None:
    cfg = place_config(tmp_path, stock_text())
    result = run_fn(tmp_path, f'revert_zellij_keybinds "{cfg}"')
    assert result.returncode == 0
    assert result.stdout == f"Unchanged: {cfg}\n"
    assert read_exact(cfg) == stock_text()


# ── shapes ────────────────────────────────────────────────────────────────────

def test_config_without_keybinds_node_gains_exactly_one(tmp_path: Path) -> None:
    merged, _ = merge(tmp_path, fixture_text(MINIMAL), 2)
    assert len(re.findall(r"^keybinds\b", merged, re.M)) == 1


@pytest.mark.parametrize("name", [STOCK, PRE_V1, V1, V1_TWICE, CUSTOM])
def test_existing_keybinds_node_never_gains_a_second(tmp_path: Path, name: str) -> None:
    merged, _ = merge(tmp_path, fixture_text(name), 2)
    assert len(re.findall(r"^keybinds\b", merged, re.M)) == 1
    nodes = [n for n in parse_kdl(merged) if n["name"] == "keybinds"]
    assert len(nodes) == 1


def test_custom_user_binds_survive(tmp_path: Path) -> None:
    merged, _ = merge(tmp_path, fixture_text(CUSTOM), 2)
    binds = evaluate(merged, stock_text())
    assert "f12" in binds["normal"]


# ── refusals ──────────────────────────────────────────────────────────────────

REFUSE_CASES = {
    "two-keybinds-nodes": 'keybinds {\n  normal {\n  }\n}\nkeybinds {\n  locked {\n  }\n}\n',
    "closing-brace-shares-line": 'keybinds {\n    normal {\n        bind "F12" { Quit; }\n    } }\n',
    "begin-without-end": 'keybinds {\n    normal {\n    }\n    // [agentdesk: added] keybinds v2 begin tier1\n}\n',
    "unbalanced-braces": 'keybinds {\n    normal {\n}\n',
    "end-without-begin": 'keybinds {\n    normal {\n    }\n    // [agentdesk: added] keybinds v2 end\n}\n',
}


@pytest.mark.parametrize("case", sorted(REFUSE_CASES))
def test_ambiguous_configs_are_left_untouched(tmp_path: Path, case: str) -> None:
    original = REFUSE_CASES[case]
    cfg = place_config(tmp_path, original)
    for body in (f'manage_zellij_keybinds "{cfg}" 2', f'revert_zellij_keybinds "{cfg}"'):
        result = run_fn(tmp_path, body)
        assert result.returncode == 0, result.stderr
        assert read_exact(cfg) == original
        if body.startswith("manage"):
            assert "Warning" in result.stdout + result.stderr
    assert not list(cfg.parent.glob("config.kdl.keybinds.*.bak"))


def _stub(bindir: Path, name: str, script: str) -> None:
    path = bindir / name
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_failing_zellij_parse_check_keeps_the_config(tmp_path: Path) -> None:
    bindir = make_bin(tmp_path, with_zellij=False)
    _stub(bindir, "zellij", 'echo "bad bind on line 4" >&2\nexit 1\n')
    cfg = place_config(tmp_path, stock_text())
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2', env=make_env(tmp_path, bindir))
    assert result.returncode == 0
    assert read_exact(cfg) == stock_text()
    assert "bad bind on line 4" in result.stderr
    assert "did not parse" in result.stderr
    assert not list(cfg.parent.glob("*.bak"))


def test_noisy_successful_parse_check_stays_silent(tmp_path: Path) -> None:
    bindir = make_bin(tmp_path, with_zellij=False)
    _stub(bindir, "zellij", 'echo "noise on stdout"\necho "noise on stderr" >&2\nexit 0\n')
    env = make_env(tmp_path, bindir)
    cfg = place_config(tmp_path, stock_text())
    first = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2', env=env)
    assert first.returncode == 0
    assert "noise" not in first.stdout + first.stderr
    second = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2', env=env)
    assert second.stdout == f"Unchanged: {cfg}\n"
    assert second.stderr == ""


def test_missing_or_failing_python_leaves_the_config_alone(tmp_path: Path) -> None:
    bindir = make_bin(tmp_path, with_zellij=False, with_python=False)
    _stub(bindir, "python3", "exit 1\n")
    cfg = place_config(tmp_path, stock_text())
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" 2', env=make_env(tmp_path, bindir))
    assert result.returncode == 0
    assert read_exact(cfg) == stock_text()
    assert "Warning" in result.stderr and "KEYBIND-VERIFY.md" in result.stderr
    assert not list(cfg.parent.glob("*.bak"))
    result = run_fn(tmp_path, f'revert_zellij_keybinds "{cfg}"', env=make_env(tmp_path, bindir))
    assert result.returncode == 0 and read_exact(cfg) == stock_text()


def test_missing_config_returns_zero_for_revert_too(tmp_path: Path) -> None:
    result = run_fn(tmp_path, f'revert_zellij_keybinds "{tmp_path}/none.kdl"')
    assert result.returncode == 0
    assert "Warning" in result.stdout + result.stderr


# ── engine source guards ──────────────────────────────────────────────────────

def _engine_source():
    text = SETUP_SH.read_text(encoding="utf-8")
    start_marker = "_agentdesk_keybinds_engine() {\n  python3 - \"$@\" <<'PY'\n"
    start = text.index(start_marker) + len(start_marker)
    end = text.index("\nPY\n", start)
    return text[start:end]


def test_engine_source_is_captured() -> None:
    src = _engine_source()
    assert "# agentdesk key map" in src and "def apply(" in src and "def revert(" in src


def test_engine_parses_as_python_38() -> None:
    src = _engine_source()
    ast.parse(src, feature_version=(3, 8))
    for banned in ("removeprefix", "removesuffix", "list[", "dict[", "tuple[", " | None", "math.lcm", "match "):
        assert banned not in src, banned


def test_engine_runs_under_python_38_when_available(tmp_path: Path) -> None:
    py38 = shutil.which("python3.8")
    if not py38:
        pytest.skip("python3.8 not on PATH")
    probe = subprocess.run([py38, "--version"], capture_output=True, text=True)
    if probe.returncode != 0 or "3.8" not in probe.stdout + probe.stderr:
        pytest.skip("python3.8 shim is not a working 3.8 interpreter")
    script = tmp_path / "engine.py"
    script.write_text(_engine_source(), encoding="utf-8")
    src = place_config(tmp_path, stock_text())
    out = tmp_path / "out.kdl"
    env = make_env(tmp_path)
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    done = subprocess.run([py38, str(script), "apply", str(src), "2", str(out)], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert BEGIN_PREFIX + "2" in out.read_text(encoding="utf-8")


def test_setup_mentions_alt_s_bind_verbatim_once() -> None:
    assert SETUP_SH.read_text(encoding="utf-8").count(ALT_S) == 1


# ── Zellij accepts every merged and reverted file ─────────────────────────────

@pytest.mark.skipif(shutil.which("zellij") is None, reason="zellij not installed")
@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_zellij_parses_merged_and_reverted_files(tmp_path: Path, name: str, tier: int) -> None:
    env = make_env(tmp_path, make_bin(tmp_path))
    merged, _ = merge(tmp_path, fixture_text(name), tier, env=env)
    cfg = tmp_path / "home" / ".config" / "zellij" / "config.kdl"
    for text in (merged, revert(tmp_path, merged, env=env)[0]):
        cfg.write_text(text, encoding="utf-8", newline="")
        done = subprocess.run(
            ["zellij", "--config", str(cfg), "setup", "--check"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr


# ── KEYBIND-VERIFY.md stays in step with the key map ─────────────────────────

VERIFY_MD = SETUP_SH.parent / "KEYBIND-VERIFY.md"


def test_keybind_verify_lists_every_map_entry(tmp_path: Path) -> None:
    doc = VERIFY_MD.read_text(encoding="utf-8")
    km = engine_map(tmp_path)
    for entry in km["modes"] + [km["locked"]]:
        mode, latin, cyr = entry
        assert f"Alt {latin}" in doc and f"Alt {cyr}" in doc, mode
    for latin, cyr, _action in km["aliases"]:
        assert f"Alt {latin}" in doc and f"Alt {cyr}" in doc, latin
    for key in km["ctrl_freed"] + km["extra_freed"]:
        assert f"`{key}`" in doc, key
    for needle in ("Ctrl q", "--revert-keybinds", "macos-option-as-alt", "--keybinds-only", "--assume-alt"):
        assert needle in doc, needle


def test_keybind_verify_keeps_old_checks_and_drops_stale_notes() -> None:
    doc = VERIFY_MD.read_text(encoding="utf-8")
    assert "Option+Left" in doc and "Ctrl o" in doc
    assert "Ghostty does NOT need changes" not in doc
    assert "known collision" not in doc.lower()
    assert "## Layout checks" not in doc
    assert "zellij --config ~/.config/zellij/config.kdl setup --check" in doc
    assert "\nzellij setup --check" not in doc
