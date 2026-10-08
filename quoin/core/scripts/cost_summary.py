#!/usr/bin/env python3
"""Portable cost-summary.json normalizer.

Extracts the "best available" total-cost value from a cost-summary.json dict,
mirroring the TypeScript TOTAL_KEY_LADDER in costService.ts.

Public API
----------
normalize_total(data: dict) -> tuple[float | None, bool]
    Returns (value, is_partial).
    - value is the first finite numeric total found, or None if unavailable.
    - is_partial is True when a total IS present and any signal suggests it is
      a partial estimate (fallback_used=True, truthy fallback_note, any *_partial
      key True, or a positive unresolvable_count). Null total is "unavailable",
      NOT partial.

Runtime-neutral: pure stdlib only. MUST NOT import cost_from_jsonl, pricing tables,
or any adapter-owned module. Boundary rule: quoin/docs/runtime-portability.md §31.

fallback_used semantics
-----------------------
fallback_used=True means "partial estimate — some ledger UUIDs didn't resolve to JSONL".
It does NOT mean the cost is unavailable. A truthy fallback_used alongside a present
total should be rendered as "~$X (partial)", never as "unavailable".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# The canonical 7-key ladder — MUST match TOTAL_KEY_LADDER in costService.ts
# ---------------------------------------------------------------------------
_TOTAL_KEY_LADDER = (
    "grand_total",
    "grand_total_usd",
    "total_usd",
    "total_cost_usd",
    "period_total_cost_usd",
    "estimated_task_cost_usd",
    "task_total",
)


_PASSTHROUGH_KEYS = ("estimated_total", "estimated_count", "priced_rows", "phases_excluded")


def _is_finite_number(val: object) -> bool:
    """Return True if val is a finite real number (int or float, not NaN/inf)."""
    if not isinstance(val, (int, float)):
        return False
    if val != val:  # NaN check (NaN != NaN)
        return False
    return val != float("inf") and val != float("-inf")


def normalize_total(data: dict) -> Tuple[Optional[float], bool]:
    """Extract the best total-cost value from a cost-summary.json dict.

    Returns (value, is_partial):
      - value: first finite-number hit from the 7-key ladder, or sum of finite
               task_total across S-\\d+ blocks (hybrid fallback), or None.
      - is_partial: True when a total is present AND (fallback_used is True,
                    OR truthy fallback_note, OR any *_partial key is True).
                    When value is None (unavailable), is_partial is False.

    The ladder order exactly mirrors TOTAL_KEY_LADDER in costService.ts — any
    divergence from that order is a bug (add a test to catch it).
    """
    if not isinstance(data, dict):
        return None, False

    # Step 1: try the top-level 7-key ladder in order
    value: Optional[float] = None
    for key in _TOTAL_KEY_LADDER:
        candidate = data.get(key)
        if _is_finite_number(candidate):
            value = float(candidate)
            break

    # Step 2: hybrid fallback — sum finite task_total across S-\d+ blocks
    # Mirrors parseCostSummary step 2 in costService.ts
    if value is None:
        import re as _re
        stage_sum = 0.0
        found_any = False
        for k, v in data.items():
            if _re.match(r"^S-\d+$", k) and isinstance(v, dict):
                st = v.get("task_total")
                if _is_finite_number(st):
                    stage_sum += float(st)
                    found_any = True
        if found_any:
            value = stage_sum

    # Step 3: is_partial signal — only when a total is present
    if value is None:
        return None, False

    is_partial = False
    if data.get("fallback_used") is True:
        is_partial = True
    if data.get("fallback_note"):
        is_partial = True
    # Any top-level *_partial key that is True
    for k, v in data.items():
        if k.endswith("_partial") and v is True:
            is_partial = True
            break
    # A positive unresolvable_count is an additional partial trigger (belt-and-
    # suspenders backstop for a summary that omits fallback_used) — see T-06's
    # cost-summary.json schema (resolved_total/unresolvable_count).
    unresolvable_count = data.get("unresolvable_count")
    if _is_finite_number(unresolvable_count) and unresolvable_count > 0:
        is_partial = True

    return value, is_partial


# ---------------------------------------------------------------------------
# CLI entry point (for smoke tests: python3 cost_summary.py <file>)
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Normalize cost-summary.json total")
    parser.add_argument("file", nargs="?", help="Path to cost-summary.json")
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default text — byte-identical to the pre-existing tuple print)",
    )
    args = parser.parse_args(argv)

    if not args.file:
        parser.print_help()
        return 1

    path = Path(args.file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    value, is_partial = normalize_total(data)
    if args.format == "json":
        out = {"total": value, "is_partial": is_partial}
        # Estimate and coverage fields are passed through only when the summary
        # carries them, so a summary without them keeps the two-key shape.
        for key in _PASSTHROUGH_KEYS:
            if isinstance(data, dict) and key in data:
                out[key] = data[key]
        print(json.dumps(out))
    else:
        print((value, is_partial))
    return 0


if __name__ == "__main__":
    sys.exit(main())
