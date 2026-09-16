"""quoin router — opt-in claude-code-router (CCR) setup and status commands.

This module must NOT import from installer.py (D-01 / D-02).
All handlers always return int; never call cli._abort or sys.exit (D-07).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any, NamedTuple

from quoin.ccr_config import (
    CcrConfigError,
    backup_config,
    ccr_config_path,
    load_config,
    merge_openrouter_provider,
    merge_router_keys,
    probe_service,
    read_openrouter_key,
    write_config,
)

# ── Default model table (editable via ~/.config/quoin/models.json) ─────────────
# Values are OpenRouter model slugs verified September 2026.
DEFAULT_MODELS: dict[str, str] = {
    "haiku": "z-ai/glm-5.3-flash",
    "sonnet": "deepseek/deepseek-v4.1-flash",
    "opus": "z-ai/glm-5.3",
}

# CCR Router key mapping (request-classification keys, not Anthropic tiers — D-05).
# Values use the "provider,model" format CCR expects.
# Default snapshot only — _cmd_router_setup now builds its Router map from the
# effective (models.json-merged) table via models.build_router_map (D-02).
ROUTER_MAP: dict[str, str] = {
    "default": f"openrouter,{DEFAULT_MODELS['sonnet']}",
    "background": f"openrouter,{DEFAULT_MODELS['haiku']}",
    "think": f"openrouter,{DEFAULT_MODELS['opus']}",
    "longContext": f"openrouter,{DEFAULT_MODELS['sonnet']}",
}

# ── CCR version detection (v3 upgrade groundwork; no store read/write here) ────

CCR_KNOWN_MAJOR_MAX = 3          # highest major quoin recognises today
CCR_PINNED_VERSION = "3.1.0"     # exact; no caret or tilde range
CCR_PINNED_MAJOR = int(CCR_PINNED_VERSION.split(".")[0])
CCR_VERSION_CONSTRAINT = f"@{CCR_PINNED_VERSION}"

MIN_NODE_MAJOR = 22


class CcrVersion(NamedTuple):
    major: int                   # 2 | 3 | 0 (0 = unknown, never a guess)
    store: str | None            # "sqlite" | "json" | None
    source: str                  # "store:sqlite" | "store:json" | "npm" | "none"
                                 #   | "npm-capped" | "store:sqlite-npm-capped"


def ccr_store_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    base = home if home is not None else pathlib.Path.home()
    return base / ".claude-code-router"


_npm_query_enabled = True   # False under pytest; see _npm_global_prefix


_PACKAGE_JSON_MAX_BYTES = 1024 * 1024  # 1 MiB; refuse to read an oversized manifest


def _npm_global_prefix() -> str | None:      # seam A
    try:
        if not _npm_query_enabled and os.environ.get("PYTEST_CURRENT_TEST") is not None:
            return None                      # mid-test only; never set in production
        r = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _ccr_package_json(prefix: str) -> pathlib.Path | None:   # seam B
    p = pathlib.Path(prefix) / "lib" / "node_modules" / "@musistudio" / "claude-code-router" / "package.json"
    return p if p.is_file() else None


def _npm_major() -> int | None:              # seam C: the package reader both paths share
    """Major version of the installed npm package, or None if it is not readable."""
    prefix = _npm_global_prefix()
    if prefix is None:
        return None
    pkg = _ccr_package_json(prefix)
    if pkg is None:
        return None
    try:
        # Bounded read instead of stat-then-read: a stat cap alone leaves a
        # window where the file can grow between the size check and the
        # read that follows it. Read as bytes and decode after the cap, so
        # a multibyte manifest is bounded by its actual byte size rather
        # than by decoded character count.
        with open(pkg, "rb") as f:
            raw_bytes = f.read(_PACKAGE_JSON_MAX_BYTES + 1)
        if len(raw_bytes) > _PACKAGE_JSON_MAX_BYTES:
            return None
        data = json.loads(raw_bytes.decode("utf-8"))
        major = int(str(data["version"]).split(".")[0])
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    return major if major > 0 else None


_NPM_MAJOR_UNSET = object()  # sentinel: "caller did not precompute npm_major"


def detect_ccr(
    home: pathlib.Path | None = None,
    npm_major: int | None | object = _NPM_MAJOR_UNSET,
) -> CcrVersion:
    """Classify the installed CCR by on-disk store shape, then by npm package version.

    `npm_major` lets a caller that already spawned `npm prefix -g` for its own
    purposes (e.g. `_cmd_router_setup`'s presence check) pass the result in,
    so this function does not spawn a second one. Leave it unset (the
    default) for the normal case — it then reads npm itself exactly as
    before.
    """
    store_dir = ccr_store_dir(home)
    sqlite = (store_dir / "config.sqlite").exists()
    json_ = (store_dir / "config.json").exists()

    def _resolved_npm_major() -> int | None:
        return _npm_major() if npm_major is _NPM_MAJOR_UNSET else npm_major  # type: ignore[return-value]

    if sqlite:
        major = _resolved_npm_major()
        if major is None:
            # The store signal alone is sufficient when npm is unreadable.
            return CcrVersion(3, "sqlite", "store:sqlite")
        if major > CCR_KNOWN_MAJOR_MAX:
            # The two signals disagree upward: refuse to classify as v3.
            return CcrVersion(0, "sqlite", "store:sqlite-npm-capped")
        return CcrVersion(3, "sqlite", "store:sqlite")

    if json_:
        # No npm read here: the residual rule's antecedent is "the store says
        # v3", which config.json (reached only when config.sqlite is absent)
        # can never satisfy.
        return CcrVersion(2, "json", "store:json")

    major = _resolved_npm_major()
    if major is None:
        return CcrVersion(0, None, "none")
    if major > CCR_KNOWN_MAJOR_MAX:
        return CcrVersion(0, None, "npm-capped")
    return CcrVersion(major, None, "npm")


def quoin_models_path(home: pathlib.Path | None = None) -> pathlib.Path:
    """Return ~/.config/quoin/models.json (agentdesk precedent; outside deploy tree)."""
    base = home if home is not None else pathlib.Path.home()
    return base / ".config" / "quoin" / "models.json"


def seed_models_file_if_absent(
    path: pathlib.Path,
    defaults: dict[str, str],
) -> bool:
    """Write defaults to path ONLY if it does not already exist.

    Never overwrites an existing file (user edits preserved).
    Writes slugs only — the OPENROUTER_API_KEY is never stored here.
    Returns True if the file was seeded, False if it already existed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(defaults, f, indent=2)
        f.write("\n")
    return True


# ── Injectable seams (monkeypatched by tests so CI never shells to npm) ─────────

def _node_present() -> bool:
    """Return True if node or npx is on PATH."""
    return bool(shutil.which("node") or shutil.which("npx"))


def _node_major() -> int | None:
    """Major version of the installed node binary, or None if unreadable."""
    if not shutil.which("node"):
        return None
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out.startswith("v"):
        return None
    try:
        return int(out[1:].split(".")[0])
    except (ValueError, IndexError):
        return None


def _install_ccr() -> int:
    """Run npm install -g @musistudio/claude-code-router. Return exit code."""
    if not shutil.which("npm"):
        return 1
    try:
        result = subprocess.run(
            ["npm", "install", "-g", f"@musistudio/claude-code-router{CCR_VERSION_CONSTRAINT}"],
        )
    except (FileNotFoundError, OSError):
        return 1
    return result.returncode


# ── Command handlers ───────────────────────────────────────────────────────────

_CCR_INSTALLED_MAJORS = (2, 3)   # majors detect_ccr can resolve confidently


def _cmd_router_setup(args: argparse.Namespace) -> int:
    """quoin router setup — install CCR and scaffold the OpenRouter config.

    Always returns int; never calls cli._abort or sys.exit (D-07).
    Implements proc:R-setup with probe-first install (D-06).
    """
    dry_run: bool = getattr(args, "dry_run", False)
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    def _refuse_v3(version: CcrVersion, *, just_installed: bool = False) -> int:
        # No v3 config writer yet — refuse rather than write a v2 store
        # that v3 will not read. Shared by the pre-install and post-install
        # detection sites so a v3 package is never treated differently
        # depending on when it was found.
        if just_installed:
            # An install did run in this case, so the message must not
            # claim nothing changed — it names what landed and how to
            # remove it instead.
            print(
                f"quoin: installed claude-code-router {CCR_PINNED_VERSION} via npm, "
                "but quoin's v3 configuration writer is not available yet."
            )
            print(f"  Detected:  v3 (store: {version.store}, via {version.source})")
            print(
                "  Configure CCR itself until then, or remove the package with:\n"
                "    npm uninstall -g @musistudio/claude-code-router"
            )
        else:
            print(
                "quoin: claude-code-router 3.x is installed; quoin's v3 configuration "
                "writer is not available yet. Nothing was changed."
            )
            print(f"  Detected:  v3 (store: {version.store}, via {version.source})")
            print("  Configure CCR itself until then; nothing here needs undoing.")
        return 0            # int, never SystemExit

    # ── Steps 1-2: detection-driven install decision ──────────────────────────
    # Thread one npm read through detect_ccr and the presence check below
    # instead of letting each call `npm prefix -g` on its own: detect_ccr
    # already reads npm internally whenever a config.sqlite is present (or
    # neither store file is), so precompute it there and skip the read
    # entirely for a lone config.json, which never consults npm.
    store_dir = ccr_store_dir(home_override)
    sqlite_present = (store_dir / "config.sqlite").exists()
    json_only = (store_dir / "config.json").exists() and not sqlite_present
    npm_major = _NPM_MAJOR_UNSET if json_only else _npm_major()
    detected = detect_ccr(home=home_override, npm_major=npm_major)
    if detected.major in _CCR_INSTALLED_MAJORS:
        # A store on disk (config.json / config.sqlite) is quoin's own artifact
        # and outlives the npm package — it is not proof the package is still
        # there. Require a live presence signal alongside the store signal.
        # A version query (`ccr -v` / `ccr version`) is deliberately not that
        # signal: both exit 1 on a healthy v3 install with no providers
        # configured yet, so it would only add noise.
        installed = bool(shutil.which("ccr")) or (
            (_npm_major() if json_only else npm_major) is not None
        )
    else:
        # Unknown major: no store signal to lean on, so fall back to a plain
        # PATH check rather than assuming absent — odd installs are preserved
        # rather than reinstalled over.
        installed = bool(shutil.which("ccr"))

    if installed:
        if detected.major == 3:
            result = _refuse_v3(detected)
            if dry_run:
                print("  --dry-run has no effect here: nothing is written on v3 either way.")
            return result
        print("claude-code-router already installed — skipping npm install.")
    else:
        if not _node_present():
            print(
                "quoin: Node.js is required to install claude-code-router.\n"
                "Install it from https://nodejs.org (LTS recommended), then re-run.",
                file=sys.stderr,
            )
            return 1
        node_major = _node_major()
        if node_major is not None and node_major < MIN_NODE_MAJOR:
            print(
                f"quoin: installing claude-code-router {CCR_PINNED_VERSION} requires "
                f"Node {MIN_NODE_MAJOR} or newer; found Node {node_major}. "
                "quoin will not install an unsupported combination.",
                file=sys.stderr,
            )
            return 1
        if dry_run:
            # Short-circuit before spawning npm at all — a dry run must never
            # install anything, only report what a real run would do.
            print(
                "[dry-run] Would install claude-code-router "
                f"{CCR_PINNED_VERSION} via npm."
            )
            print("[dry-run] No files written.")
            return 0
        print("Installing claude-code-router globally...")
        rc = _install_ccr()
        if rc != 0:
            # The sudo fallback below is pinned to the same constraint as
            # the primary install command; a Node version manager (nvm,
            # fnm) is offered first because it keeps a path open to newer
            # patch and security fixes that this pinned command does not.
            print(
                f"quoin: npm install failed (exit {rc}).\n"
                "Prefer a Node version manager (nvm, fnm) over the command below —\n"
                "it avoids the permissions error without running as root. If you\n"
                "still need it:\n"
                f"  sudo npm install -g @musistudio/claude-code-router{CCR_VERSION_CONSTRAINT}\n"
                "(sudo runs the package's install scripts as root.)",
                file=sys.stderr,
            )
            return rc
        # Confirm presence directly rather than with a version query — see
        # the comment above on why `ccr -v` / `ccr version` are avoided.
        if not (bool(shutil.which("ccr")) or _npm_major() is not None):
            print(
                "quoin: ccr was installed but is not on PATH.\n"
                "Add npm's global bin directory to your PATH, then re-run.\n"
                "Tip: run `npm bin -g` to find the directory.",
                file=sys.stderr,
            )
            return 1
        if CCR_PINNED_MAJOR == 3:
            # The install just placed the pinned package on disk, so its
            # major is known by construction — asking detect_ccr to
            # reclassify would ask a store-shaped detector what quoin
            # itself just installed. A leftover config.json answers "v2"
            # regardless of what the freshly-installed package reports,
            # because config.sqlite is only created once CCR itself first
            # runs. `detected` (the store shape read before this install)
            # is still accurate here, since installing an npm package does
            # not touch the CCR store directory.
            return _refuse_v3(
                CcrVersion(CCR_PINNED_MAJOR, detected.store, "npm"),
                just_installed=True,
            )
        print("claude-code-router installed successfully.")

    # ── Step 3: Read API key ───────────────────────────────────────────────────
    try:
        key = read_openrouter_key()
    except CcrConfigError as exc:
        print(f"quoin: {exc}", file=sys.stderr)
        return 1

    # ── Step 4: Load and merge ────────────────────────────────────────────────
    config_path = ccr_config_path(home=home_override)
    cfg = load_config(config_path)

    # Function-local import — a module-level import here creates a circular
    # ImportError, since models.py back-imports DEFAULT_MODELS from this
    # module at module scope (D-01).
    from quoin.models import build_router_map, read_effective_models

    effective = read_effective_models(home=home_override)
    models_list = list(effective.values())
    cfg, prov_changes = merge_openrouter_provider(cfg, key, models_list)
    cfg, rk_changes, rk_warnings = merge_router_keys(cfg, build_router_map(effective))

    # ── Step 5: Backup (real runs only) + build summary ───────────────────────
    # The backup is deferred until here, past the dry-run return below, so a
    # dry run never writes a backup copy of the user's config to disk.
    backup = None if dry_run else backup_config(config_path)

    summary_lines = ["", "quoin router setup — changes:"]
    for change in prov_changes + rk_changes:
        summary_lines.append(f"  + {change}")
    if backup:
        summary_lines.append(f"  Backed up existing config to: {backup}")
    for warning in rk_warnings:
        summary_lines.append(f"  ⚠ {warning}")
    for tier in ("haiku", "sonnet", "opus"):
        origin = "default" if effective[tier] == DEFAULT_MODELS.get(tier) else "user"
        summary_lines.append(f"  {tier}: {effective[tier]} ({origin})")
    summary_lines.append(f"  Config path: {config_path}")
    summary = "\n".join(summary_lines)

    if dry_run:
        print(summary)
        print("\n[dry-run] No files written.")
        return 0

    # ── Step 6: Write config ───────────────────────────────────────────────────
    write_config(config_path, cfg)

    # ── Step 7: Seed models file if absent ────────────────────────────────────
    models_path = quoin_models_path(home=home_override)
    seeded = seed_models_file_if_absent(models_path, DEFAULT_MODELS)

    print(summary)
    if seeded:
        print(f"  Seeded model defaults to: {models_path}")
        print(
            "  Note: the haiku and opus defaults now route through Z.ai "
            "(haiku was previously DeepSeek) — edit models.json to pin a "
            "different provider."
        )
    else:
        print(f"  Model defaults file already exists (user edits preserved): {models_path}")

    print(
        "\nTo use open models:  ccr code    (auto-starts the proxy; quoin skills work normally)"
        "\nTo use native models: claude"
        "\n\nSanity-check: inside a `ccr code` session, type /help — the quoin skill list should resolve."
    )
    return 0


def _cmd_router_status(args: argparse.Namespace) -> int:
    """quoin router status — read-only report. Always returns 0 (D-07).

    Implements proc:R-status: derives active launch mode from liveness + config,
    never from config alone.
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    # A version query would add nothing `shutil.which` doesn't already tell
    # us, and both `ccr -v` and `ccr version` are destructive on a v3 store
    # (see the setup path above) — check PATH directly instead.
    installed = bool(shutil.which("ccr"))
    config_path = ccr_config_path(home=home_override)
    cfg_present = config_path.exists()
    live = probe_service()
    key_set = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

    if live and cfg_present:
        mode = "open via CCR (proxy running)"
    elif cfg_present and not live:
        mode = "native (CCR configured but proxy not running — run `ccr code` to start)"
    else:
        mode = "native"

    print("quoin router status:")
    print(f"  CCR installed:  {'yes' if installed else 'no'}")
    print(f"  Config present: {'yes' if cfg_present else 'no'}  ({config_path})")
    print(f"  Proxy running:  {'yes' if live else 'no'}  (127.0.0.1:3456)")
    print(f"  API key set:    {'yes' if key_set else 'no'}  (OPENROUTER_API_KEY)")
    print(f"  Active mode:    {mode}")
    return 0
