#!/usr/bin/env python3
"""Compatibility wrapper for quoin.core.scripts.comment_cleanup."""

import importlib.util
import sys
from pathlib import Path


_CORE_PATH = Path(__file__).resolve().parents[1] / "core" / "scripts" / "comment_cleanup.py"
_SPEC = importlib.util.spec_from_file_location("_quoin_core_comment_cleanup", _CORE_PATH)
_CORE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
# The core module imports its sibling `authored_content_lint` at module
# scope, and spec_from_file_location does not add the loaded module's own
# directory to sys.path — this insert is required for that import to
# resolve, not decorative pattern-parity with the twin wrapper.
sys.path.insert(0, str(_CORE_PATH.parent))
_SPEC.loader.exec_module(_CORE)

for _name in dir(_CORE):
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = getattr(_CORE, _name)


if __name__ == "__main__":
    sys.exit(_CORE.main())
