"""Install, check and uninstall the OpenCode adapter's generated files.

The install side of the adapter: a strict-schema ownership record at
``.quoin/opencode-install.json``, path-safety primitives that never follow a
symlink for reading or writing, and (added by later tasks in this module) a
planner and an applier that make a clean re-install byte-identical to the
tree it started from.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from quoin.opencode_adapter import names

METADATA_RELPATH = ".quoin/opencode-install.json"
METADATA_SCHEMA_VERSION = 1

_NAME_BODY = r"[a-z0-9]+(?:-[a-z0-9]+)*"
assert names.NAME_RE.pattern == r"^[a-z0-9]+(-[a-z0-9]+)*$"

# Full-match: the only five relpath shapes an install ever owns.
OWNED_PATH_RE = re.compile(
    r"(?:"
    r"\.opencode/commands/quoin-%(n)s\.md"
    r"|\.opencode/agents/quoin-%(n)s\.md"
    r"|\.opencode/skills/quoin-%(n)s/SKILL\.md"
    r"|\.opencode/quoin/instructions\.md"
    r"|\.opencode/opencode\.jsonc"
    r")" % {"n": _NAME_BODY}
)

# Full-match: every directory an install is ever allowed to create or prune.
CREATABLE_DIRS_RE = re.compile(
    r"(?:"
    r"\.opencode"
    r"|\.opencode/commands"
    r"|\.opencode/agents"
    r"|\.opencode/skills"
    r"|\.opencode/skills/quoin-%(n)s"
    r"|\.opencode/quoin"
    r"|\.quoin"
    r")" % {"n": _NAME_BODY}
)

KINDS = ("agent", "command", "config", "instructions", "skill")
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_METADATA_KEYS = frozenset(
    {"schema_version", "quoin_version", "opencode_version", "profile", "owned", "created_dirs"}
)
_OWNED_RECORD_KEYS = frozenset({"sha256", "source_digest", "kind", "id"})


class InstallError(Exception):
    """Raised for a usage, metadata or generation error.

    ``exit_code`` is the process exit code the CLI should return; every
    raise site in this module uses 2 (usage, metadata or generation error).
    """

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Metadata:
    quoin_version: str
    opencode_version: str
    profile: Optional[str]
    owned: Dict[str, dict]
    created_dirs: List[str] = field(default_factory=list)


@dataclass
class PathState:
    state: str
    data: Optional[bytes] = None


def _lstat_or(path: Path, missing_state: str):
    try:
        return os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError("cannot stat %s: %s" % (path, exc), 2) from exc


def load_metadata(root) -> Optional[Metadata]:
    """Load and strictly validate the ownership record under ``root``.

    Returns ``None`` only when ``.quoin`` is absent, or ``.quoin`` is a real
    directory and the metadata file is absent. Every other irregularity
    (wrong type, symlink, malformed JSON, an owned path or directory outside
    the families this module knows about, ...) raises ``InstallError`` and
    never as a side effect of reading the file's contents into the message.
    """
    root = Path(root)
    quoin_dir = root / ".quoin"
    dir_st = _lstat_or(quoin_dir, "missing")
    if dir_st is None:
        return None
    if stat.S_ISLNK(dir_st.st_mode):
        raise InstallError("%s is a symlink, expected a directory" % quoin_dir, 2)
    if not stat.S_ISDIR(dir_st.st_mode):
        raise InstallError("%s exists but is not a directory" % quoin_dir, 2)

    meta_path = root / METADATA_RELPATH
    file_st = _lstat_or(meta_path, "missing")
    if file_st is None:
        return None
    if stat.S_ISLNK(file_st.st_mode):
        raise InstallError("%s is a symlink, expected a regular file" % meta_path, 2)
    if not stat.S_ISREG(file_st.st_mode):
        raise InstallError("%s exists but is not a regular file" % meta_path, 2)

    try:
        raw = meta_path.read_bytes()
    except OSError as exc:
        raise InstallError("cannot read %s: %s" % (meta_path, exc), 2) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("%s is not valid UTF-8" % meta_path, 2) from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError("%s is not valid JSON: %s" % (meta_path, exc), 2) from exc

    if not isinstance(obj, dict) or set(obj.keys()) != _METADATA_KEYS:
        raise InstallError("%s does not have the expected top-level keys" % meta_path, 2)
    if obj.get("schema_version") != METADATA_SCHEMA_VERSION:
        raise InstallError(
            "%s schema_version is %r, expected %d" % (meta_path, obj.get("schema_version"), METADATA_SCHEMA_VERSION),
            2,
        )
    quoin_version = obj["quoin_version"]
    opencode_version = obj["opencode_version"]
    if not isinstance(quoin_version, str) or not isinstance(opencode_version, str):
        raise InstallError("%s quoin_version and opencode_version must be strings" % meta_path, 2)

    profile = obj["profile"]
    if profile is not None and (not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile)):
        raise InstallError("%s profile %r is not a valid profile label" % (meta_path, profile), 2)

    owned = obj["owned"]
    if not isinstance(owned, dict):
        raise InstallError("%s 'owned' must be an object" % meta_path, 2)
    for relpath, record in owned.items():
        if not isinstance(relpath, str) or not OWNED_PATH_RE.fullmatch(relpath):
            raise InstallError("%s owned path %r is outside the generated families" % (meta_path, relpath), 2)
        if not isinstance(record, dict) or set(record.keys()) != _OWNED_RECORD_KEYS:
            raise InstallError("%s owned record for %r is malformed" % (meta_path, relpath), 2)
        sha256 = record["sha256"]
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise InstallError("%s owned record for %r has an invalid sha256" % (meta_path, relpath), 2)
        if record["kind"] not in KINDS:
            raise InstallError(
                "%s owned record for %r has an invalid kind %r" % (meta_path, relpath, record["kind"]), 2
            )
        if not isinstance(record["source_digest"], str):
            raise InstallError("%s owned record for %r has a non-string source_digest" % (meta_path, relpath), 2)
        id_value = record["id"]
        if id_value is not None and not isinstance(id_value, str):
            raise InstallError("%s owned record for %r has a non-string id" % (meta_path, relpath), 2)

    created_dirs = obj["created_dirs"]
    if not isinstance(created_dirs, list):
        raise InstallError("%s 'created_dirs' must be a list" % meta_path, 2)
    for entry in created_dirs:
        if not isinstance(entry, str) or not CREATABLE_DIRS_RE.fullmatch(entry):
            raise InstallError("%s created_dirs entry %r is not a creatable directory" % (meta_path, entry), 2)

    return Metadata(
        quoin_version=quoin_version,
        opencode_version=opencode_version,
        profile=profile,
        owned=owned,
        created_dirs=list(created_dirs),
    )


def serialize_metadata(meta: Metadata) -> bytes:
    """Deterministic bytes for ``meta``: sorted keys, no timestamps, no absolute paths."""
    created_dirs = sorted(meta.created_dirs, key=lambda d: (-d.count("/"), d))
    obj = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "quoin_version": meta.quoin_version,
        "opencode_version": meta.opencode_version,
        "profile": meta.profile,
        "owned": meta.owned,
        "created_dirs": created_dirs,
    }
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    return text.encode("utf-8")


def inspect_path(root, relpath: str) -> PathState:
    """Walk ``relpath`` under ``root`` component by component, never following a symlink."""
    if not isinstance(relpath, str) or not relpath or relpath.startswith("/") or "\\" in relpath:
        raise InstallError("relpath %r is not a relative POSIX path" % (relpath,), 2)
    parts = relpath.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise InstallError("relpath %r has an invalid path component %r" % (relpath, part), 2)

    root = Path(root)
    current = root
    for part in parts[:-1]:
        current = current / part
        st = _lstat_or(current, "missing")
        if st is None:
            return PathState("missing")
        if stat.S_ISLNK(st.st_mode):
            return PathState("symlinked-parent")
        if not stat.S_ISDIR(st.st_mode):
            return PathState("parent-not-dir")

    target = root.joinpath(*parts)
    st = _lstat_or(target, "missing")
    if st is None:
        return PathState("missing")
    if stat.S_ISLNK(st.st_mode):
        return PathState("symlink")
    if not stat.S_ISREG(st.st_mode):
        return PathState("not-regular")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise InstallError("cannot read %s: %s" % (target, exc), 2) from exc
    return PathState("regular", data)


def atomic_write(path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a same-directory temp file and ``os.replace``.

    Uses ``O_CREAT | O_EXCL`` (not ``tempfile.mkstemp``) so the process umask
    applies to the written file's mode instead of ``mkstemp``'s fixed 0600.
    On any exception the temp file is unlinked (if it exists) before
    re-raising; nothing is left behind on a failed write.
    """
    path = Path(path)
    tmp_path = path.parent / (".quoin-tmp-%s" % secrets.token_hex(8))
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise

    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
