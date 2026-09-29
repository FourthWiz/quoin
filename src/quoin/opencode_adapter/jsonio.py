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
from typing import Any, Iterator, List, Tuple

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


def _check_ancestor(directory: Path) -> None:
    info = os.stat(directory)
    if info.st_uid != os.getuid():
        raise UnsafeDirectoryError("directory is owned by another user")
    if info.st_mode & 0o002 and not info.st_mode & stat.S_ISVTX:
        raise UnsafeDirectoryError("directory is world-writable")


def _ensure_private_dirs(directory: Path) -> None:
    missing = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    _check_ancestor(probe)
    for new in reversed(missing):
        try:
            os.mkdir(new, 0o700)
        except FileExistsError:
            # Created concurrently; it must still be a trustworthy directory.
            _check_ancestor(new)
            continue
        os.chmod(new, 0o700)


def write_private_atomic(path, data: bytes) -> None:
    path = Path(path)
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
