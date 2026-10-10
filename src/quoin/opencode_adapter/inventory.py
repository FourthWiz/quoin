"""Where OpenCode looks for configuration, and how to read what it finds.

The discovery helpers here were part of the doctor; the launch checks and the
effective-configuration inventory share them so every consumer walks the same
directories in the same order. Nothing here reads the process environment:
callers pass mappings in. Only `jsonio` and `paths` may be imported from the
rest of the adapter.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import List, Optional, Tuple

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
