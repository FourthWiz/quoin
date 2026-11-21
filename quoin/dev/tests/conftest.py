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

# The interface fixture is a sample project; its test files are scanner input, not tests.
collect_ignore_glob = ["fixtures/interface_project/*"]
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
        # Both ends: replacing something *onto* the store is as destructive
        # as moving the store *away* from itself, and only the destination
        # was checked before — shutil.move, Path.rename and a bare
        # os.replace/os.rename of the real store all went unguarded on the
        # source side.
        _check(_target_path(src, {}))
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

    # Checked on both ends, mirroring os.replace above — a rename that moves
    # the real store away is as destructive as deleting it, and checking
    # the destination alone (as this guard originally did) does not stop
    # that direction. os.rmdir and os.truncate close the two
    # remaining seams that would move or zero the store undetected
    # (Path.rename and shutil.move's same-filesystem path both route
    # through os.rename, Path.rmdir through os.rmdir).
    real_os_rename = os.rename

    def _guarded_os_rename(src, dst, *args, **kwargs):
        _check(_target_path(src, {}))
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

    # A symlink or hard link placed at the store's own filename is
    # store-shaping, not metadata-only: detection keys directly on
    # config.json / config.sqlite existing at that path, so either call
    # can change what quoin classifies and, for a symlink, what it reads
    # and writes through afterwards. Only the destination — the link
    # being created — needs checking for a symlink; the source argument is
    # just the link target and need not exist. A hard link is different:
    # its source is dereferenced to a real inode, so the source also needs
    # checking there.
    real_os_symlink = os.symlink

    def _guarded_os_symlink(src, dst, *args, **kwargs):
        _check(_target_path(dst, {}))
        return real_os_symlink(src, dst, *args, **kwargs)

    real_os_link = os.link

    def _guarded_os_link(src, dst, *args, **kwargs):
        _check(_target_path(src, {}))
        _check(_target_path(dst, {}))
        return real_os_link(src, dst, *args, **kwargs)

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
    mp.setattr(os, "symlink", _guarded_os_symlink)
    mp.setattr(os, "link", _guarded_os_link)
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


# ── install questions stay off for the whole run (autouse, session-wide) ────
# Session scope is required: module-scoped fixtures that run installs are built
# before any function-scoped autouse fixture. The env var forces the real
# is_interactive() to False (and reaches subprocess installs, for example under
# `pytest -s` in a terminal) without hiding the function, so its own tests can
# still call it.
@pytest.fixture(scope="session", autouse=True)
def _install_prompts_non_interactive():
    mp = pytest.MonkeyPatch()
    mp.setenv("QUOIN_INSTALL_NO_PROMPT", "1")
    try:
        yield
    finally:
        mp.undo()


_FAKE_CLAUDE = """#!/bin/sh
# Stand-in for the claude CLI during tests: logs its arguments and succeeds.
[ -n "$QUOIN_FAKE_CLAUDE_LOG" ] && printf '%s\\n' "$*" >> "$QUOIN_FAKE_CLAUDE_LOG"
case "$*" in
  "plugin list --json"*|"plugin marketplace list --json"*) echo "[]" ;;
esac
exit 0
"""


@pytest.fixture(scope="session", autouse=True)
def _no_real_claude_cli(tmp_path_factory):
    """No test may reach the real claude CLI: `quoin install` registers a
    marketplace and installs a plugin through it, which would change the
    developer's own Claude Code configuration. A stub goes first on PATH and
    the plugin is staged under a temp folder. Tests that need a specific
    claude stand-in prepend their own; a read-only use of the real CLI finds
    it in QUOIN_REAL_CLAUDE."""
    root = tmp_path_factory.mktemp("fake-claude")
    stub = root / "bin" / "claude"
    stub.parent.mkdir()
    stub.write_text(_FAKE_CLAUDE)
    stub.chmod(0o755)
    mp = pytest.MonkeyPatch()
    real = shutil.which("claude")
    if real:
        mp.setenv("QUOIN_REAL_CLAUDE", real)
    mp.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    mp.setenv("QUOIN_PLUGIN_DIR", str(root / "plugin"))
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="session", autouse=True)
def _no_ambient_claude_session_id():
    """Tests run inside a Claude session that exports CLAUDE_CODE_SESSION_ID;
    clear it so mtime-based session resolution tests are hermetic. Tests that
    exercise the variable set it explicitly."""
    mp = pytest.MonkeyPatch()
    mp.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    try:
        yield
    finally:
        mp.undo()


# Modules whose tests build or drive the paired-run machinery. Any attempt to
# start a real `claude` or `docker` there lands on a marker shim instead and
# fails the test at teardown.
PAID_PATH_GUARDED_MODULES = frozenset({
    "test_arm_identity",
    "test_paired_spend",
    "test_cell_paired_params",
    "test_arm_sandbox",
    "test_runner_verified",
    "test_paired_run",
    "test_run_benchmark_paired_cli",
    "test_tiered_report",
    "test_paired_e2e",
})
_PAID_PATH_COMMANDS = ("claude", "docker")


def make_paid_path_guard_dir(root):
    """Create marker shims named `claude` and `docker` under `root`; return the dir."""
    guard = Path(root) / "paid_path_guard"
    guard.mkdir(parents=True, exist_ok=True)
    for name in _PAID_PATH_COMMANDS:
        shim = guard / name
        shim.write_text('#!/bin/sh\n: > "%s/%s.invoked"\nexit 99\n' % (guard, name))
        shim.chmod(0o755)
    return guard


def paid_path_violations(guard):
    """Names of guarded commands that were started through the shims in `guard`."""
    return [n for n in _PAID_PATH_COMMANDS if (Path(guard) / (n + ".invoked")).exists()]


@pytest.fixture(autouse=True)
def _paid_path_guard(request, tmp_path_factory):
    path = getattr(request.node, "path", None)
    if path is None or path.stem not in PAID_PATH_GUARDED_MODULES:
        yield
        return
    monkeypatch = request.getfixturevalue("monkeypatch")
    guard = make_paid_path_guard_dir(tmp_path_factory.mktemp("paid-path-guard"))
    monkeypatch.setenv("PATH", "%s%s%s" % (guard, os.pathsep, os.environ.get("PATH", "")))
    yield
    used = paid_path_violations(guard)
    if used:
        pytest.fail(
            f"{request.node.nodeid} started a paid-path command: " + ", ".join(used)
        )
