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
from typing import Any, NamedTuple, cast

import quoin.ccr_store as ccr_store
from quoin.ccr_config import (
    V3_ROUTING_GAP_NOTICE,
    CcrConfigError,
    backup_config,
    ccr_config_path,
    ccr_store_path,
    launch_command_phrase,
    launch_guidance,  # by-name here; a test must also stub quoin.router.launch_guidance,
    # not only quoin.ccr_config.launch_guidance — models.py uses the module-qualified
    # form instead, so a single-location stub proves only one of the two call sites.
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

# The only npm major a `config.json` store is ever a live artifact for. Kept
# separate from CCR_KNOWN_MAJOR_MAX (which tracks what quoin can *recognise*,
# not what a v2-shaped store can *serve*) so bumping the recognised ceiling
# — the edit that happens the moment quoin learns about a new CCR major —
# can never silently change which packages the stale-config.json guard lets
# through.
_CONFIG_JSON_STORE_MAJOR = 2

MIN_NODE_MAJOR = 22


class CcrVersion(NamedTuple):
    major: int                   # a real npm major when one was read directly
                                 #   (so 4, 5, ... are possible, not just 2 | 3);
                                 #   0 only when detection declined to classify,
                                 #   never a guess
    store: str | None            # "sqlite" | "json" | None
    source: str                  # "store:sqlite" | "store:json" | "npm" | "none"
                                 #   | "npm-capped" | "store:sqlite-npm-capped"


def ccr_store_dir(home: pathlib.Path | None = None) -> pathlib.Path:
    base = home if home is not None else pathlib.Path.home()
    return base / ".claude-code-router"


_npm_query_enabled = True   # False under pytest; see _npm_global_prefix


_PACKAGE_JSON_MAX_BYTES = 1024 * 1024  # 1 MiB; refuse to read an oversized manifest


def _scrubbed_env() -> dict[str, str]:
    """A copy of the process environment with OPENROUTER_API_KEY removed.

    Every subprocess this module spawns inherits the environment by
    default, which would otherwise hand the key to a probe or install that
    never needs it. Used for all three spawn sites (the npm and node
    version probes, and the npm install itself) — the key's own
    file-permission exposure is unrelated and unchanged.
    """
    return {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}


def _fast_npm_prefix_guess(npm_path: str) -> str | None:
    """Best-effort global npm prefix guessed from npm's own binary location.

    npm ships at <prefix>/bin/npm on the standard POSIX layout and directly
    inside <prefix> as npm.cmd on Windows; guessing from that avoids
    spawning `npm prefix -g` (measured 0.6-1.2s) on the common case.

    Deliberately does NOT resolve through symlinks: on most real installs
    (Homebrew, nvm) `<prefix>/bin/npm` is itself a symlink to npm's own
    `lib/node_modules/npm/bin/npm-cli.js`, and fully resolving it lands
    inside npm's own package rather than at the prefix — the convention
    this guess relies on is where the symlink sits, not where it points.
    A hint only, never a source of truth — a custom npm `prefix` config or
    a nonstandard layout can still make the guess wrong, so the caller
    must always fall back to the real spawn when it doesn't pan out.
    """
    try:
        candidate = pathlib.Path(npm_path)
        if not candidate.is_absolute():
            candidate = candidate.absolute()
    except OSError:
        return None
    parent = candidate.parent
    if parent.name == "bin":
        return str(parent.parent)
    return str(parent)


def _npmrc_sets_prefix(npmrc: pathlib.Path) -> bool:
    """True when `npmrc` holds a `prefix = ...` line (any surrounding blanks)."""
    try:
        with open(npmrc, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", ";")):
                    continue
                key, sep, _ = stripped.partition("=")
                if sep and key.strip() == "prefix":
                    return True
    except OSError:
        return False
    return False


def _npm_prefix_override_in_play(guess: str) -> bool:
    """True when something could steer npm's global prefix away from `guess`.

    npm resolves its prefix from (highest to lowest precedence) a
    `--prefix` flag, the `npm_config_prefix` / `NPM_CONFIG_PREFIX`
    environment variables, a `prefix=` line in `$HOME/.npmrc`, one in the
    guessed prefix's own `etc/npmrc`, and only then the convention this
    fast path's guess relies on — where npm's own binary happens to sit.
    `npm config set prefix ...` (npm's documented remedy for a global
    install permission error) writes exactly one of the `.npmrc` forms
    above, which is the machine shape this check exists to catch: a
    leftover CCR package sitting at the binary-adjacent prefix while npm
    itself has been pointed somewhere else. Any override present makes the
    guess untrustworthy, so the caller falls through to the real spawn
    rather than trying to reproduce npm's full resolution order itself.
    """
    if os.environ.get("npm_config_prefix") or os.environ.get("NPM_CONFIG_PREFIX"):
        return True
    for npmrc in (
        pathlib.Path.home() / ".npmrc",
        pathlib.Path(guess) / "etc" / "npmrc",
    ):
        if _npmrc_sets_prefix(npmrc):
            return True
    return False


def _npm_global_prefix() -> str | None:      # seam A
    try:
        if not _npm_query_enabled and os.environ.get("PYTEST_CURRENT_TEST") is not None:
            return None                      # mid-test only; never set in production
        # Resolve npm's path once and spawn that path — argv[0] "npm" is
        # never looked up against Windows' PATHEXT without a shell, so a
        # bare subprocess.run(["npm", ...]) raises FileNotFoundError on
        # every Windows machine even when npm.cmd is on PATH. Spawning the
        # shutil.which-resolved path also closes the gap between checking
        # and spawning (the binary can't be replaced/removed between the
        # two calls because there is only one).
        npm_path = shutil.which("npm")
        if npm_path is None:
            return None
        # Fast path: skip the spawn below when the guess from npm's own
        # binary location already resolves to a real, installed CCR
        # package. Gated strictly on the absence of PYTEST_CURRENT_TEST
        # (never merely on _npm_query_enabled) — a test that deliberately
        # re-enables the flag to exercise the real spawn is asking for the
        # spawn, not for whatever CCR happens to be installed on the
        # machine running the suite, and this guess reads real disk state
        # the same way no_real_ccr_store's rationale warns against.
        if os.environ.get("PYTEST_CURRENT_TEST") is None:
            guess = _fast_npm_prefix_guess(npm_path)
            if (
                guess is not None
                and _ccr_package_json(guess) is not None
                and not _npm_prefix_override_in_play(guess)
            ):
                return guess
        # 10s, not 5s: now that npm_path resolves to the real npm.cmd on
        # Windows, this call actually reaches it, and npm.cmd plus
        # antivirus/Defender scanning routinely pushes a cold-start
        # `npm prefix -g` past 2s there; a timeout here silently degrades
        # to "not installed" (see the `_npm_global_prefix` docstring on
        # `_npm_major`'s caller side).
        r = subprocess.run(
            [npm_path, "prefix", "-g"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_scrubbed_env(),
        )
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
        # Slice before int(): an unbounded digit run reaching int() can hang
        # (quadratic on Python < 3.11, which has no conversion-length cap).
        # No real semver major is anywhere near 16 digits.
        major = int(str(data["version"]).split(".")[0][:16])
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


def _sqlite_file_is_empty(path: pathlib.Path) -> bool:
    """True when `config.sqlite` is a zero-byte file.

    sqlite treats a zero-byte file as a valid empty database — the exact
    shape a fresh, not-yet-written v3 store has the moment `ccr start`
    first opens it, before writing anything. That makes a zero-byte file
    genuinely ambiguous by content alone: it reads identically whether CCR
    really created it or it is unrelated stray bytes. `detect_ccr` only
    treats this as a reason to distrust the store's presence when something
    else (a readable npm major that disagrees) contradicts it; on its own,
    an empty file is left exactly where a real, freshly-initialised store
    would be.
    """
    try:
        return path.stat().st_size == 0
    except OSError:
        return True  # gone entirely carries no more evidence than empty


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
    sqlite_path = store_dir / "config.sqlite"
    sqlite = sqlite_path.exists()
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
        if major != 3 and _sqlite_file_is_empty(sqlite_path):
            # npm gives a definite, in-range answer that is not v3, and the
            # store carries nothing to weigh against it — fall through
            # exactly as if config.sqlite did not exist, rather than
            # letting an empty file outrank a real, disagreeing signal.
            sqlite = False
        else:
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


class CcrRoute(NamedTuple):
    version: CcrVersion
    route: str                   # "v2" | "v3" | "v3-store-absent" | "unknown"
    npm_major: int | None


def dispatch_store(detected: CcrVersion, npm_major: int | None) -> str:
    """Which store shape quoin should serve, given a detection result.

    Pure. The order of the tests is load-bearing: a capped detection carries
    major 0, so the capped tests must come before any test on the major.
    """
    if detected.source.endswith("npm-capped"):
        return "unknown"
    if npm_major is not None and npm_major > CCR_KNOWN_MAJOR_MAX:
        return "unknown"
    if detected.store == "sqlite":
        return "v3"
    if (
        detected.store == "json"
        and npm_major is not None
        and npm_major != _CONFIG_JSON_STORE_MAJOR
    ):
        # config.json is a live store only for major 2; a newer package beside
        # one means the store CCR actually reads has not been created yet.
        return "v3-store-absent"
    if detected.major > _CONFIG_JSON_STORE_MAJOR:
        return "v3-store-absent"
    return "v2"


def resolve_ccr_route(
    home: pathlib.Path | None = None,
    *,
    assume_installed_major: int | None = None,
) -> CcrRoute:
    """Detect CCR once, then decide which store shape serves it.

    The sole dispatch authority: every handler that needs to know which CCR
    store it is talking to calls this rather than re-deriving the rule.
    Spawns `npm prefix -g` at most once per call — detect_ccr already reads
    npm internally whenever a config.sqlite is present (or neither store file
    is), so the read is precomputed there; the lone config.json case defers
    its read to the stale-store question below, which needs the same value.

    `assume_installed_major` is for the one caller that has just installed a
    known version itself. On a machine where npm is unreadable and the only
    store signal is absent (or a leftover config.json), detection has no
    signal at all and falls through to v2 — correct before an install, wrong
    immediately after one quoin performed. A readable npm is a real signal
    and still wins, so the assumption is one of last resort.
    """
    store_dir = ccr_store_dir(home)
    # Deliberately bare existence, not detect_ccr's own empty-file question:
    # this only decides whether the npm read below can be deferred to the
    # stale-store question further down. Either way the same npm_major value
    # ends up threaded into dispatch_store, so a zero-byte config.sqlite that
    # detect_ccr falls through on (npm disagreeing) still resolves through
    # the correct branch there — it costs one eager npm read instead of a
    # deferred one, not a wrong answer.
    sqlite_present = (store_dir / "config.sqlite").exists()
    json_only = (store_dir / "config.json").exists() and not sqlite_present
    npm_major = _NPM_MAJOR_UNSET if json_only else _npm_major()
    detected = detect_ccr(home=home, npm_major=npm_major)
    # When json_only is False, npm_major was already resolved to int | None
    # above (never the sentinel) — cast narrows the seam's static type to
    # match, rather than leaving it widened to the sentinel's type.
    json_npm_major = _npm_major() if json_only else cast("int | None", npm_major)
    if (
        assume_installed_major is not None
        and detected.source in ("none", "store:json")
        and json_npm_major is None
    ):
        # The substitution is on the detected version, not on the npm
        # reading: with major 0 and no store, overriding npm alone would
        # still leave every rule in dispatch_store missing.
        detected = CcrVersion(assume_installed_major, detected.store, "just-installed")
    return CcrRoute(detected, dispatch_store(detected, json_npm_major), json_npm_major)


def _decline_unknown(version: CcrVersion, *, lead: str | None = None) -> None:
    """Report that the installed CCR major is one quoin does not recognise.

    Prints and returns None — the return code belongs to the caller, because
    `router setup` and the `models` handlers do not agree on it. `lead`, when
    given, is printed first so a caller can report work it already did.
    """
    if lead:
        print(lead)
    # Derived from the version we were handed, not hardcoded — this path is
    # reached with major 4, 5, and the capped sentinel 0 alike, and each must
    # say what it detected.
    if version.major == 0:
        headline = "claude-code-router (a version newer than quoin recognises) is installed"
        detected_label = "unrecognised"
    else:
        headline = f"claude-code-router {version.major}.x is installed"
        detected_label = f"v{version.major}"
    print(
        f"quoin: {headline}; quoin recognises claude-code-router up to major "
        f"{CCR_KNOWN_MAJOR_MAX} and declined to write rather than guess at a "
        "configuration shape it does not know."
    )
    print(f"  Detected:  {detected_label} (store: {version.store or 'none'}, via {version.source})")
    print("  This is caution, not a failure — your CCR install is untouched.")
    print(
        "  To move to the version quoin supports:  npm install -g "
        f"@musistudio/claude-code-router@{CCR_PINNED_VERSION}"
    )
    print("quoin declined to write to CCR; nothing in your CCR configuration was changed.")


def _decline_store_absent(
    version: CcrVersion,
    store_dir: pathlib.Path,
    *,
    lead: str | None = None,
) -> None:
    """Report that CCR v3 is installed but has not created its store yet.

    Reads exactly one field off `version` — `major` — so no detection source
    string ever reaches this path's output; the detected version is not what
    this message is about, and no `Detected:` line is printed.
    """
    if lead:
        print(lead)
    print(
        f"quoin: CCR v{version.major} is installed, but it has not created its "
        "config store yet, so there is nothing for quoin to merge into."
    )
    print(f"  Store directory: {store_dir}")
    print(
        "  Run `ccr start` once and stop it again to let CCR create the store, "
        "then re-run `quoin router setup`."
    )
    print(
        "  Do not run `ccr -v` or `ccr version` first: on v3 a version query "
        "triggers migration and removes config.json from disk. The old bytes "
        "survive only inside the store's legacy_storage_backups table, which "
        "quoin cannot read."
    )
    print("quoin declined to write to CCR; nothing in your CCR configuration was changed.")


def _effective_version(route: CcrRoute) -> CcrVersion:
    """The version to *render*, never the one to dispatch on.

    detect_ccr's config.json arm reports major 2 without reading npm at all,
    so on a machine with a leftover config.json beside a newer package the
    raw detected major is a false claim. Where npm was readable and disagrees,
    the npm reading is the honest thing to show. Identity on every other cell,
    including every genuine v2 machine.
    """
    npm_major = route.npm_major
    if (
        route.version.source == "store:json"
        and npm_major is not None
        and npm_major != _CONFIG_JSON_STORE_MAJOR
    ):
        return CcrVersion(npm_major, "json", "npm")
    return route.version


def _dry_run_tail(args: argparse.Namespace) -> None:
    """The one line a decline adds under --dry-run, printed by the caller.

    Lives at the dispatch site rather than inside the decline helpers: the
    helpers take no args, and the `models` call sites have no --dry-run flag.
    """
    if getattr(args, "dry_run", False):
        print("  --dry-run has no effect here: nothing is written on v3 either way.")


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
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_scrubbed_env(),
        )
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
    # The npm-presence guard lives here rather than in _cmd_router_setup so
    # it protects every caller of this function, not just the setup
    # handler's own npm-less-Node case — a test or a future caller that
    # stubs this function wholesale bypasses it either way, which is why
    # test_detection.py exercises it directly.
    npm_path = shutil.which("npm")
    if npm_path is None:
        return 1
    try:
        result = subprocess.run(
            [npm_path, "install", "-g", f"@musistudio/claude-code-router{CCR_VERSION_CONSTRAINT}"],
            env=_scrubbed_env(),
        )
    except (FileNotFoundError, OSError):
        return 1
    return result.returncode


# ── Command handlers ───────────────────────────────────────────────────────────

_CCR_INSTALLED_MAJORS = (2, 3)   # majors detect_ccr can resolve confidently


def _setup_v3(args: argparse.Namespace, route: CcrRoute, store_dir: pathlib.Path) -> int:
    """Merge the OpenRouter provider and the built-in route into a v3 store."""
    dry_run: bool = getattr(args, "dry_run", False)
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)
    store_path = ccr_store_path(home=home_override)

    try:
        key = read_openrouter_key()
    except CcrConfigError as exc:
        print(f"quoin: {exc}", file=sys.stderr)
        return 1

    # Function-local import — a module-level import here creates a circular
    # ImportError, since models.py back-imports DEFAULT_MODELS from this
    # module at module scope (D-01).
    from quoin.models import read_effective_models

    effective = read_effective_models(home=home_override)
    models_list = list(effective.values())

    # Read the upgrade-loss signals from the store as it stands *before* the
    # write. After a successful write the provider and the key are present by
    # definition, so a post-write read can never report the loss these three
    # exist to detect. Do not collapse them into the write's own read.
    pre = ccr_store.read_v3_config(store_path)
    pre_rows = ccr_store.v3_api_key_row_count(store_path)
    pre_json = ccr_config_path(home=home_override).exists()

    def mutate(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
        cfg, prov = merge_openrouter_provider(cfg, key, models_list)
        br_changes, br_warnings = ccr_store.merge_built_in_claude_code_route(cfg)
        return prov + br_changes, br_warnings

    try:
        result = ccr_store.update_v3_config(
            store_path, mutate, backup_dir=store_dir, dry_run=dry_run
        )
    except ccr_store.CcrStoreError as exc:
        print(f"quoin: {exc}", file=sys.stderr)
        return 1

    summary_lines = ["", "quoin router setup — changes:"]
    for change in result.changes:
        summary_lines.append(f"  + {change}")
    if result.backup:
        summary_lines.append(f"  Backed up existing store value to: {result.backup}")
    elif result.wrote:
        # Said rather than left silent: the rollback recipe promises a backup
        # path on every write, and "there was nothing to back up" is the only
        # other way a write can end.
        summary_lines.append(
            "  no backup needed — the store had no existing configuration blob"
        )
    for warning in result.warnings:
        summary_lines.append(f"  ⚠ {warning}")
    for tier in ("haiku", "sonnet", "opus"):
        origin = "default" if effective[tier] == DEFAULT_MODELS.get(tier) else "user"
        summary_lines.append(f"  {tier}: {effective[tier]} ({origin})")
    summary_lines.append(f"  Config store: {store_path}")
    print("\n".join(summary_lines))

    if not dry_run:
        models_path = quoin_models_path(home=home_override)
        if seed_models_file_if_absent(models_path, DEFAULT_MODELS):
            print(f"  Seeded model defaults to: {models_path}")
            print(
                "  Note: the haiku and opus defaults now route through Z.ai "
                "(haiku was previously DeepSeek) — edit models.json to pin a "
                "different provider."
            )
        else:
            print(f"  Model defaults file already exists (user edits preserved): {models_path}")

        major = _effective_version(route).major
        phrase = launch_command_phrase(major)
        if phrase:
            open_models_line = f"\nTo use open models:  {phrase}"
        else:
            _cmd, note = launch_guidance(major)
            open_models_line = f"\n{note}"
        print(
            open_models_line +
            "\nTo use native models: claude"
            "\n\nSanity-check: inside an open-model session, type /help — the quoin skill list should resolve."
        )

    print("")
    print(V3_ROUTING_GAP_NOTICE)

    # detected_major is a condition input rather than a rendered value, and
    # this path is reached only on a store:sqlite detection, where the
    # effective version is the identity — so the raw major is used here.
    for line in ccr_store.upgrade_loss_lines(
        route.version.major, pre_json, pre.config, pre_rows, rebuilt=result.wrote
    ):
        print(line)

    if dry_run:
        print("\n[dry-run] No files written.")
    return 0


def _dispatch_installed(
    args: argparse.Namespace,
    route: CcrRoute,
    store_dir: pathlib.Path,
) -> int | None:
    """The three decline/write arms. None means no arm matched."""
    ev = _effective_version(route)
    if route.route == "unknown":
        _decline_unknown(ev)
        _dry_run_tail(args)
        return 2
    if route.route == "v3-store-absent":
        _decline_store_absent(ev, store_dir)
        _dry_run_tail(args)
        return 2
    if route.route == "v3":
        return _setup_v3(args, route, store_dir)
    return None


def _cmd_router_setup(args: argparse.Namespace) -> int:
    """quoin router setup — install CCR and scaffold the OpenRouter config.

    Always returns int; never calls cli._abort or sys.exit (D-07).
    Implements proc:R-setup with probe-first install (D-06).
    """
    dry_run: bool = getattr(args, "dry_run", False)
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    # ── Steps 1-2: detection-driven install decision ──────────────────────────
    # One resolution for the whole handler: which CCR is installed, and which
    # store shape serves it. store_dir survives separately because the
    # dispatch helpers below need it and the route does not carry it.
    store_dir = ccr_store_dir(home_override)
    route = resolve_ccr_route(home_override)
    detected = route.version
    json_npm_major = route.npm_major

    # A capped source (`npm-capped` / `store:sqlite-npm-capped`) is positive
    # proof a newer package is already on disk — treat it as installed so
    # the branch below never reinstalls the (older) pinned version over it.
    if detected.major in _CCR_INSTALLED_MAJORS or detected.source.endswith("npm-capped"):
        # A store on disk (config.json / config.sqlite) is quoin's own artifact
        # and outlives the npm package — it is not proof the package is still
        # there. Require a live presence signal alongside the store signal.
        # A version query (`ccr -v` / `ccr version`) is deliberately not that
        # signal: both exit 1 on a healthy v3 install with no providers
        # configured yet, so it would only add noise.
        installed = bool(shutil.which("ccr")) or (json_npm_major is not None)
    else:
        # Unknown major: no store signal to lean on, so fall back to a plain
        # PATH check rather than assuming absent — odd installs are preserved
        # rather than reinstalled over.
        installed = bool(shutil.which("ccr"))

    if installed:
        disp = _dispatch_installed(args, route, store_dir)
        if disp is not None:
            return disp
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
        # The install changed what quoin knows, so re-resolve rather than
        # keep dispatching on the pre-install reading. The keyword says the
        # one thing detection cannot see on a machine whose npm is
        # unreadable: quoin just put this major there itself. Resolved once,
        # here, above the presence check below — that check only needs to
        # know whether npm's major was readable, which this same call
        # already answers via route.npm_major, so the two no longer spawn
        # npm separately.
        route = resolve_ccr_route(home_override, assume_installed_major=CCR_PINNED_MAJOR)
        # Confirm presence directly rather than with a version query — see
        # the comment above on why `ccr -v` / `ccr version` are avoided.
        if not (bool(shutil.which("ccr")) or route.npm_major is not None):
            print(
                "quoin: ccr was installed but is not on PATH.\n"
                "Add npm's global bin directory to your PATH, then re-run.\n"
                "Tip: run `npm bin -g` to find the directory.",
                file=sys.stderr,
            )
            return 1
        disp = _dispatch_installed(args, route, store_dir)
        if disp is not None:
            return disp
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

    # The rendered major, not the dispatched one: the clause that qualifies
    # `ccr code` is reachable only through launch_command_phrase's v2 arm, so
    # a v3 machine can never be handed the v2 command here.
    major = _effective_version(route).major
    phrase = launch_command_phrase(major)
    if phrase:
        open_models_line = f"\nTo use open models:  {phrase}"
    else:
        _cmd, note = launch_guidance(major)
        open_models_line = f"\n{note}"
    print(
        open_models_line +
        "\nTo use native models: claude"
        "\n\nSanity-check: inside an open-model session, type /help — the quoin skill list should resolve."
    )
    return 0


def _status_version_line(version: CcrVersion) -> str:
    """The `CCR version:` value, keyed on the detection source.

    The two degenerate renderings are separated by source and never by the
    major: a capped reading and a no-signal reading both carry major 0, so a
    major test would match both and order would silently decide which one
    won. "not detected" is `source == "none"` and nothing else; a capped
    source is the only thing that reads "unrecognised".
    """
    if version.source == "none":
        # No suffix: a "(via …)" clause here would contradict the
        # `CCR installed: no` line printed directly above it.
        return "not detected"
    if version.source.endswith("npm-capped"):
        return f"unrecognised (via {version.source})"
    return f"v{version.major} (via {version.source})"


def _cmd_router_status(args: argparse.Namespace) -> int:
    """quoin router status — read-only report. Always returns 0 (D-07).

    Implements proc:R-status: derives active launch mode from liveness + config,
    never from config alone.
    """
    home_override: pathlib.Path | None = getattr(args, "_home_override", None)

    # One resolution for the whole handler, exactly as the setup path does it.
    route = resolve_ccr_route(home_override)
    # The version every line below renders *and* every predicate below keys
    # on. A leftover config.json beside a newer package detects as major 2,
    # and reporting that file as a present, active config would be a false
    # report about a machine whose CCR cannot read it.
    version = _effective_version(route)
    config_path = ccr_config_path(home=home_override)
    config_json_present = config_path.exists()
    store_path = ccr_store_path(home=home_override)

    # A version query would add nothing `shutil.which` doesn't already tell
    # us, and both `ccr -v` and `ccr version` are destructive on a v3 store
    # (see the setup path above) — check PATH directly instead.
    on_path = bool(shutil.which("ccr"))
    live = probe_service()
    key_set = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())

    v3_store = route.route == "v3"
    populated: bool | None = None
    populated_detail = ""
    store_cfg: dict[str, Any] = {}
    store_read_ok = False
    if v3_store:
        # read_v3_config never raises: a malformed, locked or unreadable
        # store comes back as a status string, which becomes the "unknown"
        # rendering below rather than a traceback out of a read-only report.
        read = ccr_store.read_v3_config(store_path)
        store_cfg = read.config
        store_read_ok = read.status == "ok"
        if store_read_ok:
            populated = not ccr_store.v3_is_empty(read.config)
        else:
            populated_detail = read.detail
        configured = populated is True
    else:
        # config.json is a live store only where the effective major still
        # reads it. On a v3 machine it is a leftover, so it cannot make the
        # machine configured and cannot put CCR in front of Claude.
        configured = config_json_present and version.major != 3

    note = ""
    if live and configured:
        mode = "open via CCR (proxy running)"
    elif configured and not live:
        cmd, note = launch_guidance(version.major)
        if cmd:
            mode = f"native (CCR configured but proxy not running — run `{cmd}` to start)"
        else:
            mode = "native (CCR configured but proxy not running)"
    else:
        mode = "native"

    print("quoin router status:")
    if v3_store:
        # A live config.sqlite is presence evidence in its own right — CCR is
        # the only thing that creates one — so a user whose PATH is missing
        # npm's global bin is not told CCR is absent directly above a store
        # path marked authoritative. The PATH fact is reported, not hidden.
        installed_value = "yes" if on_path else "yes  (store present; `ccr` not on PATH)"
        print(f"  CCR installed:   {installed_value}")
        print(f"  CCR version:     {_status_version_line(version)}")
        print(f"  Config store:    {store_path}  (authoritative for v3)")
        if populated is None:
            print(f"  Store populated: unknown  ({populated_detail})")
        elif populated:
            print("  Store populated: yes")
        else:
            print("  Store populated: no  (no providers, no API key)")
        print(f"  Proxy running:   {'yes' if live else 'no'}  (127.0.0.1:3456)")
        print(f"  API key set:     {'yes' if key_set else 'no'}  (OPENROUTER_API_KEY)")
        print(f"  Active mode:     {mode}")
    else:
        if config_json_present and version.major == 3:
            presence = f"no  ({config_path} exists but CCR v3 does not read it)"
        else:
            presence = f"{'yes' if config_json_present else 'no'}  ({config_path})"
        print(f"  CCR installed:  {'yes' if on_path else 'no'}")
        print(f"  CCR version:    {_status_version_line(version)}")
        print(f"  Config present: {presence}")
        print(f"  Proxy running:  {'yes' if live else 'no'}  (127.0.0.1:3456)")
        print(f"  API key set:    {'yes' if key_set else 'no'}  (OPENROUTER_API_KEY)")
        print(f"  Active mode:    {mode}")
    if note:
        print(f"  {note}")

    # Every input to the report is derived here rather than threaded in: the
    # store read above is the only one on this path, and `rebuilt` is False
    # because a read-only report has rebuilt nothing. Skipped entirely on a
    # v3 store quoin could not read (malformed / unreadable / locked) — a
    # store nobody actually read can't be reported empty. The api-key row
    # count is itself only opened when the other two gating signals already
    # hold, since a v3 machine with no leftover config.json can never fire
    # the report and that second read-only open is otherwise wasted (and,
    # under a live `ccr`, doubles the wait).
    if not v3_store or store_read_ok:
        api_key_rows = (
            ccr_store.v3_api_key_row_count(store_path)
            if version.major == 3 and config_json_present
            else None
        )
        for line in ccr_store.upgrade_loss_lines(
            version.major,
            config_json_present,
            store_cfg,
            api_key_rows,
            rebuilt=False,
            store_absent=route.route == "v3-store-absent",
        ):
            print(line)
    return 0
