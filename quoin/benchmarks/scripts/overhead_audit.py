"""
overhead_audit.py - Fixed overhead of quoin-claude sessions against plain ones.

Usage (from a source checkout of the quoin repo):

    python3 quoin/benchmarks/scripts/overhead_audit.py RUN_PATH
        [--quoin-root quoin] [--format md|json]

Read-only over the run directory. See quoin/benchmarks/harness/overhead_audit.py.
"""

from __future__ import annotations

# Path bootstrap: mirrors run_benchmark.py so the script works when invoked directly.
import sys as _sys
from pathlib import Path as _Path
_repo_root = _Path(__file__).resolve().parent.parent.parent.parent  # .../quoin/
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
try:
    import quoin as _q
    _inner = str(_repo_root / "quoin")
    if _inner not in _q.__path__:
        _q.__path__.append(_inner)
except Exception:
    pass

from quoin.benchmarks.harness.overhead_audit import main  # noqa: E402

if __name__ == "__main__":
    _sys.exit(main())
