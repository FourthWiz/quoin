"""Where runtime configuration lives on disk.

Environment and home are always injected by the caller, so resolution is a
pure function of its arguments and tests never depend on the real
environment. `adapter_data_dir` mirrors the first two tiers of the
command-line source-directory resolution but never exits the process.
"""
from __future__ import annotations

import hashlib
import importlib.resources
import os
from pathlib import Path
from typing import Mapping, Optional

from .install import PROFILE_RE

ENV_XDG_CONFIG = "XDG_CONFIG_HOME"
ENV_XDG_STATE = "XDG_STATE_HOME"
ENV_MANAGED_POLICY = "QUOIN_OPENCODE_MANAGED_POLICY"

SCHEMA_FILE = "runtime-config.schema.json"
NATIVE_SCHEMA_FILE = "opencode-1.18.32-config.subset.schema.json"


class AdapterDataMissing(RuntimeError):
    """The packaged adapter data (schemas, fixtures) cannot be located."""


def _xdg_dir(env: Mapping[str, str], home: Path, var: str, default: str) -> Path:
    value = env.get(var)
    # The XDG base-directory spec says relative values must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path(home) / default


def config_home(env: Mapping[str, str], home: Path) -> Path:
    return _xdg_dir(env, home, ENV_XDG_CONFIG, ".config")


def state_home(env: Mapping[str, str], home: Path) -> Path:
    return _xdg_dir(env, home, ENV_XDG_STATE, ".local/state")


def opencode_config_dir(env: Mapping[str, str], home: Path) -> Path:
    return config_home(env, home) / "quoin" / "opencode"


def profiles_dir(env: Mapping[str, str], home: Path) -> Path:
    return opencode_config_dir(env, home) / "profiles"


def profile_path(name: str, env: Mapping[str, str], home: Path) -> Path:
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("profile name must be validated by the caller")
    return profiles_dir(env, home) / (name + ".json")


def qualifications_dir(env: Mapping[str, str], home: Path) -> Path:
    return opencode_config_dir(env, home) / "qualifications"


def qualification_path(name: str, env: Mapping[str, str], home: Path) -> Path:
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("name must be validated by the caller")
    return qualifications_dir(env, home) / (name + ".json")


def state_dir(env: Mapping[str, str], home: Path) -> Path:
    return state_home(env, home) / "quoin" / "opencode"


def managed_policy_path(env: Mapping[str, str]) -> Optional[Path]:
    value = env.get(ENV_MANAGED_POLICY)
    if not value:
        return None
    return Path(os.path.abspath(value))


def project_runtime_path(project_root: Path) -> Path:
    return Path(project_root) / ".quoin" / "runtime.json"


def project_key(project_root: Path) -> str:
    """Stable short key for a project directory; symlinked aliases of the
    same directory share a key."""
    real = os.path.realpath(str(project_root))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def _source_root() -> Optional[Path]:
    import quoin as pkg

    pkg_file = Path(pkg.__file__).resolve()
    editable = pkg_file.parent.name == "quoin" and pkg_file.parent.parent.name == "src"
    if not editable:
        try:
            data_ref = importlib.resources.files("quoin") / "data"
            with importlib.resources.as_file(data_ref) as data_path:
                data_path = Path(data_path)
                if (data_path / "skills").is_dir():
                    return data_path
        except (TypeError, AttributeError, FileNotFoundError):
            pass
    candidate = (pkg_file.parent / ".." / ".." / "quoin").resolve()
    if (candidate / "skills").is_dir():
        return candidate
    return None


def adapter_data_dir() -> Optional[Path]:
    root = _source_root()
    if root is None:
        return None
    target = root / "adapters" / "opencode"
    return target if target.is_dir() else None


def runtime_config_schema_path() -> Path:
    data_dir = adapter_data_dir()
    if data_dir is None:
        raise AdapterDataMissing("adapter data directory not found")
    path = data_dir / "schemas" / SCHEMA_FILE
    if not path.is_file():
        raise AdapterDataMissing("runtime configuration schema not found")
    return path


def native_schema_path() -> Path:
    data_dir = adapter_data_dir()
    if data_dir is None:
        raise AdapterDataMissing("adapter data directory not found")
    path = data_dir / "schemas" / NATIVE_SCHEMA_FILE
    if not path.is_file():
        raise AdapterDataMissing("native configuration schema not found")
    return path


def compiled_output_dir(
    profile: str, project_root: Path, env: Mapping[str, str], home: Path
) -> Path:
    """Default directory for the compiled configuration of one profile and
    one project."""
    if not PROFILE_RE.fullmatch(profile):
        raise ValueError("profile name must be validated by the caller")
    return state_dir(env, home) / profile / project_key(project_root)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samestat(os.stat(a), os.stat(b))
    except OSError:
        return False


def git_worktree_root(project_root: Path, home: Optional[Path] = None) -> Optional[Path]:
    """Nearest directory at or above the project that holds a `.git` entry (a
    directory, or the file a linked worktree uses), found without running
    git. A match that is the home directory or one of its ancestors is
    ignored, so a dotfiles checkout at `~/.git` never makes everything under
    home count as inside the project; the project root itself is still
    checked."""
    real = Path(os.path.realpath(str(project_root)))
    home_chain = []
    if home is not None:
        probe = Path(os.path.realpath(str(home)))
        while True:
            home_chain.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
    current = real
    while True:
        if os.path.lexists(current / ".git"):
            if current == real or not any(_same_file(current, item) for item in home_chain):
                return current
            return None
        if current.parent == current:
            return None
        current = current.parent
