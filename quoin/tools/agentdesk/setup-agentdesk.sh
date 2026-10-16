#!/usr/bin/env bash
set -euo pipefail

ZSHRC="$HOME/.zshrc"
AGENTDESK_DIR="$HOME/.config/agentdesk"
AGENTDESK_HELPERS="$AGENTDESK_DIR/agentdesk.zsh"
ZELLIJ_LAYOUT_DIR="$HOME/.config/zellij/layouts"
ZELLIJ_LAYOUT="$ZELLIJ_LAYOUT_DIR/agent-desk.kdl"
ZELLIJ_CONFIG_DIR="$HOME/.config/zellij"
ZELLIJ_CONFIG="$ZELLIJ_CONFIG_DIR/config.kdl"

SOURCE_LINE='[ -f "$HOME/.config/agentdesk/agentdesk.zsh" ] && source "$HOME/.config/agentdesk/agentdesk.zsh"'

timestamp() {
  date +%Y%m%d-%H%M%S
}

write_if_changed() {
  local target="$1"
  local tmp="$2"
  local backup_suffix="$3"

  if [ -f "$target" ] && cmp -s "$target" "$tmp"; then
    echo "Unchanged: $target"
    rm -f "$tmp"
    return 0
  fi

  if [ -f "$target" ]; then
    local backup="${target}.${backup_suffix}.$(timestamp).bak"
    cp "$target" "$backup"
    echo "Backup created: $backup"
  fi

  mv "$tmp" "$target"
  echo "Updated: $target"
}

add_homebrew_shellenv_once() {
  local brew_path="$1"
  local line="eval \"\$(${brew_path} shellenv)\""

  touch "$ZSHRC"

  if grep -Fq "$line" "$ZSHRC"; then
    echo "Homebrew shellenv already present in ~/.zshrc"
  else
    {
      echo
      echo "# Homebrew"
      echo "$line"
    } >> "$ZSHRC"
    echo "Added Homebrew shellenv to ~/.zshrc"
  fi
}

install_homebrew_if_missing() {
  if command -v brew >/dev/null 2>&1; then
    echo "Found Homebrew: $(brew --version | head -n 1)"
    return 0
  fi

  echo "Homebrew not found. Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    add_homebrew_shellenv_once "/opt/homebrew/bin/brew"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
    add_homebrew_shellenv_once "/usr/local/bin/brew"
  fi

  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew installation finished, but brew is still not on PATH."
    echo "Restart the terminal, then rerun this script."
    exit 1
  fi
}

brew_install_if_missing() {
  local formula="$1"
  local command_name="${2:-$formula}"

  if command -v "$command_name" >/dev/null 2>&1; then
    echo "Found: $command_name"
  else
    echo "Installing: $formula"
    brew install "$formula"
  fi
}

brew_cask_install_if_missing() {
  local cask="$1"
  local app_path="$2"

  if [ -e "$app_path" ]; then
    echo "Found app: $app_path"
  elif brew list --cask "$cask" >/dev/null 2>&1; then
    echo "Found cask: $cask"
  else
    echo "Installing cask: $cask"
    brew install --cask "$cask" || {
      echo "Could not install $cask via Homebrew Cask."
      echo "Agent Desk still works in another terminal."
    }
  fi
}

patch_zshrc_once() {
  touch "$ZSHRC"

  if grep -Fq "$SOURCE_LINE" "$ZSHRC"; then
    echo "~/.zshrc already sources Agent Desk helpers"
    return 0
  fi

  local backup="$HOME/.zshrc.agentdesk.backup.$(timestamp)"
  cp "$ZSHRC" "$backup"
  echo "Backup created: $backup"

  {
    echo
    echo "# Agent Desk"
    echo "$SOURCE_LINE"
  } >> "$ZSHRC"

  echo "Added Agent Desk source line to ~/.zshrc"
}

detect_clipboard_backend() {
  local os
  os="$(uname -s)"
  if [ "$os" = "Darwin" ]; then
    echo "pbcopy"
  elif [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    echo "wl-copy"
  else
    echo "xclip -selection clipboard"
  fi
}

clipboard_backend_label() {
  case "$1" in
    pbcopy)   echo "macOS (pbcopy)" ;;
    wl-copy)  echo "Linux Wayland (wl-copy)" ;;
    *)        echo "Linux X11 (xclip -selection clipboard)" ;;
  esac
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/setup-agentdesk.sh" ]; then
  SCRIPT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/agentdesk"
fi

check_command() {
  local cmd="$1"
  local label="$2"

  if command -v "$cmd" >/dev/null 2>&1; then
    echo "OK: $label -> $(command -v "$cmd")"
  else
    echo "MISSING: $label"
  fi
}

usage() {
  cat <<'USAGE'
Usage: setup-agentdesk.sh [options]

With no options, runs the full Agent Desk setup (tools, helpers, layout,
Zellij and Ghostty keybinding steps, ~/.zshrc).

Options:
  --keybinds-only          Only run the Ghostty and Zellij keybinding steps.
                           Use this after reloading Ghostty to finish the setup.
  --revert-keybinds        Remove the Agent Desk keybinding changes from the
                           Zellij config (and say where the Ghostty edit is).
                           Cannot be combined with other options.
  --assume-alt             Treat Option as already sending Alt in your terminal,
                           so the Ctrl mode keys are freed too.
  --ghostty-option-as-alt  Add "macos-option-as-alt = left" to the Ghostty
                           config without asking (when it is not set yet).
  -h, --help               Show this help.
USAGE
}

# Parse-check a candidate Zellij config without touching HOME or any session.
# Prints Zellij's complaint only when the file does not parse.
_agentdesk_zellij_parse_check() {
  local file="$1"
  local out rc=0

  command -v zellij >/dev/null 2>&1 || return 0
  out="$(zellij --config "$file" setup --check 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out" >&2
  fi
  return "$rc"
}

# Merge the Agent Desk key map into the Zellij config.
# $1 = config path, $2 = tier (1 = Alt delivery not confirmed, 2 = confirmed).
# The edit is a marked region (plus, for the older clear-defaults layout, the
# `// [agentdesk: freed]` comment-outs), so revert_zellij_keybinds can undo it.
manage_zellij_keybinds() {
  local config="${1:-$HOME/.config/zellij/config.kdl}"
  local tier="${2:-1}"

  if [ ! -f "$config" ]; then
    echo "Warning: Zellij config not found at $config — skipping keybind merge."
    return 0
  fi

  local tmp
  tmp="$(mktemp "${config}.keybinds.XXXXXX")"

  if ! _agentdesk_keybinds_engine apply "$config" "$tier" "$tmp"; then
    echo "Warning: could not run the keybind merge (python3 missing or failing); config left unchanged." >&2
    echo "         Apply the key map by hand: see KEYBIND-VERIFY.md next to this script." >&2
    rm -f "$tmp"
    return 0
  fi

  if ! _agentdesk_zellij_parse_check "$tmp"; then
    echo "Warning: the merged Zellij config did not parse; config left unchanged." >&2
    rm -f "$tmp"
    return 0
  fi

  write_if_changed "$config" "$tmp" "keybinds"
}

# Remove every Agent Desk keybinding edit from the Zellij config.
revert_zellij_keybinds() {
  local config="${1:-$HOME/.config/zellij/config.kdl}"

  if [ ! -f "$config" ]; then
    echo "Warning: Zellij config not found at $config — nothing to revert."
    return 0
  fi

  local tmp
  tmp="$(mktemp "${config}.keybinds.XXXXXX")"

  if ! _agentdesk_keybinds_engine revert "$config" 0 "$tmp"; then
    echo "Warning: could not run the keybind revert (python3 missing or failing); config left unchanged." >&2
    rm -f "$tmp"
    return 0
  fi

  if ! _agentdesk_zellij_parse_check "$tmp"; then
    echo "Warning: the reverted Zellij config did not parse; config left unchanged." >&2
    rm -f "$tmp"
    return 0
  fi

  write_if_changed "$config" "$tmp" "keybinds-revert"
}

# Keybind merge engine (stdlib Python, 3.8 compatible).
# Usage: _agentdesk_keybinds_engine apply|revert CONFIG TIER OUT
#        _agentdesk_keybinds_engine map
_agentdesk_keybinds_engine() {
  python3 - "$@" <<'PY'
import json
import re
import sys

BEGIN = "// [agentdesk: added] keybinds v2 begin tier"
END = "// [agentdesk: added] keybinds v2 end"
ADDED = "// [agentdesk: added]"
FREED = "// [agentdesk: freed]"
ALT_S_BIND = 'bind "Alt s" { SwitchToMode "session"; }'

# agentdesk key map: one entry per Zellij mode reachable from normal mode.
# (mode, Latin key, Cyrillic key on the same physical key)
LOCKED_KEY = ("locked", "g", "п")
MODE_KEYS = [
    ("pane", "a", "ф"),
    ("tab", "x", "ч"),
    ("resize", "r", "к"),
    ("move", "m", "ь"),
    ("scroll", "v", "м"),
    ("session", "s", "ы"),
    ("tmux", "z", "я"),
]
# Stock Alt binds that also get a Russian-layout twin: (Latin, Cyrillic, action)
ALIASES = [
    ("f", "а", "ToggleFloatingPanes;"),
    ("n", "т", "NewPane;"),
    ("h", "р", 'MoveFocusOrTab "left";'),
    ("j", "о", 'MoveFocus "down";'),
    ("k", "л", 'MoveFocus "up";'),
    ("l", "д", 'MoveFocusOrTab "right";'),
    ("i", "ш", 'MoveTab "left";'),
    ("[", "х", "PreviousSwapLayout;"),
    ("]", "ъ", "NextSwapLayout;"),
]
# Keys unbound in normal mode so they reach the focused application.
# Tier 1 (Alt delivery not confirmed) frees only Ctrl q.
CTRL_MODE_KEYS = ["Ctrl t", "Ctrl b", "Ctrl g", "Ctrl s", "Ctrl p", "Ctrl n", "Ctrl h"]
EXTRA_FREED = ["Ctrl o", "Alt Left", "Alt Right", "Alt p", "Alt o"]
QUIT_KEYS = ("q", "й")
# end agentdesk key map

RAW_OPEN = re.compile(r'r(#*)"')


def split_lines(text):
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def scan(text):
    """Per-line structure: masked code (strings and comments blanked), depths."""
    infos = []
    depth = 0
    block = 0
    raw = None
    instr = False
    bad = False
    for line in split_lines(text):
        d0 = depth
        out = []
        i = 0
        n = len(line)
        while i < n:
            c = line[i]
            if block:
                if line.startswith("*/", i):
                    block -= 1
                    i += 2
                elif line.startswith("/*", i):
                    block += 1
                    i += 2
                else:
                    i += 1
                continue
            if raw is not None:
                close = '"' + raw
                if line.startswith(close, i):
                    raw = None
                    i += len(close)
                else:
                    i += 1
                continue
            if instr:
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    instr = False
                i += 1
                continue
            if line.startswith("//", i):
                break
            if line.startswith("/*", i):
                block = 1
                i += 2
                continue
            m = RAW_OPEN.match(line, i)
            if m and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] in "_-")):
                raw = m.group(1)
                out.append('""')
                i = m.end()
                continue
            if c == '"':
                instr = True
                out.append('""')
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth < 0:
                    bad = True
                    depth = 0
            out.append(c)
            i += 1
        infos.append({"raw": line, "code": "".join(out), "d0": d0, "d1": depth})
    if depth != 0 or block or raw is not None or instr:
        bad = True
    return infos, bad


def find_nodes(infos):
    """Top-level keybinds nodes as (header index, closing index or None)."""
    nodes = []
    for idx, info in enumerate(infos):
        if info["d0"] != 0:
            continue
        code = info["code"].strip()
        if not re.match(r"keybinds(\s|\{|$)", code):
            continue
        if not code.endswith("{"):
            continue
        close = None
        for j in range(idx + 1, len(infos)):
            if infos[j]["d1"] == 0:
                close = j
                break
        nodes.append((idx, close))
    return nodes


def regions(lines):
    begins = [i for i, l in enumerate(lines) if l.strip().startswith(BEGIN)]
    ends = [i for i, l in enumerate(lines) if l.strip() == END]
    return begins, ends


def strip_region(text):
    lines = split_lines(text)
    begins, ends = regions(lines)
    if not begins:
        return text
    b, e = begins[0], ends[0]
    suffix = lines[b].strip()[len(BEGIN):].split()
    start = b
    if "appended" in suffix or "appended-nonl" in suffix:
        if b > 0 and lines[b - 1].strip() == "":
            start = b - 1
    rest = lines[:start] + lines[e + 1:]
    out = "".join(rest)
    if "appended-nonl" in suffix and out.endswith("\n"):
        out = out[:-1]
    return out


def region_tier(text):
    lines = split_lines(text)
    begins, _ = regions(lines)
    if not begins:
        return None
    word = lines[begins[0]].strip()[len(BEGIN):].split()
    return 2 if word and word[0] == "2" else 1


def remove_v1_pairs(text):
    lines = split_lines(text)
    out = []
    i = 0
    while i < len(lines):
        if (
            lines[i].strip() == ADDED
            and i + 1 < len(lines)
            and lines[i + 1].strip().startswith("bind")
        ):
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def restore_freed(text):
    out = []
    for line in split_lines(text):
        m = re.match(r"^[ \t]*// \[agentdesk: freed\] (.*)$", line, re.S)
        out.append(m.group(1) if m else line)
    return "".join(out)


def mode_args(raw):
    return [a.lower() for a in re.findall(r'"((?:[^"\\]|\\.)*)"', raw)]


def v1_edits(text):
    """Comment out the three binds the earlier merge freed (clear-defaults shape)."""
    infos, _ = scan(text)
    nodes = find_nodes(infos)
    if not nodes or nodes[0][1] is None:
        return text
    start, end = nodes[0]
    lines = [info["raw"] for info in infos]
    block_set = None
    block_start_depth = None
    for idx in range(start + 1, end):
        info = infos[idx]
        code = info["code"].strip()
        if info["d0"] == 1 and code.endswith("{"):
            m = re.match(r"(shared_except|shared_among)\b", code)
            if m and m.group(1) == "shared_except":
                block_set = set(mode_args(info["raw"].split("//")[0]))
            else:
                block_set = None
            continue
        if info["d0"] != 2 or block_set is None:
            continue
        raw = info["raw"]
        if info["d1"] != info["d0"] or not code:
            continue
        if block_set == {"locked"} and re.search(
            r'bind\s+"Alt (?:left|right)"\s*\{', raw, re.I
        ) and len(mode_args(raw.split("{")[0])) == 1:
            lines[idx] = "        " + FREED + " " + raw
        elif block_set == {"locked", "session"} and re.search(
            r'bind\s+"Ctrl o"\s*\{\s*SwitchToMode\s+"session"', raw, re.I
        ):
            lines[idx] = "        " + FREED + " " + raw
    return "".join(lines)


def render_body(tier):
    """Region lines as (indent level, text)."""
    out = []

    def mark(text, tag=ADDED):
        return text + "  " + tag

    out.append((0, mark('shared_except "locked" {')))
    out.append((1, mark('unbind "Ctrl q"', FREED)))
    out.append((1, mark('bind "Alt g" { SwitchToMode "locked"; }')))
    out.append((1, mark('bind "Alt %s" { SwitchToMode "locked"; }' % LOCKED_KEY[2])))
    for mode, lat, cyr in MODE_KEYS:
        if mode == "session":
            out.append((1, mark(ALT_S_BIND)))
        else:
            out.append((1, mark('bind "Alt %s" { SwitchToMode "%s"; }' % (lat, mode))))
        out.append((1, mark('bind "Alt %s" { SwitchToMode "%s"; }' % (cyr, mode))))
    for lat, cyr, action in ALIASES:
        out.append((1, mark('bind "Alt %s" { %s }' % (cyr, action))))
    out.append((0, mark("}")))
    out.append((0, mark("locked {")))
    for key in ("Alt g", "Alt " + LOCKED_KEY[2], "Ctrl " + LOCKED_KEY[2]):
        out.append((1, mark('bind "%s" { SwitchToMode "normal"; }' % key)))
    out.append((0, mark("}")))
    for mode, lat, cyr in MODE_KEYS:
        out.append((0, mark(mode + " {")))
        out.append((1, mark('bind "Alt %s" { SwitchToMode "normal"; }' % lat)))
        out.append((1, mark('bind "Alt %s" { SwitchToMode "normal"; }' % cyr)))
        if mode == "session":
            for key in QUIT_KEYS:
                out.append((1, mark('bind "%s" { Quit; }' % key)))
        out.append((0, mark("}")))
    if tier == 2:
        out.append((0, mark("normal {")))
        for key in EXTRA_FREED[:3] + CTRL_MODE_KEYS + EXTRA_FREED[3:]:
            out.append((1, mark('unbind "%s"' % key, FREED)))
        out.append((0, mark("}")))
    return out


def indent_lines(body, base):
    return [base + "    " * lvl + text + "\n" for lvl, text in body]


def child_indent(infos, start, end):
    for idx in range(start + 1, end):
        code = infos[idx]["code"].strip()
        if code:
            raw = infos[idx]["raw"]
            return raw[: len(raw) - len(raw.lstrip())]
    return "    "


def check(text):
    """Return (infos, node, reason). reason is non-empty when the file is refused."""
    infos, bad = scan(text)
    if bad:
        return infos, None, "unbalanced braces, comments or strings"
    lines = [i["raw"] for i in infos]
    begins, ends = regions(lines)
    if len(begins) != len(ends) or len(begins) > 1:
        return infos, None, "unpaired or repeated agentdesk keybind markers"
    if begins and begins[0] > ends[0]:
        return infos, None, "agentdesk keybind markers are out of order"
    nodes = find_nodes(infos)
    if len(nodes) > 1:
        return infos, None, "more than one keybinds node"
    if nodes:
        header, close = nodes[0]
        if close is None:
            return infos, None, "keybinds node never closes"
        if infos[close]["code"].strip() != "}":
            return infos, None, "the keybinds closing brace shares its line"
    return infos, (nodes[0] if nodes else None), ""


def apply(text, tier):
    infos, node, reason = check(text)
    if reason:
        return text, reason
    prior = region_tier(text)
    desired = 2 if prior == 2 else tier
    work = strip_region(text) if prior else text
    work = remove_v1_pairs(work)
    infos, node, reason = check(work)
    if reason:
        return text, reason
    if node is not None and prior is None:
        header = infos[node[0]]["code"]
        if re.search(r"clear-defaults\s*=\s*#?true", header):
            work = v1_edits(work)
            infos, node, reason = check(work)
            if reason:
                return text, reason
    if node is not None:
        header, close = node
        base = child_indent(infos, header, close)
        block = [base + BEGIN + str(desired) + "\n"]
        block += indent_lines(render_body(desired), base)
        block.append(base + END + "\n")
        lines = [i["raw"] for i in infos]
        return "".join(lines[:close] + block + lines[close:]), ""
    nonl = not work.endswith("\n")
    out = work + ("\n" if nonl else "")
    out += "\n"
    out += BEGIN + str(desired) + (" appended-nonl" if nonl else " appended") + "\n"
    out += "keybinds {  " + ADDED + "\n"
    out += "".join(indent_lines(render_body(desired), "    "))
    out += "}  " + ADDED + "\n"
    out += END + "\n"
    return out, ""


def revert(text):
    lines = split_lines(text)
    begins, ends = regions(lines)
    if len(begins) != len(ends) or len(begins) > 1:
        return text, "unpaired or repeated agentdesk keybind markers"
    infos, bad = scan(text)
    if bad:
        return text, "unbalanced braces, comments or strings"
    work = strip_region(text)
    work = restore_freed(work)
    work = remove_v1_pairs(work)
    return work, ""


def main(argv):
    mode = argv[1]
    if mode == "map":
        print(json.dumps({
            "locked": list(LOCKED_KEY),
            "modes": [list(m) for m in MODE_KEYS],
            "aliases": [list(a) for a in ALIASES],
            "ctrl_freed": CTRL_MODE_KEYS,
            "extra_freed": EXTRA_FREED,
            "quit": list(QUIT_KEYS),
        }, ensure_ascii=False))
        return 0
    config, tier, out = argv[2], int(argv[3]), argv[4]
    with open(config, encoding="utf-8", newline="") as fh:
        text = fh.read()
    if mode == "apply":
        result, reason = apply(text, tier)
    else:
        result, reason = revert(text)
    if reason:
        sys.stderr.write(
            "Warning: Zellij keybind merge skipped for %s (%s); config left unchanged.\n"
            % (config, reason)
        )
        result = text
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(result)
    return 0


sys.exit(main(sys.argv))
PY
}

# Reports the effective macos-option-as-alt value (stdlib Python, 3.8 compatible).
_agentdesk_ghostty_engine() {
  python3 - "$@" <<'PY'
import os
import re
import shutil
import subprocess

KEY = "macos-option-as-alt"
VALID = ("true", "false", "left", "right")

home = os.environ.get("HOME", "")
xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
app_support = os.path.join(home, "Library", "Application Support", "com.mitchellh.ghostty")

# Default config files in Ghostty's load order; later files override earlier ones.
candidates = [
    os.path.join(xdg, "ghostty", "config"),
    os.path.join(xdg, "ghostty", "config.ghostty"),
    os.path.join(app_support, "config"),
    os.path.join(app_support, "config.ghostty"),
]
existing = [p for p in candidates if os.path.isfile(p)]


def clean(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def parse(path):
    """Yield (line number, key, value) for the assignments in one config file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, _, value = text.partition("=")
                yield number, key.strip(), clean(value)
    except OSError:
        return


queue = list(existing)
seen = set()
assigned = []
position = 0
while position < len(queue):
    path = queue[position]
    position += 1
    real = os.path.realpath(path)
    if real in seen:
        continue
    seen.add(real)
    for number, key, value in parse(path):
        if key == KEY:
            assigned.append((path, number, value))
        elif key == "config-file":
            optional = value.startswith("?")
            target = value[1:] if optional else value
            target = clean(target)
            if target.startswith("~/"):
                target = os.path.join(home, target[2:])
            elif not os.path.isabs(target):
                target = os.path.join(os.path.dirname(path), target)
            if os.path.isfile(target):
                queue.append(target)


def normalize(value):
    value = value.lower()
    if value == "":
        return "unset"
    return value if value in VALID else "ambiguous"


files_value = normalize(assigned[-1][2]) if assigned else "unset"
value = files_value
source = "files" if existing else "none"

# The binary is only consulted when a config file exists: with no default file
# present `ghostty +show-config` writes a template config into Application Support.
binary = os.environ.get("AGENTDESK_GHOSTTY_BIN")
if existing and binary != "none":
    if not binary:
        binary = shutil.which("ghostty") or ""
        if not binary:
            fallback = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
            binary = fallback if os.access(fallback, os.X_OK) else ""
    if binary:
        try:
            proc = subprocess.run(
                [binary, "+show-config"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if proc.returncode == 0:
                found = "unset"
                for line in proc.stdout.decode("utf-8", "replace").splitlines():
                    m = re.match(r"\s*" + re.escape(KEY) + r"\s*=\s*(.*)$", line)
                    if m:
                        found = normalize(clean(m.group(1)))
                value = found
                source = "binary"
        except (OSError, subprocess.SubprocessError):
            pass

target = existing[-1] if existing else os.path.join(xdg, "ghostty", "config.ghostty")

print("value=" + value)
print("source=" + source)
for path, number, _ in assigned:
    print("set_in=%s:%d" % (path, number))
print("target=" + target)
PY
}

# Prints value=, source=, set_in= (one per assignment) and target= lines.
ghostty_option_as_alt_state() {
  local out
  if out="$(_agentdesk_ghostty_engine 2>/dev/null)" && [ -n "$out" ]; then
    printf '%s\n' "$out"
  else
    printf 'value=unset\nsource=none\ntarget=%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/ghostty/config.ghostty"
  fi
}

_agentdesk_load_ghostty_state() {
  local line
  GH_VALUE=unset
  GH_SOURCE=none
  GH_SET_IN=""
  GH_TARGET=""
  while IFS= read -r line; do
    case "$line" in
      value=*)  GH_VALUE="${line#value=}" ;;
      source=*) GH_SOURCE="${line#source=}" ;;
      set_in=*) GH_SET_IN="${GH_SET_IN:+$GH_SET_IN$'\n'}${line#set_in=}" ;;
      target=*) GH_TARGET="${line#target=}" ;;
    esac
  done < <(ghostty_option_as_alt_state)
}

manage_ghostty_option_as_alt() {
  local answer="" write_it=0 backup

  GHOSTTY_WROTE=0
  echo "Ghostty: macos-option-as-alt"

  if [ -n "$GH_SET_IN" ] || { [ "$GH_SOURCE" = "binary" ] && [ "$GH_VALUE" != "unset" ]; }; then
    echo "  Already configured (effective value: $GH_VALUE); Ghostty config left unchanged."
    if [ -n "$GH_SET_IN" ]; then
      printf '%s\n' "$GH_SET_IN" | sed 's/^/  set in: /'
    fi
    case "$GH_VALUE" in
      right) echo "  Note: only the right Option key sends Alt; the left Option key keeps typing special characters." ;;
      false) echo "  Note: Option does not send Alt, so Alt keybindings will not reach Zellij." ;;
      ambiguous) echo "  Note: the value is not true, false, left or right; Alt delivery is not assumed." ;;
    esac
    return 0
  fi

  echo "  Not set. The Alt-based Zellij keybindings need the Option key to send Alt."
  echo "  Recommended: macos-option-as-alt = left (the right Option key still types accents and symbols)."
  echo "  Target file: $GH_TARGET"

  if [ "${GHOSTTY_OPT_IN:-0}" = "1" ]; then
    write_it=1
  elif [ -t 0 ]; then
    printf "  Add 'macos-option-as-alt = left' to %s? [y/N]: " "$GH_TARGET"
    read -r answer || true
    case "$answer" in
      y|Y|yes|YES|Yes) write_it=1 ;;
    esac
  fi

  if [ "$write_it" != "1" ]; then
    echo "  Skipped. Re-run with --ghostty-option-as-alt to add it, or add the line yourself."
    return 0
  fi

  mkdir -p "$(dirname "$GH_TARGET")"
  if [ -f "$GH_TARGET" ]; then
    backup="${GH_TARGET}.agentdesk.$(timestamp).bak"
    cp "$GH_TARGET" "$backup"
    echo "  Backup created: $backup"
    if [ -s "$GH_TARGET" ] && [ -n "$(tail -c 1 "$GH_TARGET")" ]; then
      printf '\n' >> "$GH_TARGET"
    fi
  fi
  {
    printf '%s\n' '# [agentdesk: added] treat the left Option key as Alt for Zellij keybindings'
    printf '%s\n' 'macos-option-as-alt = left'
  } >> "$GH_TARGET"
  echo "  Updated: $GH_TARGET"
  GHOSTTY_WROTE=1
}

# Names where the Ghostty edit lives so a revert can be done by hand.
_agentdesk_ghostty_revert_note() {
  local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
  local app="$HOME/Library/Application Support/com.mitchellh.ghostty"
  local f b found=0

  for f in "$xdg/ghostty/config" "$xdg/ghostty/config.ghostty" "$app/config" "$app/config.ghostty"; do
    if [ -f "$f" ] && grep -q '^# \[agentdesk: added\] treat the left Option key as Alt' "$f" 2>/dev/null; then
      echo "Ghostty: remove the line marked '# [agentdesk: added]' (and 'macos-option-as-alt = left') in $f"
      found=1
    fi
    for b in "$f".agentdesk.*.bak; do
      if [ -f "$b" ]; then
        echo "Ghostty: backup of the original: $b"
        found=1
      fi
    done
  done
  if [ "$found" = "0" ]; then
    echo "Ghostty: no Agent Desk edit found; nothing to undo there."
  fi
}

# Checksum of a file ("none" when absent), to tell whether a merge changed it.
_agentdesk_keybind_fingerprint() {
  if [ -f "$1" ]; then
    cksum < "$1" 2>/dev/null || echo "unreadable"
  else
    echo "none"
  fi
}

# Tier recorded in the begin marker of the Agent Desk key map region: 1, 2, or 0 when absent.
_agentdesk_keybind_tier() {
  local line
  line="$(grep -m1 '^[[:space:]]*// \[agentdesk: added\] keybinds v2 begin tier[12]' "$1" 2>/dev/null || true)"
  case "$line" in
    *"begin tier2"*) echo 2 ;;
    *"begin tier1"*) echo 1 ;;
    *) echo 0 ;;
  esac
}

# True when an earlier setup commented out a bind of one of the given keys (pipe-separated, any case).
_agentdesk_keybind_has_freed() {
  grep -i -E -q "^[[:space:]]*// \[agentdesk: freed\][[:space:]]+bind[[:space:]]+\"($2)\"" "$1" 2>/dev/null
}

run_keybind_steps() {
  local alt_ok=0 tier answer=""

  _agentdesk_load_ghostty_state
  case "$GH_VALUE" in
    true|left|right) alt_ok=1 ;;
  esac
  manage_ghostty_option_as_alt
  if [ "$GHOSTTY_WROTE" = "1" ]; then
    # A fresh write only takes effect after Ghostty reloads its config.
    alt_ok=0
  fi

  if [ "${ASSUME_ALT:-0}" = "1" ]; then
    alt_ok=1
  elif [ "$alt_ok" = "0" ] && [ "$GHOSTTY_WROTE" != "1" ] && [ -t 0 ]; then
    printf "Free Ctrl t/b/g/s/p/n/h anyway? Only answer yes if Option already sends Alt in this terminal [y/N]: "
    read -r answer || true
    case "$answer" in
      y|Y|yes|YES|Yes) alt_ok=1 ;;
    esac
  fi

  if [ "$alt_ok" = "1" ]; then
    tier=2
  else
    tier=1
  fi

  local before after eff
  before="$(_agentdesk_keybind_fingerprint "$ZELLIJ_CONFIG")"
  manage_zellij_keybinds "$ZELLIJ_CONFIG" "$tier"
  after="$(_agentdesk_keybind_fingerprint "$ZELLIJ_CONFIG")"
  eff="$(_agentdesk_keybind_tier "$ZELLIJ_CONFIG")"

  # Report what the config holds now, not what was requested.
  if [ "$eff" = "0" ]; then
    echo "Keybindings: not applied. The Zellij config was left unchanged (see the warning above, if any)."
    echo "  Nothing was removed or rebound; Ctrl q and the Ctrl mode keys still behave as before."
  elif [ "$eff" = "1" ] && [ "$tier" = "2" ] && [ "$before" = "$after" ]; then
    echo "Keybindings: not applied. The Zellij config was left unchanged (see the warning above, if any)."
    echo "  The existing Agent Desk key map still frees only Ctrl q; the Ctrl mode keys are unchanged."
  elif [ "$eff" = "1" ]; then
    echo "Keybindings: Alt delivery is not confirmed, so only Ctrl q was removed (quit is Alt s, then q)."
    echo "  The Ctrl mode keys (Ctrl t/b/g/s/p/n/h) stay bound to Zellij modes for now."
    if _agentdesk_keybind_has_freed "$ZELLIJ_CONFIG" 'Ctrl o'; then
      echo "  Ctrl o (session mode) was already freed on this config by an earlier setup, so session mode"
      echo "  needs Alt s until Option sends Alt; use the command-line fallbacks below meanwhile."
    fi
    if _agentdesk_keybind_has_freed "$ZELLIJ_CONFIG" 'Alt left|Alt right'; then
      echo "  Alt Left and Alt Right (Option-arrow) were also freed in locked mode by that earlier setup."
    fi
    echo "  Without working Alt keys, leave or clean up sessions with the command line:"
    echo "    zellij action detach"
    echo "    zellij kill-session NAME"
    echo "    agentdesk-delete NAME"
    echo "  Once Option sends Alt, re-run: bash \"$SCRIPT_DIR/setup-agentdesk.sh\" --keybinds-only"
    echo "  (or add --assume-alt to skip the Ghostty check)."
  else
    if [ "$tier" = "1" ]; then
      echo "Keybindings: this config already has the Alt key map with the Ctrl mode keys freed (kept, never downgraded)."
      echo "  Alt delivery is not confirmed on this run. If Option does not send Alt, you can enter no mode from"
      echo "  normal, and the Ctrl mode keys remain freed. To restore them run:"
      echo "    bash \"$SCRIPT_DIR/setup-agentdesk.sh\" --revert-keybinds"
    else
      echo "Keybindings: Alt delivery confirmed; Ctrl t/b/g/s/p/n/h are free for your programs."
    fi
    echo "  Mode keys: Alt a pane, Alt x tab, Alt r resize, Alt m move, Alt v scroll, Alt s session, Alt z tmux, Alt g lock."
    echo "  Quit: Alt s, then q."
  fi
  if [ "$GHOSTTY_WROTE" = "1" ]; then
    echo "Ghostty must reload before Option sends Alt: press cmd+shift+, in Ghostty or restart it."
    echo "Then finish with: bash \"$SCRIPT_DIR/setup-agentdesk.sh\" --keybinds-only"
    echo "  (or add --assume-alt if you have already checked that Option sends Alt)."
  fi
}

# The layout older setup scripts wrote when no generator was available. Kept
# byte-for-byte: standalone runs still use it when the Standard layout is
# missing, and tests compare against it.
_agentdesk_legacy_layout() {
  cat <<'EOF'
layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }

        children

        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }

    tab name="main" {
        pane split_direction="vertical" {
            pane name="Claude Code" size="65%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Claude Code - project root:' \"$PWD\" && if command -v claude >/dev/null 2>&1; then claude; else echo 'claude command not found. Install Claude Code, then run claude here.'; fi; zsh"
            }

            pane split_direction="horizontal" {
                pane name="Tests/logs" size="50%" {
                    command "zsh"
                    args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Tests/logs shell'; zsh"
                }

                pane name="Shell" {
                    command "zsh"
                    args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Shell'; zsh"
                }
            }
        }
    }

    tab name="review" {
        pane split_direction="vertical" {
            pane name="Git review / repos" size="50%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Project root:' && pwd && echo && echo 'Detected repos:' && repos && echo && echo 'Git status across repos:' && grepos && echo && echo 'Run gitreview to open lazygit for a selected repo.' && echo 'Run gitrootreview only if project root itself is a Git repo.' && zsh"
            }

            pane name="Workflow artifacts" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Project root:' && pwd && echo && echo 'Root folder:' && ls -la && echo && echo 'Workflow artifacts:' && ls -la .workflow_artifacts 2>/dev/null && echo && echo 'Artifact files:' && find .workflow_artifacts -maxdepth 2 -type f -print 2>/dev/null; zsh"
            }
        }
    }

    tab name="repos" {
        pane split_direction="vertical" {
            pane name="Repos list" size="50%" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && cat .workflow_artifacts/repos.md 2>/dev/null; zsh"
            }

            pane name="Repos status" {
                command "zsh"
                args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && echo 'Git status across repos:' && grepos; zsh"
            }
        }
    }

    tab name="shell" {
        pane name="Project shell" {
            command "zsh"
            args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && zsh"
        }
    }

    tab name="Spend" {
        pane name="Token Spend" {
            command "zsh"
            args "-lc" "source \"$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$PROJECT_ROOT\" && python3 \"$HOME/.claude/scripts/spend_monitor.py\" --compact --watch"
        }
    }
}
EOF
}

# write_standard_layout TARGET
# Writes the generated Standard layout to TARGET. The launcher helper file next
# to this script renders it and decides what TARGET currently is: a missing,
# generated or known older shipped file is replaced (with a backup of a changed
# file); a customised file is left alone and the new layout goes beside it as
# TARGET.new. Without a generator the old layout is written only when TARGET is
# missing. Never fails the setup run.
write_standard_layout() {
  local target="$1" helper tmp kind
  helper="$SCRIPT_DIR/agentdesk.zsh"
  if ! tmp="$(mktemp)"; then
    echo "Warning: could not create a temporary file; keeping the existing layout: $target"
    return 0
  fi

  if [ ! -f "$helper" ] || ! command -v zsh >/dev/null 2>&1; then
    if [ -e "$target" ]; then
      rm -f "$tmp"
      echo "Warning: $helper or zsh not found; keeping the existing layout: $target"
      return 0
    fi
    _agentdesk_legacy_layout > "$tmp"
    write_if_changed "$target" "$tmp" "agentdesk-layout"
    return 0
  fi

  if ! zsh -f -c 'source "$1" && _agentdesk_gen_standard_layout --stdout' _ "$helper" > "$tmp" || [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    echo "Warning: could not generate the Standard layout from $helper"
    if [ ! -e "$target" ]; then
      tmp="$(mktemp)"
      _agentdesk_legacy_layout > "$tmp"
      write_if_changed "$target" "$tmp" "agentdesk-layout"
    else
      echo "Keeping the existing layout: $target"
    fi
    return 0
  fi

  kind="$(zsh -f -c 'source "$1" && _agentdesk_fixed_layout_kind "$2"' _ "$helper" "$target" 2>/dev/null)" || kind="custom"
  case "$kind" in
    missing|generated|legacy)
      write_if_changed "$target" "$tmp" "agentdesk-layout"
      ;;
    *)
      if [ -f "$target.new" ] && cmp -s "$target.new" "$tmp"; then
        rm -f "$tmp"
        return 0
      fi
      if ! mv "$tmp" "$target.new"; then
        rm -f "$tmp"
        echo "Warning: could not write $target.new; keeping your layout: $target"
        return 0
      fi
      echo "Kept your customised layout: $target"
      echo "New Standard layout written beside it: $target.new"
      echo "To adopt it: mv $(printf '%q' "$target.new") $(printf '%q' "$target")"
      echo "The .new file is rewritten on every setup run; delete it once you no longer need it."
      ;;
  esac
  return 0
}

# Sourcing the script (for tests or reuse) defines the functions above and stops here.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

KEYBINDS_ONLY=0
REVERT_KEYBINDS=0
ASSUME_ALT=0
GHOSTTY_OPT_IN=0
GHOSTTY_WROTE=0
_flag_count=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keybinds-only)         KEYBINDS_ONLY=1 ;;
    --revert-keybinds)       REVERT_KEYBINDS=1 ;;
    --assume-alt)            ASSUME_ALT=1 ;;
    --ghostty-option-as-alt) GHOSTTY_OPT_IN=1 ;;
    -h|--help)               usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  _flag_count=$((_flag_count + 1))
  shift
done
if [ "$REVERT_KEYBINDS" = "1" ] && [ "$_flag_count" -gt 1 ]; then
  echo "--revert-keybinds cannot be combined with other options." >&2
  usage >&2
  exit 2
fi

if [ "$REVERT_KEYBINDS" = "1" ]; then
  revert_zellij_keybinds "$ZELLIJ_CONFIG"
  _agentdesk_ghostty_revert_note
  exit 0
fi

if [ "$KEYBINDS_ONLY" = "1" ]; then
  echo "Agent Desk: Ghostty and Zellij keybindings"
  run_keybind_steps
  exit 0
fi

echo "============================================================"
echo "Agent Desk setup"
echo "Ghostty + Zellij + Claude default + Codex on demand"
echo "============================================================"
echo

echo "Step 1: install base tools"
touch "$ZSHRC"

install_homebrew_if_missing

echo
echo "Installing required CLI tools if missing..."
brew_install_if_missing "zellij" "zellij"
brew_install_if_missing "lazygit" "lazygit"
brew_install_if_missing "fzf" "fzf"

echo
echo "Installing Ghostty if missing..."
brew_cask_install_if_missing "ghostty" "/Applications/Ghostty.app"

echo
echo "Step 2: write/update Agent Desk helper file if changed"

mkdir -p "$AGENTDESK_DIR"
helpers_tmp="$(mktemp)"

# T-03: two-mode strategy — prefer source file when available (quoin install),
# fall back to inline heredoc for standalone / legacy use.
AGENTDESK_ZSH_SRC="$SCRIPT_DIR/agentdesk.zsh"

if [ -f "$AGENTDESK_ZSH_SRC" ]; then
  # Source file available (installed via quoin or cloned repo)
  cp "$AGENTDESK_ZSH_SRC" "$helpers_tmp"
else
  # Fallback: write inline heredoc (standalone / legacy use)
  cat > "$helpers_tmp" <<'EOF'
# ============================================================
# Agent Desk helpers (legacy inline — install via quoin for full features)
# Ghostty/Zellij + Claude default + Codex on demand
# ============================================================

# IVG-135: work around upstream zellij socket-path length bug (#4211/#2817).
# See agentdesk.zsh for the full comment; kept byte-identical in intent here
# (literal $ZELLIJ_SOCKET_DIR / $(id -u) since this heredoc is single-quoted).
if [ -z "${ZELLIJ_SOCKET_DIR:-}" ]; then
  _base="${XDG_RUNTIME_DIR:-/tmp}"
  export ZELLIJ_SOCKET_DIR="${_base}/zellij-agentdesk-$(id -u 2>/dev/null || echo 0)"
  mkdir -p "$ZELLIJ_SOCKET_DIR" 2>/dev/null || true
  unset _base
fi

_agentdesk_realpath() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
}

_agentdesk_session_name() {
  local root="$1"
  local name

  name="$(
    basename "$root" \
      | tr -c '[:alnum:]_.-' '-' \
      | sed 's/-\{2,\}/-/g' \
      | sed 's/^-//' \
      | sed 's/-$//'
  )"

  if [ -z "$name" ]; then
    name="agentdesk"
  fi

  echo "$name"
}

_sanitize_session_name() {
  local raw="$1"
  local name

  name="$(
    printf "%s" "$raw" \
      | tr -c '[:alnum:]_.-' '-' \
      | sed 's/-\{2,\}/-/g' \
      | sed 's/^-//' \
      | sed 's/-$//'
  )"

  echo "$name"
}

_zellij_session_exists() {
  local session_name="$1"

  zellij list-sessions 2>/dev/null \
    | sed 's/\x1b\[[0-9;]*m//g' \
    | awk '{print $1}' \
    | grep -Fxq "$session_name"
}

_agentdesk_next_session_name() {
  local base="$1"
  local existing
  existing="$(zellij list-sessions 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk '{print $1}')"
  if ! printf '%s\n' "$existing" | grep -Fxq "$base"; then
    echo "$base"; return 0
  fi
  local n=1 candidate
  while [ "$n" -le 9999 ]; do
    candidate="${base}_${n}"
    if ! printf '%s\n' "$existing" | grep -Fxq "$candidate"; then
      echo "$candidate"; return 0
    fi
    n=$((n+1))
  done
  # bound exhausted — fall back to base_EPOCH so we never collide/hang
  echo "${base}_$(date +%s)"; return 0
}

_detect_repos() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  local root
  root="$(_agentdesk_realpath "$PROJECT_ROOT")"

  if [ -e "$root/.git" ]; then
    echo "."
  fi

  find "$root" -mindepth 2 -maxdepth 4 \( -name .git -type d -o -name .git -type f \) 2>/dev/null \
    | while read -r git_entry; do
        repo_dir="$(dirname "$git_entry")"
        rel="${repo_dir#$root/}"

        if [ "$rel" != "." ] && [ -n "$rel" ]; then
          echo "$rel"
        fi
      done \
    | sort -u
}

_repo_path() {
  local repo="$1"

  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if [ "$repo" = "." ]; then
    printf "%s\n" "$PROJECT_ROOT"
  else
    printf "%s\n" "$PROJECT_ROOT/$repo"
  fi
}

agentdesk() {
  local project_root session_name repo_count layout_path custom_name
  local layout_path="$HOME/.config/zellij/layouts/agent-desk.kdl"

  if [ -n "${ZELLIJ:-}" ]; then
    echo "You are already inside a Zellij session."
    echo "Detach with Alt+s, then d (needs Ghostty macos-option-as-alt), or run: zellij action detach"
    return 1
  fi

  custom_name=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      -n|--name)
        shift
        if [ "$#" -eq 0 ]; then
          echo "Error: --name requires a value."
          return 1
        fi
        custom_name="$1"
        ;;
      -h|--help)
        cat <<'HELP'
Usage:
  agentdesk
  agentdesk <session-name>
  agentdesk --name <session-name>
  agentdesk -n <session-name>

  (prompts to attach to a live same-named session by default; reply 'n' to start new.
   Non-interactive callers always start new, unchanged.)

Install via quoin for --mode and layout options.
HELP
        return 0
        ;;
      *)
        if [ -z "$custom_name" ]; then
          custom_name="$1"
        else
          echo "Error: unexpected argument: $1"
          return 1
        fi
        ;;
    esac
    shift
  done

  project_root="$(_agentdesk_realpath "$PWD")"
  export PROJECT_ROOT="$project_root"

  if [ -n "$custom_name" ]; then
    session_name="$(_sanitize_session_name "$custom_name")"
  else
    session_name="$(_agentdesk_session_name "$project_root")-agents"
  fi

  if [ -z "$session_name" ]; then
    echo "Error: empty session name after sanitization."
    return 1
  fi

  # NOTE: mirrors the primary agentdesk.zsh collision block (IVG-109) — keep both in sync.
  # ── Resolve session name suffix (attach-or-new prompt, IVG-109, fallback copy) ──
  local resolved_name
  resolved_name="$(_agentdesk_next_session_name "$session_name")"
  if [ "$resolved_name" != "$session_name" ]; then
    # $session_name is currently a live Zellij session (collision).
    if [ -t 0 ]; then
      printf "Session '%s' already exists. Attach to it, or start a new session '%s'? [A/n]: " \
        "$session_name" "$resolved_name" >&2
      local reply
      read -r reply
      case "$reply" in
        [Nn]|[Nn][Ee][Ww])
          echo "Starting new session '$resolved_name'."
          session_name="$resolved_name"
          ;;
        *)
          # Default (bare Enter, 'a', 'attach', or anything else): attach.
          echo "Attaching to existing session '$session_name'."
          agentdesk-attach "$session_name"
          return $?
          ;;
      esac
    else
      # Non-TTY: preserve exact prior behavior — never prompt, never hang.
      echo "Session '$session_name' already exists; starting new session '$resolved_name' instead."
      echo "(To resume the existing session: agentdesk-attach $session_name)"
      session_name="$resolved_name"
    fi
  fi

  mkdir -p "$project_root/.workflow_artifacts"

  touch "$project_root/.workflow_artifacts/repos.md"

  {
    echo "# Repositories"
    echo
    _detect_repos | while read -r repo; do
      [ -n "$repo" ] && echo "- $repo"
    done
  } > "$project_root/.workflow_artifacts/repos.md"

  repo_count="$(_detect_repos | wc -l | tr -d ' ')"

  if [ "$repo_count" = "0" ]; then
    echo "No Git repo found at current folder or in child folders."
    echo "Starting anyway at project root: $project_root"
  fi

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed. Install it with:"
    echo "  brew install zellij"
    return 1
  fi

  if [ ! -f "$layout_path" ]; then
    echo "Zellij layout not found:"
    echo "  $layout_path"
    return 1
  fi

  echo "Starting agent desk:"
  echo "  PROJECT_ROOT=$PROJECT_ROOT"
  echo "  SESSION=$session_name"
  echo "  REPOS:"
  cat "$project_root/.workflow_artifacts/repos.md"
  echo

  # IVG-135 (T-03, required): pre-flight guard against a would-be socket path
  # that overflows the AF_UNIX sun_path cap (~104 bytes macOS / ~108 Linux).
  # Best-effort heuristic; see agentdesk.zsh for the full comment.
  _sock_len=$(( ${#ZELLIJ_SOCKET_DIR} + ${#session_name} + 20 ))
  if [ "$_sock_len" -gt 100 ]; then
    echo "agentdesk: session socket path would be too long (~${_sock_len} bytes; limit ~104)." >&2
    echo "  session: $session_name" >&2
    echo "  ZELLIJ_SOCKET_DIR: $ZELLIJ_SOCKET_DIR" >&2
    echo "  Fix: set a shorter ZELLIJ_SOCKET_DIR, or use --name <shorter-name>." >&2
    return 1
  fi

  PROJECT_ROOT="$project_root" zellij --new-session-with-layout "$layout_path" --session "$session_name"
}

agentdesk-attach() {
  local session_name=""

  if [ "$#" -eq 0 ]; then
    agentdesk-sessions
    echo
    echo "Usage: agentdesk-attach <session-name>"
    return 1
  fi

  session_name="$1"

  if ! _zellij_session_exists "$session_name"; then
    echo "No such active session: $session_name"
    echo
    echo "Existing sessions:"
    agentdesk-sessions
    return 1
  fi

  zellij attach "$session_name"
}

repos() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  _detect_repos
}

crepo() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf is not installed. Install it with:"
    echo "  brew install fzf"
    return 1
  fi

  local repo
  repo="$(repos | fzf)"

  [ -z "$repo" ] && return 1

  cd "$(_repo_path "$repo")" || return 1
}

grepos() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  repos | while read -r repo; do
    [ -z "$repo" ] && continue

    echo

    if [ "$repo" = "." ]; then
      echo "===== . ====="
      git -C "$PROJECT_ROOT" status --short 2>/dev/null || true
    else
      echo "===== $repo ====="
      git -C "$PROJECT_ROOT/$repo" status --short 2>/dev/null || true
    fi
  done
}

gitreview() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf is not installed. Install it with:"
    echo "  brew install fzf"
    return 1
  fi

  local repo repo_path
  repo="$(repos | fzf --prompt='Select repo for git review: ')"

  [ -z "$repo" ] && return 1

  repo_path="$(_repo_path "$repo")"

  echo "Opening git review for: $repo"
  echo "Path: $repo_path"

  if command -v lazygit >/dev/null 2>&1; then
    (cd "$repo_path" && lazygit)
  else
    git -C "$repo_path" status
  fi
}

gitrootreview() {
  if [ -z "${PROJECT_ROOT:-}" ]; then
    export PROJECT_ROOT="$PWD"
  fi

  if [ -e "$PROJECT_ROOT/.git" ]; then
    if command -v lazygit >/dev/null 2>&1; then
      (cd "$PROJECT_ROOT" && lazygit)
    else
      git -C "$PROJECT_ROOT" status
    fi
  else
    echo "Project root is not a Git repo."
    echo "Use gitreview to select a child repo."
  fi
}

croot() {
  if [ -n "${PROJECT_ROOT:-}" ] && [ -d "$PROJECT_ROOT" ]; then
    cd "$PROJECT_ROOT" || return 1
  else
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [ -n "$top" ]; then
      cd "$top" || return 1
    else
      echo "PROJECT_ROOT is not set and current folder is not inside a Git repo."
      return 1
    fi
  fi
}

agentdesk-sessions() {
  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  zellij list-sessions
}

agentdesk-delete() {
  local force=""
  local session_name=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --force|-f)
        force="--force"
        ;;
      -h|--help)
        cat <<'HELP'
Usage:
  agentdesk-delete <session-name>
  agentdesk-delete --force <session-name>
  agentdesk-delete <session-name> --force

Aliases:
  agentdesk-kill
HELP
        return 0
        ;;
      *)
        if [ -z "$session_name" ]; then
          session_name="$1"
        else
          echo "Unexpected argument: $1"
          return 1
        fi
        ;;
    esac
    shift
  done

  if [ -z "$session_name" ]; then
    echo "Please specify the session name to delete."
    echo
    echo "Existing sessions:"
    zellij list-sessions 2>/dev/null || true
    return 1
  fi

  if [ -n "$force" ]; then
    zellij delete-session "$session_name" --force
  else
    zellij delete-session "$session_name"
  fi
}

agentdesk-kill() {
  agentdesk-delete "$@"
}

codexpane() {
  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex" \
    --floating \
    --width "45%" \
    --height "85%" \
    --x "55%" \
    --y "5%" \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex - project root:' \"\$PWD\" && codex"
}

codexright() {
  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex" \
    --direction right \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex - project root:' \"\$PWD\" && codex"
}

codexcritic() {
  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v codex >/dev/null 2>&1; then
    echo "codex command not found."
    return 1
  fi

  zellij run \
    --name "Codex Critic" \
    --floating \
    --width "50%" \
    --height "85%" \
    --x "50%" \
    --y "5%" \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Codex Critic - project root:' \"\$PWD\" && codex"
}

claudepane() {
  local root="${PROJECT_ROOT:-$PWD}"

  if ! command -v zellij >/dev/null 2>&1; then
    echo "zellij is not installed."
    return 1
  fi

  if ! command -v claude >/dev/null 2>&1; then
    echo "claude command not found."
    return 1
  fi

  zellij run \
    --name "Claude" \
    --direction right \
    -- zsh -lc "source \"\$HOME/.config/agentdesk/agentdesk.zsh\" 2>/dev/null || true; cd \"$root\" && echo 'Claude Code - project root:' \"\$PWD\" && claude"
}

agentprompt() {
  cat <<'PROMPT'
You are working from a project root that may either be a Git repo itself or may contain several child Git repositories.

First read:
- .workflow_artifacts/repos.md
- .workflow_artifacts/current-plan.md if present
- .workflow_artifacts/lessons-learned.md if relevant

Repository convention:
- If repos.md contains ".", the project root itself is the repo.
- If repos.md contains child paths, choose the relevant child repo before editing.

Before editing, identify which repo contains the relevant code.

For every task, maintain:
- the current plan in .workflow_artifacts/current-plan.md
- important review notes in .workflow_artifacts/review.md
- durable lessons in .workflow_artifacts/lessons-learned.md

When making changes, report:
- repo path
- files changed
- tests run from that repo
- cross-repo assumptions
PROMPT
}

codexcriticprompt() {
  cat <<'PROMPT'
You are the critic agent, not the primary implementation agent.

Read:
- .workflow_artifacts/current-plan.md
- .workflow_artifacts/repos.md
- current git diff across relevant repos

Do not edit files unless explicitly asked.

Return:
- critical risks
- likely bugs
- missing tests
- cross-repo assumptions
- suggested minimal changes
PROMPT
}
EOF
fi

write_if_changed "$AGENTDESK_HELPERS" "$helpers_tmp" "agentdesk-helpers"

echo
echo "Step 3: write/update Zellij layout if changed"

mkdir -p "$ZELLIJ_LAYOUT_DIR"
write_standard_layout "$ZELLIJ_LAYOUT"

echo
echo "Step 3b: configure Zellij copy_command"

_detected_backend="$(detect_clipboard_backend)"
_detected_label="$(clipboard_backend_label "$_detected_backend")"
echo "Detected clipboard backend: $_detected_label"

_chosen_backend="$_detected_backend"
if [ -t 0 ]; then
  echo "  1) $_detected_label  [detected, default]"
  echo "  2) macOS (pbcopy)"
  echo "  3) Linux Wayland (wl-copy)"
  echo "  4) Linux X11 (xclip -selection clipboard)"
  printf "Select clipboard backend [1]: "
  read -r _choice || true
  case "${_choice:-1}" in
    1|"") _chosen_backend="$_detected_backend" ;;
    2)    _chosen_backend="pbcopy" ;;
    3)    _chosen_backend="wl-copy" ;;
    4)    _chosen_backend="xclip -selection clipboard" ;;
    *)    echo "Invalid choice; using detected backend."; _chosen_backend="$_detected_backend" ;;
  esac
fi

mkdir -p "$ZELLIJ_CONFIG_DIR"

if [ ! -f "$ZELLIJ_CONFIG" ]; then
  printf 'copy_command "%s"\ncopy_on_select true\n' "$_chosen_backend" > "$ZELLIJ_CONFIG"
  echo "Created: $ZELLIJ_CONFIG (with copy_command \"$_chosen_backend\")"
elif grep -qE '^\s*copy_command\s' "$ZELLIJ_CONFIG" 2>/dev/null; then
  _existing_val="$(grep -E '^\s*copy_command\s' "$ZELLIJ_CONFIG" | head -1 | sed 's/.*copy_command[[:space:]]*"\(.*\)".*/\1/')"
  if [ "$_existing_val" = "$_chosen_backend" ]; then
    echo "Unchanged: copy_command already set to \"$_chosen_backend\""
  else
    _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
    cp "$ZELLIJ_CONFIG" "$_cfg_backup"
    echo "Backup created: $_cfg_backup"
    sed -i.tmp "s|^\([[:space:]]*\)copy_command .*|\1copy_command \"$_chosen_backend\"|" "$ZELLIJ_CONFIG"
    rm -f "${ZELLIJ_CONFIG}.tmp"
    echo "Updated copy_command to \"$_chosen_backend\" in $ZELLIJ_CONFIG"
  fi
elif grep -qE '^\s*//\s*copy_command\s' "$ZELLIJ_CONFIG" 2>/dev/null; then
  _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
  cp "$ZELLIJ_CONFIG" "$_cfg_backup"
  echo "Backup created: $_cfg_backup"
  # Use awk to replace only the FIRST commented copy_command line.
  # Zellij's default config has three example lines (x11/wayland/osx); sed would
  # uncomment all three and produce multiple active directives.
  awk -v cmd="$_chosen_backend" '
    !replaced && /^[[:space:]]*\/\/[[:space:]]*copy_command[[:space:]]/ {
      print "copy_command \"" cmd "\""
      replaced = 1
      next
    }
    { print }
  ' "$ZELLIJ_CONFIG" > "${ZELLIJ_CONFIG}.tmp" && mv "${ZELLIJ_CONFIG}.tmp" "$ZELLIJ_CONFIG"
  echo "Uncommented and set copy_command to \"$_chosen_backend\" in $ZELLIJ_CONFIG"
else
  _cfg_backup="${ZELLIJ_CONFIG}.copy-cmd.$(timestamp).bak"
  cp "$ZELLIJ_CONFIG" "$_cfg_backup"
  echo "Backup created: $_cfg_backup"
  printf '\ncopy_command "%s"\n' "$_chosen_backend" >> "$ZELLIJ_CONFIG"
  echo "Appended copy_command \"$_chosen_backend\" to $ZELLIJ_CONFIG"
fi

# Keybindings: Ghostty Option-as-Alt check and the Zellij key map merge.
echo
echo "Step 3c: Ghostty and Zellij keybindings"
run_keybind_steps

echo
echo "Step 4: patch ~/.zshrc if needed"
patch_zshrc_once

echo
echo "Step 5: checks"

check_command "brew" "Homebrew"
check_command "zellij" "Zellij"
check_command "lazygit" "lazygit"
check_command "fzf" "fzf"
check_command "git" "git"
check_command "python3" "python3"

if command -v claude >/dev/null 2>&1; then
  echo "OK: Claude Code -> $(command -v claude)"
else
  echo "MISSING: claude"
  echo "Claude Code is not installed by this script."
fi

if command -v codex >/dev/null 2>&1; then
  echo "OK: Codex -> $(command -v codex)"
else
  echo "MISSING: codex"
  echo "Codex is optional. codexpane/codexcritic require it."
fi

if [ -d "/Applications/Ghostty.app" ]; then
  echo "OK: Ghostty app found"
else
  echo "Ghostty app not found in /Applications. Agent Desk still works in another terminal."
fi

echo
echo "============================================================"
echo "Setup complete."
echo "============================================================"
echo
echo "Run:"
echo "  source ~/.zshrc"
echo
echo "Then from a repo or project root:"
echo "  agentdesk"
echo "  agentdesk my-session"
echo "  agentdesk --name my-session"
echo "  agentdesk --mode solo|duo|trio"
echo "  agentdesk claude codex shell"
echo "  agentdesk claude ccr shell"
echo "  agentdesk --pick                  (force layout picker, even if a layout is saved)"
echo
echo "Useful commands:"
echo "  repos"
echo "  grepos"
echo "  gitreview"
echo "  gitrootreview"
echo "  crepo"
echo "  croot"
echo "  codexpane"
echo "  codexright"
echo "  codexcritic"
echo "  agentdesk-sessions"
echo "  agentdesk-attach <session-name>"
echo "    (agentdesk prompts to attach-or-new when a matching session exists; use agentdesk-attach to resume directly)"
echo "  agentdesk-delete <session-name>"
echo "  agentdesk-delete --force <session-name>"
echo "  agentprompt"
echo "  codexcriticprompt"
echo
