#!/usr/bin/env python3
# CLAUDE-ADAPTER-OWNED — this file provides cost data for the dashboard by
# reading Claude Code JSONL sessions. Do NOT import this module from any file
# in quoin/core/. The portable cost model is dashboard_model.py. The adapter
# boundary: only this file and its siblings in quoin/scripts/ may import
# cost_from_jsonl.
#
# dashboard_cost.py — adapter cost provider for the workflow dashboard.
# Implements make_cost_provider(project_root, home=None) -> CostProvider.
# CostProvider signature: provider(task_name, rows) -> dict|None
#   where rows is a list of ledger-row dicts with keys:
#     {uuid, date, phase, model_or_effort, note, fallback_fires, attribution}
# Returns:
#   {"mode": "usd", "usd": float, "tokens": int, "partial": bool,
#    "by_phase": {phase: {"usd": float}}}  # usd mode
#   {"mode": "tokens", "usd": None, "tokens": int, "partial": bool,
#    "by_phase": {phase: {"tokens": int}}}  # tokens mode
#   None  # nothing resolved and no JSONL found for any row UUID — counts mode
#
# Inline-first precedence (stage 4, D-1/D-2): a row whose col-8 attribution
# classifies as "resolved" contributes its inline usd directly (no JSONL
# lookup); "unresolvable" sets partial=True and contributes nothing (never a
# silent $0); "legacy" (no col 8) uses the existing JSONL path, and a missing
# JSONL there also sets partial=True. The "partial" key only reaches the
# dashboard JSON via dashboard_model's merge whitelist.
#
# Per D-04/D-05: memo-cache keyed by (uuid, jsonl_mtime); missing JSONL cached
# as sentinel (uuid, None) to avoid repeated stat storms.

import importlib.util
import os
import pathlib
import sys

# ---------------------------------------------------------------------------
# Cross-load cost_from_jsonl (same-dir spec-load — adapter sibling)
# ---------------------------------------------------------------------------

def _load_sibling(name: str):
    """Load a sibling script from the same directory via spec_from_file_location."""
    path = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        raise ImportError(f"Cannot load {name}: {path} not found")
    module_key = f"_dashboard_cost_{name}"
    # Re-use cached module if already loaded to avoid re-exec
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_cfj = _load_sibling("cost_from_jsonl")

project_hash = _cfj.project_hash
jsonl_path_for = _cfj.jsonl_path_for
parse_session = _cfj.parse_session


def _load_core_cost_event():
    """Import cost_event from core/scripts/ (adapter -> core, allowed).

    Works at source (relative traversal) and deployed (sibling core/scripts/
    dir, same shape verified for analyze_cost_ledger.py). Fail-open: returns
    None on any load failure — the provider then treats every row as legacy.
    """
    try:
        core_path = pathlib.Path(__file__).resolve().parent.parent / "core" / "scripts" / "cost_event.py"
        if not core_path.exists():
            return None
        module_key = "_dashboard_cost_cost_event"
        if module_key in sys.modules:
            return sys.modules[module_key]
        spec = importlib.util.spec_from_file_location(module_key, core_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError):
        return None


_ce = _load_core_cost_event()
classify_attribution = _ce.classify_attribution if _ce is not None else None
cohort_attribution = _ce.cohort_attribution if _ce is not None else None


# ---------------------------------------------------------------------------
# Memo-cache (D-05)
# ---------------------------------------------------------------------------

def _make_cache():
    """Return a fresh memo-cache dict keyed by (uuid, mtime_or_None)."""
    return {}


def _lookup_cached(cache: dict, uuid: str, jsonl_path: pathlib.Path):
    """Return cached parse_session result, or _MISS sentinel if not cached.

    Cache key: (uuid, mtime_float_or_None).
    None mtime = file absent (sentinel: never re-stat a missing file for this uuid).
    """
    try:
        mtime = jsonl_path.stat().st_mtime if jsonl_path.exists() else None
    except OSError:
        mtime = None
    key = (uuid, mtime)
    if key in cache:
        return cache[key], key
    return _MISS, key


_MISS = object()  # sentinel for cache miss


# ---------------------------------------------------------------------------
# Core provider factory (D-04)
# ---------------------------------------------------------------------------

def make_cost_provider(project_root, home=None):
    """Return a CostProvider closure bound to project_root.

    project_root: str or pathlib.Path
    home: override for Path.home() (used in tests)

    The returned provider(task_name, rows) -> dict|None:
      - Iterates rows, resolves each row's UUID to a JSONL path
      - Parses each found JSONL (with memo-cache)
      - Aggregates: totalCost > 0 → mode=usd; totalTokens > 0 → mode=tokens; else None
      - by_phase is NESTED: {phase: {"usd": float}} or {phase: {"tokens": int}}
    """
    root = pathlib.Path(project_root).resolve()
    proj_hash = project_hash(str(root))
    cache = _make_cache()

    def _resolve_via_cache(uuid: str):
        """(cost, has_cost) for uuid via the shared memo-cache. Returns
        (0.0, False) when the JSONL is absent or unreadable."""
        jsonl_path = jsonl_path_for(uuid, proj_hash, home=home or pathlib.Path.home())
        cached_result, cache_key = _lookup_cached(cache, uuid, jsonl_path)
        if cached_result is _MISS:
            if jsonl_path.exists():
                try:
                    result = parse_session(jsonl_path)
                except (IOError, OSError, Exception):
                    result = None
            else:
                result = None
            cache[cache_key] = result
            cached_result = result
        if cached_result is None:
            return 0.0, False
        return cached_result.get("totalCost", 0.0) or 0.0, True

    def _legacy_usd_accumulation(rows):
        """Pre-cohort per-row usd accumulation — fail-open fallback used only
        when the cohort helper is unavailable (core module failed to load).
        Kept byte-for-byte equivalent to the original stage-4 usd logic."""
        total_usd = 0.0
        by_phase_usd: dict = {}
        for row in rows:
            uuid = row.get("uuid", "")
            phase = row.get("phase", "")
            if not uuid:
                continue
            verdict, inline_usd = ("legacy", None)
            if classify_attribution is not None:
                verdict, inline_usd = classify_attribution(row.get("attribution", ""))
            if verdict == "resolved":
                usd = inline_usd or 0.0
                total_usd += usd
                if phase:
                    by_phase_usd[phase] = by_phase_usd.get(phase, 0.0) + usd
                continue
            if verdict == "unresolvable":
                continue
            usd, has_cost = _resolve_via_cache(uuid)
            if not has_cost:
                continue
            total_usd += usd
            if phase:
                by_phase_usd[phase] = by_phase_usd.get(phase, 0.0) + usd
        return total_usd, by_phase_usd

    def provider(task_name, rows):
        """Resolve cost for a task given its ledger rows.

        Returns dict with mode/usd/tokens/by_phase, or None if no JSONL found.
        per D-04 ladder and D-14 nested by_phase contract.

        USD totals/by_phase are shaped by `cohort_attribution` (IVG-157) so a
        shared session UUID (e.g. an inline checkpoint sharing its parent
        phase's session UUID) is counted ONCE, not once per participating
        phase. Tokens-mode aggregation is unchanged (documented residual,
        MIN-3): the cohort helper is USD-oriented only.
        """
        total_tokens = 0
        any_jsonl_found = False
        any_resolved = False
        partial = False

        by_phase_tokens = {}  # {phase: int}

        for row in rows:
            uuid = row.get("uuid", "")
            phase = row.get("phase", "")
            if not uuid:
                continue

            verdict, inline_usd = ("legacy", None)
            if classify_attribution is not None:
                verdict, inline_usd = classify_attribution(row.get("attribution", ""))

            if verdict == "resolved":
                # Inline-first: no JSONL lookup. any_resolved is set on EVERY
                # resolved row (including a genuine resolved usd=0.0 — D-8).
                any_resolved = True
                continue

            if verdict == "unresolvable":
                partial = True
                continue

            # verdict == "legacy": existing JSONL path, unchanged for tokens.
            jsonl_path = jsonl_path_for(uuid, proj_hash, home=home or pathlib.Path.home())
            cached_result, cache_key = _lookup_cached(cache, uuid, jsonl_path)
            if cached_result is _MISS:
                if jsonl_path.exists():
                    try:
                        result = parse_session(jsonl_path)
                    except (IOError, OSError, Exception):
                        result = None
                else:
                    result = None
                cache[cache_key] = result
                cached_result = result

            if cached_result is None:
                # JSONL absent or unreadable — never a silent contribution.
                partial = True
                continue

            any_jsonl_found = True
            tokens = cached_result.get("totalTokens", 0) or 0
            total_tokens += tokens
            if phase:
                by_phase_tokens[phase] = by_phase_tokens.get(phase, 0) + tokens

        if not (any_jsonl_found or any_resolved):
            return None

        # ---- USD shaping via cohort_attribution (F-04, fixes N x-inflation) ----
        if cohort_attribution is not None:
            cohort_result = cohort_attribution(rows, _resolve_via_cache)
        else:
            cohort_result = None

        if cohort_result is not None:
            total_usd = cohort_result.resolved_total
            by_phase_usd = {ph: d["cost"] for ph, d in cohort_result.by_phase.items()}
            if cohort_result.shared_bucket:
                # MIN-5 (UX, confirmed intended): the shared bucket surfaces
                # as a labeled pseudo-phase row so the dashboard total stays
                # correct without inventing a per-phase dollar figure.
                by_phase_usd["shared-session (multi-phase)"] = cohort_result.shared_bucket["cost"]
            if cohort_result.unresolvable_count > 0:
                partial = True
        else:
            total_usd, by_phase_usd = _legacy_usd_accumulation(rows)

        # MINOR-5/D-8: a resolved row (even usd=0.0) forces usd mode — it is
        # a genuine resolved value, distinguishable from no-data/tokens-only.
        if total_usd > 0 or any_resolved:
            # USD mode — nested by_phase: {phase: {"usd": float}}
            by_phase = {ph: {"usd": usd_val} for ph, usd_val in by_phase_usd.items()}
            return {
                "mode": "usd",
                "usd": total_usd,
                "tokens": total_tokens,
                "partial": partial,
                "by_phase": by_phase,
            }
        elif total_tokens > 0:
            # Tokens mode — nested by_phase: {phase: {"tokens": int}}
            # KNOWN RESIDUAL (MIN-3): a shared cohort's tokens remain summed
            # per-row here (N x over-counted) — the cohort helper is USD-
            # oriented; this path is a rare unpriceable-model fallback, and
            # the loud user-facing USD overstatement is what this fix
            # addresses. See memory/cost-ledger-format.md.
            by_phase = {ph: {"tokens": tok_val} for ph, tok_val in by_phase_tokens.items()}
            return {
                "mode": "tokens",
                "usd": None,
                "tokens": total_tokens,
                "partial": partial,
                "by_phase": by_phase,
            }
        else:
            # JSONL found but zero cost and zero tokens — treat as no data
            return None

    return provider
