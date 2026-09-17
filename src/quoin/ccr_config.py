"""Shared CCR (claude-code-router) config IO, liveness probe, and secret guard.

This module is stdlib-only and must NOT import from installer.py to keep
the router opt-in path decoupled from the install path (D-01).
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import socket
import sys
from typing import Any


class CcrConfigError(Exception):
    """Raised for configuration problems that the user must resolve."""


# ── Path resolution ────────────────────────────────────────────────────────────

def ccr_config_path(home: pathlib.Path | None = None) -> pathlib.Path:
    """Return the default CCR config path (~/.claude-code-router/config.json).

    Pass `home` to override the home directory (for tests).
    """
    base = home if home is not None else pathlib.Path.home()
    return base / ".claude-code-router" / "config.json"


def ccr_store_path(home: pathlib.Path | None = None) -> pathlib.Path:
    """Return the v3 CCR store path (~/.claude-code-router/config.sqlite).

    Path arithmetic only — this helper never reads, opens, or creates the
    database. Pass `home` to override the home directory (for tests).
    """
    base = home if home is not None else pathlib.Path.home()
    return base / ".claude-code-router" / "config.sqlite"


# ── Secret handling ────────────────────────────────────────────────────────────

def read_openrouter_key() -> str:
    """Read OPENROUTER_API_KEY from the environment.

    Returns the key string.
    Raises CcrConfigError if the variable is missing or empty/whitespace.
    NEVER logs or prints the value.
    """
    val = os.environ.get("OPENROUTER_API_KEY", "")
    if not val.strip():
        raise CcrConfigError(
            "OPENROUTER_API_KEY is not set or is empty.\n"
            "Export it before running: export OPENROUTER_API_KEY=sk-or-..."
        )
    return val


def assert_no_secret_in(text: str, key: str) -> None:
    """Raise AssertionError if the secret key appears in text.

    Used by tests to verify no accidental secret leakage.
    """
    if key and key in text:
        raise AssertionError(
            "Secret key found in text — accidental leakage detected."
        )


# ── Backup ─────────────────────────────────────────────────────────────────────

def backup_config(path: pathlib.Path) -> pathlib.Path | None:
    """If path exists, copy it to a timestamped sibling and return the backup path.

    Mirrors installer.py:382-384 backup idiom.
    Returns None if the file does not exist.
    """
    if not path.exists():
        return None
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.parent / f"{path.name}.bak-{ts}"
    shutil.copyfile(path, backup)
    return backup


# ── Load / write ───────────────────────────────────────────────────────────────

def load_config(path: pathlib.Path) -> dict[str, Any]:
    """Load the CCR config JSON.

    - Missing file → returns {}.
    - Malformed JSON → backs up the broken file, warns to stderr, returns {}.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        broken = path.parent / f"{path.name}.broken-{ts}"
        try:
            shutil.copyfile(path, broken)
            print(
                f"quoin: CCR config parse error ({exc}); backed up to {broken} and starting fresh",
                file=sys.stderr,
            )
        except OSError:
            print(
                f"quoin: CCR config parse error ({exc}); starting fresh",
                file=sys.stderr,
            )
        return {}


def write_config(path: pathlib.Path, cfg: dict[str, Any]) -> None:
    """Write cfg to path as indented JSON with a trailing newline.

    Creates parent directories as needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# ── Merge helpers ──────────────────────────────────────────────────────────────

# Router keys quoin owns. Callers that write a different store must apply the
# same ownership rule; kept here so the two paths cannot drift apart.
OWNED_KEYS: frozenset[str] = frozenset({"default", "background", "think", "longContext"})

# The provider prefix a Router value must carry for quoin to consider the key
# its own. `models.build_router_map` writes exactly this prefix.
OPENROUTER_PREFIX = "openrouter,"


def owned_key_is_writable(existing_val: Any) -> bool:
    """Return True if quoin may set an owned Router key over `existing_val`.

    True when the key is absent (None) or already points at quoin's openrouter
    provider. False means the key belongs to something else: preserve it and
    warn — never overwrite.
    """
    if existing_val is None:
        return True
    return isinstance(existing_val, str) and existing_val.startswith(OPENROUTER_PREFIX)


def merge_openrouter_provider(
    cfg: dict[str, Any],
    key: str,
    models: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """Add or update the 'openrouter' provider entry in cfg.

    - Calls cfg.setdefault("Providers", []) before mutating.
    - If cfg["Providers"] is not a list, warns and treats as [].
    - Matches by name == "openrouter"; no duplicates on re-run.
    - Preserves all other providers.
    - The key is ONLY written into cfg; it is never printed/logged.

    Returns (updated_cfg, list_of_change_descriptions).
    """
    cfg.setdefault("Providers", [])
    if not isinstance(cfg["Providers"], list):
        print(
            "quoin: CCR config Providers field is not a list; treating as empty.",
            file=sys.stderr,
        )
        cfg["Providers"] = []

    provider_entry = {
        "name": "openrouter",
        "api_base_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": key,
        "models": list(models),
        "transformer": {"use": ["openrouter"]},
    }

    changes: list[str] = []
    existing = [p for p in cfg["Providers"] if isinstance(p, dict) and p.get("name") == "openrouter"]
    others = [p for p in cfg["Providers"] if not (isinstance(p, dict) and p.get("name") == "openrouter")]

    if existing:
        changes.append("openrouter provider: updated")
    else:
        changes.append("openrouter provider: added")

    cfg["Providers"] = others + [provider_entry]
    return cfg, changes


def merge_router_keys(
    cfg: dict[str, Any],
    mapping: dict[str, str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge owned Router keys into cfg["Router"].

    Owned keys: default, background, think, longContext.
    Per D-05: set a key only if absent OR it currently points at the openrouter
    provider (value starts with "openrouter,"). If it points elsewhere, preserve
    and append a warning.

    - Calls cfg.setdefault("Router", {}) before mutating.
    - NEVER sets NON_INTERACTIVE_MODE or any key outside the four owned.
    - Preserves longContextThreshold, webSearch, image, and all foreign keys.

    Returns (updated_cfg, list_of_changes, list_of_warnings).
    """
    cfg.setdefault("Router", {})
    if not isinstance(cfg["Router"], dict):
        print(
            "quoin: CCR config Router field is not a dict; treating as empty.",
            file=sys.stderr,
        )
        cfg["Router"] = {}

    changes: list[str] = []
    warnings: list[str] = []

    for key, value in mapping.items():
        if key not in OWNED_KEYS:
            continue
        existing_val = cfg["Router"].get(key)
        if owned_key_is_writable(existing_val):
            was_absent = existing_val is None
            cfg["Router"][key] = value
            changes.append(
                f"Router.{key}: set to {value!r}" if was_absent
                else f"Router.{key}: updated to {value!r}"
            )
        else:
            warnings.append(
                f"Router.{key} points at {existing_val!r} (not openrouter); preserved — not overwritten."
            )

    return cfg, changes, warnings


# ── Service liveness ───────────────────────────────────────────────────────────

def probe_service(
    host: str = "127.0.0.1",
    port: int = 3456,
    timeout: float = 0.5,
) -> bool:
    """Return True if the CCR proxy is listening on host:port.

    Primary liveness check: short-timeout TCP connect.
    Returns False on any connection error.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Launch guidance ────────────────────────────────────────────────────────────

V2_LAUNCH_COMMAND = "ccr code"
V3_LAUNCH_COMMAND = "ccr default-claude-code"
V3_PROFILE_NAME = "default-claude-code"

# The note text is a constant, not an inline literal, so the doc-surface
# agreement test and every call site read the same string.
#
# The note must NOT contain the literal substring "ccr code": callers assert
# that substring's absence from the guidance output on the unknown/v3 path,
# and a note carrying it would make those assertions unsatisfiable.
V3_LAUNCH_NOTE = (
    "v3 has no `code` subcommand; the "
    f"{V3_PROFILE_NAME} profile exists but routes nothing until you configure its models in `ccr ui`."
)
UNKNOWN_LAUNCH_NOTE = (
    "your claude-code-router version could not be identified; "
    "launch it yourself rather than following quoin's guidance."
)


def launch_guidance(major: int | None) -> tuple[str, str]:
    """Return (command, note) describing how to launch open-model routing.

    `major` is a detected CCR major: 2, 3, or 0 for unknown. `None` is passed by
    callers that have no detection result in hand and yields the same decline
    branch as 0. The command is the string a doc surface or handler should name;
    the note is a short trailing explanation, empty when the command needs no
    qualification.
    """
    if major == 3:
        return V3_LAUNCH_COMMAND, V3_LAUNCH_NOTE
    if major == 2:
        return V2_LAUNCH_COMMAND, ""
    return "", UNKNOWN_LAUNCH_NOTE


# The clause that qualifies `ccr code` wherever it is rendered. Kept as its
# own constant, reachable only through launch_command_phrase's major == 2
# arm, so a v3 caller can never be handed the v2 command by accident.
V2_LAUNCH_CLAUSE = "auto-starts the proxy; quoin skills work normally"


def launch_command_phrase(major: int | None) -> str:
    """One rendered clause: the command plus the qualification that command needs.

    Callers pass the *effective* detected CCR major, not a raw
    `route.version.major` — on a leftover-config.json machine those two
    differ, and passing the raw one would offer `ccr code` to a v3 user.
    Returns "" for any major this function does not recognise; callers fall
    back to UNKNOWN_LAUNCH_NOTE in that case.
    """
    if major == 2:
        return f"`{V2_LAUNCH_COMMAND}` ({V2_LAUNCH_CLAUSE})"
    if major == 3:
        return f"`{V3_LAUNCH_COMMAND}` — {V3_LAUNCH_NOTE}"
    return ""


# ── Version-boundary notice (v3 upgrade groundwork) ────────────────────────────

# v3 has no equivalent of the v2 `background`, `think`, and `longContext`
# Router keys — those route to distinct models under v2's key-per-tier map,
# and v3 has no matching concept. quoin writes the provider, the model list,
# and the built-in Claude Code route, and deliberately does not translate
# the three lost routes into rules that would only look equivalent. Printed
# unconditionally on every v3 write path, including --dry-run, so a v3 user
# always sees it rather than only on a detected upgrade.
#
# Must NOT contain the literal substring "ccr code" — the same constraint
# V3_LAUNCH_NOTE carries above, for the same reason: several tests assert
# that substring's absence from v3-path output.
V3_ROUTING_GAP_NOTICE = (
    "quoin wrote the OpenRouter provider, your model list, and the built-in "
    "Claude Code route to CCR v3. The v2 `background`, `think`, and "
    "`longContext` routes have no v3 equivalent, so quoin deliberately did "
    "not translate them into rules that would only look equivalent — "
    "configure those by hand in `ccr ui` if you need them."
)
