"""The pinned Quoin lifecycle plugin: where it ships, how it is checked and
how an interactive launch hands it a run.

Exactly one plugin may load in a Quoin launch, and only this file. The
launcher admits it by content, not by name: the packaged copy must match the
digest pinned here, the verified bytes are copied into the profile's private
state directory, and the copy is checked again before its ``file://`` URL is
written into the launch configuration. A packaged file that differs (edited in
place, a partial install, a rebuild without updating this module) refuses the
launch rather than loading unknown code.

The plugin reads two variables: ``QUOIN_OPENCODE_RUN_ID`` and
``QUOIN_OPENCODE_PLUGIN_LOG`` (an absolute path). Both are set by the launcher.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import jsonio, paths
from .launch_env import PLUGIN_ENV_NAMES

PLUGIN_FILE = "quoin-opencode-plugin.js"
# Pinned identity of the shipped artifact. A test keeps both in step with the
# file, so a rebuild must update them deliberately.
PLUGIN_VERSION = "0.2.0"
PLUGIN_SHA256 = "106dfba1e8d01d0680dd678b3f14195d79e8d858ca209f1b6393f95992a92915"
# The OpenCode release the plugin's hooks were qualified against.
PLUGIN_OPENCODE_VERSION = "1.18.32"

ENV_RUN_ID, ENV_LOG = PLUGIN_ENV_NAMES

# Profile-relative locations. Neither is a directory OpenCode scans.
_INSTALL_DIR = "lifecycle-plugin"
_LOG_DIR = "plugin-events"
INTERACTIVE_CONFIG_FILE = "opencode.interactive.json"

MAX_PLUGIN_BYTES = 1024 * 1024
_VERSION_RE = re.compile(rb'^const PLUGIN_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)";$', re.MULTILINE)


class PluginRefused(Exception):
    """The pinned plugin cannot be admitted. ``code`` is a closed token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PluginStatus:
    """What the packaged artifact looks like now. ``state`` is one of
    ``ok``, ``missing`` or ``digest-mismatch``."""

    state: str
    version: Optional[str]
    sha256: Optional[str]


def packaged_path() -> Optional[Path]:
    data_dir = paths.adapter_data_dir()
    return None if data_dir is None else data_dir / "plugin" / PLUGIN_FILE


def version_of(data: bytes) -> Optional[str]:
    match = _VERSION_RE.search(data)
    return match.group(1).decode("ascii") if match else None


def _read(path: Path) -> Optional[bytes]:
    try:
        found = jsonio.read_regular_bytes(path, max_bytes=MAX_PLUGIN_BYTES)
    except OSError:
        return None
    return None if found is None else found[0]


def status() -> PluginStatus:
    path = packaged_path()
    data = _read(path) if path is not None else None
    if data is None:
        return PluginStatus("missing", None, None)
    digest = hashlib.sha256(data).hexdigest()
    state = "ok" if digest == PLUGIN_SHA256 else "digest-mismatch"
    return PluginStatus(state, version_of(data), digest)


def install(profile_root: Path) -> Path:
    """Copy the verified packaged plugin into the profile and return the
    copy's path. Raises `PluginRefused` when the packaged file is missing or
    differs from the pinned digest, or when the copy does not read back
    identical."""
    path = packaged_path()
    data = _read(path) if path is not None else None
    if data is None:
        raise PluginRefused("plugin-artifact-missing", "the packaged lifecycle plugin was not found; reinstall quoin")
    if hashlib.sha256(data).hexdigest() != PLUGIN_SHA256:
        raise PluginRefused(
            "plugin-artifact-changed",
            "the packaged lifecycle plugin does not match the pinned build; reinstall quoin",
        )
    target = Path(profile_root) / _INSTALL_DIR / PLUGIN_FILE
    try:
        jsonio.ensure_private_directory(target.parent)
        jsonio.write_private_atomic(target, data, private_parent=True)
    except (OSError, jsonio.UnsafeDirectoryError):
        raise PluginRefused("plugin-install-failed", "the lifecycle plugin could not be copied into the profile") from None
    copied = _read(target)
    if copied is None or hashlib.sha256(copied).hexdigest() != PLUGIN_SHA256:
        raise PluginRefused("plugin-artifact-changed", "the profile copy of the lifecycle plugin does not match the pinned build")
    return target


def log_path(profile_root: Path, run_id: str) -> Path:
    """The run's plugin log, inside a private profile directory that exists
    on return. The file itself is created by the plugin."""
    directory = Path(profile_root) / _LOG_DIR
    try:
        jsonio.ensure_private_directory(directory)
    except (OSError, jsonio.UnsafeDirectoryError):
        raise PluginRefused("plugin-install-failed", "the lifecycle plugin log directory could not be created") from None
    return directory / ("%s.jsonl" % run_id)


def launch_env(run_id: str, log: Path) -> Dict[str, str]:
    return {ENV_RUN_ID: run_id, ENV_LOG: os.path.abspath(str(log))}


def file_url(path: Path) -> str:
    return Path(os.path.abspath(str(path))).as_uri()
