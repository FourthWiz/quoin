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


def test_npm_global_prefix_fast_path_skips_spawn_when_package_present(
    monkeypatch, tmp_path: Path
) -> None:
    """F-15: the production-only fast path (never engaged under pytest —
    see the marker-absence assertion below) must not spawn `npm prefix -g`
    when npm's own binary location already resolves to an installed CCR
    package."""
    from quoin.router import _fast_npm_prefix_guess, _npm_global_prefix

    prefix = tmp_path / "guessed_prefix"
    npm_bin = prefix / "bin" / "npm"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("#!/bin/sh\n")
    _seed_npm_package(prefix, "3.1.0")

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout="/should-not-be-used\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: str(npm_bin) if cmd == "npm" else None,
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # The override check reads real disk state (`$HOME/.npmrc`) and the
    # environment — both pinned here so this test's result doesn't depend
    # on whatever npm config happens to exist on the machine running the
    # suite.
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _npm_global_prefix()

    assert result == str(prefix)
    assert recorded == []
    # The pure guess helper is exercised directly too, hermetically.
    assert _fast_npm_prefix_guess(str(npm_bin)) == str(prefix)


def test_npm_global_prefix_fast_path_never_engaged_under_pytest(
    monkeypatch, tmp_path: Path
) -> None:
    """The same guessable layout as above, but with PYTEST_CURRENT_TEST
    left in place (the normal case for every other test in this suite):
    the fast path must not engage, and the real spawn must still happen —
    a test that wants the spawn must always get it, never a guess read off
    whatever CCR happens to be installed on the machine running the
    suite."""
    from quoin.router import _npm_global_prefix

    prefix = tmp_path / "guessed_prefix"
    npm_bin = prefix / "bin" / "npm"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("#!/bin/sh\n")
    _seed_npm_package(prefix, "3.1.0")

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout="/fake/prefix\n")

    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: str(npm_bin) if cmd == "npm" else None,
    )

    with monkeypatch.context() as m:
        m.setattr("quoin.router._npm_query_enabled", True)
        result = _npm_global_prefix()

    assert recorded, "the real spawn must still happen under pytest"
    assert result == "/fake/prefix"


def _enter_fast_path_production_mode(monkeypatch, tmp_path: Path) -> None:
    """Common setup for the production-mode fast-path tests below: engages
    the fast path (clears the pytest marker) and pins every input the
    override check reads from real disk/environment state, so none of
    these tests depends on whatever npm config the machine running the
    suite happens to have."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")


def test_npm_global_prefix_fast_path_falls_back_when_no_package_at_guess(
    monkeypatch, tmp_path: Path
) -> None:
    """The plain fallback cell: the marker is cleared (fast path
    engaged) but no CCR package exists at the guessed prefix, so the guess
    must not be returned — the real spawn must run exactly once, and its
    result (not the guess) must win. Without this, a future edit that
    dropped the package-present conjunct and returned the guess
    unconditionally would pass the rest of the suite unnoticed."""
    from quoin.router import _npm_global_prefix

    guessed_prefix = tmp_path / "guessed_prefix"
    npm_bin = guessed_prefix / "bin" / "npm"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("#!/bin/sh\n")
    # Deliberately no _seed_npm_package(guessed_prefix, ...) — no CCR
    # package at the guessed location.

    spawned_prefix = tmp_path / "spawned_prefix"
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout=f"{spawned_prefix}\n")

    _enter_fast_path_production_mode(monkeypatch, tmp_path)
    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: str(npm_bin) if cmd == "npm" else None,
    )

    result = _npm_global_prefix()

    assert len(recorded) == 1
    assert result == str(spawned_prefix)


def test_npm_global_prefix_fast_path_falls_back_on_divergent_version(
    monkeypatch, tmp_path: Path
) -> None:
    """The divergent-layout cell: the guessed prefix carries a *different*
    CCR version than the one the real spawn would find — a leftover
    package sitting where npm's binary happens to live, while npm itself
    has been pointed elsewhere by a prefix override. The spawn must win,
    not the stale guess."""
    from quoin.router import _npm_global_prefix

    guessed_prefix = tmp_path / "guessed_prefix"
    npm_bin = guessed_prefix / "bin" / "npm"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("#!/bin/sh\n")
    _seed_npm_package(guessed_prefix, "2.0.0")  # stale, at the binary-adjacent prefix

    spawned_prefix = tmp_path / "spawned_prefix"
    _seed_npm_package(spawned_prefix, "3.1.0")  # the real, current install
    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        return _FakeCompletedProcess(returncode=0, stdout=f"{spawned_prefix}\n")

    _enter_fast_path_production_mode(monkeypatch, tmp_path)
    # The override: a configured prefix means npm's real resolution can
    # diverge from where its binary sits, which is exactly the machine
    # shape this test recreates.
    monkeypatch.setenv("npm_config_prefix", str(spawned_prefix))
    monkeypatch.setattr("quoin.router.subprocess.run", fake_run)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: str(npm_bin) if cmd == "npm" else None,
    )

    result = _npm_global_prefix()

    assert len(recorded) == 1
    assert result == str(spawned_prefix)
    assert result != str(guessed_prefix)


def test_npm_prefix_override_env_var_defeats_the_guess(
    monkeypatch, tmp_path: Path
) -> None:
    """`_npm_prefix_override_in_play` itself, for each environment-variable
    spelling npm recognises."""
    from quoin.router import _npm_prefix_override_in_play

    guess = str(tmp_path / "guessed_prefix")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    assert _npm_prefix_override_in_play(guess) is False

    with monkeypatch.context() as m:
        m.setenv("npm_config_prefix", "/somewhere/else")
        assert _npm_prefix_override_in_play(guess) is True

    with monkeypatch.context() as m:
        m.delenv("npm_config_prefix", raising=False)
        m.setenv("NPM_CONFIG_PREFIX", "/somewhere/else")
        assert _npm_prefix_override_in_play(guess) is True


def test_npm_prefix_override_home_npmrc_defeats_the_guess(
    monkeypatch, tmp_path: Path
) -> None:
    """A `prefix=` line in `$HOME/.npmrc` is npm's own documented remedy
    for a global-install permission error — the exact machine shape this
    override check exists for."""
    from quoin.router import _npm_prefix_override_in_play

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".npmrc").write_text("prefix=/custom/npm/prefix\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)

    guess = str(tmp_path / "guessed_prefix")
    assert _npm_prefix_override_in_play(guess) is True


def test_npm_prefix_override_guessed_prefix_etc_npmrc_defeats_the_guess(
    monkeypatch, tmp_path: Path
) -> None:
    """The other `.npmrc` npm consults: one sitting inside the guessed
    prefix's own `etc/` directory."""
    from quoin.router import _npm_prefix_override_in_play

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)

    guessed_prefix = tmp_path / "guessed_prefix"
    (guessed_prefix / "etc").mkdir(parents=True)
    (guessed_prefix / "etc" / "npmrc").write_text(
        "prefix=/somewhere/else\n", encoding="utf-8"
    )
    assert _npm_prefix_override_in_play(str(guessed_prefix)) is True


def test_npm_prefix_override_ignores_comments_and_other_keys(
    monkeypatch, tmp_path: Path
) -> None:
    """A `.npmrc` that exists but sets no `prefix` (comments, unrelated
    keys, a key merely containing "prefix") must not be mistaken for an
    override."""
    from quoin.router import _npm_prefix_override_in_play

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    (fake_home / ".npmrc").write_text(
        "# a comment mentioning prefix=/nope\n"
        "registry=https://registry.npmjs.org/\n"
        "some-other-prefix-like-key=value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("npm_config_prefix", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)

    guess = str(tmp_path / "guessed_prefix")
    assert _npm_prefix_override_in_play(guess) is False


def test_fast_npm_prefix_guess_non_bin_parent_is_the_prefix_itself(
    tmp_path: Path,
) -> None:
    """The non-bin layout (e.g. npm.cmd sitting directly inside the global
    prefix on Windows): the guess is the binary's own parent directory,
    not its grandparent."""
    from quoin.router import _fast_npm_prefix_guess

    npm_bin = tmp_path / "nodejs" / "npm.cmd"
    npm_bin.parent.mkdir(parents=True)
    npm_bin.write_text("")

    assert _fast_npm_prefix_guess(str(npm_bin)) == str(npm_bin.parent)


def test_npm_global_prefix_none_on_timeout(monkeypatch) -> None:
    """A wedged `npm prefix -g` degrades to None, not a raise — and it must
    actually carry a positive timeout kwarg, or a mutant that deletes
    `timeout=5` from the call site would still pass this test."""
    from quoin.router import _npm_global_prefix

    recorded_timeouts: list[object] = []

    def hanging_run(argv, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

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
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

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
