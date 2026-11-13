#!/usr/bin/env python3
"""Compatibility wrapper for quoin.core.scripts.repo_comment_cleanup."""

import importlib.util
import sys
from pathlib import Path


_CORE_PATH = Path(__file__).resolve().parents[1] / "core" / "scripts" / "repo_comment_cleanup.py"
_SPEC = importlib.util.spec_from_file_location("_quoin_core_repo_comment_cleanup", _CORE_PATH)
_CORE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
# spec_from_file_location does not put the loaded module's own directory on
# sys.path, and the core module resolves its sibling `authored_content_lint`
# from there.
sys.path.insert(0, str(_CORE_PATH.parent))
sys.modules[_SPEC.name] = _CORE
_SPEC.loader.exec_module(_CORE)

for _name in dir(_CORE):
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = getattr(_CORE, _name)


if __name__ == "__main__":
    sys.exit(_CORE.main())
