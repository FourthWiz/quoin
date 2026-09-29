"""Strict JSON loading, canonical dumping and private atomic writes.

Loading rejects everything the standard library would silently accept:
duplicate keys, NaN and infinities, a byte-order mark, oversize files and
absurd nesting. Failures are `ConfigErrors`; no file content is echoed.
"""
from __future__ import annotations

import json
import math
import os
import secrets as _stdlib_secrets
import stat
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from .errors import ConfigErrors, make_error

MAX_CONFIG_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_INT_DIGITS = 32


class UnsafeDirectoryError(RuntimeError):
    """The directory a private file would be written into is not trustworthy."""


class _DupDict(dict):
    duplicates: Tuple[str, ...] = ()


def _pairs(pairs: List[Tuple[str, Any]]) -> dict:
    out = _DupDict()
    dups = []
    for key, value in pairs:
        if key in out:
            dups.append(key)
            continue
        out[key] = value
    out.duplicates = tuple(dups)
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite number")


def _reject_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


class _NumberTooLarge(ValueError):
    pass


def _parse_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise _NumberTooLarge("number literal too large")
    return int(text)


def _fail(label: str, message_id: str, **params: Any) -> ConfigErrors:
    return ConfigErrors([make_error("invalid-json", label, "$", message_id, **params)])


def _depth_exceeded(tree: Any, limit: int) -> bool:
    """Iterative walk so deep input never reaches a recursive step."""
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        for child in children:
            stack.append((child, depth + 1))
    return False


def find_duplicates(tree: Any, prefix: Tuple[Any, ...] = ()) -> Iterator[Tuple[Any, ...]]:
    """Yield the path segments of every duplicated key."""
    if isinstance(tree, dict):
        for key in getattr(tree, "duplicates", ()):
            yield prefix + (key,)
        for key, value in tree.items():
            yield from find_duplicates(value, prefix + (key,))
    elif isinstance(tree, list):
        for index, value in enumerate(tree):
            yield from find_duplicates(value, prefix + (index,))


def _plain(tree: Any) -> Any:
    if isinstance(tree, dict):
        return {k: _plain(v) for k, v in tree.items()}
    if isinstance(tree, list):
        return [_plain(v) for v in tree]
    return tree


def _read_and_parse(
    path, file_label: str, max_bytes: int, nofollow: bool = False
) -> Tuple[Any, os.stat_result]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if nofollow:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            if info.st_size > max_bytes:
                raise _fail(file_label, "file-too-large", limit=max_bytes)
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise _fail(file_label, "unreadable-file") from None
    if len(raw) > max_bytes:
        raise _fail(file_label, "file-too-large", limit=max_bytes)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _fail(file_label, "has-bom")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail(file_label, "not-utf8") from None
    try:
        tree = json.loads(
            text, object_pairs_hook=_pairs,
            parse_constant=_reject_constant, parse_float=_reject_float,
            parse_int=_parse_int,
        )
    except json.JSONDecodeError as exc:
        raise _fail(file_label, "malformed-json", line=exc.lineno, column=exc.colno) from None
    except RecursionError:
        raise _fail(file_label, "nesting-too-deep", limit=MAX_DEPTH) from None
    except _NumberTooLarge:
        raise _fail(file_label, "number-too-large") from None
    except ValueError:
        raise _fail(file_label, "non-finite-number") from None
    if _depth_exceeded(tree, MAX_DEPTH):
        raise _fail(file_label, "nesting-too-deep", limit=MAX_DEPTH)
    dups = list(find_duplicates(tree))
    if dups:
        raise ConfigErrors(
            [make_error("duplicate-key", file_label, seg, "duplicate-key") for seg in dups]
        )
    return _plain(tree), info


def load_strict_with_stat(
    path, *, file_label: str, max_bytes: int = MAX_CONFIG_BYTES
) -> Tuple[Any, os.stat_result]:
    """Like `load_strict`, also returning the `fstat` of the descriptor that
    was read (no stat-then-reopen window). A symlink is refused where the
    platform supports `O_NOFOLLOW`."""
    return _read_and_parse(path, file_label, max_bytes, nofollow=True)


def load_strict(path, *, file_label: str, max_bytes: int = MAX_CONFIG_BYTES) -> Any:
    return _read_and_parse(path, file_label, max_bytes)[0]


def dump_canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _check_ancestor(directory: Path, allow_root_sticky: bool = False) -> None:
    """The nearest existing directory new private directories are created in
    must be ours and closed to world writes unless it is sticky. With
    `allow_root_sticky`, a root-owned sticky directory (a shared temporary
    directory) is accepted too: the directories created inside it are ours
    and cannot be renamed or removed by others."""
    info = os.stat(directory)
    if info.st_uid != os.getuid():
        if allow_root_sticky and info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
            return
        raise UnsafeDirectoryError("directory is owned by another user")
    if info.st_mode & 0o002 and not info.st_mode & stat.S_ISVTX:
        raise UnsafeDirectoryError("directory is world-writable")


def _ensure_private_dirs(directory: Path, allow_root_sticky: bool = False) -> None:
    missing = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    _check_ancestor(probe, allow_root_sticky)
    for new in reversed(missing):
        try:
            os.mkdir(new, 0o700)
        except FileExistsError:
            # Created concurrently; it must still be a trustworthy directory.
            _check_ancestor(new, allow_root_sticky)
            continue
        os.chmod(new, 0o700)


def _check_chain(directory: Path) -> None:
    """Every existing ancestor, from the directory (or its nearest existing
    ancestor) up to the filesystem root, must belong to the current user or
    to root and must not be writable by group or others, unless it is
    sticky. Anyone who can write to an ancestor could otherwise swap the
    directory the files are trusted in."""
    probe = Path(os.path.abspath(str(directory)))
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    uid = os.getuid()
    while True:
        info = os.stat(probe)
        if info.st_uid not in (uid, 0):
            raise UnsafeDirectoryError("directory is owned by another user")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise UnsafeDirectoryError("directory is writable by group or others")
        if probe.parent == probe:
            return
        probe = probe.parent


def _require_private_parent(directory: Path, *, exact: bool) -> None:
    """Rule for a directory other tools will trust files in. An existing
    directory must be ours; with `exact` it must have no group or other
    permission bits at all (mode 700), otherwise it must merely be closed to
    group and other writes. A directory that has to be created is checked
    through its nearest existing ancestor, which must be ours (or root's) and
    closed to group and other writes, unless it is sticky (a shared
    temporary directory, where the new 0700 directory is ours and cannot be
    renamed by others)."""
    if directory.exists():
        info = os.stat(directory)
        if info.st_uid != os.getuid():
            raise UnsafeDirectoryError("directory is owned by another user")
        if exact:
            if info.st_mode & 0o077:
                raise UnsafeDirectoryError("directory must be private (mode 700)")
        elif info.st_mode & 0o022:
            raise UnsafeDirectoryError("directory is writable by group or others")
        return
    probe = directory
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    info = os.stat(probe)
    if info.st_uid not in (os.getuid(), 0):
        raise UnsafeDirectoryError("directory is owned by another user")
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise UnsafeDirectoryError("directory is writable by group or others")


def ensure_private_directory(directory) -> None:
    """Create `directory` (mode 700) or verify an existing one is ours and
    closed to group and other writes; every ancestor is checked too."""
    directory = Path(directory)
    _check_chain(directory)
    _require_private_parent(directory, exact=False)
    _ensure_private_dirs(directory, allow_root_sticky=True)


def read_regular_bytes(path, *, max_bytes: int) -> Optional[Tuple[bytes, os.stat_result]]:
    """Read a regular file through one descriptor that never follows a
    symlink. Returns `None` for anything else: absent, symlink, non-regular,
    unreadable or larger than `max_bytes`."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                return None
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    return raw, info


def write_private_atomic(path, data: bytes, *, private_parent: bool = False) -> None:
    path = Path(path)
    if private_parent:
        _check_chain(path.parent)
        _require_private_parent(path.parent, exact=True)
        _ensure_private_dirs(path.parent, allow_root_sticky=True)
    else:
        _ensure_private_dirs(path.parent)
    tmp = path.parent / (".%s.%s.tmp" % (path.name, _stdlib_secrets.token_hex(8)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
