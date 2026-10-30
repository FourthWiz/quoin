"""Where OpenCode looks for configuration, and how to read what it finds.

The discovery helpers here were part of the doctor; the launch checks and the
effective-configuration inventory share them so every consumer walks the same
directories in the same order. Nothing here reads the process environment:
callers pass mappings in. Only `errors`, `jsonio` and `paths` may be imported from the
rest of the adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import errors, jsonio

_LEGACY_TRUTHY = frozenset(("true", "1"))


def display_path(p, roots: List[Tuple[str, str]]) -> str:
    """Render `p` under the longest matching root in `roots`.

    `roots` is a list of `(prefix, rendered_name)` pairs. Every root and
    `p` are normalized with `os.path.abspath` (never realpath) and matched
    by path components, so a sibling that merely shares a string prefix
    (`/t/ocd-x` against root `/t/ocd`) does not match. When no root
    matches, `p` is printed absolute (never realpath-resolved).
    """
    norm_p = os.path.abspath(str(p))
    p_parts = Path(norm_p).parts
    best_parts: Optional[Tuple[str, ...]] = None
    best_name: Optional[str] = None
    for prefix, rendered_name in roots:
        norm_prefix = os.path.abspath(str(prefix))
        prefix_parts = Path(norm_prefix).parts
        if len(prefix_parts) > len(p_parts):
            continue
        if p_parts[: len(prefix_parts)] != prefix_parts:
            continue
        if best_parts is None or len(prefix_parts) > len(best_parts):
            best_parts = prefix_parts
            best_name = rendered_name
    if best_parts is None:
        return norm_p
    rest = "/".join(p_parts[len(best_parts):])
    name = best_name or ""
    if not rest:
        return name
    if name.endswith("/"):
        return name + rest
    return name + "/" + rest


class ConfigUnreadable(Exception):
    """Internal sentinel: a config file exists but could not be read or
    parsed as a JSONC object. Callers translate it into their own refusal or finding."""


def xdg_config_dir(env, home) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    return Path(base) / "opencode"


def worktree_root(root) -> Path:
    """Nearest ancestor of `root` holding `.git` (file or dir); the
    filesystem root if none is found, matching OpenCode's non-git worktree
    fallback (project/project.ts L217)."""
    current = Path(os.path.abspath(str(root)))
    while True:
        # `os.path.lexists` (unlike `Path.exists()`) swallows any `OSError`
        # — including `EACCES` on Python 3.10-3.13, where `Path.exists()`
        # re-raises a permission error instead of treating it as absent.
        if os.path.lexists(str(current / ".git")):
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


def dir_chain(worktree: Path, root: Path) -> List[Path]:
    """`root`, its parent, ... up to and including `worktree` (nearest
    first). `root` must be `worktree` or one of its descendants."""
    chain: List[Path] = []
    current = Path(root)
    worktree = Path(worktree)
    while True:
        chain.append(current)
        if current == worktree:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return chain


def build_roots(root, env, home) -> List[Tuple[str, str]]:
    roots: List[Tuple[str, str]] = [(str(root), ".")]
    config_dir = env.get("OPENCODE_CONFIG_DIR")
    if config_dir:
        roots.append((config_dir, "$OPENCODE_CONFIG_DIR"))
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        roots.append((xdg, "$XDG_CONFIG_HOME"))
    roots.append((str(home), "~"))
    config_file = env.get("OPENCODE_CONFIG")
    if config_file:
        roots.append((config_file, "$OPENCODE_CONFIG"))
    return roots


def strip_jsonc(text: str) -> str:
    """Strip `//` and `/* */` comments outside string literals, then strip
    a trailing comma before a closing `}` or `]`."""
    out: List[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    stripped = "".join(out)

    # Strip a trailing comma before a closing `}` or `]`, outside string
    # literals only: the naive whole-text regex this replaced would also
    # strip a `,}`/`,]` substring sitting inside a JSON string *value*
    # (comments are already gone above, so this second string-aware pass
    # only has to track quoting, not comments too).
    result: List[str] = []
    in_string = False
    escape = False
    j = 0
    m = len(stripped)
    while j < m:
        c = stripped[j]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            j += 1
            continue
        if c == '"':
            in_string = True
            result.append(c)
            j += 1
            continue
        if c == ",":
            k = j + 1
            while k < m and stripped[k] in " \t\r\n":
                k += 1
            if k < m and stripped[k] in "}]":
                j += 1
                continue  # drop the trailing comma; the closing brace/bracket is appended on its own turn
        result.append(c)
        j += 1
    return "".join(result)


def _read_capped_text(path) -> Optional[str]:
    """UTF-8 text of a regular file (a symlink to one is followed, as
    OpenCode does), read through one non-blocking descriptor and capped at
    `MAX_FILE_BYTES`. `None` for a FIFO, device, oversized or unreadable
    file, so a hostile path can neither hang nor exhaust the launch."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return None
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_config_file(path: Path) -> Optional[dict]:
    """`None` when the file does not exist. Raises `ConfigUnreadable` when
    it exists but cannot be read or parsed as a JSON object."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigUnreadable()
    if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        raise ConfigUnreadable()
    text = _read_capped_text(path)
    if text is None:
        raise ConfigUnreadable()
    try:
        payload = json.loads(strip_jsonc(text))
    except (json.JSONDecodeError, ValueError, RecursionError):
        # `ValueError` also covers a bare int-string-conversion limit error
        # (a huge integer literal, e.g. `subagent_depth` with thousands of
        # digits) that `json.loads` can raise outside `JSONDecodeError` —
        # not a parse error by class, but this config file is equally
        # unreadable either way.
        raise ConfigUnreadable()
    if not isinstance(payload, dict):
        raise ConfigUnreadable()
    return payload


def project_config_dirs(worktree: Path, root: Path) -> List[Path]:
    """Worktree first, project root last: later files override earlier
    ones, so the directory nearest `root` wins."""
    return list(reversed(dir_chain(worktree, root)))


def read_config_files(root: Path, worktree: Path, env, home) -> Tuple[List[Tuple[Path, dict]], List[Path]]:
    """The configuration documents OpenCode merges, in load order, and the
    paths that exist but could not be read."""
    docs: List[Tuple[Path, dict]] = []
    unreadable: List[Path] = []

    def _try(path: Path) -> None:
        try:
            payload = load_config_file(path)
        except ConfigUnreadable:
            unreadable.append(path)
            return
        if payload is not None:
            docs.append((path, payload))

    g_xdg = xdg_config_dir(env, home)
    for name in ("config.json", "opencode.json", "opencode.jsonc"):
        _try(g_xdg / name)

    config_file_env = env.get("OPENCODE_CONFIG")
    if config_file_env:
        _try(Path(config_file_env))

    project_disabled = (env.get("OPENCODE_DISABLE_PROJECT_CONFIG") or "").lower() in _LEGACY_TRUTHY
    project_dirs: List[Path] = []
    if not project_disabled:
        project_dirs = project_config_dirs(worktree, root)
        for d in project_dirs:
            _try(d / "opencode.json")
            _try(d / "opencode.jsonc")

    # `project_dirs` are worktree-chain directories that each need their own
    # `.opencode` subdir appended. `home_opencode` and `config_dir_env`, by
    # contrast, already name a `.opencode`-suffixed (or override) directory
    # and must be read directly — appending another `.opencode` under them
    # reads a directory OpenCode never does (`config/paths.ts` L23-39: the
    # walked-up home entry stops at `home`, so it is exactly `home/.opencode`;
    # `config/config.ts` L438-448 reads `opencode.json(c)` directly from any
    # directory in that list whose name already ends in `.opencode`, or that
    # equals `OPENCODE_CONFIG_DIR`, with no extra subdir appended).
    dotted_dirs: List[Path] = list(project_dirs)  # each dir's own `.opencode` subdir, appended below
    already_dotted: set = set()
    home_opencode = Path(home) / ".opencode"
    # `os.path.isdir` (unlike `Path.is_dir()`) swallows any `OSError` —
    # including `EACCES` on Python 3.10-3.13 for an unreadable parent dir.
    if os.path.isdir(str(home_opencode)):
        dotted_dirs.append(home_opencode)
        already_dotted.add(os.path.abspath(str(home_opencode)))
    config_dir_env = env.get("OPENCODE_CONFIG_DIR")
    if config_dir_env:
        dotted_dirs.append(Path(config_dir_env))
        already_dotted.add(os.path.abspath(str(config_dir_env)))

    seen: set = set()
    for d in dotted_dirs:
        candidate = d if os.path.abspath(str(d)) in already_dotted else d / ".opencode"
        norm = os.path.abspath(str(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        _try(candidate / "opencode.json")
        _try(candidate / "opencode.jsonc")

    return docs, unreadable


# ---------------------------------------------------------------- collection

LAYERS = (
    "remote-wellknown", "account", "global", "compiled", "project", "dot-opencode",
    "home-opencode", "config-dir-env", "managed", "managed-prefs", "env", "claude-compat", "data",
)
SOURCE_STATES = ("read", "absent", "unreadable", "truncated", "busy")
MAX_FILE_BYTES = 1_048_576
DEFAULT_MAX_ENTRIES = 5000
SQLITE_BACKOFF = (0.1, 0.3, 0.9)
# The backoff above is the only real wait; a busy connection gives up fast.
SQLITE_CONNECT_TIMEOUT = 0.05
CONFIG_ENV_NAMES = (
    "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_AUTH_CONTENT",
    "OPENCODE_DB", "OPENCODE_MODELS_URL", "OPENCODE_PERMISSION",
)
BENIGN_KEYS = frozenset(("$schema", "theme", "keybinds", "tui", "username", "layout"))
COMPILED_KEYS = frozenset(("model", "small_model", "share", "autoupdate"))
_AGENT_SUBDIRS = (("agent", "agent"), ("agents", "agent"), ("mode", "agent"), ("modes", "agent"),
                  ("command", "command"), ("commands", "command"))
_PLUGIN_SUBDIRS = ("plugin", "plugins")
_TOOL_SUBDIRS = ("tool", "tools")
_SKILL_SUBDIRS = ("skill", "skills")
_MAX_WALK = 20000


@dataclass(frozen=True)
class Source:
    kind: str
    layer: str
    display: str
    sha256: Optional[str]
    state: str


@dataclass(frozen=True)
class Entry:
    kind: str
    layer: str
    source_index: int
    key_path: str
    name: str
    value_sha256: Optional[str]
    owned: bool
    facts: Mapping[str, Any]


@dataclass
class Inventory:
    sources: List[Source]
    entries: List[Entry]
    docs: List[Tuple[Path, dict]] = field(default_factory=list, repr=False)
    markdown: List[Tuple[Path, str, str]] = field(default_factory=list, repr=False)  # (path, kind, name)
    plugin_dirs: List[Tuple[Path, str]] = field(default_factory=list, repr=False)
    fetch_watch: List[Entry] = field(default_factory=list, repr=False)
    fetch_dirs: List[Tuple[Path, str]] = field(default_factory=list, repr=False)  # (directory, layer), in memory only
    config_env: List[Entry] = field(default_factory=list, repr=False)
    unreadable: List[Path] = field(default_factory=list, repr=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def value_digest(value: Any) -> Optional[str]:
    try:
        return sha256_hex(jsonio.dump_canonical(value))
    except (TypeError, ValueError, RecursionError):
        return None


def name_digest(text: str) -> str:
    return "h-" + sha256_hex(text.encode("utf-8", "surrogatepass"))[:32]


def plugin_dir_states(bases: Iterable[Path]) -> List[Tuple[Path, str]]:
    """Plugin directories under `bases` that need attention, in visit order:
    `populated` when one holds a top-level `.ts` or `.js` file that is not a
    directory, `unreadable` when listing it failed."""
    found: List[Tuple[Path, str]] = []
    for base in bases:
        for sub in _PLUGIN_SUBDIRS:
            directory = Path(base) / sub
            try:
                populated = os.path.isdir(str(directory)) and any(
                    entry.name.endswith((".ts", ".js")) and not entry.is_dir(follow_symlinks=False)
                    for entry in os.scandir(str(directory))
                )
            except OSError:
                found.append((directory, "unreadable"))
                continue
            if populated:
                found.append((directory, "populated"))
    return found


_WS = " \t"
_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
# Printable ASCII plus tab, CR and LF. Anything else in a frontmatter block
# (a non-ASCII space, a control character) is a place where Python and the
# YAML loader OpenCode uses could disagree, so the block is unknown.
_FRONTMATTER_CHARS = re.compile(r"[^\t\r\n\x20-\x7e]")
_DOCUMENT_END = re.compile(r"\.\.\.(?:[ \t]+(?:#.*)?)?")
_COMMENT = re.compile(r"[ \t]#")
# Plain scalars YAML resolves to something other than a string: null, a
# boolean, a number (including `.inf` and `0x1f`) or a date.
_NON_STRING_PLAIN = re.compile(r"~|null|true|false|[-+.0-9].*", re.IGNORECASE)


def _unquote(text: str) -> Tuple[str, bool]:
    """(value, quoted). A quoted scalar that closes on this line, followed by
    nothing but spaces, tabs or a comment, is returned without its quotes (a
    doubled single quote inside single quotes is one quote). Anything else
    comes back as it was, with `quoted` False."""
    text = text.strip(_WS)
    if len(text) < 2 or text[0] not in "\"'":
        return text, False
    quote = text[0]
    end = text.find(quote, 1)
    while quote == "'" and end != -1 and text[end + 1:end + 2] == "'":
        end = text.find("'", end + 2)
    if end == -1:
        return text, False
    tail = text[end + 1:]
    if tail.strip(_WS) and not (tail[:1] in _WS and tail.lstrip(_WS).startswith("#")):
        return text, False
    inner = text[1:end]
    return (inner.replace("''", "'") if quote == "'" else inner), True


def _closed_flow(value: str) -> bool:
    """True when a flow value closes on its own line with nothing but spaces,
    tabs or a comment after it. Quotes are not tracked, so a bracket inside a
    quoted item can only make the value look unclosed (and so unknown)."""
    depth = 0
    for position, char in enumerate(value):
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                tail = value[position + 1:]
                return not tail.strip(_WS) or (tail[:1] in _WS and tail.lstrip(_WS).startswith("#"))
    return False


def _blank_or_comment(line: str) -> bool:
    stripped = line.lstrip(_WS)
    return not stripped or stripped.startswith("#")


def scan_frontmatter(text: str) -> Dict[str, Any]:
    """`_scan_frontmatter` with the candidate-model list left out when empty."""
    facts = _scan_frontmatter(text)
    if not facts.get("model_readings"):
        facts.pop("model_readings", None)
    return facts


def _scan_frontmatter(text: str) -> Dict[str, Any]:  # noqa: C901 - one strict grammar, read top to bottom
    """Read the top-level `model`, `permission` and `tools` keys of a markdown
    file's YAML frontmatter without a YAML parser.

    The block is closed where gray-matter closes it (the first later line that
    starts with `---`) and is read with a deliberately small grammar: printable
    ASCII only, one `key: value` per unindented line, plain or single-line
    quoted values. Anything outside that grammar (a non-ASCII or control
    character, block scalars, tags, anchors, multi-line flow or quoted values,
    duplicate keys, a missing closing fence, an indented root mapping, escaped
    or non-identifier keys, a language-tagged opening fence, content after a
    `...` document end) is reported as `unknown`, which the work profile
    refuses. An unquoted `model` that holds a colon stays known but carries
    `model_readings`, every string OpenCode might load for it. The `permission` and
    `tools` keys are noted from the key alone, whatever shape their value has."""
    unknown: Dict[str, Any] = {"model": None, "has_permission": False, "has_tools": False, "unknown": True}
    clean: Dict[str, Any] = {"model": None, "has_permission": False, "has_tools": False, "unknown": False}
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        head = re.split(r"\r\n|\r|\n", text, maxsplit=3)[:3]
        if any(line.strip(_WS) == "---" for line in head):
            return unknown
        return clean
    rest = text[3:]
    newline = rest.find("\n")
    if newline == -1 or not re.fullmatch(r"[ \t]*\r?", rest[:newline]):
        return unknown  # `----`, `---yaml`, `---json`, or a fence line ended by a bare CR
    close = rest.find("\n---")
    if close == -1:
        return unknown  # gray-matter would read the whole file as frontmatter
    body = rest[newline + 1:close] if close > newline else ""
    if _FRONTMATTER_CHARS.search(rest[:close]):
        return unknown
    if body.endswith("\r"):
        body = body[:-1]  # the closing fence's own CRLF, not a bare CR
    if "\r" in body and any(
        "\r" in raw and (raw.count(":") > 1 or raw.lstrip(_WS).startswith("#"))
        for raw in re.split(r"\r?\n", body)
    ):
        # A comment line holding a bare CR is read differently by js-yaml and
        # gray-matter, which can drop the keys after it.
        # OpenCode's retry sanitizer splits lines on LF and CRLF only, so a
        # bare CR can fold the next key into a model value it then keeps raw.
        return unknown
    block = re.split(r"\r\n|\r|\n", body)  # js-yaml breaks lines on a bare CR too
    for position, line in enumerate(block):
        if line.startswith("..."):
            if not _DOCUMENT_END.fullmatch(line) or not all(_blank_or_comment(ln) for ln in block[position + 1:]):
                return unknown
            block = block[:position]
            break
    facts = dict(clean)
    unknown["model_readings"] = readings = []  # shared with every early `unknown` return below
    seen = set()
    first = next((line for line in block if not _blank_or_comment(line)), None)
    if first is not None and first[0] in _WS:
        return unknown  # an indented root mapping: every key below is top level
    for index, line in enumerate(block):
        stripped = line.lstrip(_WS)
        if not stripped:
            continue
        if "\t" in line[: len(line) - len(stripped)] and not stripped.startswith("#"):
            return unknown  # js-yaml 3 counts only spaces as indentation
        if line[0] in " \t#":
            continue
        if line.startswith(("{", "[", "]", "}", "<<", "&", "*", "%", "-", "?", "!", "|", ">", ",", "@", "`")):
            return unknown
        if line[0] in "\"'":
            raw_key, plain = _unquote_key(line)
            if raw_key is None:
                return unknown
            key, rest_of_line = raw_key, plain
        else:
            key, sep, rest_of_line = line.partition(":")
            if not sep:
                return unknown
            key = key.rstrip(_WS)
        if rest_of_line[:1] not in ("", " ", "\t"):
            return unknown  # `a:b` is one plain scalar to YAML, not a key
        if not key or not all(c in _KEY_CHARS for c in key):
            return unknown
        if key in seen:
            return unknown
        seen.add(key)
        value = rest_of_line.strip(_WS)
        if value and value[0] in "\"'" and not _unquote(value)[1]:
            return unknown  # a quoted value that does not close on its own line
        if value and value[0] in "[{" and not _closed_flow(value):
            return unknown  # a flow value continued on a later line, or followed by more text
        if key == "permission":
            facts["has_permission"] = True
        elif key == "tools":
            facts["has_tools"] = True
        elif key == "model":
            if value.startswith(("|", ">", "!", "&", "*", "{", "[", "@", "`", "%")):
                return unknown
            text_value, quoted = _unquote(value)
            if quoted and value[0] == '"' and "\\" in text_value:
                return unknown
            if not quoted and ":" in value:
                # OpenCode parses again after a parse error, rewriting an
                # unquoted `key: value` that holds a colon into a block scalar
                # that keeps the raw value, comment included, and any indented
                # comment lines after it. Which reading it loads depends on
                # whether the rest of the file parses, so report every one
                # and let the policy check each against the allow-list.
                stripped_value = "" if value.startswith("#") else _COMMENT.split(value, 1)[0].rstrip(_WS)
                tail = []
                for following in block[index + 1:]:
                    if following and following[0] in " \t" or not following.strip(_WS):
                        tail.append(following.strip(_WS))
                    else:
                        break
                while tail and not tail[-1]:
                    tail.pop()
                found = [stripped_value, value]
                for count in range(1, len(tail) + 1):
                    found.append("\n".join([value] + tail[:count]))
                readings.extend(r for r in found if r and r not in readings)
                facts["model_readings"] = readings
            if not quoted:
                text_value = "" if text_value.startswith("#") else _COMMENT.split(text_value, 1)[0].rstrip(_WS)
            later = next((ln for ln in block[index + 1:] if not _blank_or_comment(ln)), None)
            if later is not None and later[0] in _WS:
                return unknown  # a nested value under an empty model, or a plain scalar folded onto it
            if not quoted and _NON_STRING_PLAIN.fullmatch(text_value):
                return unknown  # a non-string model: OpenCode would not read it as a model name
            if text_value:
                facts["model"] = text_value
    return facts


def _unquote_key(line: str) -> Tuple[Optional[str], str]:
    """For a line whose key is quoted: (key, text after the colon), or
    (None, "") when the key is not a closed, escape-free quoted scalar
    followed by a colon."""
    quote = line[0]
    end = line.find(quote, 1)
    while quote == "'" and end != -1 and line[end + 1:end + 2] == "'":
        end = line.find("'", end + 2)
    if end == -1:
        return None, ""
    inner = line[1:end]
    if quote == '"' and "\\" in inner:
        return None, ""
    after = line[end + 1:].lstrip(_WS)
    if not after.startswith(":"):
        return None, ""
    return (inner.replace("''", "'") if quote == "'" else inner), after[1:]


class _Builder:
    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        self.sources: List[Source] = []
        self.entries: List[Entry] = []
        self._counts: Dict[int, int] = {}
        self._truncated: set = set()

    def source(self, kind: str, layer: str, display: str, sha256: Optional[str], state: str) -> int:
        self.sources.append(Source(kind, layer, display, sha256, state))
        return len(self.sources) - 1

    def set_state(self, index: int, state: str) -> None:
        self.sources[index] = replace(self.sources[index], state=state)

    def entry(
        self, index: int, kind: str, key_path: str, name: str, value: Any = None, *,
        owned: bool = False, facts: Optional[Mapping[str, Any]] = None, digest: Optional[str] = None,
    ) -> Optional[Entry]:
        count = self._counts.get(index, 0)
        if count >= self.max_entries:
            self._truncated.add(index)
            return None
        self._counts[index] = count + 1
        made = Entry(
            kind, self.sources[index].layer, index, key_path, name,
            digest if digest is not None else (value_digest(value) if value is not None else None),
            owned, dict(facts or {}),
        )
        self.entries.append(made)
        return made

    def finish(self) -> Tuple[List[Source], List[Entry]]:
        for index in self._truncated:
            self.set_state(index, "truncated")
        return self.sources, self.entries


def _document_entries(builder: _Builder, index: int, doc: Mapping[str, Any], compiled_doc: Mapping[str, Any]) -> None:
    compiled_providers = compiled_doc.get("provider")
    compiled_names = set(compiled_providers) if isinstance(compiled_providers, dict) else set()

    def named(kind: str, key: str, value: Any, facts_of: Callable[[Any], Mapping[str, Any]] = lambda v: {}) -> None:
        if isinstance(value, dict):
            for name, sub in value.items():
                builder.entry(index, kind(sub) if callable(kind) else kind, key, str(name), sub, facts=facts_of(sub))
        else:
            builder.entry(index, kind(value) if callable(kind) else kind, key, key, value)

    def agent_facts(sub: Any) -> Mapping[str, Any]:
        if not isinstance(sub, dict):
            return {"unknown": True}
        model = sub.get("model")
        return {
            "model": model if isinstance(model, str) else None,
            "has_permission": "permission" in sub,
            "has_tools": "tools" in sub,
            "unknown": "model" in sub and not isinstance(model, str),
        }

    def mcp_kind(sub: Any) -> str:
        if isinstance(sub, dict):
            kind = sub.get("type")
            if kind == "remote":
                return "mcp-remote"
            if kind == "local":
                return "mcp-local"
            if set(sub) <= {"enabled"}:
                return "mcp-toggle"
        return "mcp-local"

    for key, value in doc.items():
        if key == "plugin":
            if isinstance(value, list):
                for item in value:
                    builder.entry(index, "plugin", key, name_digest(str(item)), item)
            else:
                builder.entry(index, "plugin", key, key, value)
        elif key == "provider":
            named("provider", key, value)
        elif key == "enabled_providers":
            for item in value if isinstance(value, list) else [value]:
                builder.entry(index, "provider", key, str(item), item)
        elif key == "disabled_providers":
            builder.entry(index, "disabled-providers", key, key, value)
        elif key == "mcp":
            named(mcp_kind, key, value, lambda sub: {"enabled": sub.get("enabled") if isinstance(sub, dict) else None})
        elif key == "lsp":
            named("lsp", key, value)
        elif key == "formatter":
            named("formatter", key, value)
        elif key == "tools":
            named("tool", key, value)
        elif key in ("agent", "mode"):
            named("agent", key, value, agent_facts)
        elif key == "command":
            named("command", key, value, lambda sub: {
                "model": sub.get("model") if isinstance(sub, dict) and isinstance(sub.get("model"), str) else None,
                "unknown": isinstance(sub, dict) and "model" in sub and not isinstance(sub.get("model"), str),
            })
        elif key == "permission":
            builder.entry(index, "permission", key, key, value)
        elif key == "instructions":
            for item in value if isinstance(value, list) else [value]:
                url = isinstance(item, str) and item.startswith(("http://", "https://"))
                builder.entry(index, "instruction", key, name_digest(str(item)), item, facts={"url": url})
        elif key == "skills":
            if isinstance(value, dict):
                for sub_key, items in value.items():
                    for item in items if isinstance(items, list) else [items]:
                        url = sub_key == "urls" or (isinstance(item, str) and item.startswith(("http://", "https://")))
                        builder.entry(index, "skill", "skills.%s" % sub_key, name_digest(str(item)), item, facts={"url": bool(url)})
            else:
                builder.entry(index, "skill", key, key, value)
        elif key in COMPILED_KEYS:
            builder.entry(index, "compiled-key", key, key, value)
        elif key in BENIGN_KEYS:
            builder.entry(index, "benign-key", key, key, value)
        elif key == "watcher":
            only_ignore = isinstance(value, dict) and set(value) <= {"ignore"}
            builder.entry(index, "benign-key" if only_ignore else "unknown-key", key, key, value)
        else:
            builder.entry(index, "unknown-key", key, key, value)
    # Provider entries know whether the compiled configuration carries them.
    for position, entry in enumerate(builder.entries):
        if entry.source_index == index and entry.kind == "provider":
            builder.entries[position] = replace(entry, facts={**entry.facts, "in_compiled_set": entry.name in compiled_names})


def _read_text(path: Path) -> Optional[str]:
    got = jsonio.read_regular_bytes(path, max_bytes=MAX_FILE_BYTES)
    if got is None:
        return None
    try:
        return got[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _query_account(path: Path, sleep: Callable[[float], None]) -> Tuple[str, Dict[str, Any]]:
    """(state, facts) for one session database. Read-only URI, never hashed."""
    uri = Path(path).as_uri() + "?mode=ro"
    delays = list(SQLITE_BACKOFF)
    while True:
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_CONNECT_TIMEOUT)
            try:
                row = connection.execute("SELECT active_account_id FROM account_state WHERE id = 1").fetchone()
            finally:
                connection.close()
            return "read", {"account": bool(row and row[0])}
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "no such table" in text:
                return "read", {"account": False}
            if "database is locked" in text or "database is busy" in text:
                if delays:
                    sleep(delays.pop(0))
                    continue
                return "busy", {"busy": True}
            return "unreadable", {"unknown": True}
        except (sqlite3.Error, OSError, ValueError):
            return "unreadable", {"unknown": True}


def _lock_covers(directory: Path) -> bool:
    """node_modules exists and the lock file's root entry lists every
    dependency package.json declares, so OpenCode skips the install."""
    if not os.path.isdir(str(directory / "node_modules")):
        return False
    try:
        declared = json.loads(_read_capped_text(directory / "package.json") or "{}").get("dependencies") or {}
    except (ValueError, AttributeError):
        declared = {}
    if not declared:
        return True
    try:
        lock_text = _read_capped_text(directory / "package-lock.json")
        if lock_text is None:
            return False
        lock = json.loads(lock_text)
        root = (lock.get("packages") or {}).get("") or {}
        locked = root.get("dependencies") or {}
    except (ValueError, AttributeError):
        return False
    return isinstance(declared, dict) and isinstance(locked, dict) and set(declared) <= set(locked)


def _walk_markdown(directory: Path, problems: List[str]):
    """Yield `(path, is_symlinked_dir)` for every markdown file under
    `directory`, and for every symlinked subdirectory (not descended into, so
    the caller can record it). A directory that crosses the entry bound yields
    nothing from that listing (the source is marked truncated, so it still fails
    closed). Appends `"unreadable"` to `problems` for a
    directory that cannot be listed and `"truncated"` when the walk stops at
    the entry bound. Each listing is read one entry at a time, so a single huge
    directory is never read past the bound."""
    count = 0
    pending = [str(directory)]
    while pending:
        current = pending.pop()
        subdirs: List[str] = []
        files: List[str] = []
        links: List[str] = []
        try:
            with os.scandir(current) as listing:
                for item in listing:
                    count += 1
                    if count > _MAX_WALK:
                        problems.append("truncated")
                        return
                    try:
                        is_dir = item.is_dir()
                        is_link = is_dir and item.is_symlink()
                    except OSError:
                        is_dir = is_link = False
                    if is_link:
                        links.append(item.name)
                    elif is_dir:
                        subdirs.append(item.name)
                    else:
                        files.append(item.name)
        except OSError:
            problems.append("unreadable")
            continue
        descend = [os.path.join(current, name) for name in sorted(subdirs)]
        for name in sorted(links):
            yield Path(os.path.join(current, name)), True
        for name in sorted(files):
            if name.endswith(".md"):
                yield Path(current) / name, False
        pending.extend(reversed(descend))


def collect(
    *,
    cwd, scan_env: Mapping[str, str], ambient_env: Optional[Mapping[str, str]] = None, home,
    compiled_path=None, compiled_doc: Optional[Mapping[str, Any]] = None,
    owned: Optional[Mapping[str, str]] = None, data_dir=None, managed_dir=None,
    managed_prefs: Optional[Sequence[Any]] = None, plutil: Optional[Callable[[Path], str]] = None,
    hardening_names: Sequence[str] = (), max_entries: int = DEFAULT_MAX_ENTRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> Inventory:
    """Every configuration source OpenCode 1.18.32 reads for this launch, as
    entries. Never raises for a filesystem condition: each failure is recorded
    on the source that hit it."""
    cwd, home = Path(cwd), Path(home)
    compiled_doc = compiled_doc or {}
    owned = owned or {}
    ambient = scan_env if ambient_env is None else ambient_env
    builder = _Builder(max_entries)
    inv = Inventory([], [])
    roots = build_roots(cwd, scan_env, home)

    def shown(path) -> str:
        return display_path(path, roots)

    def same(a, b) -> bool:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))

    worktree = worktree_root(cwd)
    global_dir = xdg_config_dir(scan_env, home)
    project_dirs = project_config_dirs(worktree, cwd)
    config_dir_env = scan_env.get("OPENCODE_CONFIG_DIR")

    # -- remote well-known config and console accounts (profile data) --------
    if data_dir is not None:
        opencode_data = Path(data_dir) / "opencode"
        auth_path = opencode_data / "auth.json"
        got = jsonio.read_regular_bytes(auth_path, max_bytes=MAX_FILE_BYTES)
        if got is None:
            present = os.path.lexists(str(auth_path))
            builder.source("auth-file", "remote-wellknown", shown(auth_path), None, "unreadable" if present else "absent")
            if present:
                idx = len(builder.sources) - 1
                builder.entry(idx, "remote-config", "auth", "auth.json", facts={"unknown": True}, digest=name_digest("auth.json"))
        else:
            try:
                auth = json.loads(got[0].decode("utf-8"))
                if not isinstance(auth, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError, RecursionError):
                idx = builder.source("auth-file", "remote-wellknown", shown(auth_path), sha256_hex(got[0]), "unreadable")
                builder.entry(idx, "remote-config", "auth", "auth.json", facts={"unknown": True}, digest=name_digest("auth.json"))
            else:
                idx = builder.source("auth-file", "remote-wellknown", shown(auth_path), sha256_hex(got[0]), "read")
                for key in sorted(auth):
                    value = auth[key]
                    kind = value.get("type") if isinstance(value, dict) else None
                    if kind in ("api", "oauth"):
                        continue
                    builder.entry(idx, "remote-config", "auth", name_digest(str(key)), facts={"type": str(kind)}, digest=name_digest(str(key)))
        try:
            databases = sorted(p for p in opencode_data.iterdir() if p.name.endswith(".db") and p.is_file())
        except (FileNotFoundError, NotADirectoryError):
            databases = []
        except OSError:
            # The directory exists but cannot be listed: whether an account is
            # signed in is unknown, which is not the same as no account.
            databases = []
            idx = builder.source("database", "account", shown(opencode_data), None, "unreadable")
            builder.entry(idx, "account-config", "account_state", "opencode", facts={"unknown": True},
                          digest=name_digest("opencode"))
        for database in databases:
            state, facts = _query_account(database, sleep)
            idx = builder.source("database", "account", shown(database), None, state)
            if facts.get("account") or facts.get("busy") or facts.get("unknown"):
                builder.entry(idx, "account-config", "account_state", database.name, facts=facts, digest=name_digest(database.name))

    # -- config documents -----------------------------------------------------
    docs, unreadable = read_config_files(cwd, worktree, scan_env, home)
    inv.docs, inv.unreadable = docs, list(unreadable)
    home_opencode = home / ".opencode"
    dotted = {os.path.abspath(str(d / ".opencode")) for d in project_dirs}

    def layer_of(path: Path) -> str:
        parent = path.parent
        if compiled_path is not None and same(path, compiled_path):
            return "compiled"
        if same(parent, global_dir):
            return "global"
        if config_dir_env and same(parent, config_dir_env):
            return "config-dir-env"
        if same(parent, home_opencode):
            return "home-opencode"
        if os.path.abspath(str(parent)) in dotted:
            return "dot-opencode"
        return "project"

    for path in unreadable:
        builder.source("config-file", layer_of(path), shown(path), None, "unreadable")
    for path, doc in docs:
        layer = layer_of(path)
        idx = builder.source("config-file", layer, shown(path), value_digest(doc), "read")
        if layer == "compiled":
            builder.entry(idx, "compiled-file", "", path.name, doc, owned=True)
            continue
        _document_entries(builder, idx, doc, compiled_doc)
    legacy = global_dir / "config"
    if os.path.lexists(str(legacy)):
        idx = builder.source("legacy-config", "global", shown(legacy), None, "read")
        builder.entry(idx, "legacy-config", "config", "config", digest=name_digest("config"))

    # -- managed files and preferences ---------------------------------------
    if managed_dir is not None:
        for name in ("opencode.json", "opencode.jsonc"):
            path = Path(managed_dir) / name
            try:
                payload = load_config_file(path)
            except ConfigUnreadable:
                builder.source("config-file", "managed", shown(path), None, "unreadable")
                inv.unreadable.append(path)
                continue
            if payload is not None:
                inv.docs.append((path, payload))
                idx = builder.source("config-file", "managed", shown(path), value_digest(payload), "read")
                _document_entries(builder, idx, payload, compiled_doc)
    for plist in managed_prefs or ():
        plist = Path(plist)
        if not os.path.lexists(str(plist)):
            continue
        try:
            payload = json.loads(plutil(plist)) if plutil is not None else None
        except Exception:  # noqa: BLE001 - any conversion failure is an unreadable source
            payload = None
        if not isinstance(payload, dict):
            builder.source("managed-prefs", "managed-prefs", shown(plist), None, "unreadable")
            inv.unreadable.append(plist)
            continue
        inv.docs.append((plist, payload))
        idx = builder.source("managed-prefs", "managed-prefs", shown(plist), value_digest(payload), "read")
        _document_entries(builder, idx, payload, compiled_doc)

    # -- directories: markdown, tools, plugins, skills, fetch watch ----------
    bases: List[Tuple[Path, str]] = [(d / ".opencode", "dot-opencode") for d in project_dirs]
    if os.path.isdir(str(home_opencode)):
        bases.append((home_opencode, "home-opencode"))
    bases.append((global_dir, "global"))
    if config_dir_env:
        bases.append((Path(config_dir_env), "config-dir-env"))
    seen_bases = set()
    for base, layer in bases:
        key = os.path.abspath(str(base))
        if key in seen_bases or not os.path.isdir(str(base)):
            continue
        seen_bases.add(key)
        idx = builder.source("directory", layer, shown(base), None, "read")
        for sub, kind in _AGENT_SUBDIRS:
            directory = base / sub
            if not os.path.isdir(str(directory)):
                continue
            problems: List[str] = []
            for path, is_link_dir in _walk_markdown(directory, problems):
                if is_link_dir:
                    # A symlinked directory is not followed; it may hold agents
                    # or commands the scanner cannot judge, so it is unknown.
                    link_name = path.relative_to(directory).as_posix()
                    builder.entry(idx, kind, sub, link_name, owned=False,
                                  facts={"model": None, "has_permission": False, "has_tools": False, "unknown": True},
                                  digest=name_digest(str(path)))
                    continue
                rel_name = path.relative_to(directory).as_posix()[: -len(".md")]
                got = jsonio.read_regular_bytes(path, max_bytes=MAX_FILE_BYTES)
                if got is None:
                    facts: Dict[str, Any] = {"model": None, "has_permission": False, "has_tools": False, "unknown": True}
                    digest = None
                    is_owned = False
                else:
                    try:
                        facts = scan_frontmatter(got[0].decode("utf-8"))
                    except UnicodeDecodeError:
                        facts = {"model": None, "has_permission": False, "has_tools": False, "unknown": True}
                    digest = sha256_hex(got[0])
                    rel = os.path.relpath(os.path.abspath(str(path)), os.path.abspath(str(cwd))).replace(os.sep, "/")
                    is_owned = owned.get(rel) == digest
                inv.markdown.append((path, kind, rel_name))
                builder.entry(idx, kind, sub, rel_name, owned=is_owned, facts=facts,
                              digest=digest or name_digest(str(path)))
            if problems:
                state = "unreadable" if "unreadable" in problems else "truncated"
                if builder.sources[idx].state != "unreadable":
                    builder.set_state(idx, state)
        for sub in _TOOL_SUBDIRS:
            directory = base / sub
            try:
                # Any non-directory `.js`/`.ts` entry counts, symlinks included:
                # OpenCode imports whatever the name resolves to.
                names = sorted(e.name for e in os.scandir(str(directory))
                               if e.name.endswith((".js", ".ts")) and not e.is_dir(follow_symlinks=False))
            except (FileNotFoundError, NotADirectoryError):
                names = []
            except OSError:
                names = []
                builder.set_state(idx, "unreadable")
            for name in names:
                builder.entry(idx, "tool", sub, name, digest=name_digest(name))
        for sub in _SKILL_SUBDIRS:
            directory = base / sub
            try:
                names = sorted(e.name for e in os.scandir(str(directory)))
            except OSError:
                names = []
            for name in names:
                builder.entry(idx, "skill", sub, name, digest=name_digest(name), facts={"url": False})
        inv.plugin_dirs.extend(plugin_dir_states([base]))
        fetch = builder.entry(
            idx, "fetch-watch", "node_modules", base.name or "dir", digest=name_digest(layer),
            facts={
                "writable": os.access(str(base), os.W_OK),
                "node_modules": os.path.isdir(str(base / "node_modules")),
                "locked": _lock_covers(base),
            },
        )
        if fetch is not None:
            inv.fetch_watch.append(fetch)
            inv.fetch_dirs.append((base, layer))

    # -- ambient environment inputs ------------------------------------------
    idx = builder.source("environment", "env", "environment", None, "read")
    for name in CONFIG_ENV_NAMES:
        value = ambient.get(name)
        if value is None:
            continue
        inherited = name == "OPENCODE_CONFIG" and compiled_path is not None and bool(value) and same(value, compiled_path)
        made = builder.entry(idx, "config-env", name, name, digest=sha256_hex(value.encode("utf-8", "surrogatepass")),
                             facts={"inherited": inherited})
        if made is not None:
            inv.config_env.append(made)

    # -- Claude compatibility ---------------------------------------------------
    for rel, flag in ((".claude/CLAUDE.md", "OPENCODE_DISABLE_CLAUDE_CODE"), (".claude/skills", "OPENCODE_DISABLE_EXTERNAL_SKILLS")):
        path = home / rel
        if os.path.lexists(str(path)):
            idx = builder.source("claude-compat", "claude-compat", shown(path), None, "read")
            builder.entry(idx, "claude-compat", rel, Path(rel).name, digest=name_digest(rel),
                          facts={"flag_set": flag in hardening_names})

    inv.sources, inv.entries = builder.finish()
    return inv


# ------------------------------------------------------------- evaluation

# Whether the offline npm setting is cited as preventing dependency installs
# (rows npm-config-env, npm-install-skip-conditions, npm-config-precedence).
# A test ties this flag to those rows being verified.
NPM_OFFLINE_VERIFIED = True

VERDICTS = ("ok", "owned", "benign", "overridden", "recorded", "stripped", "prevented-offline", "refused")

# Fixed refusal text per code. No path, URL, host or value ever appears.
REFUSAL_TEXT: Dict[str, str] = {
    "inventory-plugin": "a plugin is configured, which could hook permission handling",
    "inventory-provider": "a provider block or enabled-provider entry is not controlled by the profile; add the provider to the profile or remove the block",
    "inventory-disabled-providers": "disabled_providers changes which providers load and is not allowed",
    "inventory-mcp-local": "a local MCP server is configured that the profile policy does not allow",
    "inventory-mcp-remote": "a remote MCP server is configured that the profile policy does not allow",
    "inventory-tool": "a custom tool is defined, which would run arbitrary code",
    "inventory-lsp": "a language server entry is configured outside the compiled configuration",
    "inventory-formatter": "a formatter entry is configured outside the compiled configuration",
    "inventory-agent-permission": "an agent defines its own permission or tool rules",
    "inventory-agent-unreadable": "an agent or command definition could not be read completely, so its model and rules are unknown (quote a model value that contains a colon)",
    "inventory-config-permission": "a top-level permission block is configured outside the compiled configuration",
    "inventory-remote-config": "remote or account configuration is present for this profile",
    "inventory-account-busy": "the profile's OpenCode session database is locked by another process; close other OpenCode sessions using this profile and retry",
    "inventory-remote-instructions": "instructions or skills are loaded from a URL",
    "inventory-unknown-key": "a configuration key the adapter does not recognise is set",
    "inventory-legacy-config": "a legacy TOML configuration file is present",
    "agent-model-outside-allowlist": "an agent selects a model that is not in the profile's allowed models",
    "command-model-outside-allowlist": "a command selects a model that is not in the profile's allowed models",
    "config-env-opencode-config": "OPENCODE_CONFIG points at a configuration file other than the compiled one",
    "config-env-opencode-config-dir": "OPENCODE_CONFIG_DIR adds a configuration directory",
    "config-env-opencode-config-content": "OPENCODE_CONFIG_CONTENT injects configuration",
    "executable-fetch-possible": "a configuration directory is writable and unlocked, so a dependency install could fetch executable code",
    "executable-fetch-detected": "an executable package was written under node_modules during the run",
    "config-layer-unreadable": "a configuration source could not be read completely",
}

_ENV_CODES = {
    "OPENCODE_CONFIG": "config-env-opencode-config",
    "OPENCODE_CONFIG_DIR": "config-env-opencode-config-dir",
    "OPENCODE_CONFIG_CONTENT": "config-env-opencode-config-content",
}
_BELOW_COMPILED = frozenset(("remote-wellknown", "account", "global"))


# A refusal message is shown more widely than the record, so it digests
# anything with a well-known credential prefix on top of the shared rule.
_SECRET_PREFIX = re.compile(r"(sk-|ghp_|gho_|xox|AKIA|eyJ|Bearer)", re.IGNORECASE)


def entry_token(name: str) -> str:
    """A name safe to show: kept when it is short and plain, otherwise an
    ``h-`` digest (`name_digest` already carries the prefix)."""
    if errors.is_plain_token(name or "") and not _SECRET_PREFIX.search(name):
        return name
    return name_digest(name or "")


@dataclass(frozen=True)
class Expected:
    compiled_doc: Mapping[str, Any]
    owned: Mapping[str, str]
    model_allowlist: frozenset
    policy: Any  # profile_policy.ProfilePolicy

    @classmethod
    def from_compiled(cls, compiled_doc: Mapping[str, Any], owned: Mapping[str, str], policy: Any) -> "Expected":
        pairs = set()
        providers = compiled_doc.get("provider")
        if isinstance(providers, dict):
            for pid, entry in providers.items():
                models = entry.get("whitelist") if isinstance(entry, dict) else None
                for model in models if isinstance(models, list) else []:
                    pairs.add("%s/%s" % (pid, model))
        return cls(compiled_doc, dict(owned), frozenset(pairs), policy)


@dataclass(frozen=True)
class Verdict:
    entry_index: int  # index into Inventory.entries; -1 for a source-level verdict
    verdict: str
    code: Optional[str]
    source_index: int = -1
    token: str = ""
    layer: str = ""


def _explicit(policy: Any, key: str) -> bool:
    return any(origin != "default" for origin in policy.origins.get(key, ("default",)))


# Layers the user (not a repository) controls. An allow-listed MCP server is
# trusted by name, so its definition must come from one of these: a project
# file that redefines an allowed name could otherwise run any command.
_USER_MCP_LAYERS = frozenset(("compiled", "global", "home-opencode", "managed", "managed-prefs"))


def _mcp_allowed(policy: Any, remote: bool, name: str, layer: str) -> bool:
    mode = policy.mcp_remote if remote else policy.mcp_local
    if mode == "any":
        return True
    return mode == "allow-listed" and name in policy.mcp_allowed and layer in _USER_MCP_LAYERS


def _judge(entry: Entry, expected: Expected) -> Tuple[str, Optional[str]]:  # noqa: C901 - one flat table
    policy = expected.policy
    work = policy.classification != "personal"
    kind, layer, facts = entry.kind, entry.layer, entry.facts

    def work_refuses(code: str, personal: Tuple[str, Optional[str]] = ("recorded", None)) -> Tuple[str, Optional[str]]:
        return ("refused", code) if work else personal

    if entry.owned or kind == "compiled-file":
        return "owned", None
    if kind == "benign-key":
        return "benign", None
    if kind == "compiled-key":
        return "overridden", None
    if kind == "plugin":
        return "refused", "inventory-plugin"
    if kind == "provider":
        if not facts.get("in_compiled_set"):
            return "refused", "inventory-provider"
        if layer in _BELOW_COMPILED:
            return work_refuses("inventory-provider")
        return "refused", "inventory-provider"
    if kind == "disabled-providers":
        return "refused", "inventory-disabled-providers"
    if kind in ("mcp-local", "mcp-remote"):
        remote = kind == "mcp-remote"
        code = "inventory-mcp-remote" if remote else "inventory-mcp-local"
        key = "mcp.remote" if remote else "mcp.local"
        if work or _explicit(policy, key):
            return ("ok", None) if _mcp_allowed(policy, remote, entry.name, layer) else ("refused", code)
        return "recorded", None
    if kind == "mcp-toggle":
        if facts.get("enabled") is False:
            return "benign", None
        if work or _explicit(policy, "mcp.local"):
            # A toggle cannot change a server's command, so it is judged on the
            # allow-list alone; the definition it toggles is judged by its layer.
            return ("ok", None) if _mcp_allowed(policy, False, entry.name, "global") else ("refused", "inventory-mcp-local")
        return "recorded", None
    if kind in ("lsp", "formatter"):
        code = "inventory-" + kind
        if work:
            return "refused", code
        mode = policy.lsp_mode if kind == "lsp" else policy.formatter_mode
        if _explicit(policy, kind + ".mode") and mode == "deny":
            return "refused", code
        return "recorded", None
    if kind == "tool":
        return "refused", "inventory-tool"
    if kind in ("agent", "command"):
        code = "agent-model-outside-allowlist" if kind == "agent" else "command-model-outside-allowlist"
        model = facts.get("model")
        if model is not None and model not in expected.model_allowlist:
            return "refused", code
        if any(reading not in expected.model_allowlist for reading in facts.get("model_readings") or ()):
            return "refused", code
        if facts.get("unknown"):
            return work_refuses("inventory-agent-unreadable")
        if kind == "agent" and (facts.get("has_permission") or facts.get("has_tools")):
            return work_refuses("inventory-agent-permission")
        return "recorded", None
    if kind in ("instruction", "skill"):
        if facts.get("url"):
            return work_refuses("inventory-remote-instructions")
        return "recorded", None
    if kind == "permission":
        return work_refuses("inventory-config-permission")
    if kind == "remote-config":
        return work_refuses("inventory-remote-config")
    if kind == "account-config":
        if facts.get("busy"):
            return work_refuses("inventory-account-busy")
        return work_refuses("inventory-remote-config")
    if kind == "config-env":
        code = _ENV_CODES.get(entry.name)
        if code and work and not facts.get("inherited"):
            return "refused", code
        return "stripped", None
    if kind == "unknown-key":
        return work_refuses("inventory-unknown-key")
    if kind == "legacy-config":
        return "refused", "inventory-legacy-config"
    if kind == "fetch-watch":
        if policy.package_install == "deny" and facts.get("writable") and not facts.get("locked"):
            if NPM_OFFLINE_VERIFIED:
                return "prevented-offline", None
            return "refused", "executable-fetch-possible"
        return "recorded", None
    return "recorded", None


def evaluate(inv: Inventory, expected: Expected) -> List[Verdict]:
    """One verdict per entry, plus one per unreadable or truncated source,
    in source load order so the first refusal is deterministic. Pure."""
    work = expected.policy.classification != "personal"
    by_source: Dict[int, List[int]] = {}
    for position, entry in enumerate(inv.entries):
        by_source.setdefault(entry.source_index, []).append(position)
    out: List[Verdict] = []
    for index, source in enumerate(inv.sources):
        if source.state in ("unreadable", "truncated") and source.layer != "compiled" and source.kind not in ("auth-file", "database"):
            out.append(Verdict(-1, "refused" if work else "recorded", "config-layer-unreadable" if work else None,
                               index, "", source.layer))
        for position in by_source.get(index, ()):
            entry = inv.entries[position]
            verdict, code = _judge(entry, expected)
            out.append(Verdict(position, verdict, code, index, entry_token(entry.name), entry.layer))
    return out


def first_refusal(verdicts: Iterable[Verdict]) -> Optional[Verdict]:
    for verdict in verdicts:
        if verdict.verdict == "refused":
            return verdict
    return None


def refusal_message(code: str, layer: str, name_token: str) -> str:
    text = REFUSAL_TEXT.get(code, "the configuration is not allowed for this profile")
    suffix = " (layer %s" % layer
    if name_token:
        suffix += ", entry %s" % name_token
    return text + suffix + ")"
