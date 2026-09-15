"""
conftest.py — Pytest configuration for quoin dev tests.

Adds the repo root to sys.path so that `quoin.benchmarks` and other
quoin sub-packages are importable without installation.

When both `src/quoin` (the installed package) and `quoin/` (the source
tree containing benchmarks) need to be importable, we extend the `quoin`
package's __path__ to include the source-tree `quoin/` directory. This
allows `from quoin.benchmarks.*` to resolve even when the regular package
at `src/quoin` takes precedence for CLI/installer imports.
"""
import sys
from pathlib import Path

# Repo root is 3 levels up from this file: quoin/dev/tests/conftest.py → quoin/
REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure quoin.benchmarks is importable even when src/quoin (a regular package)
# takes precedence over the quoin/ namespace package at the repo root.
# We extend quoin.__path__ to include the source-tree quoin/ directory so that
# sub-packages like quoin.benchmarks resolve correctly in both cases.
import importlib
_quoin_src_dir = str(REPO_ROOT / "quoin")
try:
    import quoin as _quoin_pkg
    if _quoin_src_dir not in _quoin_pkg.__path__:
        _quoin_pkg.__path__.append(_quoin_src_dir)
except ImportError:
    pass  # quoin not yet importable; sys.path insert above will handle it

# Disable quoin.router's real npm-prefix query for the whole test run so
# detect_ccr's empty-store fallback never reads this machine's actual global
# npm install (host state such as a real globally-installed CCR would
# otherwise leak into detection results). See quoin.router._npm_global_prefix
# and the ccr-v3-upgrade stage-1 plan's Test Plan section for the full
# rationale. Test-only; never set in production.
try:
    import quoin.router as _quoin_router
    _quoin_router._npm_query_enabled = False
except ImportError:
    pass  # quoin.router not yet importable; individual tests will fail loudly
