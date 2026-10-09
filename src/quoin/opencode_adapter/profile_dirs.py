"""Per-profile storage layout for headless OpenCode runs.

Every profile owns one directory under the adapter state root. The directory
carries a stamp naming the profile and its classification, so a store can
never be reused under a different profile, and an index of the projects that
used it. Nothing here reads the process environment: callers pass mappings in.

Refusals are raised as the small exception classes below; the driver maps them
to registered launch codes.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from . import jsonio, paths
from .install import PROFILE_RE

STAMP_NAME = "profile.json"
INDEX_NAME = "projects.json"
STAMP_FORMAT = 1
INDEX_FORMAT = 1
SUBDIRS = ("data", "state", "cache", "tmp")
LAZY_SUBDIRS = ("records", "exports", "snapshots", "tests", "workflow")
MAX_STAMP_BYTES = 4096
MAX_INDEX_BYTES = 1_048_576

_MESSAGES = {
    "not-private": "the profile storage directory is not private to this user",
    "stamp-unreadable": "the profile stamp cannot be read",
    "stamp-invalid": "the profile stamp is not valid",
    "index-unwritable": "the profile project index cannot be updated",
}


class ProfileDirError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(_MESSAGES.get(reason, reason))
        self.reason = reason


class ProfileDirUnsafe(ProfileDirError):
    """The directory, stamp or index cannot be trusted."""


class StampMismatch(ProfileDirError):
    def __init__(self, field: str) -> None:
        super().__init__("stamp-mismatch")
        self.field = field


class ProfileSwitch(ProfileDirError):
    def __init__(self, field: str) -> None:
        super().__init__("profile-switch")
        self.field = field


def profile_root(adapter_root, profile: str) -> Path:
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        raise ValueError("profile name must be validated by the caller")
    return Path(adapter_root) / profile


@dataclass(frozen=True)
class ProfileDirs:
    root: Path
    data: Path
    state: Path
    cache: Path
    tmp: Path

    @classmethod
    def at(cls, root) -> "ProfileDirs":
        root = Path(root)
        return cls(root, root / "data", root / "state", root / "cache", root / "tmp")

    @property
    def records(self) -> Path:
        return self.root / "records"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def tests(self) -> Path:
        return self.root / "tests"

    @property
    def workflow(self) -> Path:
        return self.root / "workflow"

    @property
    def stamp(self) -> Path:
        return self.root / STAMP_NAME

    @property
    def index(self) -> Path:
        return self.root / INDEX_NAME

    @property
    def npm_cache(self) -> Path:
        return self.cache / "npm"

    @property
    def npm_userconfig(self) -> Path:
        return self.state / "npmrc"


def _no_duplicates(pairs):
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _parse_object(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_stamp(stamp_path: Path) -> Dict[str, Any]:
    got = jsonio.read_regular_bytes(stamp_path, max_bytes=MAX_STAMP_BYTES)
    if got is None:
        raise ProfileDirUnsafe("stamp-unreadable")
    data = _parse_object(got[0])
    if (
        data is None
        or set(data) != {"created_format", "profile", "classification"}
        or data["created_format"] != STAMP_FORMAT
        or not isinstance(data["profile"], str)
        or not isinstance(data["classification"], str)
        or isinstance(data["created_format"], bool)
    ):
        raise ProfileDirUnsafe("stamp-invalid")
    return data


def _valid_stamp(root: Path) -> Optional[Tuple[str, str]]:
    """The (profile, classification) of a readable, valid stamp whose profile
    names the directory, else None."""
    try:
        data = _read_stamp(root / STAMP_NAME)
    except ProfileDirError:
        return None
    if data["profile"] != root.name:
        return None
    return data["profile"], data["classification"]


def stamped_classification(root) -> Optional[str]:
    """The classification recorded in `root`'s valid stamp, or None when the
    stamp is missing, unreadable, invalid or names another profile."""
    got = _valid_stamp(Path(root))
    return None if got is None else got[1]


def _stamped_launcher_root(env: Mapping[str, str]) -> Optional[Path]:
    value = env.get(paths.ENV_STATE_DIR)
    if not value or not os.path.isabs(value):
        return None
    root = Path(value)
    if not PROFILE_RE.fullmatch(root.name):
        return None
    return root if _valid_stamp(root) is not None else None


def adapter_root(env: Mapping[str, str], home) -> Path:
    """The adapter state root: the parent of a stamped launcher profile root
    when the environment names one, else the ordinary state directory."""
    root = _stamped_launcher_root(env)
    if root is not None:
        return root.parent
    return paths.state_dir(env, Path(home))


def compiled_dir(adapter_root_path, profile: str, project_root) -> Path:
    return profile_root(adapter_root_path, profile) / paths.project_key(Path(project_root))


def child_storage_env(dirs: ProfileDirs) -> Dict[str, str]:
    return {
        "XDG_STATE_HOME": str(dirs.state),
        "XDG_CACHE_HOME": str(dirs.cache),
        "TMPDIR": str(dirs.tmp),
        "npm_config_cache": str(dirs.npm_cache),
    }


def state_root_for(drv, profile: str) -> Optional[Path]:
    method = getattr(drv, "profile_state_root", None)
    if callable(method):
        return method(profile)
    base = getattr(drv, "state_root", None)
    if base is None:
        return None
    return profile_root(base, profile)


def prepare(adapter_root_path, profile: str, classification: str) -> ProfileDirs:
    root = profile_root(adapter_root_path, profile)
    dirs = ProfileDirs.at(root)
    try:
        jsonio.ensure_private_directory(root)
        for sub in SUBDIRS:
            jsonio.ensure_private_directory(root / sub)
    except (jsonio.UnsafeDirectoryError, OSError):
        raise ProfileDirUnsafe("not-private")
    stamp_path = dirs.stamp
    body = jsonio.dump_canonical(
        {"created_format": STAMP_FORMAT, "profile": profile, "classification": classification}
    )
    # Publish with a hard link so a concurrent first launch never reads a
    # half-written stamp: the link either appears complete or not at all.
    staging = stamp_path.with_name("%s.%d.%d.tmp" % (STAMP_NAME, os.getpid(), threading.get_ident()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(staging), flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            try:
                os.link(str(staging), str(stamp_path))
                return dirs
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(str(staging))
            except OSError:
                pass
    except OSError:
        raise ProfileDirUnsafe("stamp-unreadable")
    data = _read_stamp(stamp_path)
    if data["profile"] != profile:
        raise StampMismatch("profile")
    if data["classification"] != classification:
        raise StampMismatch("classification")
    return dirs


def record_project(dirs: ProfileDirs, project_root) -> None:
    lock_path = dirs.root / (INDEX_NAME + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        raise ProfileDirUnsafe("index-unwritable")
    try:
        with os.fdopen(fd, "r+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            projects = []
            if os.path.lexists(str(dirs.index)):
                got = jsonio.read_regular_bytes(dirs.index, max_bytes=MAX_INDEX_BYTES)
                data = _parse_object(got[0]) if got is not None else None
                if (
                    data is None
                    or set(data) != {"index_format", "projects"}
                    or data["index_format"] != INDEX_FORMAT
                    or not isinstance(data["projects"], list)
                    or not all(isinstance(p, str) for p in data["projects"])
                ):
                    raise ProfileDirUnsafe("index-unwritable")
                projects = list(data["projects"])
            entry = os.path.realpath(str(project_root))
            if entry not in projects:
                projects.append(entry)
            projects = sorted(set(projects))
            jsonio.write_private_atomic(
                dirs.index,
                jsonio.dump_canonical({"index_format": INDEX_FORMAT, "projects": projects}),
            )
    except ProfileDirError:
        raise
    except OSError:
        raise ProfileDirUnsafe("index-unwritable")


class LaunchSession:
    """The single (profile, classification) pair one process may use."""

    def __init__(self, pair: Optional[Tuple[str, str]] = None) -> None:
        self._pair = pair

    @classmethod
    def from_launcher_env(cls, env: Mapping[str, str]) -> "LaunchSession":
        root = _stamped_launcher_root(env)
        if root is None:
            return cls()
        return cls(_valid_stamp(root))

    @property
    def pair(self) -> Optional[Tuple[str, str]]:
        return self._pair

    def check_profile(self, name: str) -> None:
        if self._pair is not None and self._pair[0] != name:
            raise ProfileSwitch("profile")

    def claim(self, profile: str, classification: str) -> None:
        if self._pair is None:
            self._pair = (profile, classification)
            return
        if self._pair[0] != profile:
            raise ProfileSwitch("profile")
        if self._pair[1] != classification:
            raise ProfileSwitch("classification")
