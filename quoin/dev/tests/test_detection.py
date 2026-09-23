"""Tests for CCR version detection.

Pure units only — everything that drives a handler (_cmd_router_setup /
_cmd_router_status) lives in test_router_setup.py instead.

All tests run in CI with NO network/npm/ccr access.
- The shared conftest.py sets quoin.router._npm_query_enabled = False for the
  whole run, so detect_ccr's empty-store npm fallback never reads this
  machine's real global npm install.
- Tests that need to observe a "real" npm read stub subprocess.run (or the
  seams) directly rather than re-enabling the flag against host state; the
  two tests that do need the flag genuinely on scope it with
  monkeypatch.context() so it never leaks to another test.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.router import (  # noqa: E402
    CCR_KNOWN_MAJOR_MAX,
    CCR_PINNED_VERSION,
    CCR_VERSION_CONSTRAINT,
    CcrVersion,
    _install_ccr,
    _node_major,
    _npm_major,
    _scrubbed_env,
    detect_ccr,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _seed_store(
    tmp_path: Path, *, sqlite: bool = False, json_: bool = False, sqlite_bytes: bytes | None = None
) -> None:
    """Seed `config.sqlite` and/or `config.json` under a synthetic home.

    `sqlite=True` writes a real, non-empty, openable database — a zero-byte
    file satisfies sqlite's own definition of a valid empty database, which
    is exactly the ambiguity `_sqlite_file_is_empty` exists to resolve, so a
    fixture meaning "a real v3 store is here" must not hand back one.
    `sqlite_bytes` overrides that with arbitrary content, for tests that
    want the zero-byte or corrupt-file cases on purpose.
    """
    store_dir = tmp_path / ".claude-code-router"
    store_dir.mkdir(parents=True, exist_ok=True)
    if sqlite_bytes is not None:
        (store_dir / "config.sqlite").write_bytes(sqlite_bytes)
    elif sqlite:
        path = store_dir / "config.sqlite"
        con = sqlite3.connect(str(path))
        try:
            con.execute(
                "CREATE TABLE app_config (key TEXT PRIMARY KEY, "
                "value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            con.commit()
        finally:
            con.close()
    if json_:
        (store_dir / "config.json").write_text("{}", encoding="utf-8")


def _seed_npm_package(prefix: Path, version: str) -> None:
    pkg_dir = prefix / "lib" / "node_modules" / "@musistudio" / "claude-code-router"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── (a) Detection matrix ─────────────────────────────────────────────────────

def test_sqlite_only_is_v3(tmp_path: Path) -> None:
    _seed_store(tmp_path, sqlite=True)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.store == "sqlite"
    assert result.source == "store:sqlite"


def test_json_only_is_v2(monkeypatch, tmp_path: Path) -> None:
    _seed_store(tmp_path, json_=True)

    def _raise_if_called() -> str | None:
        raise AssertionError("json_ branch must not consult npm")

    monkeypatch.setattr("quoin.router._npm_global_prefix", _raise_if_called)
    result = detect_ccr(home=tmp_path)
    assert (result.major, result.store, result.source) == (2, "json", "store:json")


def test_both_stores_prefers_sqlite(tmp_path: Path) -> None:
    _seed_store(tmp_path, sqlite=True, json_=True)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3


def test_neither_store_is_unknown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: None)
    result = detect_ccr(home=tmp_path)
    assert result.major == 0
    assert result.store is None
    assert result.source == "none"


def test_npm_higher_major_is_unknown(monkeypatch, tmp_path: Path) -> None:
    """The store-absent half of AC-4."""
    prefix = tmp_path / "npm_prefix"
    _seed_npm_package(prefix, "4.2.0")
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: str(prefix))
    result = detect_ccr(home=tmp_path)
    assert result.major == 0
    assert result.major != 3
    assert result.source == "npm-capped"


def test_major_is_never_an_enum(monkeypatch, tmp_path: Path) -> None:
    # sqlite branch
    sqlite_dir = tmp_path / "sqlite_case"
    _seed_store(sqlite_dir, sqlite=True)
    assert type(detect_ccr(home=sqlite_dir).major) is int

    # json branch
    json_dir = tmp_path / "json_case"
    _seed_store(json_dir, json_=True)
    assert type(detect_ccr(home=json_dir).major) is int

    # empty store, npm absent
    empty_dir = tmp_path / "empty_case"
    empty_dir.mkdir()
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: None)
    assert type(detect_ccr(home=empty_dir).major) is int

    # sqlite + npm-capped (the new branch)
    capped_dir = tmp_path / "capped_case"
    _seed_store(capped_dir, sqlite=True)
    monkeypatch.setattr("quoin.router._npm_major", lambda: 4)
    result = detect_ccr(home=capped_dir)
    assert result.source == "store:sqlite-npm-capped"
    assert type(result.major) is int
    assert result.major == 0


# ── (b) Seam isolation (AC-5, AC-6) ─────────────────────────────────────────

def test_does_not_invoke_ccr_version(monkeypatch, tmp_path: Path) -> None:
    """Exercises the exclusion rather than asserting it: seam A is left
    intact (with the flag genuinely on, scoped to this test) and
    subprocess.run is stubbed to dispatch on argv, so a "ccr" call would
    really raise and an "npm" call is really answered. The resolved npm
    path (not the bare "npm" argv[0] the pre-fix code spawned, which never
    reaches npm.cmd's PATHEXT resolution on Windows) is what gets spawned."""
    prefix = tmp_path / "npm_prefix"
    _seed_npm_package(prefix, "3.0.5")
    fake_npm_path = "/usr/local/bin/npm"
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        if argv[0] == "ccr":
            raise FileNotFoundError("ccr must never be invoked by detect_ccr")
        if argv[:2] == [fake_npm_path, "prefix"]:
            return _FakeCompletedProcess(returncode=0, stdout=str(prefix))
        raise AssertionError(f"unexpected subprocess call: {argv}")

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        m.setattr("quoin.router.subprocess.run", fake_run)
        m.setattr(
            "quoin.router.shutil.which",
            lambda cmd: fake_npm_path if cmd == "npm" else None,
        )
        result = detect_ccr(home=tmp_path)  # no store present

    assert result.major == 3
    assert result.source == "npm"
    assert not any(argv[0] == "ccr" for argv in recorded)
    assert any(argv[:2] == [fake_npm_path, "prefix"] for argv in recorded)
    # The resolved-path plumbing, not bare "npm" — pins F-08's fix.
    assert not any(argv and argv[0] == "npm" for argv in recorded)


def test_detect_uses_no_ccr_subcommand(monkeypatch, tmp_path: Path) -> None:
    """The v2 half of the same exclusion: detection on the store path need
    not shell out at all."""
    _seed_store(tmp_path, json_=True)
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        if argv[0] == "ccr":
            raise FileNotFoundError("ccr must never be invoked by detect_ccr")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert recorded == []


def test_no_writes_attempted(monkeypatch, tmp_path: Path) -> None:
    """Asserts the property directly (zero write-mode open calls) rather
    than as a side effect of directory permissions — a read-only-directory
    trick is unreliable across platforms and doesn't pin what actually
    changed."""
    _seed_store(tmp_path, sqlite=True)

    write_calls: list[tuple] = []
    import builtins
    import os as os_module

    orig_open = builtins.open

    def fake_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            write_calls.append((file, mode))
        return orig_open(file, mode, *args, **kwargs)

    orig_os_open = os_module.open

    def fake_os_open(path, flags, *args, **kwargs):
        write_flags = os_module.O_WRONLY | os_module.O_RDWR | os_module.O_CREAT
        if flags & write_flags:
            write_calls.append((path, flags))
        return orig_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os_module, "open", fake_os_open)

    detect_ccr(home=tmp_path)

    assert write_calls == []


def test_subprocess_seam_failure_still_resolves_v2(monkeypatch, tmp_path: Path) -> None:
    """Store signal alone resolves v2 with every subprocess seam raising."""
    _seed_store(tmp_path, json_=True)

    def raise_run(*args, **kwargs):
        raise OSError("subprocess must not be needed on the json_ branch")

    monkeypatch.setattr("quoin.router.subprocess.run", raise_run)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert result.store == "json"
    assert result.source == "store:json"


def test_npm_flag_gates_seam_a(monkeypatch) -> None:
    """The flag gate, asserted where the flag is read: _npm_global_prefix
    itself."""
    from quoin.router import _npm_global_prefix

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout="/fake/prefix\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None)

    # Default under pytest (conftest.py): flag is False.
    assert _npm_global_prefix() is None
    assert len(recorded) == 0

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        result = _npm_global_prefix()

    assert len(recorded) == 1
    assert result == "/fake/prefix"


def test_npm_global_prefix_spawns_resolved_path(monkeypatch) -> None:
    """F-08: the spawn argv[0] is the shutil.which-resolved npm path, not
    a bare "npm" literal — a bare argv[0] never reaches npm.cmd's PATHEXT
    resolution on Windows, so the fix is to spawn the path shutil.which
    already found rather than re-resolving "npm" at spawn time."""
    from quoin.router import _npm_global_prefix

    resolved_path = "/opt/homebrew/bin/npm"
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout="/fake/prefix\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: resolved_path if cmd == "npm" else None,
    )

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        _npm_global_prefix()

    assert recorded
    assert recorded[0][0] == resolved_path
    assert recorded[0][0] != "npm"


def test_npm_global_prefix_none_when_npm_absent_without_spawning(monkeypatch) -> None:
    """When shutil.which finds nothing, _npm_global_prefix must not spawn
    at all — the gate is checked once, not re-resolved at spawn time."""
    from quoin.router import _npm_global_prefix

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout="/fake/prefix\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        result = _npm_global_prefix()

    assert result is None
    assert recorded == []


def test_npm_global_prefix_none_on_timeout(monkeypatch) -> None:
    """A wedged `npm prefix -g` degrades to None, not a raise — and it must
    actually carry a positive timeout kwarg, or a mutant that deletes
    `timeout=10` from the call site would still pass this test."""
    from quoin.router import _npm_global_prefix

    recorded_timeouts: list[object] = []

    def hanging_run(argv, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=cast("float", kwargs.get("timeout")))

    monkeypatch.setattr("quoin.router.subprocess.run", hanging_run)
    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        m.setattr("quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None)
        assert _npm_global_prefix() is None

    assert recorded_timeouts and all(
        isinstance(t, (int, float)) and t > 0 for t in recorded_timeouts
    )


def test_npm_flag_off_vs_on_through_detect(monkeypatch, tmp_path: Path) -> None:
    """The flag gate end-to-end, with seam A intact: both the gate and the
    package.json read are genuinely in the path, so the flip is the only
    difference."""
    prefix = tmp_path / "npm_prefix"
    _seed_npm_package(prefix, "2.0.0")

    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout=str(prefix))

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)

    result_off = detect_ccr(home=tmp_path)
    assert result_off.major == 0
    assert result_off.source == "none"

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        m.setattr("quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None)
        result_on = detect_ccr(home=tmp_path)

    assert result_on.major == 2
    assert result_on.source == "npm"


def test_store_branch_ignores_npm_flag(tmp_path: Path) -> None:
    """Unreadable npm degrades to 'no opinion', it does not degrade to
    'a value'. With config.sqlite present and the conftest-default flag
    (False), detection still resolves v3 — the store signal alone is
    sufficient when npm is unreadable."""
    _seed_store(tmp_path, sqlite=True)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.source == "store:sqlite"
    assert result.store == "sqlite"


def test_sqlite_and_npm_capped_is_unknown(monkeypatch, tmp_path: Path) -> None:
    """The AC-4 conjunction. Also asserts the sibling below-max case so an
    over-broad implementation of the residual rule (capping on ANY
    npm/store mismatch) would be caught."""
    _seed_store(tmp_path, sqlite=True)

    monkeypatch.setattr("quoin.router._npm_major", lambda: 4)
    capped = detect_ccr(home=tmp_path)
    assert capped.major == 0
    assert capped.major != 3
    assert capped.source == "store:sqlite-npm-capped"
    assert capped.store == "sqlite"

    monkeypatch.setattr("quoin.router._npm_major", lambda: 2)
    below_max = detect_ccr(home=tmp_path)
    assert below_max.major == 3


def test_sqlite_with_unreadable_npm_is_v3(monkeypatch, tmp_path: Path) -> None:
    """The AC-1/AC-4 boundary, stated as its own case: the store signal
    alone is sufficient when npm cannot be read."""
    _seed_store(tmp_path, sqlite=True)
    monkeypatch.setattr("quoin.router._npm_major", lambda: None)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.source == "store:sqlite"


# ── zero-byte config.sqlite: ambiguous by content, resolved by npm ──────────

def test_zero_byte_sqlite_alone_is_still_v3(monkeypatch, tmp_path: Path) -> None:
    """With nothing to disagree with it (npm unreadable, the conftest
    default), a zero-byte config.sqlite is left exactly where a real,
    freshly-initialised store would be — the same boundary
    test_sqlite_with_unreadable_npm_is_v3 above pins, stated for the
    literal zero-byte shape rather than a seeded one."""
    _seed_store(tmp_path, sqlite_bytes=b"")
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.source == "store:sqlite"


def test_zero_byte_sqlite_with_matching_npm_is_still_v3(
    monkeypatch, tmp_path: Path
) -> None:
    """npm confirming major 3 is consistent with a real store that simply
    has not been written to yet, so the empty file is trusted rather than
    discarded."""
    _seed_store(tmp_path, sqlite_bytes=b"")
    monkeypatch.setattr("quoin.router._npm_major", lambda: 3)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.source == "store:sqlite"


def test_zero_byte_sqlite_with_disagreeing_npm_falls_through_to_json(
    monkeypatch, tmp_path: Path
) -> None:
    """A zero-byte config.sqlite must not outrank a definite, disagreeing
    npm reading. On a machine npm reports as major 2 with a leftover
    config.json present, the empty sqlite file carries nothing to weigh
    against that — detection must fall through exactly as if config.sqlite
    did not exist, landing on the genuine v2 signal rather than reporting
    a v2 -> v3 upgrade that never happened."""
    _seed_store(tmp_path, sqlite_bytes=b"", json_=True)
    monkeypatch.setattr("quoin.router._npm_major", lambda: 2)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert result.store == "json"
    assert result.source == "store:json"


def test_zero_byte_sqlite_with_disagreeing_npm_and_no_json_falls_through_to_npm(
    monkeypatch, tmp_path: Path
) -> None:
    """Same disagreement, but with no config.json to fall through to
    either — detection lands on the npm reading alone, as if config.sqlite
    were entirely absent."""
    _seed_store(tmp_path, sqlite_bytes=b"")
    monkeypatch.setattr("quoin.router._npm_major", lambda: 2)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert result.store is None
    assert result.source == "npm"


def test_non_zero_but_corrupt_sqlite_still_outranks_npm(
    monkeypatch, tmp_path: Path
) -> None:
    """The zero-byte ambiguity is specific to sqlite's own empty-database
    semantics — a non-empty file that happens to be unreadable (corrupt, or
    a real store a live `ccr` holds locked) is left to the existing
    store-present-but-unreadable handling, not folded into this fallback.
    Deliberately does not also gate on read status; see
    `_sqlite_file_is_empty`'s docstring for why."""
    _seed_store(tmp_path, sqlite_bytes=b"not a sqlite database, just bytes", json_=True)
    monkeypatch.setattr("quoin.router._npm_major", lambda: 2)
    result = detect_ccr(home=tmp_path)
    assert result.major == 3
    assert result.source == "store:sqlite"


def test_zero_byte_sqlite_with_npm_major_one_and_no_json_falls_through_to_npm(
    monkeypatch, tmp_path: Path
) -> None:
    """A CCR 1.x machine with a stray zero-byte config.sqlite and no
    leftover config.json must fall through to the npm reading, landing on
    the v2 route — the same cell a config.json-major-2 store already falls
    through on. A guard narrowed to major == 2 only leaves this cell with
    no arm that could object to a definite, in-range, disagreeing major-1
    reading, so it dispatches a v3 store write the machine could never
    read back."""
    _seed_store(tmp_path, sqlite_bytes=b"")
    monkeypatch.setattr("quoin.router._npm_major", lambda: 1)
    result = detect_ccr(home=tmp_path)
    assert result.major == 1
    assert result.store is None
    assert result.source == "npm"


def test_zero_byte_sqlite_with_npm_major_one_and_json_falls_through_to_json(
    monkeypatch, tmp_path: Path
) -> None:
    """Same npm-major-1 machine, but with a leftover config.json: the
    fall-through must land on the store-absent path (via the json arm,
    dispatched as "v3-store-absent" by dispatch_store) rather than
    reporting v3 and firing a false upgrade-loss notice."""
    _seed_store(tmp_path, sqlite_bytes=b"", json_=True)
    monkeypatch.setattr("quoin.router._npm_major", lambda: 1)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert result.store == "json"
    assert result.source == "store:json"


def test_zero_byte_sqlite_fallthrough_resolves_npm_major_once(
    monkeypatch, tmp_path: Path
) -> None:
    """The disagreeing-npm-major fall-through must not read npm twice: a
    default-argument caller resolves the major once in the sqlite branch
    and the tail reuses that value rather than resolving it again. No
    config.json here — the double read this test pins only happens on the
    tail that runs when json_ is absent; seeding config.json returns from
    the json arm before ever reaching that tail, so the read count is
    satisfied whether or not the tail actually resolves the major only
    once."""
    _seed_store(tmp_path, sqlite_bytes=b"")
    calls = {"n": 0}

    def _counting_npm_major():
        calls["n"] += 1
        return 2

    monkeypatch.setattr("quoin.router._npm_major", _counting_npm_major)
    result = detect_ccr(home=tmp_path)
    assert result.major == 2
    assert result.store is None
    assert result.source == "npm"
    assert calls["n"] == 1


# ── D-06: the zero-byte rule, both halves, over the full eight-cell table ─────

_ZERO_BYTE_TABLE: dict[tuple[int | None, bool], tuple[CcrVersion, str]] = {
    (None, False): (CcrVersion(3, "sqlite", "store:sqlite"), "v3"),
    (None, True): (CcrVersion(3, "sqlite", "store:sqlite"), "v3"),
    (1, False): (CcrVersion(1, None, "npm"), "v2"),
    (1, True): (CcrVersion(2, "json", "store:json"), "v3-store-absent"),
    (2, False): (CcrVersion(2, None, "npm"), "v2"),
    (2, True): (CcrVersion(2, "json", "store:json"), "v2"),
    (3, False): (CcrVersion(3, "sqlite", "store:sqlite"), "v3"),
    (3, True): (CcrVersion(3, "sqlite", "store:sqlite"), "v3"),
}


@pytest.mark.parametrize("cell", list(_ZERO_BYTE_TABLE))
def test_zero_byte_sqlite_table_pins_detection_and_dispatch(
    monkeypatch, tmp_path: Path, cell: tuple[int | None, bool]
) -> None:
    """D-06, both halves at once: for every (npm major, config.json
    present) cell with a zero-byte config.sqlite on disk, pin the exact
    `detect_ccr` tuple *and* the `dispatch_store` route it produces. The
    four npm-1/npm-2 cells distrust the empty file and fall through; the
    four npm-None/npm-3 cells trust it and stay v3."""
    from quoin.router import dispatch_store

    npm_major, json_present = cell
    expected_version, expected_route = _ZERO_BYTE_TABLE[cell]
    _seed_store(tmp_path, sqlite_bytes=b"", json_=json_present)
    monkeypatch.setattr("quoin.router._npm_major", lambda: npm_major)

    result = detect_ccr(home=tmp_path, npm_major=npm_major)
    assert result == expected_version
    assert dispatch_store(result, npm_major) == expected_route


def test_zero_byte_table_covers_all_eight_cells() -> None:
    """The count is derived from the enumeration, never asserted beside it."""
    expected = {(npm, json_) for npm in (None, 1, 2, 3) for json_ in (False, True)}
    assert set(_ZERO_BYTE_TABLE) == expected


def test_sqlite_file_is_empty_docstring_states_the_rule() -> None:
    """A future docstring edit that contradicts D-06 must redden here
    rather than drift silently — the mechanism whose absence let this one
    rule drift for three consecutive rounds. Normalized because both
    load-bearing clauses span a line break in the source."""
    from quoin.router import _sqlite_file_is_empty

    doc = _sqlite_file_is_empty.__doc__ or ""
    normalized = " ".join(doc.split())
    assert "only treats this as a reason to distrust the store's presence" in normalized
    assert "left exactly where a real, freshly-initialised store would be" in normalized


# ── (c) package.json parser ─────────────────────────────────────────────────

def test_bad_package_json_is_unknown(monkeypatch, tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    pkg_dir = prefix / "lib" / "node_modules" / "@musistudio" / "claude-code-router"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: str(prefix))
    assert _npm_major() is None


def test_missing_package_json_is_unknown(monkeypatch, tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: str(prefix))
    assert _npm_major() is None


def test_non_numeric_version_is_unknown(monkeypatch, tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    _seed_npm_package(prefix, "abc.def")
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: str(prefix))
    assert _npm_major() is None


def test_oversized_package_json_is_unknown(monkeypatch, tmp_path: Path) -> None:
    """Refuse to read an oversized manifest rather than loading it."""
    prefix = tmp_path / "prefix"
    pkg_dir = prefix / "lib" / "node_modules" / "@musistudio" / "claude-code-router"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    oversized = {"version": "3.0.0", "padding": "x" * (1024 * 1024 + 1)}
    (pkg_dir / "package.json").write_text(json.dumps(oversized), encoding="utf-8")
    monkeypatch.setattr("quoin.router._npm_global_prefix", lambda: str(prefix))
    assert _npm_major() is None


# ── (d) Pin (AC-11, AC-12) and Node-major parse (AC-13, seam level) ─────────

def test_install_command_carries_pinned_version(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None
    )
    _install_ccr()

    assert recorded
    argv = recorded[0]
    assert f"@musistudio/claude-code-router@{CCR_PINNED_VERSION}" in argv
    assert "@musistudio/claude-code-router" not in argv


def test_install_ccr_spawns_resolved_path(monkeypatch) -> None:
    """F-08: `_install_ccr` spawns the shutil.which-resolved npm path as
    argv[0], not a bare "npm" literal — the same fix as
    `_npm_global_prefix`, closing the check-then-spawn re-resolution race
    at both npm spawn sites."""
    resolved_path = "/opt/homebrew/bin/npm"
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: resolved_path if cmd == "npm" else None,
    )
    _install_ccr()

    assert recorded
    assert recorded[0][0] == resolved_path
    assert recorded[0][0] != "npm"


def test_install_command_derives_from_constant(monkeypatch) -> None:
    assert CCR_VERSION_CONSTRAINT == f"@{CCR_PINNED_VERSION}"

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr("quoin.router.CCR_VERSION_CONSTRAINT", "@9.9.9")
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None
    )
    _install_ccr()

    assert "@musistudio/claude-code-router@9.9.9" in recorded[0]


def test_install_ccr_returns_1_when_npm_absent(monkeypatch) -> None:
    """Node without npm (some distributions ship it separately) must
    return a plain failure code rather than spawning at all."""
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

    assert _install_ccr() == 1
    assert recorded == []


def test_install_ccr_returns_1_on_missing_binary(monkeypatch) -> None:
    """A spawn that raises FileNotFoundError (npm resolved on PATH but the
    binary itself is gone, or a race with the presence check) must return
    an int, never propagate the exception — every handler on this path
    must always return int, never raise."""

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("npm")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None
    )

    assert _install_ccr() == 1


def test_node_major_parses_v22_11_0(monkeypatch) -> None:
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )

    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="v22.11.0\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    assert _node_major() == 22


def test_node_major_none_when_absent(monkeypatch) -> None:
    monkeypatch.setattr("quoin.router.shutil.which", lambda name: None)
    assert _node_major() is None


def test_node_major_none_on_timeout(monkeypatch) -> None:
    """A wedged `node --version` degrades to None, not a raise — and it
    must actually carry a positive timeout kwarg, or a mutant that deletes
    `timeout=5` from the call site would still pass this test."""
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )

    recorded_timeouts: list[object] = []

    def hanging_run(argv, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=cast("float", kwargs.get("timeout")))

    monkeypatch.setattr("quoin.router.subprocess.run", hanging_run)
    assert _node_major() is None

    assert recorded_timeouts and all(
        isinstance(t, (int, float)) and t > 0 for t in recorded_timeouts
    )


def test_node_major_none_on_bad_output(monkeypatch) -> None:
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )

    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="not-a-version\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    assert _node_major() is None


# ── Environment scrubbing (the env passed to every spawn) ──────────────────────

def test_scrubbed_env_removes_key_and_copies_the_rest(monkeypatch) -> None:
    """`_scrubbed_env` must drop the API key and nothing else, and must
    hand back a copy — not the live `os.environ` mapping — so a caller
    mutating the result can never touch the real process environment."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")

    env = _scrubbed_env()

    assert "OPENROUTER_API_KEY" not in env
    assert env["SOME_OTHER_VAR"] == "keep-me"
    assert env["PATH"] == os.environ["PATH"]
    assert env is not os.environ


def test_npm_global_prefix_spawn_env_is_scrubbed(monkeypatch) -> None:
    """Seam A (`npm prefix -g`, used by `_npm_major`) must pass a scrubbed
    `env` kwarg — a probe never needs the OpenRouter key, so it must never
    see it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
    recorded: list[dict] = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs)
        return _FakeCompletedProcess(returncode=0, stdout="/fake/prefix\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        m.setattr("quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None)
        from quoin.router import _npm_global_prefix

        _npm_global_prefix()

    assert recorded
    env = recorded[0]["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_node_major_spawn_env_is_scrubbed(monkeypatch) -> None:
    """Seam for `node --version` must also receive a scrubbed `env` —
    a probe that only reads a version string never needs the key either."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )
    recorded: list[dict] = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs)
        return _FakeCompletedProcess(returncode=0, stdout="v22.11.0\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    _node_major()

    assert recorded
    env = recorded[0]["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_install_ccr_spawn_env_is_scrubbed(monkeypatch) -> None:
    """The highest-value site: `npm install -g` runs the package's
    lifecycle scripts, so this is the spawn the scrub matters most for.
    Nothing observable downstream records which environment was handed to
    npm, so only a test catches a regression here."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
    monkeypatch.setattr(
        "quoin.router.shutil.which", lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None
    )
    recorded: list[dict] = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    _install_ccr()

    assert recorded
    env = recorded[0]["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert env["PATH"] == os.environ["PATH"]
