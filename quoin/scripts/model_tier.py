"""model_tier.py — Map a Claude model name to a tier and decide whether to dispatch.

CLAUDE-ADAPTER-OWNED — this file knows Claude model names. Do NOT import it
from quoin/core/.

Light /run implements on Opus. Called with the session's model name (the
"powered by the model named X" line, or a model id such as claude-sonnet-5-5),
it says whether the implementation steps can run inline or must be handed to
an Opus subagent. Any tier other than the target triggers a dispatch, in both
directions: haiku and sonnet are below Opus, fable and mythos are above it.
An unrecognised name also dispatches, because a spare Opus spawn is cheaper
than implementing on an unknown model. QUOIN_DISABLE_MINTIER_GUARD=1 opts out:
the verdict is always INLINE|optout.

Usage:
  python3 ~/.claude/scripts/model_tier.py "<model name or id>" [--target opus]

Stdout: exactly one line, exit code 0.
  INLINE|<tier>      the current tier equals the target
  DISPATCH|<tier>    any other tier, including "unknown"
  INLINE|optout      QUOIN_DISABLE_MINTIER_GUARD=1 is set
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Cheapest first. Fable and Mythos rank above Opus.
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3, "mythos": 3}


def tier_of(model: str) -> Optional[str]:
    """Return the tier named in ``model`` (case-insensitive), or None."""
    name = (model or "").lower()
    for tier in TIER_RANK:
        if tier in name:
            return tier
    return None


def compare_tier(model: str, target: str = "opus") -> str:
    """Return "lower", "equal", "higher" or "unknown" relative to ``target``."""
    tier = tier_of(model)
    if tier is None or target not in TIER_RANK:
        return "unknown"
    diff = TIER_RANK[tier] - TIER_RANK[target]
    if diff < 0:
        return "lower"
    if diff > 0:
        return "higher"
    return "equal"


def verdict(model: str, target: str = "opus") -> str:
    """Return the one-line CLI verdict for ``model`` against ``target``."""
    tier = tier_of(model) or "unknown"
    action = "INLINE" if compare_tier(model, target) == "equal" else "DISPATCH"
    return f"{action}|{tier}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", nargs="?", default="")
    parser.add_argument("--target", default="opus", choices=sorted(TIER_RANK))
    args = parser.parse_args(argv)
    if os.environ.get("QUOIN_DISABLE_MINTIER_GUARD") == "1":
        print("INLINE|optout")
    else:
        print(verdict(args.model, args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
