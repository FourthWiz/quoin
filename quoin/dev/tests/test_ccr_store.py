"""Tests for quoin.ccr_store — the v3 (sqlite-backed) CCR config store."""
from __future__ import annotations

import json
import os
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


def test_v3_is_empty_true_for_fresh_install_shape():
    assert ccr_store.v3_is_empty({"Providers": [], "APIKEY": ""}) is True


def test_v3_is_empty_false_with_provider():
    assert ccr_store.v3_is_empty({"Providers": [{"name": "openrouter"}]}) is False


def test_v3_is_empty_false_with_api_key():
    assert ccr_store.v3_is_empty({"Providers": [], "APIKEY": "sk-x"}) is False


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
