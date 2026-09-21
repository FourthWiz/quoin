"""CCR v3 config store IO — reads and writes the sqlite-backed `config.sqlite`.

v3 of claude-code-router replaces the v2 `config.json` file with a sqlite
database at `~/.claude-code-router/config.sqlite`, holding one JSON blob in
`app_config` keyed `'default'`. This module is the sole place that opens
that database.

Deliberately stdlib-only (json, os, pathlib, re, sqlite3, datetime, typing) —
it must NOT import quoin.router, quoin.models, or quoin.installer, keeping the
v3 store mechanism decoupled from CLI dispatch and the install path. It must
never derive the current user's home directory on its own — every entry
point takes an explicit path so tests can point it at a synthetic store.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sqlite3
from typing import Any, Callable, NamedTuple


class CcrStoreError(Exception):
    """Raised for v3 store problems the caller must surface to the user."""


APP_CONFIG_TABLE = "app_config"
APP_CONFIG_KEY = "default"

# The two updated_at shapes this module recognises when a fresh write
# should preserve the store's existing timestamp convention rather than
# imposing its own. Anything else (including an absent prior value) falls
# back to the ISO-with-milliseconds-Z default.
_ISO_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_SQLITE_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class StoreRead(NamedTuple):
    config: dict[str, Any]
    status: str  # "ok" | "missing" | "malformed" | "unreadable"
    detail: str  # one short human-readable clause; never the API key
    raw: str  # the exact value_json column text; "" when no row exists
    updated_at: str  # the exact updated_at column text; "" when no row exists


class WriteResult(NamedTuple):
    backup: pathlib.Path | None  # None on no-op / dry-run / nothing to back up
    changes: list[str]
    warnings: list[str]
    wrote: bool


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def read_v3_config(path: pathlib.Path) -> StoreRead:
    """Read the v3 store's `app_config['default']` blob, read-only.

    Never connects to a missing path (a bare sqlite3.connect() on a missing
    path would create an empty database file, which would be a write from a
    read-only function). The table-absent case is decided structurally, by
    probing sqlite_master, never by matching an exception message.
    """
    if not path.exists():
        return StoreRead({}, "missing", "store file does not exist", "", "")

    try:
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.DatabaseError, OSError) as exc:
        return StoreRead({}, "unreadable", f"could not open store: {exc}", "", "")

    try:
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (APP_CONFIG_TABLE,),
            )
            table_row = cur.fetchone()
        except (sqlite3.DatabaseError, OSError) as exc:
            return StoreRead({}, "unreadable", f"could not read store: {exc}", "", "")

        if table_row is None:
            return StoreRead({}, "ok", "empty store", "", "")

        try:
            cur = con.execute(
                f"SELECT value_json, updated_at FROM {APP_CONFIG_TABLE} WHERE key=?",
                (APP_CONFIG_KEY,),
            )
            row = cur.fetchone()
        except (sqlite3.DatabaseError, OSError) as exc:
            return StoreRead({}, "unreadable", f"could not read store: {exc}", "", "")

        if row is None:
            return StoreRead({}, "ok", "empty store", "", "")

        raw, updated_at = row[0], row[1]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return StoreRead({}, "malformed", f"invalid JSON: {exc}", raw, updated_at)
        if not isinstance(parsed, dict):
            return StoreRead(
                {}, "malformed", "stored value is not a JSON object", raw, updated_at
            )
        return StoreRead(parsed, "ok", "", raw, updated_at)
    finally:
        con.close()


def _write_backup_sidecar(raw: str, backup_dir: pathlib.Path) -> pathlib.Path:
    """Write `raw` to a new, owner-only sidecar under backup_dir and verify it.

    Uses O_CREAT | O_EXCL so an existing sidecar is never clobbered; retries
    with -1 .. -99 appended on a same-second collision.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffixes = [""] + [f"-{i}" for i in range(1, 100)]
    for suffix in suffixes:
        candidate = backup_dir / f"config.sqlite.value-bak-{ts}{suffix}.json"
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise CcrStoreError(
                f"failed to create backup sidecar {candidate}: {exc}"
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise CcrStoreError(
                f"failed to write backup sidecar {candidate}: {exc}"
            ) from exc
        try:
            readback = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise CcrStoreError(
                f"failed to verify backup sidecar {candidate}: {exc}"
            ) from exc
        if readback != raw:
            raise CcrStoreError(
                f"backup sidecar {candidate} failed verification (content mismatch)"
            )
        return candidate
    raise CcrStoreError(
        f"could not create a unique backup sidecar under {backup_dir} after 100 attempts"
    )


def _format_updated_at(previous: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    if previous and _SQLITE_TS_RE.match(previous):
        return now.strftime("%Y-%m-%d %H:%M:%S")
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z"


def update_v3_config(
    path: pathlib.Path,
    mutate: Callable[[dict[str, Any]], tuple[list[str], list[str]]],
    *,
    backup_dir: pathlib.Path,
    dry_run: bool = False,
) -> WriteResult:
    """Read-mutate-write the v3 store's `app_config['default']` blob.

    `mutate(cfg)` mutates cfg in place and returns (changes, warnings). Any
    exception it raises propagates untouched — no backup is written and no
    database is opened for writing before that call returns.
    """
    read = read_v3_config(path)
    if read.status == "missing":
        raise CcrStoreError(f"CCR v3 store not found at {path}.")
    if read.status == "unreadable":
        raise CcrStoreError(f"CCR v3 store at {path} is unreadable: {read.detail}")
    if read.status == "malformed":
        sidecar = _write_backup_sidecar(read.raw, backup_dir)
        raise CcrStoreError(
            f"CCR v3 store at {path} holds malformed data ({read.detail}); "
            f"the existing value was backed up to {sidecar} untouched."
        )

    raw = read.raw
    cfg = read.config
    before_canon = _canonical(cfg)

    changes, warnings = mutate(cfg)

    after_canon = _canonical(cfg)
    if after_canon == before_canon:
        return WriteResult(None, changes, warnings, wrote=False)

    if dry_run:
        return WriteResult(None, changes, warnings, wrote=False)

    if raw:
        backup = _write_backup_sidecar(raw, backup_dir)
    else:
        backup = None

    updated_at = _format_updated_at(read.updated_at)
    try:
        # allow_nan=False: json.loads happily accepts an overflowing
        # literal (1e999 -> inf) and the default json.dumps would then
        # emit the bare token Infinity/NaN, which JavaScript's JSON.parse
        # (what CCR itself uses to read this file back) rejects outright.
        # Refuse the write rather than hand CCR a blob it cannot parse —
        # the sidecar above already preserves the pre-write value.
        payload = json.dumps(cfg, ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise CcrStoreError(
            f"CCR v3 store at {path} would be written with a non-finite "
            f"number ({exc}); refusing to write a value CCR's own JSON "
            "parser could not read back."
        ) from exc

    # Re-checked here rather than trusted from the read above: `mutate` ran
    # in between, and a store deleted in that window must be refused, not
    # silently re-created empty by a bare connect() on a missing path.
    if not path.exists():
        raise CcrStoreError(f"CCR v3 store not found at {path}.")

    try:
        con = sqlite3.connect(path, isolation_level=None, timeout=2.0)
    except sqlite3.Error as exc:
        raise CcrStoreError(
            f"failed to open CCR v3 store at {path}: {exc}"
        ) from exc
    try:
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {APP_CONFIG_TABLE} ("
                "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            cur = con.execute(
                f"UPDATE {APP_CONFIG_TABLE} SET value_json=?, updated_at=? WHERE key=?",
                (payload, updated_at, APP_CONFIG_KEY),
            )
            if cur.rowcount == 0:
                con.execute(
                    f"INSERT INTO {APP_CONFIG_TABLE} (key, value_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    (APP_CONFIG_KEY, payload, updated_at),
                )
            con.execute("COMMIT")
        except sqlite3.Error as exc:
            # The guard is not optional: after a successful COMMIT no
            # transaction is active, and an unguarded ROLLBACK there raises
            # "cannot rollback - no transaction is active". The same is true
            # when BEGIN IMMEDIATE itself fails on a locked store (a live
            # `ccr` process) — con.in_transaction is False in both cases.
            if con.in_transaction:
                con.execute("ROLLBACK")
            detail = str(exc)
            if "lock" in detail.lower():
                raise CcrStoreError(
                    f"failed to write CCR v3 store at {path}: {detail} "
                    "(a running ccr process may be holding the store open)"
                ) from exc
            raise CcrStoreError(
                f"failed to write CCR v3 store at {path}: {detail}"
            ) from exc
    finally:
        con.close()

    return WriteResult(backup, changes, warnings, wrote=True)


def v3_provider_names(cfg: dict[str, Any]) -> list[str]:
    """Names of every dict in cfg['Providers'] that has one, in store order.

    The same predicate `merge_openrouter_provider` matches providers on
    (src/quoin/ccr_config.py) — the emptiness probe and the merge must not
    disagree about what counts as a provider.
    """
    names: list[str] = []
    providers = cfg.get("Providers")
    if not isinstance(providers, list):
        return names
    for entry in providers:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                names.append(name)
    return names


def v3_is_empty(cfg: dict[str, Any]) -> bool:
    """True when the store carries no provider and no API key. Presence-only."""
    if v3_provider_names(cfg):
        return False
    if cfg.get("APIKEY"):
        return False
    if cfg.get("APIKEYS"):
        return False
    return True


def upgrade_loss_lines(
    detected_major: int,
    config_json_present: bool,
    cfg: dict[str, Any],
    api_key_rows: int | None,
    *,
    rebuilt: bool = False,
    store_absent: bool = False,
) -> list[str]:
    """Lines warning that a v2 -> v3 upgrade silently discarded the old config.

    Fires only when all four signals agree: the detected major is 3, a
    leftover config.json is still on disk, the v3 store carries no provider
    and no API key (`v3_is_empty`), and the store's api_keys table is empty
    or unreadable. No content comparison between the two stores is
    performed (FR-2.9) — this is a presence-only heuristic, not a diff.
    Never prints or echoes the key itself (FR-2.12).

    `rebuilt` parameterises the one clause that differs by caller: False
    (the default) is the forward-looking instruction, correct on
    `router status` and on a `--dry-run` `setup` where nothing was in fact
    written; True is correct on a `setup` run that just wrote, where the
    forward-looking instruction would be false by the time it is printed.

    `store_absent` selects a third closing clause for the one machine shape
    where a v3 package is detected but has never created a store at all —
    `router status`'s leftover-config.json-with-no-config.sqlite machine.
    `router setup` declines that exact machine with an instruction to run
    `ccr start` first; sending the user straight to `quoin router setup`
    here would just walk them into that same decline, so this clause gives
    the same instruction `router setup` would.
    """
    if detected_major != 3:
        return []
    if not config_json_present:
        return []
    if not v3_is_empty(cfg):
        return []
    if api_key_rows not in (0, None):
        return []
    if rebuilt:
        closing = "quoin has just rebuilt them from `models.json` and your exported key."
    elif store_absent:
        closing = (
            "run `ccr start` once and stop it again to let CCR create its config "
            "store, then re-run `quoin router setup` with OPENROUTER_API_KEY "
            "exported to rebuild them."
        )
    else:
        closing = (
            "re-run `quoin router setup` with OPENROUTER_API_KEY exported to rebuild them."
        )
    return [
        "quoin: this looks like a claude-code-router v2 -> v3 upgrade that lost your "
        "configuration. CCR v3 does not read the old config.json store, so your "
        "providers, models, and API key were not carried over.",
        "  The old config.json is still on disk but CCR no longer reads it — this is "
        'why CCR reports "No available models".',
        f"  {closing}",
    ]


def v3_api_key_row_count(path: pathlib.Path) -> int | None:
    """Count of rows in the store's api_keys table, or None if unreadable.

    Opens with the same read-only URI form as read_v3_config and carries the
    same narrowed contract: never creates the database file, and on a WAL
    store may materialise -wal / -shm sidecars.
    """
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.DatabaseError, OSError):
        return None
    try:
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
            )
            if cur.fetchone() is None:
                return None
            cur = con.execute("SELECT COUNT(*) FROM api_keys")
            row = cur.fetchone()
            return int(row[0]) if row is not None else 0
        except (sqlite3.DatabaseError, OSError):
            return None
    finally:
        con.close()


def built_in_route_is_writable(existing: Any) -> bool:
    """True when quoin may set builtInRules['claude-code'].enabled = true.

    True when `existing` is absent (None). True when it is a dict whose
    `enabled` is truthy or absent. False for a dict with a falsy `enabled`
    (the user turned it off), or any non-dict.
    """
    if existing is None:
        return True
    if isinstance(existing, dict):
        return bool(existing.get("enabled", True))
    return False


def merge_built_in_claude_code_route(
    cfg: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Enable Router.builtInRules['claude-code'] if writable; else warn.

    Three outcomes: (1) writable and absent-or-not-yet-true -> set true,
    report a change; (2) writable and already true -> no change, no warning
    — this is the normal path on a store CCR itself created, and must be
    silent; (3) not writable -> preserve byte-for-byte, append one warning.
    The container guard applies this same three-way check at both `Router`
    and `builtInRules`: a malformed v3 blob is refused, never overwritten.
    """
    changes: list[str] = []
    warnings: list[str] = []

    router_val = cfg.get("Router")
    if router_val is None:
        router = cfg.setdefault("Router", {})
    elif isinstance(router_val, dict):
        router = router_val
    else:
        warnings.append("Router is not an object; preserved — not modified.")
        return changes, warnings

    rules_val = router.get("builtInRules")
    if rules_val is None:
        rules = router.setdefault("builtInRules", {})
    elif isinstance(rules_val, dict):
        rules = rules_val
    else:
        warnings.append(
            "Router.builtInRules is not an object; preserved — not modified."
        )
        return changes, warnings

    existing = rules.get("claude-code")
    if not built_in_route_is_writable(existing):
        warnings.append(
            "Router.builtInRules['claude-code'] is disabled; preserved — not overwritten."
        )
        return changes, warnings

    if isinstance(existing, dict) and existing.get("enabled") is True:
        return changes, warnings

    if isinstance(existing, dict):
        existing["enabled"] = True
    else:
        rules["claude-code"] = {"enabled": True}
    changes.append("Router.builtInRules['claude-code'].enabled: set to true")
    return changes, warnings
