"""Tests for quoin.ccr_store — the v3 (sqlite-backed) CCR config store."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from conftest import make_v3_store, store_value_snapshot

from quoin import ccr_store


# ── module hygiene ───────────────────────────────────────────────────────────


def test_module_never_calls_path_home_or_names_encrypted_key():
    src = Path(ccr_store.__file__).read_text(encoding="utf-8")
    assert "Path.home()" not in src
    assert "encrypted_key" not in src


# ── read matrix ──────────────────────────────────────────────────────────────


def test_read_absent_file(tmp_path):
    result = ccr_store.read_v3_config(tmp_path / "config.sqlite")
    assert result.status == "missing"
    assert result.raw == ""
    assert result.config == {}


def test_read_does_not_create_the_file(tmp_path):
    path = tmp_path / "config.sqlite"
    ccr_store.read_v3_config(path)
    assert not path.exists()


def test_read_not_a_database(tmp_path):
    path = tmp_path / "config.sqlite"
    path.write_bytes(b"not a sqlite database, just some bytes")
    result = ccr_store.read_v3_config(path)
    assert result.status == "unreadable"
    assert result.raw == ""


def test_read_table_absent(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    result = ccr_store.read_v3_config(path)
    assert result.status == "ok"
    assert result.raw == ""
    assert result.config == {}


def test_read_row_absent(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    con.commit()
    con.close()
    result = ccr_store.read_v3_config(path)
    assert result.status == "ok"
    assert result.raw == ""
    assert result.config == {}


def test_read_malformed_json(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO app_config VALUES ('default', '{not valid json', '2026-01-01T00:00:00.000Z')"
    )
    con.commit()
    con.close()
    result = ccr_store.read_v3_config(path)
    assert result.status == "malformed"
    assert result.raw == "{not valid json"


def test_read_ok(tmp_path):
    blob = {"Providers": [], "APIKEY": "", "observability": {"x": 1}}
    path = make_v3_store(tmp_path, blob)
    result = ccr_store.read_v3_config(path)
    assert result.status == "ok"
    assert result.config == blob
    assert result.raw == json.dumps(blob, ensure_ascii=False)
    assert result.updated_at == "2026-01-01T00:00:00.000Z"


# ── write: backup and no-op semantics ───────────────────────────────────────


def test_sidecar_holds_pre_modification_blob_not_post(tmp_path):
    """The sidecar must parse to P1 (before), never P2 (after) — CRIT-1."""
    p1 = {"Providers": [{"name": "p1"}], "APIKEY": ""}
    path = make_v3_store(tmp_path, p1)
    backup_dir = tmp_path

    def mutate(cfg):
        cfg["Providers"] = [{"name": "p2"}]
        return ["Providers: replaced"], []

    result = ccr_store.update_v3_config(path, mutate, backup_dir=backup_dir)
    assert result.wrote is True
    assert result.backup is not None
    sidecar_blob = json.loads(result.backup.read_text(encoding="utf-8"))
    assert sidecar_blob == p1
    assert sidecar_blob != {"Providers": [{"name": "p2"}], "APIKEY": ""}


def test_backup_created_mode_0600_and_reread_verified(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX file-mode bits only")
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})

    def mutate(cfg):
        cfg["Providers"] = []
        return ["cleared"], []

    result = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    mode = stat.S_IMODE(result.backup.stat().st_mode)
    assert mode == 0o600


def test_idempotency_second_write_is_a_noop(tmp_path):
    blob = {"Providers": []}
    path = make_v3_store(tmp_path, blob)

    def mutate(cfg):
        cfg.setdefault("Providers", [])
        if not any(p.get("name") == "openrouter" for p in cfg["Providers"]):
            cfg["Providers"].append({"name": "openrouter"})
            return ["added"], []
        return [], []

    first = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert first.wrote is True
    snap_after_first = store_value_snapshot(path)

    backups_after_first = list(tmp_path.glob("config.sqlite.value-bak-*.json"))
    assert len(backups_after_first) == 1

    second = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert second.wrote is False
    assert second.backup is None
    assert store_value_snapshot(path) == snap_after_first
    backups_after_second = list(tmp_path.glob("config.sqlite.value-bak-*.json"))
    assert len(backups_after_second) == 1


def test_same_second_sidecar_collision_takes_dash_1_suffix(tmp_path, monkeypatch):
    import datetime as _real_datetime

    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})
    fixed_ts = "20260101T000000Z"

    class _FixedDateTime(_real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(ccr_store.datetime, "datetime", _FixedDateTime)
    existing = tmp_path / f"config.sqlite.value-bak-{fixed_ts}.json"
    existing.write_text("pre-existing", encoding="utf-8")

    def mutate(cfg):
        cfg["Providers"] = []
        return ["cleared"], []

    result = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert result.backup == tmp_path / f"config.sqlite.value-bak-{fixed_ts}-1.json"


def test_dry_run_writes_nothing(tmp_path):
    blob = {"Providers": [{"name": "p1"}]}
    path = make_v3_store(tmp_path, blob)
    before = store_value_snapshot(path)

    def mutate(cfg):
        cfg["Providers"] = []
        return ["cleared"], []

    result = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path, dry_run=True)
    assert result.wrote is False
    assert result.backup is None
    assert result.changes == ["cleared"]
    assert store_value_snapshot(path) == before
    assert list(tmp_path.glob("config.sqlite.value-bak-*.json")) == []


def test_dry_run_refuses_a_write_the_real_run_would_refuse(tmp_path):
    """Dry run must not report success for a write the real run would
    refuse. The non-finite check is pure, so it is hoisted above the
    dry-run return rather than living only on the write path below it."""
    blob = {"Providers": [{"name": "p1"}]}
    path = make_v3_store(tmp_path, blob)

    def mutate(cfg):
        cfg["Weird"] = 1e999  # json.loads parses this to float("inf")
        return ["added weird key"], []

    with pytest.raises(ccr_store.CcrStoreError, match="non-finite"):
        ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path, dry_run=True)

    assert list(tmp_path.glob("config.sqlite.value-bak-*.json")) == []


def test_empty_store_write_has_no_sidecar_even_though_it_writes(tmp_path):
    """AC-24 empty-store cell: raw == '' means nothing to back up."""
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.close()

    def mutate(cfg):
        cfg["Providers"] = [{"name": "openrouter"}]
        return ["added"], []

    result = ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert result.wrote is True
    assert result.backup is None
    assert list(tmp_path.glob("config.sqlite.value-bak-*.json")) == []


def test_backup_failure_aborts_before_database_is_touched(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores mode bits; the read-only backup_dir cell is vacuous")

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    path = make_v3_store(store_dir, {"Providers": [{"name": "p1"}]}, wal=True)
    before = store_value_snapshot(path)

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    backup_dir.chmod(0o555)
    try:

        def mutate(cfg):
            cfg["Providers"] = []
            return ["cleared"], []

        with pytest.raises(ccr_store.CcrStoreError):
            ccr_store.update_v3_config(path, mutate, backup_dir=backup_dir)

        assert store_value_snapshot(path) == before
        assert list(store_dir.glob("config.sqlite.value-bak-*.json")) == []
        assert list(backup_dir.glob("config.sqlite.value-bak-*.json")) == []
    finally:
        backup_dir.chmod(0o755)


def test_mutate_non_ccr_store_error_propagates_with_no_sidecar(tmp_path):
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})

    def mutate(cfg):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert list(tmp_path.glob("config.sqlite.value-bak-*.json")) == []


def test_lock_contention_raises_ccr_store_error_promptly(tmp_path):
    # WAL mode: a reader (read_v3_config, opened mode=ro) is not blocked by
    # another connection's EXCLUSIVE write lock — only the writer's own
    # BEGIN IMMEDIATE (timeout=2.0) contends, matching the measured
    # production scenario (a live `ccr` process holding the store open).
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]}, wal=True)
    holder = sqlite3.connect(str(path), isolation_level=None, timeout=0.5)
    holder.execute("BEGIN EXCLUSIVE")
    try:

        def mutate(cfg):
            cfg["Providers"] = []
            return ["cleared"], []

        started = time.monotonic()
        with pytest.raises(ccr_store.CcrStoreError):
            ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
        elapsed = time.monotonic() - started
        assert elapsed < 4.0
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_write_refuses_non_finite_number_without_stranding_a_sidecar(tmp_path):
    # json.loads accepts an overflowing literal (1e999 -> inf); the
    # default json.dumps would then emit the bare Infinity token, which
    # CCR's own JSON.parse rejects. Refuse the write instead — and since
    # the check runs before the backup sidecar would be written (it is
    # pure, so hoisting it above --dry-run costs nothing), a refused write
    # here leaves no key-bearing sidecar behind either.
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO app_config VALUES ('default', '{\"Providers\": []}', "
        "'2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()
    before = store_value_snapshot(path)

    def mutate(cfg):
        cfg["Weird"] = 1e999  # json.loads parses this to float("inf")
        return ["added weird key"], []

    with pytest.raises(ccr_store.CcrStoreError, match="non-finite"):
        ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)

    assert store_value_snapshot(path) == before
    sidecars = list(tmp_path.glob("config.sqlite.value-bak-*.json"))
    assert sidecars == []


def test_write_connect_error_is_wrapped_not_raw(tmp_path, monkeypatch):
    # F-04: a connect-time sqlite3.Error must not escape past a handler
    # that only catches CcrStoreError.
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})
    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        if "isolation_level" in kwargs:
            raise sqlite3.OperationalError("simulated connect failure")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(ccr_store.sqlite3, "connect", flaky_connect)

    def mutate(cfg):
        cfg["Providers"] = []
        return ["cleared"], []

    with pytest.raises(ccr_store.CcrStoreError, match="simulated connect failure"):
        ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)


def test_write_refuses_when_store_deleted_between_read_and_connect(tmp_path):
    # F-04: re-checks path.exists() immediately before the write connect,
    # so a store deleted after the read (but before the write) is refused
    # rather than silently re-created empty by a bare connect() call.
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})

    def mutate(cfg):
        path.unlink()
        cfg["Providers"] = []
        return ["cleared"], []

    with pytest.raises(ccr_store.CcrStoreError, match="not found"):
        ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    assert not path.exists()


def test_updated_at_reuses_sqlite_style_shape(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO app_config VALUES ('default', '{\"Providers\": []}', "
        "'2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    def mutate(cfg):
        cfg["Providers"] = [{"name": "p1"}]
        return ["added"], []

    ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    snap = store_value_snapshot(path)
    assert ccr_store._SQLITE_TS_RE.match(snap[1])


def test_updated_at_falls_back_to_iso_ms_on_unrecognised_shape(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO app_config VALUES ('default', '{\"Providers\": []}', 'garbage')"
    )
    con.commit()
    con.close()

    def mutate(cfg):
        cfg["Providers"] = [{"name": "p1"}]
        return ["added"], []

    ccr_store.update_v3_config(path, mutate, backup_dir=tmp_path)
    snap = store_value_snapshot(path)
    assert ccr_store._ISO_MS_RE.match(snap[1])


# ── v3_provider_names / v3_is_empty / v3_api_key_row_count ─────────────────


def test_v3_provider_names_skips_non_dicts_and_nameless():
    cfg = {"Providers": [{"name": "openrouter"}, "not-a-dict", {"no": "name"}, {"name": ""}]}
    assert ccr_store.v3_provider_names(cfg) == ["openrouter"]


def test_v3_provider_names_container_guard_non_list_int():
    # F-01: a non-iterable Providers value must return empty, not raise.
    assert ccr_store.v3_provider_names({"Providers": 5}) == []


def test_v3_provider_names_container_guard_non_list_dict():
    assert ccr_store.v3_provider_names({"Providers": {}}) == []


def test_v3_provider_names_container_guard_absent_key():
    assert ccr_store.v3_provider_names({}) == []


def test_v3_is_empty_true_for_fresh_install_shape():
    assert ccr_store.v3_is_empty({"Providers": [], "APIKEY": ""}) is True


def test_v3_is_empty_false_with_provider():
    assert ccr_store.v3_is_empty({"Providers": [{"name": "openrouter"}]}) is False


def test_v3_is_empty_false_with_api_key():
    assert ccr_store.v3_is_empty({"Providers": [], "APIKEY": "sk-x"}) is False


def test_upgrade_loss_lines_fires_when_all_four_signals_agree():
    lines = ccr_store.upgrade_loss_lines(3, True, {"Providers": []}, 0)
    assert lines
    assert any("No available models" in line for line in lines)
    assert not any("sk-" in line for line in lines)


def test_upgrade_loss_lines_fires_with_unreadable_row_count():
    lines = ccr_store.upgrade_loss_lines(3, True, {"Providers": []}, None)
    assert lines


def test_upgrade_loss_lines_empty_unless_major_is_3():
    assert ccr_store.upgrade_loss_lines(2, True, {"Providers": []}, 0) == []


def test_upgrade_loss_lines_empty_unless_config_json_present():
    assert ccr_store.upgrade_loss_lines(3, False, {"Providers": []}, 0) == []


def test_upgrade_loss_lines_empty_unless_store_is_empty():
    cfg = {"Providers": [{"name": "openrouter"}]}
    assert ccr_store.upgrade_loss_lines(3, True, cfg, 0) == []


def test_upgrade_loss_lines_empty_when_api_key_rows_present():
    assert ccr_store.upgrade_loss_lines(3, True, {"Providers": []}, 2) == []


def test_upgrade_loss_lines_closing_clause_by_rebuilt():
    forward = ccr_store.upgrade_loss_lines(3, True, {"Providers": []}, 0, rebuilt=False)
    done = ccr_store.upgrade_loss_lines(3, True, {"Providers": []}, 0, rebuilt=True)
    assert any("re-run" in line and "quoin router setup" in line for line in forward)
    assert any("just rebuilt" in line for line in done)


def test_upgrade_loss_lines_store_absent_points_at_ccr_start_first():
    # F-03: on the store-absent route, the report must not send the user
    # straight to `quoin router setup` — that command declines on this
    # exact machine and tells them to run `ccr start` first. The report
    # must give the same instruction, not a contradictory one.
    lines = ccr_store.upgrade_loss_lines(
        3, True, {"Providers": []}, 0, store_absent=True
    )
    assert any("ccr start" in line for line in lines)
    joined = "\n".join(lines)
    assert "ccr start" in joined.split("re-run `quoin router setup`")[0]


def test_upgrade_loss_lines_rebuilt_wins_over_store_absent():
    # rebuilt=True is only ever reached from a write that just succeeded —
    # the store cannot be absent there, but the precedence is pinned anyway
    # so a future caller error degrades to the correct clause rather than
    # a nonsensical one.
    lines = ccr_store.upgrade_loss_lines(
        3, True, {"Providers": []}, 0, rebuilt=True, store_absent=True
    )
    assert any("just rebuilt" in line for line in lines)
    assert not any("ccr start" in line for line in lines)


def test_v3_api_key_row_count_none_when_absent(tmp_path):
    assert ccr_store.v3_api_key_row_count(tmp_path / "config.sqlite") is None


def test_v3_api_key_row_count_counts_rows(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO api_keys DEFAULT VALUES")
    con.execute("INSERT INTO api_keys DEFAULT VALUES")
    con.commit()
    con.close()
    assert ccr_store.v3_api_key_row_count(path) == 2


def test_read_v3_config_pinned_timeout_not_default_five_seconds(tmp_path):
    # F-14: the readers must not inherit sqlite3's 5s default busy timeout
    # — they must match the writer's own pinned 2.0s, or a concurrent `ccr`
    # process turns a read-only status check into an ~11s apparent hang.
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})
    holder = sqlite3.connect(str(path), isolation_level=None, timeout=0.5)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        result = ccr_store.read_v3_config(path)
        elapsed = time.monotonic() - started
        assert result.status == "unreadable"
        assert elapsed < 3.0
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_v3_api_key_row_count_pinned_timeout_not_default_five_seconds(tmp_path):
    path = make_v3_store(tmp_path, {"Providers": [{"name": "p1"}]})
    holder = sqlite3.connect(str(path), isolation_level=None, timeout=0.5)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        result = ccr_store.v3_api_key_row_count(path)
        elapsed = time.monotonic() - started
        assert result is None
        assert elapsed < 3.0
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ── merge_built_in_claude_code_route / built_in_route_is_writable ──────────


def test_built_in_route_writable_absent_or_enabled():
    assert ccr_store.built_in_route_is_writable(None) is True
    assert ccr_store.built_in_route_is_writable({"enabled": True}) is True
    assert ccr_store.built_in_route_is_writable({}) is True


def test_built_in_route_not_writable_disabled_or_non_dict():
    assert ccr_store.built_in_route_is_writable({"enabled": False}) is False
    assert ccr_store.built_in_route_is_writable("nope") is False


def test_merge_built_in_route_sets_when_absent():
    cfg = {}
    changes, warnings = ccr_store.merge_built_in_claude_code_route(cfg)
    assert changes
    assert warnings == []
    assert cfg["Router"]["builtInRules"]["claude-code"]["enabled"] is True


def test_merge_built_in_route_already_enabled_is_silent():
    cfg = {"Router": {"builtInRules": {"claude-code": {"enabled": True}}}}
    changes, warnings = ccr_store.merge_built_in_claude_code_route(cfg)
    assert changes == []
    assert warnings == []


def test_merge_built_in_route_disabled_preserved_with_warning():
    cfg = {"Router": {"builtInRules": {"claude-code": {"enabled": False}}}}
    changes, warnings = ccr_store.merge_built_in_claude_code_route(cfg)
    assert changes == []
    assert len(warnings) == 1
    assert cfg["Router"]["builtInRules"]["claude-code"]["enabled"] is False


def test_merge_built_in_route_container_guard_router_not_a_dict():
    cfg = {"Router": []}
    changes, warnings = ccr_store.merge_built_in_claude_code_route(cfg)
    assert changes == []
    assert len(warnings) == 1
    assert cfg["Router"] == []


def test_merge_built_in_route_write_path_via_update_v3_config(tmp_path):
    path = make_v3_store(tmp_path, {"Router": []})

    result = ccr_store.update_v3_config(
        path, ccr_store.merge_built_in_claude_code_route, backup_dir=tmp_path
    )
    assert result.wrote is False
    assert result.warnings


# ── hermeticity guard itself ────────────────────────────────────────────────


def test_hermeticity_guard_fires_on_real_ccr_dir():
    # The guard resolves the real ~/.claude-code-router root once, at
    # session setup (T-02's fixture-mechanics rule), so it cannot be
    # redirected by monkeypatching Path.home() in an individual test.
    # Target the real forbidden root directly: _check() raises before
    # real_sqlite_connect is ever called, so this never touches disk.
    target = Path.home() / ".claude-code-router" / "config.sqlite"
    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        sqlite3.connect(str(target))


def test_hermeticity_guard_normalises_file_uri_before_check():
    target = Path.home() / ".claude-code-router" / "config.sqlite"
    uri = f"{target.as_uri()}?mode=ro"
    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        sqlite3.connect(uri, uri=True)


def test_hermeticity_guard_allows_tmp_path(tmp_path):
    path = tmp_path / "config.sqlite"
    con = sqlite3.connect(str(path))
    con.close()
    assert path.exists()


def test_hermeticity_guard_catches_keyword_only_connect():
    # F-08: a keyword-only sqlite3.connect(database=...) must not bypass
    # the guard just because args is empty.
    target = Path.home() / ".claude-code-router" / "config.sqlite"
    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        sqlite3.connect(database=str(target), uri=True)


def test_hermeticity_guard_catches_mkdir_and_makedirs():
    # F-09: os.mkdir / os.makedirs were unwrapped seams — a test that
    # forgets its home override could create real directories under
    # ~/.claude-code-router before the next (wrapped) seam aborts it. The
    # real directory may already exist on a machine that actually runs
    # CCR (this one does) — the guard's job is to block the write, not to
    # assert anything about pre-existing state.
    target_dir = Path.home() / ".claude-code-router"
    leaf = target_dir / "quoin-hermeticity-guard-canary" / "leaf"
    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.mkdir(str(leaf))
    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.makedirs(str(leaf))
    assert not leaf.parent.exists()


def test_hermeticity_guard_catches_rename_rmdir_truncate(tmp_path):
    # os.rename and os.truncate can move or zero the real store as
    # destructively as deleting it; os.rmdir can remove its directory. All
    # three were unwrapped seams. Probes a canary subpath under the real
    # root rather than config.sqlite or the root directory itself — the
    # guard raises before any real syscall runs, so a regression here
    # leaves a droppable stray instead of damaging a real store.
    canary_dir = Path.home() / ".claude-code-router" / "quoin-hermeticity-guard-canary"
    canary_target = canary_dir / "config.sqlite"
    harmless_src = tmp_path / "harmless.txt"
    harmless_src.write_text("x")

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.rename(str(harmless_src), str(canary_target))
    assert harmless_src.exists()

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.rmdir(str(canary_dir))

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.truncate(str(canary_target), 0)


def test_hermeticity_guard_catches_moving_the_store_away(tmp_path):
    # The onto-direction (writing something *at* the real store) was
    # already covered; moving a real store *away* is exactly as
    # destructive and was reached through os.rename's and os.replace's
    # source argument, which the guard did not check. Probes a canary
    # source path so a regression here leaves a droppable stray, never a
    # real config.sqlite.
    canary_source = (
        Path.home() / ".claude-code-router" / "quoin-hermeticity-guard-canary" / "config.sqlite"
    )
    dest = tmp_path / "moved-away.sqlite"

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.rename(str(canary_source), str(dest))
    assert not dest.exists()

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.replace(str(canary_source), str(dest))
    assert not dest.exists()

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        shutil.move(str(canary_source), str(dest))
    assert not dest.exists()

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        Path(canary_source).rename(dest)
    assert not dest.exists()


def test_hermeticity_guard_catches_symlink_and_link(tmp_path):
    # A symlink or hard link placed at the store's own filename is
    # store-shaping, not metadata-only: detection keys directly on
    # config.json / config.sqlite existing at that path. Probes a
    # canary path under the real root.
    canary_target = (
        Path.home() / ".claude-code-router" / "quoin-hermeticity-guard-canary" / "config.json"
    )
    source = tmp_path / "attacker-controlled.json"
    source.write_text("{}")

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.symlink(str(source), str(canary_target))
    assert not canary_target.exists()

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.link(str(source), str(canary_target))
    assert not canary_target.exists()


def test_hermeticity_guard_catches_hard_link_from_the_real_store(tmp_path):
    # os.link dereferences its source to a real inode — unlike os.symlink,
    # whose target need not exist — so a hard link *from* the real store is
    # a read path into it, not just a write path onto it. Only the
    # destination was checked before; the source argument went unguarded.
    # Probes a canary source path so a regression here leaves a droppable
    # stray, never a real config.sqlite.
    canary_source = (
        Path.home() / ".claude-code-router" / "quoin-hermeticity-guard-canary" / "config.sqlite"
    )
    dest = tmp_path / "hard-linked-from-store.sqlite"

    with pytest.raises(AssertionError, match="quoin test hermeticity"):
        os.link(str(canary_source), str(dest))
    assert not dest.exists()
