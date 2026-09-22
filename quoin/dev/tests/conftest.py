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
import builtins
import io
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

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
# for the full rationale. Test-only; never set in production.
try:
    import quoin.router as _quoin_router
    _quoin_router._npm_query_enabled = False
except ImportError:
    pass  # quoin.router not yet importable; individual tests will fail loudly


# ── v3 store hermeticity guard (autouse, session-wide) ──────────────────────
# Wraps the seams through which any v3 store code can reach disk and fails a
# test whose resolved target is under the real ~/.claude-code-router/. The
# guard is process-wide (not module-local) so it also covers v3 writes
# reached indirectly from test_router_setup.py and test_models.py, and
# writes that bypass sqlite3.connect (the backup sidecar's os.open, its
# re-read, and any temp file).


def _target_path(arg, kwargs):
    """Resolve the on-disk path a guarded call is about to touch, or None.

    Normalises a `file:` URI (the form both v3 store readers use) before
    resolving, and recognises the sqlite ":memory:" / "file::memory:" /
    "file:?mode=memory" forms as having no file target at all.
    """
    s = str(arg)
    if kwargs.get("uri") or s.startswith("file:"):
        s = unquote(urlparse(s).path)
        if not s:
            return None  # file:?mode=memory and friends: no file target
    if s == ":memory:" or s.startswith("file::memory:"):
        return None
    try:
        return Path(s).resolve()
    except (OSError, ValueError):
        return None


@pytest.fixture(scope="session", autouse=True)
def no_real_ccr_store(tmp_path_factory):
    forbidden = (Path.home() / ".claude-code-router").resolve()
    escape_root = Path(str(tmp_path_factory.getbasetemp())).resolve()

    def _check(path):
        if path is None:
            return
        if path == escape_root or escape_root in path.parents:
            return
        if path == forbidden or forbidden in path.parents:
            raise AssertionError(
                "quoin test hermeticity: refusing to touch the real CCR "
                f"config directory ({path}). Point the code under test at "
                "tmp_path."
            )

    mp = pytest.MonkeyPatch()

    real_sqlite_connect = sqlite3.connect

    def _guarded_sqlite_connect(*args, **kwargs):
        target = args[0] if args else kwargs.get("database")
        if target is not None:
            _check(_target_path(target, kwargs))
        return real_sqlite_connect(*args, **kwargs)

    real_os_open = os.open

    def _guarded_os_open(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_os_open(path, *args, **kwargs)

    real_os_mkdir = os.mkdir

    def _guarded_os_mkdir(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_os_mkdir(path, *args, **kwargs)

    real_os_makedirs = os.makedirs

    def _guarded_os_makedirs(name, *args, **kwargs):
        _check(_target_path(name, {}))
        return real_os_makedirs(name, *args, **kwargs)

    real_builtins_open = builtins.open

    def _guarded_builtins_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            _check(_target_path(file, {}))
        return real_builtins_open(file, *args, **kwargs)

    real_io_open = io.open

    def _guarded_io_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            _check(_target_path(file, {}))
        return real_io_open(file, *args, **kwargs)

    real_os_replace = os.replace

    def _guarded_os_replace(src, dst, *args, **kwargs):
        _check(_target_path(dst, {}))
        return real_os_replace(src, dst, *args, **kwargs)

    # Delete seams: no v3 store code calls any of these today, but the guard
    # is meant to catch a stray test reaching the real config directory, not
    # only production code — an unwrapped delete would let such a test wipe
    # a user's real store undetected.
    real_os_unlink = os.unlink

    def _guarded_os_unlink(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_os_unlink(path, *args, **kwargs)

    real_os_remove = os.remove

    def _guarded_os_remove(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_os_remove(path, *args, **kwargs)

    real_shutil_rmtree = shutil.rmtree

    def _guarded_shutil_rmtree(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_shutil_rmtree(path, *args, **kwargs)

    # os.rename is checked on its destination, mirroring os.replace above —
    # a rename that moves the real store away is as destructive as deleting
    # it. os.rmdir and os.truncate close the two remaining seams that would
    # move or zero the store undetected (Path.rename routes through
    # os.rename, Path.rmdir through os.rmdir).
    real_os_rename = os.rename

    def _guarded_os_rename(src, dst, *args, **kwargs):
        _check(_target_path(dst, {}))
        return real_os_rename(src, dst, *args, **kwargs)

    real_os_rmdir = os.rmdir

    def _guarded_os_rmdir(path, *args, **kwargs):
        _check(_target_path(path, {}))
        return real_os_rmdir(path, *args, **kwargs)

    real_os_truncate = os.truncate

    def _guarded_os_truncate(path, length, *args, **kwargs):
        # An open file descriptor (an int) is not a path to resolve — the
        # file it refers to was opened through an already-guarded seam.
        if not isinstance(path, int):
            _check(_target_path(path, {}))
        return real_os_truncate(path, length, *args, **kwargs)

    mp.setattr(sqlite3, "connect", _guarded_sqlite_connect)
    mp.setattr(os, "open", _guarded_os_open)
    mp.setattr(os, "mkdir", _guarded_os_mkdir)
    mp.setattr(os, "makedirs", _guarded_os_makedirs)
    mp.setattr(builtins, "open", _guarded_builtins_open)
    mp.setattr(io, "open", _guarded_io_open)
    mp.setattr(os, "replace", _guarded_os_replace)
    mp.setattr(os, "unlink", _guarded_os_unlink)
    mp.setattr(os, "remove", _guarded_os_remove)
    mp.setattr(shutil, "rmtree", _guarded_shutil_rmtree)
    mp.setattr(os, "rename", _guarded_os_rename)
    mp.setattr(os, "rmdir", _guarded_os_rmdir)
    mp.setattr(os, "truncate", _guarded_os_truncate)
    try:
        yield
    finally:
        mp.undo()


def make_v3_store(dir_, blob, *, wal=False):
    """Build a v3 sqlite store at `dir_/config.sqlite` seeded with `blob`.

    `blob` is written as app_config['default']. With wal=True the database
    is switched to WAL journal mode before the seed write, matching the
    shape v3 actually ships (config.sqlite-wal / config.sqlite-shm).
    """
    path = Path(dir_) / "config.sqlite"
    con = sqlite3.connect(str(path))
    try:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS app_config ("
            "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        payload = json.dumps(blob, ensure_ascii=False)
        con.execute(
            "INSERT INTO app_config (key, value_json, updated_at) VALUES (?, ?, ?)",
            ("default", payload, "2026-01-01T00:00:00.000Z"),
        )
        con.commit()
    finally:
        con.close()
    return path


def store_value_snapshot(path):
    """Read-only (value_json, updated_at) tuple, or None iff file/table/row
    is absent. Raises on any other sqlite3.Error — an oracle that cannot
    read must fail loudly, never report "unchanged". Use this, not a byte
    comparison of config.sqlite, as the "quoin wrote nothing" oracle: a
    committed WAL write is invisible in the main file for as long as any
    connection holds the store open.
    """
    path = Path(path)
    if not path.exists():
        return None
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_config'"
        )
        if cur.fetchone() is None:
            return None
        cur = con.execute(
            "SELECT value_json, updated_at FROM app_config WHERE key='default'"
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1])
    finally:
        con.close()
