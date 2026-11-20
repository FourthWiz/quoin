#!/usr/bin/env python3
"""dispatch_config — 1M-dispatch config + per-tier sentinel cache reader/writer.

Provides two CLI modes:
  --decide --tier T             Read config + cache → print "dispatch" or "safe-path".
  --write-cache --tier T --result safe|unsafe
                                Atomically overwrite the per-tier sentinel file.

Fail-OPEN everywhere: any error in --decide → print "dispatch", exit 0.
Any error in --write-cache → silent skip, exit 0.

Stage 1 scope: config read + cache read/write + --decide/--write-cache CLI.
Stage 2 will wire --decide into the §0 preamble dispatch logic.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# T-01: Config layer
# ---------------------------------------------------------------------------


def read_config() -> dict:
    """Return config dict with keys: one_m_dispatch, one_m_fallback_model.

    Precedence per knob (evaluated independently):
      env var > ~/.config/quoin/dispatch.json value > None.

    QUOIN_1M_DISPATCH valid values: off | on | tier-csv (e.g. "haiku,sonnet").
      Empty or unknown/garbage → treat as unset (None), fall through to file/cache.
    QUOIN_1M_FALLBACK_MODEL: parsed and stored verbatim.
      INERT in Stage 1 — the architecture fallback-deferral decision means no dispatch
      branch reads this value. It exists here only to satisfy the config contract.
    """
    result: dict = {"one_m_dispatch": None, "one_m_fallback_model": None}

    # --- env knobs (highest precedence) ---
    env_dispatch = os.environ.get("QUOIN_1M_DISPATCH", "")
    if env_dispatch:  # non-empty → treat as set
        result["one_m_dispatch"] = env_dispatch

    env_fallback = os.environ.get("QUOIN_1M_FALLBACK_MODEL", "")
    if env_fallback:
        result["one_m_fallback_model"] = env_fallback

    # --- file (lower precedence; fills only knobs still None) ---
    if result["one_m_dispatch"] is None or result["one_m_fallback_model"] is None:
        cfg_path = Path(os.path.expanduser("~/.config/quoin/dispatch.json"))
        try:
            file_cfg = json.loads(cfg_path.read_text())
            if result["one_m_dispatch"] is None:
                val = file_cfg.get("one_m_dispatch")
                if val is not None:
                    result["one_m_dispatch"] = val
            if result["one_m_fallback_model"] is None:
                val = file_cfg.get("one_m_fallback_model")
                if val is not None:
                    result["one_m_fallback_model"] = val
        except Exception:
            # Missing file / unreadable / malformed JSON / missing keys → contribute nothing.
            pass

    return result


def config_verdict(tier: str, cfg: dict) -> str:
    """Return "safe" | "unsafe" | "unset" for the given tier + config dict.

    Decision table (architecture config-knob decision):
      one_m_dispatch == "on"      → safe  (dispatch freely; skips cache check)
      one_m_dispatch == "off"     → unsafe (safe path for all tiers)
      one_m_dispatch == tier-csv  → safe if tier ∈ csv list, unsafe otherwise
      None / unset / garbage      → unset (fall through to cache layer)
    """
    val = cfg.get("one_m_dispatch")
    if not val:  # None or empty string
        return "unset"

    if val == "on":
        return "safe"
    if val == "off":
        return "unsafe"

    candidates = [t.strip().lower() for t in val.split(",") if t.strip()]
    if candidates:
        tier_norm = tier.strip().lower()
        return "safe" if tier_norm in candidates else "unsafe"

    # Garbage (e.g. pure whitespace or weird value with no recognized form)
    return "unset"


# ---------------------------------------------------------------------------
# T-02: Project-root resolution + cache layer
# ---------------------------------------------------------------------------


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from start to find a directory containing .workflow_artifacts/.

    Copied from status_graph._find_project_root (L355-365) — repo convention for
    project-root walk-up. Self-contained copy; do NOT cross-import from status_graph.

    NOTE: `path_resolve.py --print-project-root` (IVG-119) is now a valid self-inclusive
    walk-up print mode and mirrors this function's semantics. `status_graph._find_project_root`
    remains the alternative in-process precedent. (The old `--project-root` flag is an input
    arg with no print mode — do NOT invoke it bare; it exits 2.)
    """
    current = (start or Path.cwd()).resolve()
    for _ in range(20):
        if (current / ".workflow_artifacts").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _tier_token(tier: str) -> str:
    """Sanitize tier name to a safe filename segment (lower-case alpha only).

    Returns an empty string if the tier contains non-alpha characters (e.g. path
    separators). Callers treat an empty token as a signal to skip the cache silently
    (fail-OPEN: malformed tier → unknown/skip).
    """
    t = tier.strip().lower()
    # Allow only [a-z] — reject path separators, digits, and any other char
    if not re.fullmatch(r"[a-z]+", t):
        return ""
    return t


def cache_path(tier: str, root: Path) -> Optional[Path]:
    """Return the sentinel path for this tier, or None if tier is malformed."""
    tok = _tier_token(tier)
    if not tok:
        return None
    return root / ".workflow_artifacts" / "memory" / f"1m-tier-{tok}.txt"


def read_cache(tier: str) -> str:
    """Return "safe" | "unsafe" | "unknown".

    unknown is returned on:
      - no project root found
      - malformed tier token
      - sentinel missing / unreadable / contains anything other than exactly "safe" or "unsafe"
    """
    root = find_project_root()
    if root is None:
        return "unknown"
    cp = cache_path(tier, root)
    if cp is None:
        return "unknown"
    try:
        content = cp.read_text().strip()
    except Exception:
        return "unknown"
    if content == "safe":
        return "safe"
    if content == "unsafe":
        return "unsafe"
    # malformed sentinel (empty, multi-token, junk) → unknown (R-04)
    return "unknown"


def write_cache(tier: str, result: str) -> None:
    """Atomically overwrite the per-tier sentinel with result ("safe" or "unsafe").

    Whole-file overwrite via atomic rename (tmp → final). No read-modify-write.
    Any exception → silent skip / return. Exit 0 always (fail-OPEN, R-04).

    Stage 3 forward-handoff: /cleanup does NOT sweep 1m-tier-*.txt sentinels —
    it uses a hardcoded 8-family allow-list that does not include this pattern.
    Cache aging / expiry must be addressed in Stage 3 docs/cleanup work.
    """
    if result not in {"safe", "unsafe"}:
        return
    root = find_project_root()
    if root is None:
        return
    cp = cache_path(tier, root)
    if cp is None:
        return
    try:
        d = cp.parent
        d.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(cp) + ".tmp")
        tmp.write_text(result)
        os.replace(tmp, cp)
    except Exception:
        # silent skip — fail-OPEN (R-04)
        pass


# ---------------------------------------------------------------------------
# T-03: --decide logic + main()
# ---------------------------------------------------------------------------


def decide(tier: str) -> Tuple[str, str]:
    """Return (verdict, reason) where verdict ∈ {"dispatch","safe-path"} and reason ∈ {"config","cache","probe"}.

    Decision tree (architecture decision fold):
      1. Read config; evaluate config_verdict.
         - "unsafe" → ("safe-path", "config")
         - "safe"   → ("dispatch",  "config")
      2. config == "unset" → read cache:
         - "unsafe" → ("safe-path", "cache")
         - "safe"   → ("dispatch",  "cache")
         - "unknown" → ("dispatch", "probe")  — config=unset AND cache=unknown = probe (today's path)
    """
    cfg = read_config()
    cv = config_verdict(tier, cfg)

    if cv == "unsafe":
        return ("safe-path", "config")
    if cv == "safe":
        return ("dispatch", "config")

    # cv == "unset" → consult cache
    cache = read_cache(tier)
    if cache == "unsafe":
        return ("safe-path", "cache")
    if cache == "safe":
        return ("dispatch", "cache")
    # unknown (missing sentinel, malformed, no project root) → probe
    return ("dispatch", "probe")


def main(argv=None) -> int:  # type: ignore[assignment]
    """CLI entry point. Returns exit code (always 0 — fail-OPEN discipline)."""
    args_list = argv if argv is not None else sys.argv[1:]

    # Manual pre-validation to honor fail-OPEN for --decide:
    # argparse would exit 2 on missing --tier; we want to print "dispatch" + exit 0 instead.
    has_decide = "--decide" in args_list
    has_write_cache = "--write-cache" in args_list
    has_tier = "--tier" in args_list

    # --decide with missing --tier → fail-OPEN
    if has_decide and not has_tier:
        print("dispatch")
        return 0

    # --write-cache with bad args → silent exit 0
    if has_write_cache and not has_tier:
        return 0

    parser = argparse.ArgumentParser(
        prog="dispatch_config",
        description="1M-dispatch config + per-tier sentinel cache reader/writer.",
    )
    parser.add_argument("--decide", action="store_true", help="Read config+cache and print verdict.")
    parser.add_argument("--write-cache", action="store_true", help="Write per-tier sentinel file.")
    parser.add_argument("--tier", required=False, help="Tier name (haiku|sonnet|opus).")
    parser.add_argument("--result", choices=["safe", "unsafe"], help="Result to write (--write-cache only).")
    parser.add_argument("--verbose", action="store_true", help="With --decide: print reason on second line.")

    try:
        ns = parser.parse_args(args_list)
    except SystemExit:
        # argparse error or --help → fail-OPEN for --decide, silent otherwise
        if has_decide:
            print("dispatch")
        return 0

    if ns.decide:
        try:
            verdict, reason = decide(ns.tier)
            print(verdict)
            if ns.verbose:
                print(reason)
        except Exception:
            # Whole-body fail-OPEN: any exception → dispatch (R-01/R-03)
            print("dispatch")
        return 0

    if ns.write_cache:
        if ns.result:
            write_cache(ns.tier, ns.result)
        # Always exit 0 (write errors already swallowed in write_cache)
        return 0

    # No mode selected — silent exit 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
