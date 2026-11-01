"""Shared helpers for the Agent Desk keybinding tests.

Holds a small KDL reader, an evaluator for Zellij ``keybinds`` blocks and the
helpers that run setup-agentdesk.sh functions in an isolated home.
"""
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "setup-agentdesk.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agentdesk_keybinds"

ALL_MODES = [
    "normal", "locked", "resize", "pane", "move", "tab", "scroll", "entersearch",
    "search", "renametab", "renamepane", "session", "tmux", "prompt",
]

BEGIN_PREFIX = "// [agentdesk: added] keybinds v2 begin tier"
END_MARKER = "// [agentdesk: added] keybinds v2 end"


# ── KDL subset reader ──────────────────────────────────────────────────────────

def _tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            yield ("NL", c)
            i += 1
        elif c in " \t\r":
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            yield ("STR", "".join(buf))
            i = j + 1
        elif c == "{":
            yield ("LB", c)
            i += 1
        elif c == "}":
            yield ("RB", c)
            i += 1
        elif c == ";":
            yield ("SEMI", c)
            i += 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n{};\"":
                j += 1
            yield ("WORD", text[i:j])
            i = j


def parse_kdl(text):
    """Parse nodes into dicts: name, args (strings), children (list)."""
    toks = list(_tokens(text))
    pos = 0

    def nodes(until_rb):
        nonlocal pos
        out = []
        while pos < len(toks):
            kind, val = toks[pos]
            if kind in ("NL", "SEMI"):
                pos += 1
                continue
            if kind == "RB":
                if until_rb:
                    pos += 1
                    return out
                pos += 1
                continue
            if kind not in ("WORD", "STR"):
                pos += 1
                continue
            name = val
            pos += 1
            args = []
            children = []
            while pos < len(toks):
                kind, val = toks[pos]
                if kind in ("WORD", "STR"):
                    args.append(val)
                    pos += 1
                elif kind == "LB":
                    pos += 1
                    children = nodes(True)
                    break
                else:
                    break
            out.append({"name": name, "args": args, "children": children})
        return out

    return nodes(False)


def norm_key(key):
    parts = key.split()
    out = []
    for idx, part in enumerate(parts):
        last = idx == len(parts) - 1
        if not last or len(part) > 1:
            part = part.lower()
        out.append(part)
    return " ".join(out)


def _apply_block(binds, node):
    name = node["name"].lower()
    args = [a.lower() for a in node["args"]]
    if name == "shared":
        modes = ALL_MODES
    elif name == "shared_except":
        modes = [m for m in ALL_MODES if m not in args]
    elif name == "shared_among":
        modes = [m for m in ALL_MODES if m in args]
    else:
        modes = [name] if name in ALL_MODES else []
    for child in node["children"]:
        if child["name"] == "bind":
            action = [(c["name"], tuple(a.lower() for a in c["args"])) for c in child["children"]]
            for mode in modes:
                for key in child["args"]:
                    binds[mode][norm_key(key)] = action
        elif child["name"] == "unbind":
            for mode in modes:
                for key in child["args"]:
                    binds[mode].pop(norm_key(key), None)


def _keybinds_node(text):
    for node in parse_kdl(text):
        if node["name"] == "keybinds":
            return node
    return None


def evaluate(text, stock_text):
    """Per-mode {key: action} the way Zellij resolves the keybinds node."""
    binds = {m: {} for m in ALL_MODES}
    node = _keybinds_node(text)
    clear = node is not None and any(
        a.replace("#", "") == "clear-defaults=true" for a in node["args"]
    )
    if not clear:
        stock = _keybinds_node(stock_text)
        for child in stock["children"]:
            _apply_block(binds, child)
    if node is not None:
        for child in node["children"]:
            _apply_block(binds, child)
    return binds


def switch_target(action):
    if action and action[0][0] == "SwitchToMode":
        return action[0][1][0]
    return None


def has_quit(action):
    return any(name == "Quit" for name, _ in action)


# ── Running setup-agentdesk.sh functions in an isolated home ──────────────────

def make_bin(tmp_path, with_zellij=True, with_python=True):
    """A bin dir holding only the tools a test wants visible."""
    bindir = Path(tmp_path) / "bin"
    bindir.mkdir(exist_ok=True)
    if with_python:
        py = shutil.which("python3")
        if py and not (bindir / "python3").exists():
            (bindir / "python3").symlink_to(py)
    if with_zellij:
        zj = shutil.which("zellij")
        if zj and not (bindir / "zellij").exists():
            (bindir / "zellij").symlink_to(zj)
    return bindir


def make_env(tmp_path, bindir=None, extra=None):
    home = Path(tmp_path) / "home"
    home.mkdir(exist_ok=True)
    path = f"{bindir}:/usr/bin:/bin" if bindir else os.environ.get("PATH", "/usr/bin:/bin")
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": path,
        "AGENTDESK_GHOSTTY_BIN": "none",
    }
    env.update(extra or {})
    return env


def run_fn(tmp_path, body, env=None, stdin=None):
    """Source the setup script (functions only) and run ``body`` in bash."""
    script = textwrap.dedent(f"""
        source "{SETUP_SH}"
        set +euo pipefail
        {body}
    """).strip()
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env or make_env(tmp_path),
        stdin=subprocess.DEVNULL if stdin is None else None,
        input=stdin,
        timeout=120,
    )


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def place_config(tmp_path, text):
    cfg = Path(tmp_path) / "home" / ".config" / "zellij" / "config.kdl"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8", newline="")
    return cfg


def read_exact(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def merge(tmp_path, text, tier, env=None):
    """Write ``text`` as the config, merge at ``tier``, return (merged, result)."""
    cfg = place_config(tmp_path, text)
    result = run_fn(tmp_path, f'manage_zellij_keybinds "{cfg}" {tier}', env=env)
    assert result.returncode == 0, result.stderr
    return read_exact(cfg), result


def revert(tmp_path, text, env=None):
    cfg = place_config(tmp_path, text)
    result = run_fn(tmp_path, f'revert_zellij_keybinds "{cfg}"', env=env)
    assert result.returncode == 0, result.stderr
    return read_exact(cfg), result
