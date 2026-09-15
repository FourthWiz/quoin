"""quoin models — tier→open-model mapping for claude-code-router (opt-in).

This module must NOT import from installer.py to keep the router opt-in path
decoupled from the install path (D-01).  All handlers always return int; never
call cli._abort or sys.exit (Stage-1 return-int handler contract).

Import style (load-bearing for test stubbing):
  import quoin.ccr_config as ccr_config
  ccr_config.probe_service()  ← module-qualified call

This ensures a single monkeypatch.setattr('quoin.ccr_config.probe_service', ...)
intercepts all probe calls in tests — avoids Stage-1's double-stub foot-gun
(router.py used a by-name import, requiring stubs at BOTH quoin.ccr_config.probe_service
AND quoin.router.probe_service in test_router_setup.py:452-453).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any

import quoin.ccr_config as ccr_config
from quoin.router import DEFAULT_MODELS, _verify_ccr, quoin_models_path

# ── Module-level constants ─────────────────────────────────────────────────────

TIER_KEYS = ("haiku", "sonnet", "opus")

# Friendly aliases: short name → OpenRouter slug.
# These are the "blessed" open-model mappings shipped with quoin.
# Derived from DEFAULT_MODELS by tier so the two tables cannot drift apart —
# the alias→tier mapping below (flash→haiku, pro→sonnet, glm→opus) is the
# only literal content; the slugs themselves always track the tier defaults.
FRIENDLY_ALIASES: dict[str, str] = {
    "flash": DEFAULT_MODELS["haiku"],
    "pro": DEFAULT_MODELS["sonnet"],
    "glm": DEFAULT_MODELS["opus"],
}

# Hand-curated slug allowlist — ADVISORY ONLY.
# Expected to drift stale as OpenRouter evolves.
# Unknown-but-plausible slugs (containing '/') are accepted with a warning;
# only obviously-malformed input is rejected.
# Verified September 2026. Additive only — superseded slugs stay so a user
# who pinned one keeps validating with origin "user" and no advisory warning.
KNOWN_SLUGS: frozenset[str] = frozenset(
    {
        "z-ai/glm-5.3-flash",           # blessed haiku default
        "deepseek/deepseek-v4.1-flash",  # blessed sonnet default
        "z-ai/glm-5.3",                  # blessed opus default
        "deepseek/deepseek-v4-flash",    # superseded haiku default
        "deepseek/deepseek-v4-pro",      # superseded sonnet default
        "deepseek/deepseek-v4-flash-0731",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",                  # superseded opus default
        # A small extra set for user convenience.
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3-haiku",
        "anthropic/claude-3-opus",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "meta-llama/llama-3.1-8b-instruct",
        "meta-llama/llama-3.1-70b-instruct",
        "google/gemini-2.0-flash-001",
        "google/gemini-2.5-pro-preview",
    }
)


# ── Slug validation ────────────────────────────────────────────────────────────

class SlugRejected(Exception):
    """Raised by validate_slug when the slug is structurally implausible."""


def _is_plausible_slug(value: str) -> bool:
    """Return True if value looks like a provider/model slug.

    Plausibility rule: exactly one '/', non-empty on both sides, no whitespace.
    """
    if "/" not in value or " " in value or "\t" in value:
        return False
    parts = value.split("/")
    if len(parts) != 2:
        return False
    provider, model = parts
    return bool(provider.strip()) and bool(model.strip())


def validate_slug(value: str) -> tuple[str, list[str]]:
    """Resolve and validate a slug (or friendly alias).

    Resolution order:
      1. FRIENDLY_ALIASES.get(value, value) — map alias to slug.
      2. If resolved slug is in KNOWN_SLUGS → accept, no warnings.
      3. If plausible (provider/model shape) → accept + advisory warning.
      4. Otherwise → raise SlugRejected with a helpful message.

    Returns (resolved_slug, warnings).
    Raises SlugRejected if the slug is structurally implausible.

    Advisory only (D-05 / R-02): never hard-blocks a structurally valid slug.
    """
    resolved = FRIENDLY_ALIASES.get(value, value)

    if resolved in KNOWN_SLUGS:
        return (resolved, [])

    if _is_plausible_slug(resolved):
        warnings = [
            f"unknown slug {resolved!r}; not in the known list — "
            "verify it exists on openrouter.ai"
        ]
        return (resolved, warnings)

    # Implausible: build a helpful rejection message.
    alias_examples = ", ".join(f"{k!r} → {v!r}" for k, v in FRIENDLY_ALIASES.items())
    raise SlugRejected(
        f"quoin: {value!r} is not a recognised slug (no 'provider/model' shape).\n"
        f"Friendly aliases: {alias_examples}.\n"
        "Example slug: 'deepseek/deepseek-v4-pro'\n"
        "Or edit ~/.config/quoin/models.json directly."
    )


# ── Effective-models read / write ─────────────────────────────────────────────

def read_effective_models(home: pathlib.Path | None = None) -> dict[str, str]:
    """Return the effective tier→slug mapping.

    Start from DEFAULT_MODELS (the floor), then partial-merge the user file
    (~/.config/quoin/models.json) over it per-key.

    - Missing file → return a copy of DEFAULT_MODELS.
    - Malformed JSON → warn to stderr, fall back to DEFAULT_MODELS.
    - Unknown tier keys in the user file are ignored (warn once).
    - Only haiku/sonnet/opus are honoured.
    """
    models = dict(DEFAULT_MODELS)  # start from defaults
    path = quoin_models_path(home=home)

    if not path.exists():
        return models

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"quoin: models.json parse error ({exc}); using built-in defaults.",
            file=sys.stderr,
        )
        return models

    if not isinstance(data, dict):
        print(
            "quoin: models.json is not a JSON object; using built-in defaults.",
            file=sys.stderr,
        )
        return models

    stray_keys = []
    for key, value in data.items():
        if key in TIER_KEYS:
            if isinstance(value, str) and value.strip():
                models[key] = value
            else:
                print(
                    f"quoin: models.json key {key!r} has invalid value {value!r}; skipping.",
                    file=sys.stderr,
                )
        else:
            stray_keys.append(key)

    if stray_keys:
        print(
            f"quoin: models.json: unknown tier keys ignored: {stray_keys!r}. "
            "Valid tiers: haiku, sonnet, opus.",
            file=sys.stderr,
        )

    return models


def write_models(models: dict[str, str], home: pathlib.Path | None = None) -> pathlib.Path:
    """Write the full effective mapping (haiku/sonnet/opus) to models.json.

    Slugs ONLY — NEVER writes the OPENROUTER_API_KEY (D-03 / R-03).
    Creates parent directories as needed.
    Returns the path written.
    """
    path = quoin_models_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(models, f, indent=2)
        f.write("\n")
    return path


# ── Router-map builder ────────────────────────────────────────────────────────

def build_router_map(models: dict[str, str]) -> dict[str, str]:
    """Produce the CCR Router mapping from an effective-models dict.

    Mirrors router.ROUTER_MAP construction exactly:
      default    → openrouter,<sonnet-slug>
      background → openrouter,<haiku-slug>
      think      → openrouter,<opus-slug>
      longContext → openrouter,<sonnet-slug>

    The 'openrouter,' prefix is load-bearing — ccr_config.merge_router_keys
    non-clobber predicate keys on value.startswith("openrouter,").
    Bare slugs would break ownership detection.
    """
    return {
        "default": f"openrouter,{models['sonnet']}",
        "background": f"openrouter,{models['haiku']}",
        "think": f"openrouter,{models['opus']}",
        "longContext": f"openrouter,{models['sonnet']}",
    }


# ── In-place provider updater ─────────────────────────────────────────────────

def set_provider_models_inplace(
    cfg: dict[str, Any],
    quoin_models_list: list[str],
) -> bool:
    """Update ONLY the 'models' field on the existing openrouter provider entry.

    Implements union-preserve semantics (D-06 / architecture line 129):
      new models list = (user-added extras) + quoin_models_list
    Extra models the user hand-added to the openrouter provider's models array
    are preserved (prepended before quoin's list).

    Type guards mirror ccr_config.merge_openrouter_provider (ccr_config.py:135-141):
      - cfg.setdefault("Providers", [])
      - if not isinstance(cfg["Providers"], list): warn + set to []
    Both guards are required — a malformed CCR config must not raise.

    Returns True if the provider was found and updated; False otherwise.
    The caller takes the "no CCR setup" branch on False (write models.json only
    + print quoin router setup hint).

    NEVER reads or writes OPENROUTER_API_KEY (D-03 / R-03).
    """
    cfg.setdefault("Providers", [])
    if not isinstance(cfg["Providers"], list):
        print(
            "quoin: CCR config Providers field is not a list; skipping provider update.",
            file=sys.stderr,
        )
        cfg["Providers"] = []
        return False

    existing_entries = [
        p
        for p in cfg["Providers"]
        if isinstance(p, dict) and p.get("name") == "openrouter"
    ]

    if not existing_entries:
        return False

    entry = existing_entries[0]

    # Union-preserve: keep any extra models the user added.
    # Provider 'models' list ordering is cosmetic for CCR routing (Router keys
    # carry the slug); the union puts user-added extras first for visibility.
    existing_models: list[str] = entry.get("models", [])
    if not isinstance(existing_models, list):
        existing_models = []

    quoin_set = set(quoin_models_list)
    extra_user_models = [m for m in existing_models if m not in quoin_set]
    merged = extra_user_models + quoin_models_list

    entry["models"] = list(merged)
    return True


# ── Command handlers ───────────────────────────────────────────────────────────

def _cmd_models_show(args: argparse.Namespace) -> int:
    """quoin models — show tier→slug mapping and active launch mode (read-only).

    Replicates _cmd_router_status three-branch mode logic plus an additive
    install-hint for the "no config + CCR not installed" case.
    Always returns 0 (read-only).
    NEVER prints OPENROUTER_API_KEY.
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    models = read_effective_models(home=home_override)

    print("quoin models:")
    for tier in TIER_KEYS:
        slug = models[tier]
        origin = "default" if slug == DEFAULT_MODELS.get(tier) else "user"
        print(f"  {tier:<8} {slug}  ({origin})")

    live = ccr_config.probe_service()
    cfg_path = ccr_config.ccr_config_path(home=home_override)
    cfg_present = cfg_path.exists()

    if live and cfg_present:
        mode = "open via CCR (proxy running)"
    elif cfg_present and not live:
        mode = "native (CCR configured, proxy down — run `ccr code`)"
    else:
        mode = "native"

    print(f"  active mode: {mode}")

    # Additive install hint — only in the else branch AND CCR is not installed.
    # ccr_installed mirrors router.py:209 exactly.
    if not cfg_present:
        ccr_installed = _verify_ccr() or bool(shutil.which("ccr"))
        if not ccr_installed:
            print(
                "  CCR not set up — run `quoin router setup` to enable open-model routing."
            )

    return 0


def _cmd_models_set(args: argparse.Namespace) -> int:
    """quoin models set TIER MODEL — update one tier's slug (key-safe, D-03).

    Validates tier and slug, writes models.json, then re-authors the CCR Router
    keys via set_provider_models_inplace + merge_router_keys (backup-first).
    Works even when OPENROUTER_API_KEY is unset (R-03).
    Always returns int; never raises (Stage-1 return-int handler contract).
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)
    tier: str = args.tier
    model_input: str = args.model

    # Validate tier.
    if tier not in TIER_KEYS:
        print(
            f"quoin: unknown tier {tier!r}. Valid tiers: {', '.join(TIER_KEYS)}",
            file=sys.stderr,
        )
        return 1

    # Resolve and validate slug (advisory, D-05).
    try:
        slug, warnings = validate_slug(model_input)
    except SlugRejected as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for w in warnings:
        print(f"quoin: warning: {w}", file=sys.stderr)

    # Write models.json.
    models = read_effective_models(home=home_override)
    models[tier] = slug
    models_path = write_models(models, home=home_override)

    # Re-author CCR Router keys if a CCR config exists.
    config_path = ccr_config.ccr_config_path(home=home_override)
    if not config_path.exists():
        print(
            f"quoin: models.json updated ({models_path}).\n"
            "No CCR config found — run `quoin router setup` to apply to CCR."
        )
        return 0

    backup = ccr_config.backup_config(config_path)
    cfg = ccr_config.load_config(config_path)

    ok = set_provider_models_inplace(cfg, list(models.values()))
    if not ok:
        print(
            f"quoin: models.json updated ({models_path}).\n"
            "No openrouter provider in CCR config — run `quoin router setup` to apply."
        )
        return 0

    router_map = build_router_map(models)
    cfg, rk_changes, rk_warnings = ccr_config.merge_router_keys(cfg, router_map)
    ccr_config.write_config(config_path, cfg)

    print(f"quoin models set — changes:")
    print(f"  {tier}: {DEFAULT_MODELS.get(tier, '(none)')} → {slug}")
    print(f"  models.json: {models_path}")
    for change in rk_changes:
        print(f"  + {change}")
    if backup:
        print(f"  Backed up CCR config to: {backup}")
    for warning in rk_warnings:
        print(f"  ⚠ {warning}")
    print("\nTo use open models: ccr code")
    return 0


def _cmd_models_preset(args: argparse.Namespace) -> int:
    """quoin models preset open — apply the full default open mapping in one shot.

    Explicit overwrite of models.json (D-04 — unlike router setup's seed-only-if-absent).
    Re-authors CCR Router keys via set_provider_models_inplace + merge_router_keys.
    No OPENROUTER_API_KEY read/write (D-03 / R-03).
    Always returns int (Stage-1 return-int handler contract).
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)
    name: str = args.name

    if name != "open":
        print(
            f"quoin: unknown preset {name!r}. Only 'open' is supported.",
            file=sys.stderr,
        )
        return 1

    models = dict(DEFAULT_MODELS)
    models_path = write_models(models, home=home_override)

    # Re-author CCR Router keys if a CCR config exists.
    config_path = ccr_config.ccr_config_path(home=home_override)
    if not config_path.exists():
        print(
            f"quoin: models.json updated with open defaults ({models_path}).\n"
            "No CCR config found — run `quoin router setup` to apply to CCR."
        )
        return 0

    backup = ccr_config.backup_config(config_path)
    cfg = ccr_config.load_config(config_path)

    ok = set_provider_models_inplace(cfg, list(models.values()))
    if not ok:
        print(
            f"quoin: models.json updated with open defaults ({models_path}).\n"
            "No openrouter provider in CCR config — run `quoin router setup` to apply."
        )
        return 0

    router_map = build_router_map(models)
    cfg, rk_changes, rk_warnings = ccr_config.merge_router_keys(cfg, router_map)
    ccr_config.write_config(config_path, cfg)

    print("quoin models preset open — applied:")
    for tier in TIER_KEYS:
        print(f"  {tier:<8} {models[tier]}")
    print(f"  models.json: {models_path}")
    for change in rk_changes:
        print(f"  + {change}")
    if backup:
        print(f"  Backed up CCR config to: {backup}")
    for warning in rk_warnings:
        print(f"  ⚠ {warning}")
    print("\nTo use open models: ccr code")
    return 0


def _cmd_models_reset(args: argparse.Namespace) -> int:
    """quoin models reset [--native] — document native launch, backup CCR config.

    Non-destructive (D-04 / R-04): NEVER edits the Router keys, provider block,
    or models.json.  A timestamped backup of the CCR config is created as a
    belt-and-suspenders measure; the config itself is left byte-identical.
    Always returns 0 (Stage-1 return-int handler contract).
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    config_path = ccr_config.ccr_config_path(home=home_override)

    if not config_path.exists():
        print(
            "quoin: No CCR config found — `claude` already runs native Anthropic models."
        )
        return 0

    backup = ccr_config.backup_config(config_path)

    print(f"quoin: Backed up CCR config to: {backup}")
    print(
        "To use native Anthropic models, launch `claude` directly.\n"
        "Your CCR config and model mapping are intact — run `ccr code` to switch back "
        "to open models; `quoin models` shows your current mapping."
    )
    return 0
