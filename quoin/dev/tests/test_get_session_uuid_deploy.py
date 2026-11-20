"""IVG-74 T-07: install.sh deploys get_session_uuid.py to both targets.

Two-tier test:

1. Non-skipped unit-level assertions (runs on CI without `claude`):
   Import installer.py and assert "get_session_uuid.py" is in BOTH
   DEPLOYED_SCRIPTS and CORE_SCRIPTS.  This is the real regression guard
   for the silent-skip failure mode documented in lessons-learned 2026-05-31.

2. Wrapper import test (no subprocess, no dev-machine skip needed):
   Import the wrapper via importlib and verify it exports get_session_uuid
   as a callable.
"""
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "get_session_uuid.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "get_session_uuid.py"

# installer.py is at src/quoin/installer.py
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"


# ---------------------------------------------------------------------------
# Tier 1: non-skipped unit-level assertions (runs on CI)
# ---------------------------------------------------------------------------


def _load_installer():
    spec = importlib.util.spec_from_file_location("installer_get_uuid", INSTALLER_PY)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load installer from {INSTALLER_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_deployed_scripts_contains_get_session_uuid():
    """DEPLOYED_SCRIPTS in installer.py must contain 'get_session_uuid.py'.

    This is the regression guard for the silent-skip failure:
    if DEPLOYED_SCRIPTS lacks the entry, install.sh never deploys the wrapper.
    """
    mod = _load_installer()
    assert "get_session_uuid.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'get_session_uuid.py'. "
        "Missing entry means install.sh won't deploy the adapter wrapper to "
        "~/.claude/scripts/get_session_uuid.py."
    )


def test_installer_core_scripts_contains_get_session_uuid():
    """CORE_SCRIPTS in installer.py must contain 'get_session_uuid.py'.

    This is the regression guard for the silent-import-failure mode:
    if CORE_SCRIPTS lacks the entry, the wrapper's parents[1] loader
    fails at runtime with a FileNotFoundError.
    """
    mod = _load_installer()
    assert "get_session_uuid.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'get_session_uuid.py'. "
        "Missing entry means install.sh won't deploy the core impl to "
        "~/.claude/core/scripts/get_session_uuid.py, causing the wrapper's "
        "parents[1] loader to fail at runtime."
    )


def test_source_files_exist():
    """Source files for both targets must exist in the repo."""
    assert WRAPPER_SRC.is_file(), (
        f"Wrapper source not found at {WRAPPER_SRC}. "
        "This file must exist for install.sh to deploy it."
    )
    assert CORE_IMPL_SRC.is_file(), (
        f"Core impl source not found at {CORE_IMPL_SRC}. "
        "This file must exist for install.sh to deploy it."
    )


def test_wrapper_imports_core_via_parents_loader():
    """Wrapper must import the core impl via parents[1] loader and export get_session_uuid."""
    spec = importlib.util.spec_from_file_location("get_session_uuid_wrapper", WRAPPER_SRC)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load wrapper from {WRAPPER_SRC}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "get_session_uuid"), (
        "Wrapper does not export 'get_session_uuid'. "
        "Check that the for-loop in the wrapper propagates core attrs to globals()."
    )
    assert callable(mod.get_session_uuid), (
        "'get_session_uuid' attribute exported by wrapper is not callable."
    )
