"""Where OpenCode looks for configuration, and how to read what it finds.

The discovery helpers here were part of the doctor; the launch checks and the
effective-configuration inventory share them so every consumer walks the same
directories in the same order. Nothing here reads the process environment:
callers pass mappings in. Only `jsonio` and `paths` may be imported from the
rest of the adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import jsonio

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
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
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


def _unquote(text: str) -> Tuple[str, bool]:
    """(value, plain). A quoted scalar is returned without its quotes."""
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'":
        end = text.find(text[0], 1)
        while text[0] == "'" and end != -1 and text[end + 1:end + 2] == "'":
            end = text.find("'", end + 2)
        if end == -1:
            return text, False
        return text[1:end], True
    return text, False


def scan_frontmatter(text: str) -> Dict[str, Any]:
    """Read the top-level `model`, `permission` and `tools` keys of a markdown
    file's YAML frontmatter without a YAML parser. Anything this scanner
    cannot fully read (block scalars, tags, anchors, flow values, duplicate
    keys, a missing closing fence) is reported as `unknown`."""
    unknown: Dict[str, Any] = {"model": None, "has_permission": False, "has_tools": False, "unknown": True}
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if not lines or lines[0].rstrip() != "---":
        if any(line.strip() == "---" for line in lines[:3]):
            return unknown
        return {"model": None, "has_permission": False, "has_tools": False, "unknown": False}
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "...")), None)
    if end is None:
        return unknown
    facts: Dict[str, Any] = {"model": None, "has_permission": False, "has_tools": False, "unknown": False}
    seen = set()
    block = lines[1:end]
    for index, line in enumerate(block):
        if not line.strip() or line[0] in " \t#":
            continue
        if line.startswith(("{", "[", "<<", "&", "*", "%", "---")):
            return unknown
        key, sep, rest = line.partition(":")
        if not sep:
            return unknown
        key, _ = _unquote(key)
        if key in seen:
            return unknown
        seen.add(key)
        value = rest.strip()
        if value.startswith(("|", ">", "!", "&", "*", "{", "[")):
            if key == "model":
                return unknown
            continue
        if key == "permission":
            facts["has_permission"] = True
        elif key == "tools":
            facts["has_tools"] = True
        elif key == "model":
            text_value, quoted = _unquote(value)
            if not quoted:
                text_value = text_value.split(" #", 1)[0].rstrip()
            if not text_value:
                nested = index + 1 < len(block) and block[index + 1][:1] in (" ", "\t")
                return unknown if nested else {**facts, "model": None}
            facts["model"] = text_value
    return facts


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
            connection = sqlite3.connect(uri, uri=True, timeout=1)
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
        declared = json.loads((directory / "package.json").read_text(encoding="utf-8")).get("dependencies") or {}
    except (OSError, ValueError, AttributeError):
        declared = {}
    if not declared:
        return True
    try:
        lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))
        root = (lock.get("packages") or {}).get("") or {}
        locked = root.get("dependencies") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(declared, dict) and isinstance(locked, dict) and set(declared) <= set(locked)


def _walk_markdown(directory: Path):
    count = 0
    for current, dirnames, filenames in os.walk(str(directory), onerror=lambda exc: None):
        dirnames.sort()
        for filename in sorted(filenames):
            count += 1
            if count > _MAX_WALK:
                return
            if filename.endswith(".md"):
                yield Path(current) / filename


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
        except OSError:
            databases = []
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
            for path in _walk_markdown(directory):
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
        for sub in _TOOL_SUBDIRS:
            directory = base / sub
            try:
                names = sorted(e.name for e in os.scandir(str(directory))
                               if e.name.endswith((".js", ".ts")) and e.is_file(follow_symlinks=False))
            except OSError:
                names = []
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
