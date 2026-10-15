"""Structure tests for the generated Agent Desk layout.

The layout is read back through the small KDL reader used by the key-map
tests, so assertions look at the pane tree rather than counting substrings.
Everything runs in an isolated HOME with a pinned PATH; nothing touches the
real home or a real Zellij session (the parse checks only read the layout).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import parse_kdl

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_ZSH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"

pytestmark = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh not available on this system"
)

ZELLIJ = shutil.which("zellij")
needs_zellij = pytest.mark.skipif(not ZELLIJ, reason="zellij not on PATH")

EMITTERS = [
    "_agentdesk_kdl_header default",
    "_agentdesk_kdl_header compact",
    "_agentdesk_kdl_tab_open default demo",
    "_agentdesk_kdl_tab_open compact demo 'hide_floating_panes=true'",
    "_agentdesk_kdl_tab_close default",
    "_agentdesk_kdl_tab_close compact",
    "_agentdesk_kdl_pane 8 Demo shell 'size=\"25%\"'",
    "_agentdesk_kdl_spend_floating",
    "_agentdesk_kdl_agents_tab default claude claude shell",
    "_agentdesk_kdl_agents_tab compact claude shell status",
    "_agentdesk_kdl_swap_layouts default",
    "_agentdesk_kdl_swap_layouts compact",
    "_agentdesk_kdl_footer",
    "_agentdesk_pane_names claude claude shell shell spend",
]


def agentdesk_env(tmp_path: Path) -> dict:
    """Allowlisted environment: nothing from the developer's shell leaks in."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stub = tmp_path / "stubbin"
    stub.mkdir(exist_ok=True)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{stub}:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "SHELL": "/bin/sh",
        "TMPDIR": str(tmpdir),
    }


def run_fn(tmp_path: Path, body: str, env: dict | None = None):
    return subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"; {body}'],
        env=env if env is not None else agentdesk_env(tmp_path),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )


def gen_layout_file(tmp_path: Path, tokens: str, bars: str | None = None) -> Path:
    flag = f"--bars {bars} " if bars else ""
    done = run_fn(tmp_path, f"_agentdesk_gen_layout {flag}{tokens}")
    assert done.returncode == 0, done.stderr
    path = Path(done.stdout.strip())
    assert path.suffix == ".kdl" and path.exists()
    return path


def gen_text(tmp_path: Path, tokens: str, bars: str | None = None) -> str:
    return gen_layout_file(tmp_path, tokens, bars).read_text()


# ── KDL reading helpers ────────────────────────────────────────────────────────

def props(node: dict) -> dict:
    """Properties of a node as a dict (the reader yields 'key=' then value)."""
    out = {}
    args = node["args"]
    i = 0
    while i < len(args):
        a = args[i]
        if a.endswith("=") and i + 1 < len(args):
            out[a[:-1]] = args[i + 1]
            i += 2
        elif "=" in a:
            k, v = a.split("=", 1)
            out[k] = v
            i += 1
        else:
            i += 1
    return out


def layout_node(text: str) -> dict:
    return parse_kdl(text)[0]


def child(node: dict, name: str) -> list:
    return [c for c in node["children"] if c["name"] == name]


def tabs(layout: dict) -> list:
    return child(layout, "tab")


def main_tab(layout: dict) -> dict:
    found = [t for t in tabs(layout) if props(t).get("name") == "main"]
    assert len(found) == 1
    return found[0]


def is_bar(node: dict) -> bool:
    return node["name"] == "pane" and any(c["name"] == "plugin" for c in node["children"])


def bar_plugins(tab: dict) -> list:
    return [c["children"][0]["args"][-1] for c in child(tab, "pane") if is_bar(c)]


def tiled_root(tab: dict) -> dict:
    """The horizontal container that holds every tiled pane of the tab."""
    roots = [c for c in child(tab, "pane") if not is_bar(c)]
    assert len(roots) == 1
    assert props(roots[0]).get("split_direction") == "horizontal"
    return roots[0]


def panes_of(node: dict) -> list:
    return child(node, "pane")


def leaf_name(node: dict) -> str:
    return props(node)["name"]


def all_leaves(node: dict) -> list:
    subs = panes_of(node)
    if not subs:
        return [node] if "name" in props(node) else []
    out = []
    for s in subs:
        out.extend(all_leaves(s))
    return out


def names_in(node: dict) -> list:
    return [leaf_name(n) for n in all_leaves(node)]


def floating_names(tab: dict) -> list:
    out = []
    for block in child(tab, "floating_panes"):
        out.extend(leaf_name(p) for p in panes_of(block))
    return out


def assert_text_clean(text: str) -> None:
    lines = text.rstrip("\n").split("\n")
    assert lines[-1] == "}"
    assert text.count("{") == text.count("}")
    for ln in lines:
        stripped = ln.strip()
        assert not re.match(r"^(name|cmd|t|i)=", stripped), f"typeset leak: {ln!r}"
    for ln in lines:
        if 'args "-lc"' in ln:
            assert '"$HOME' not in ln.replace('\\"$HOME', "")
            assert '"$PROJECT_ROOT' not in ln.replace('\\"$PROJECT_ROOT', "")


def parse_check(tmp_path: Path, layout: Path):
    env = agentdesk_env(tmp_path)
    env["PATH"] = f"{Path(ZELLIJ).parent}:{env['PATH']}"
    return subprocess.run(
        [ZELLIJ, "--layout", str(layout), "setup", "--check"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )


# ── pane names ─────────────────────────────────────────────────────────────────

def names_for(tmp_path: Path, tokens: str) -> list:
    done = run_fn(tmp_path, f"_agentdesk_pane_names {tokens}")
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.mark.parametrize(
    "tokens,expected",
    [
        ("claude claude shell", ["Claude · A", "Claude · B", "Shell"]),
        ("codex codex shell", ["Codex · A", "Codex · B", "Shell"]),
        ("shell shell", ["Shell 1", "Shell 2"]),
        ("claude codex shell", ["Claude Code", "Codex", "Shell"]),
        ("ccr ccr", ["CCR · A", "CCR · B"]),
        ("claude spend shell spend", ["Claude Code", "Shell"]),
        ("status status claude", ["Status 1", "Status 2", "Claude Code"]),
    ],
)
def test_pane_names(tmp_path, tokens, expected):
    assert names_for(tmp_path, tokens) == expected


def test_agent_names_keep_the_substring_stream_deck_matches(tmp_path):
    for token, needle in (("claude claude", "claude"), ("codex codex", "codex"),
                          ("claude codex ccr", None)):
        for name in names_for(tmp_path, token):
            lowered = name.lower()
            if needle:
                assert needle in lowered
    ccr = names_for(tmp_path, "ccr ccr")
    assert all("claude" not in n.lower() and "codex" not in n.lower() for n in ccr)


def test_pane_name_for_spend_is_unchanged(tmp_path):
    done = run_fn(tmp_path, "_agentdesk_pane_name spend")
    assert done.stdout.strip() == "Token Spend"


# ── emitters ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("call", EMITTERS)
def test_emitters_are_deterministic_and_environment_free(tmp_path, call):
    first = run_fn(tmp_path, call)
    second = run_fn(tmp_path, call)
    assert first.returncode == 0, first.stderr
    assert first.stdout and first.stdout == second.stdout

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    bare = {"HOME": str(empty_home), "PATH": str(tmp_path / "nothing")}
    zsh_bin = shutil.which("zsh")
    done = subprocess.run(
        [zsh_bin, "-c", f'source "{AGENTDESK_ZSH}"; {call}'],
        env=bare, capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == first.stdout


def test_emitters_create_no_files(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    env = agentdesk_env(tmp_path)
    env["TMPDIR"] = str(watched)
    before = set(os.listdir(env["HOME"]))
    for call in EMITTERS:
        done = subprocess.run(
            ["zsh", "-c", f'source "{AGENTDESK_ZSH}"; {call}'],
            env=env, capture_output=True, text=True, cwd=str(watched), timeout=60,
        )
        assert done.returncode == 0, done.stderr
    assert os.listdir(watched) == []
    assert set(os.listdir(env["HOME"])) == before


def test_default_bars_are_tab_bar_and_status_bar(tmp_path):
    for call in ("_agentdesk_kdl_header default", "_agentdesk_kdl_tab_open default x",
                 "_agentdesk_kdl_tab_close default"):
        out = run_fn(tmp_path, call).stdout
        assert "compact-bar" not in out
    header = run_fn(tmp_path, "_agentdesk_kdl_header default").stdout
    assert "zellij:tab-bar" in header and "zellij:status-bar" in header
    assert 'location="zellij:tab-bar"' in run_fn(tmp_path, "_agentdesk_kdl_tab_open default x").stdout
    close = run_fn(tmp_path, "_agentdesk_kdl_tab_close default").stdout
    assert "pane size=2" in close and "zellij:status-bar" in close


def test_compact_bars_are_a_single_compact_bar(tmp_path):
    header = run_fn(tmp_path, "_agentdesk_kdl_header compact").stdout
    assert "zellij:tab-bar" not in header and "zellij:status-bar" not in header
    assert header.count("zellij:compact-bar") == 2  # new tab template and the named template
    assert run_fn(tmp_path, "_agentdesk_kdl_tab_open compact x").stdout.count("compact-bar") == 0
    close = run_fn(tmp_path, "_agentdesk_kdl_tab_close compact").stdout
    assert close.count("zellij:compact-bar") == 1 and "pane size=1" in close
    tab = main_tab(layout_node(gen_text(tmp_path, "claude shell", "compact")))
    assert bar_plugins(tab) == ["zellij:compact-bar"]


@pytest.mark.parametrize("call", [
    "_agentdesk_kdl_header bogus", "_agentdesk_kdl_tab_open bogus x",
    "_agentdesk_kdl_tab_close bogus", "_agentdesk_kdl_agents_tab bogus claude",
    "_agentdesk_kdl_swap_layouts bogus",
])
def test_emitters_reject_unknown_bar_mode(tmp_path, call):
    done = run_fn(tmp_path, call)
    assert done.returncode == 2
    assert done.stdout == ""
    assert "bar mode" in done.stderr


def test_header_has_new_tab_template_and_no_default_tab_template(tmp_path):
    header = run_fn(tmp_path, "_agentdesk_kdl_header default").stdout
    assert "new_tab_template" in header
    assert 'tab_template name="agentdesk_bars"' in header
    # Zellij drops a tab's hide_floating_panes when the layout has a
    # default_tab_template, which would open the floating pane visible.
    assert "default_tab_template" not in header


def test_env_helper_does_not_leak_developer_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDESK_COMPACT_BAR", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "real")
    env = agentdesk_env(tmp_path)
    assert set(env) == {"HOME", "XDG_CONFIG_HOME", "PATH", "TERM", "SHELL", "TMPDIR"}
    tab = main_tab(layout_node(gen_text(tmp_path, "claude shell")))
    assert "zellij:status-bar" in bar_plugins(tab)
    assert "compact-bar" not in "".join(bar_plugins(tab))


# ── builder structure ──────────────────────────────────────────────────────────

def test_two_agents_and_shell_strip(tmp_path):
    layout = layout_node(gen_text(tmp_path, "claude claude shell"))
    tab = main_tab(layout)
    assert props(tab).get("hide_floating_panes") == "true"
    assert bar_plugins(tab) == ["zellij:tab-bar", "zellij:status-bar"]
    root = tiled_root(tab)
    top, strip = panes_of(root)
    assert props(top)["size"] == "75%" and props(top)["split_direction"] == "vertical"
    assert [leaf_name(p) for p in panes_of(top)] == ["Claude · A", "Claude · B"]
    assert props(panes_of(top)[0]).get("focus") == "true"
    assert leaf_name(strip) == "Shell" and props(strip)["size"] == "25%"
    assert not panes_of(strip)


def test_single_agent_with_shell_strip(tmp_path):
    root = tiled_root(main_tab(layout_node(gen_text(tmp_path, "claude shell"))))
    top, strip = panes_of(root)
    assert leaf_name(top) == "Claude Code" and props(top)["size"] == "75%"
    assert not panes_of(top)
    assert leaf_name(strip) == "Shell" and props(strip)["size"] == "25%"


def test_lone_agent_fills_the_tiled_area(tmp_path):
    root = tiled_root(main_tab(layout_node(gen_text(tmp_path, "claude"))))
    (only,) = panes_of(root)
    assert leaf_name(only) == "Claude Code"
    assert "size" not in props(only)


def test_two_agents_without_strip_are_one_row(tmp_path):
    root = tiled_root(main_tab(layout_node(gen_text(tmp_path, "claude claude"))))
    (row,) = panes_of(root)
    assert props(row)["split_direction"] == "vertical" and "size" not in props(row)
    assert [leaf_name(p) for p in panes_of(row)] == ["Claude · A", "Claude · B"]


def test_four_agents_and_shell_use_rows_of_two(tmp_path):
    root = tiled_root(main_tab(layout_node(
        gen_text(tmp_path, "claude claude codex codex shell"))))
    top, strip = panes_of(root)
    assert props(top)["split_direction"] == "horizontal" and props(top)["size"] == "75%"
    rows = panes_of(top)
    assert [len(panes_of(r)) for r in rows] == [2, 2]
    assert all(props(r)["split_direction"] == "vertical" for r in rows)
    assert leaf_name(strip) == "Shell"


def test_five_agents_are_rows_of_two_two_one(tmp_path):
    root = tiled_root(main_tab(layout_node(
        gen_text(tmp_path, "claude claude claude codex codex"))))
    (grid,) = panes_of(root)
    rows = panes_of(grid)
    assert [len(names_in(r)) for r in rows] == [2, 2, 1]


def test_four_agents_only_have_the_grid_without_strip(tmp_path):
    root = tiled_root(main_tab(layout_node(
        gen_text(tmp_path, "claude claude codex codex"))))
    (grid,) = panes_of(root)
    assert "size" not in props(grid)
    assert [len(names_in(r)) for r in panes_of(grid)] == [2, 2]


def test_utilities_only(tmp_path):
    one = tiled_root(main_tab(layout_node(gen_text(tmp_path, "shell"))))
    assert [leaf_name(p) for p in panes_of(one)] == ["Shell"]
    pair = tiled_root(main_tab(layout_node(gen_text(tmp_path, "shell status"))))
    (row,) = panes_of(pair)
    assert props(row)["split_direction"] == "vertical"
    assert [leaf_name(p) for p in panes_of(row)] == ["Shell", "Status"]
    four = tiled_root(main_tab(layout_node(gen_text(tmp_path, "shell shell shell shell"))))
    assert [leaf_name(p) for p in panes_of(four)] == ["Shell 1", "Shell 2", "Shell 3", "Shell 4"]
    assert all(not panes_of(p) for p in panes_of(four))


@pytest.mark.parametrize("tokens", ["claude shell status", "claude status shell"])
def test_status_strip_is_taller_and_split_sixty_forty(tmp_path, tokens):
    root = tiled_root(main_tab(layout_node(gen_text(tmp_path, tokens))))
    top, strip = panes_of(root)
    assert props(top)["size"] == "70%"
    assert props(strip)["size"] == "30%"
    sizes = {leaf_name(p): props(p)["size"] for p in panes_of(strip)}
    assert sizes == {"Shell": "60%", "Status": "40%"}


def test_two_shells_in_the_strip_share_it_equally(tmp_path):
    root = tiled_root(main_tab(layout_node(gen_text(tmp_path, "claude shell shell"))))
    _, strip = panes_of(root)
    assert [leaf_name(p) for p in panes_of(strip)] == ["Shell 1", "Shell 2"]
    assert all("size" not in props(p) for p in panes_of(strip))


@pytest.mark.parametrize("tokens", ["claude shell", "claude spend shell spend", "claude claude shell"])
def test_spend_is_one_hidden_floating_pane(tmp_path, tokens):
    layout = layout_node(gen_text(tmp_path, tokens))
    assert not [t for t in tabs(layout) if props(t).get("name") == "Spend"]
    tab = main_tab(layout)
    assert floating_names(tab) == ["Token Spend"]
    assert len(child(tab, "floating_panes")) == 1
    text = gen_text(tmp_path, tokens)
    assert "spend_monitor.py" in text and "--compact --watch" in text
    assert "Token Spend" not in names_in(tiled_root(tab))


def test_spend_alone_adds_a_tiled_shell(tmp_path):
    tab = main_tab(layout_node(gen_text(tmp_path, "spend")))
    assert names_in(tiled_root(tab)) == ["Shell"]
    assert floating_names(tab) == ["Token Spend"]


def swap_layouts(layout: dict) -> list:
    return child(layout, "swap_tiled_layout")


def test_swap_layouts_default(tmp_path):
    layout = layout_node(gen_text(tmp_path, "claude claude shell"))
    swaps = swap_layouts(layout)
    assert [props(s)["name"] for s in swaps] == ["even", "focus-left", "stacked"]
    for s in swaps:
        assert [c["name"] for c in s["children"]] == ["agentdesk_bars"]
    even, focus, stacked = (s["children"][0] for s in swaps)
    assert "min_panes" not in props(focus) or props(focus)["min_panes"] == "4"
    assert props(focus)["min_panes"] == "4"
    assert "min_panes" not in props(even) and "max_panes" not in props(even)
    assert "min_panes" not in props(stacked) and "max_panes" not in props(stacked)
    (focus_split,) = panes_of(focus)
    sized, grouped = panes_of(focus_split)
    assert props(sized)["size"] == "70%"
    assert props(grouped)["stacked"] == "true" and child(grouped, "children")
    (stack,) = panes_of(stacked)
    assert props(stack)["stacked"] == "true" and child(stack, "children")


def test_swap_focus_left_minimum_counts_the_single_compact_bar(tmp_path):
    layout = layout_node(gen_text(tmp_path, "claude claude shell", "compact"))
    focus = swap_layouts(layout)[1]["children"][0]
    assert props(focus)["min_panes"] == "3"


def test_compact_main_tab_has_one_bar_at_the_bottom(tmp_path):
    tab = main_tab(layout_node(gen_text(tmp_path, "claude claude shell", "compact")))
    assert bar_plugins(tab) == ["zellij:compact-bar"]
    panes = child(tab, "pane")
    assert is_bar(panes[-1])
    default_tab = main_tab(layout_node(gen_text(tmp_path, "claude claude shell")))
    assert len(bar_plugins(default_tab)) == 2


def test_builder_rejects_unknown_bar_mode(tmp_path):
    done = run_fn(tmp_path, "_agentdesk_gen_layout --bars bogus claude")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "bar mode" in done.stderr
    assert not list(Path(agentdesk_env(tmp_path)["TMPDIR"]).glob("agentdesk-layout-*"))


SHAPES = [
    "claude", "claude shell", "claude claude shell", "claude codex shell",
    "claude ccr shell", "claude shell status", "claude status shell",
    "claude shell shell", "claude claude claude claude shell",
    "claude claude claude codex codex", "shell", "shell status",
    "shell shell shell shell", "spend", "claude spend shell spend",
    "codex codex shell", "ccr ccr",
]


@pytest.mark.parametrize("bars", [None, "compact"])
@pytest.mark.parametrize("tokens", SHAPES)
def test_every_shape_is_clean_and_parses(tmp_path, tokens, bars):
    path = gen_layout_file(tmp_path, tokens, bars)
    assert_text_clean(path.read_text())
    if ZELLIJ:
        done = parse_check(tmp_path, path)
        assert done.returncode == 0, done.stderr + done.stdout


@needs_zellij
def test_parse_check_rejects_a_broken_layout(tmp_path):
    bad = tmp_path / "bad.kdl"
    bad.write_text("layout {\n    tab bogus_property=1 {\n        pane\n    }\n}\n")
    assert parse_check(tmp_path, bad).returncode != 0


# ── compatibility with stored layouts, picker values and fixed invocations ─────

STORED = [
    "claude", "claude+shell", "claude+claude+shell", "claude+codex+shell",
    "claude+ccr+shell", "codex+codex+shell", "claude+shell+status", "claude+spend",
    "spend", "shell+shell+shell+shell",
]


@pytest.mark.parametrize("value", STORED)
def test_stored_values_still_restore(tmp_path, value):
    done = run_fn(tmp_path, f"_agentdesk_layout_from_value '{value}'")
    assert done.returncode == 0, done.stderr
    path = Path(done.stdout.strip())
    assert path.exists()
    assert_text_clean(path.read_text())
    if ZELLIJ:
        assert parse_check(tmp_path, path).returncode == 0
    compact = run_fn(tmp_path, f"_agentdesk_layout_from_value '{value}' compact")
    assert compact.returncode == 0
    assert "zellij:compact-bar" in Path(compact.stdout.strip()).read_text()


def test_stored_fixed_value_and_bad_value(tmp_path):
    env = agentdesk_env(tmp_path)
    done = run_fn(tmp_path, "_agentdesk_layout_from_value __FIXED__", env)
    assert done.stdout.strip() == f"{env['HOME']}/.config/zellij/layouts/agent-desk.kdl"
    assert run_fn(tmp_path, "_agentdesk_layout_from_value claude+bogus", env).returncode == 1


PICKER_CHOICES = {
    "2": "claude shell spend",
    "3": "claude claude shell",
    "4": "claude codex shell",
    "5": "claude ccr shell",
}


@pytest.mark.parametrize("tokens", sorted(set(PICKER_CHOICES.values())))
def test_picker_token_lists_generate_parseable_layouts(tmp_path, tokens):
    path = gen_layout_file(tmp_path, tokens)
    assert_text_clean(path.read_text())
    if ZELLIJ:
        assert parse_check(tmp_path, path).returncode == 0


def test_picker_numbers_still_map_to_token_lists(tmp_path):
    for number, expected in PICKER_CHOICES.items():
        done = subprocess.run(
            ["zsh", "-c", f'source "{AGENTDESK_ZSH}"; _agentdesk_pick_layout'],
            env=agentdesk_env(tmp_path), input=f"{number}\n", capture_output=True,
            text=True, cwd=str(tmp_path), timeout=60,
        )
        picked = done.stdout.strip().split()
        assert picked, (number, done.stderr)
        assert set(picked) <= {"claude", "codex", "ccr", "shell", "status", "spend"}


def test_api_version_marker_is_unchanged():
    lines = [ln for ln in AGENTDESK_ZSH.read_text().splitlines()
             if re.match(r"^AGENTDESK_API_VERSION=", ln)]
    assert lines == ["AGENTDESK_API_VERSION=1"]


# ── dashboard tab helper (launcher helper, reads the layout file) ──────────────

LEGACY_LAYOUT = """layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        children
    }
    tab name="main" {
        pane
    }
}
"""


def test_dashboard_helper_keeps_the_plain_block_for_legacy_layouts(tmp_path):
    layout = tmp_path / "legacy.kdl"
    layout.write_text(LEGACY_LAYOUT)
    env = agentdesk_env(tmp_path)
    done = run_fn(tmp_path, f'_agentdesk_kdl_dashboard_tab "{layout}" /tmp/dash-url.txt', env)
    assert done.returncode == 0, done.stderr
    expected = (
        '    tab name="Dashboard" {\n'
        '        pane name="quoin dashboard" {\n'
        '            command "zsh"\n'
        f'            args "-lc" "exec python3 \\"{env["HOME"]}/.claude/scripts/dashboard_server.py\\" '
        '--no-browser --url-file \\"/tmp/dash-url.txt\\""\n'
        '        }\n'
        '    }\n'
        '}\n'
    )
    assert done.stdout == expected


@pytest.mark.parametrize("bars,expected", [
    (None, ["zellij:tab-bar", "zellij:status-bar"]),
    ("compact", ["zellij:compact-bar"]),
])
def test_dashboard_helper_adds_the_layouts_own_bars(tmp_path, bars, expected):
    layout = gen_layout_file(tmp_path, "claude shell", bars)
    done = run_fn(tmp_path, f'_agentdesk_kdl_dashboard_tab "{layout}" /tmp/dash-url.txt')
    assert done.returncode == 0, done.stderr
    combined = layout.read_text().rstrip("\n").rsplit("\n", 1)[0] + "\n" + done.stdout
    assert_text_clean(combined)
    parsed = layout_node(combined)
    (dash,) = [t for t in tabs(parsed) if props(t).get("name") == "Dashboard"]
    assert bar_plugins(dash) == expected
    assert names_in(dash) == ["quoin dashboard"]
