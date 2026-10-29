#!/usr/bin/env python3
"""DEPRECATED (IVG-165, D-05): this was a one-shot historical migration script;
its roster is stale (`workspace` absent) and no live pipeline imports it. §0
is now generator-owned by `inject_pollution_dispatch.py` (IVG-165 Commit A),
which supersedes this script's role for all future §0 edits. Kept intact
(functions untouched) because `test_propagate_1m_s0_edit.py` dynamically
imports and exercises its functions; do not extend or reuse this module for
new work — see `inject_pollution_dispatch.py::render_section0_block`.

Propagate the IVG-90 Stage 2 §0 proactive 1M-precheck edit to all 19 SKILL.md files.

Applies three byte-identical insertions to each target file:
  1. DECIDE_BLOCK before SPAWN_AGENT_ANCHOR (pre-dispatch 1M check)
  2. REPLACE WAIT_ANCHOR with split form + CACHEWRITE_SAFE_BLOCK (success-path cachewrite)
  3. CACHEWRITE_UNSAFE_BLOCK before IVG89_THEN_PROCEED_ANCHOR (IVG-89 leaf cachewrite)

Idempotent: files that already contain DECIDE_BEGIN are skipped.
Errors loudly if any anchor is absent or not unique in a file.

Usage:
  python3 propagate_1m_s0_edit.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

THIS_FILE = Path(__file__).resolve()
DEV_DIR = THIS_FILE.parent.parent          # quoin/quoin/dev/
PKG_DIR = DEV_DIR.parent                   # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"

# ---------------------------------------------------------------------------
# Target file list (18 remaining after gate pilot)
# ---------------------------------------------------------------------------

MIGRATED_TO_ADAPTER: frozenset[str] = frozenset({
    "capture_insight", "cost_snapshot", "end_of_day", "end_of_task",
    "expand", "implement", "pr", "revise-fast", "rollback",
    "start_of_day", "status", "triage", "weekly_review",
    # gate is already patched (pilot); included in SECTION0_TARGETS but excluded here
})

LEGACY_TARGETS: list[str] = [
    "cleanup",
    "continue_work",
    "next_steps",
    "sleep",
]


def skill_md_path(skill_name: str) -> Path:
    return ADAPTER_SKILLS_DIR / skill_name / "SKILL.md"


ALL_TARGETS: list[str] = sorted(MIGRATED_TO_ADAPTER) + LEGACY_TARGETS

# ---------------------------------------------------------------------------
# Markers and anchor strings
# ---------------------------------------------------------------------------

DECIDE_BEGIN    = "<!-- §0-1m-decide-begin -->"
DECIDE_END      = "<!-- §0-1m-decide-end -->"
CACHEWRITE_BEGIN = "<!-- §0-1m-cachewrite-begin -->"
CACHEWRITE_END   = "<!-- §0-1m-cachewrite-end -->"

SPAWN_AGENT_ANCHOR        = "      Spawn an Agent subagent with the following arguments:"
WAIT_ANCHOR               = "      Wait for the subagent. Return its output as your final response. STOP."
IVG89_THEN_PROCEED_ANCHOR = "      Then proceed to §1 at the current tier (treat as if"

# ---------------------------------------------------------------------------
# Canonical strings (byte-identical across all 19 files)
# ---------------------------------------------------------------------------

DECIDE_BLOCK = """\
<!-- §0-1m-decide-begin -->
Pre-dispatch 1M check (IVG-90 Layer 1+2):
  - Run: python3 __QUOIN_HOME__/scripts/dispatch_config.py --decide --tier <declared_tier> --verbose
    where <declared_tier> is the tier declared for this skill (e.g. "sonnet" or "haiku",
    as shown in the dispatched-tier line immediately above).
  - If the command returns "safe-path" on line 1:
      Read the reason token from line 2 (config|cache|probe).
      Emit the one-line advisory (verbatim, substituting <reason> with the line-2 token):
        `[quoin: 1M-unsafe declared-tier per <reason>; running SAFE PATH without dispatch]`
      Then proceed to §1/§0c at the current tier (treat as if [no-redispatch] were present).
      Do NOT call the Agent dispatch. Do NOT call AskUserQuestion.
  - If the command returns "dispatch" on line 1, OR if the script is missing / errors:
      Continue to the Agent dispatch call below (today's path — fail-OPEN).
<!-- §0-1m-decide-end -->"""

CACHEWRITE_SAFE_BLOCK = """\
<!-- §0-1m-cachewrite-begin -->
      Cache the safe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result safe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->"""

CACHEWRITE_UNSAFE_BLOCK = """\
<!-- §0-1m-cachewrite-begin -->
      Cache the unsafe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result unsafe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->"""

# ---------------------------------------------------------------------------
# Replacement for WAIT_ANCHOR (split form per m-06 ordering)
# ---------------------------------------------------------------------------

WAIT_SPLIT_REPLACEMENT = (
    "      Wait for the subagent.\n"
    + CACHEWRITE_SAFE_BLOCK
    + "\n"
    + "      Return its output as your final response. STOP."
)

# ---------------------------------------------------------------------------
# Core patch function
# ---------------------------------------------------------------------------

def _count_occurrences(lines: list[str], anchor: str) -> list[int]:
    """Return 0-based indices of lines that contain the anchor string."""
    return [i for i, line in enumerate(lines) if anchor in line]


def patch_text(text: str, skill: str) -> str | None:
    """Apply the three-point edit to the file text.

    Returns the patched text, or None if the file was already patched (idempotent skip).
    Raises ValueError with a descriptive message if any anchor is absent or multi-occurrence.
    """
    # Step 2: Skip if already patched (idempotent guard — fires BEFORE anchor lookups)
    # Steps 3-5 are only reached if DECIDE_BEGIN was absent; re-running on an
    # already-patched file is fully safe — step 2 exits early before any anchor lookups.
    if DECIDE_BEGIN in text:
        return None  # already patched

    lines = text.splitlines(keepends=True)

    # Step 3: Insert DECIDE_BLOCK before SPAWN_AGENT_ANCHOR
    spawn_indices = _count_occurrences(lines, SPAWN_AGENT_ANCHOR)
    if len(spawn_indices) != 1:
        raise ValueError(
            f"{skill}: SPAWN_AGENT_ANCHOR found {len(spawn_indices)} times "
            f"(expected exactly 1): {SPAWN_AGENT_ANCHOR!r}"
        )
    spawn_idx = spawn_indices[0]
    # Insert DECIDE_BLOCK as a new set of lines before the spawn line
    decide_lines = [line + "\n" for line in DECIDE_BLOCK.splitlines()]
    lines = lines[:spawn_idx] + decide_lines + lines[spawn_idx:]

    # Re-index after insertion for subsequent anchors
    text_after_decide = "".join(lines)
    lines = text_after_decide.splitlines(keepends=True)

    # Step 4: REPLACE WAIT_ANCHOR with split version
    wait_indices = _count_occurrences(lines, WAIT_ANCHOR)
    if len(wait_indices) != 1:
        raise ValueError(
            f"{skill}: WAIT_ANCHOR found {len(wait_indices)} times "
            f"(expected exactly 1): {WAIT_ANCHOR!r}"
        )
    wait_idx = wait_indices[0]
    # Preserve the line ending of the original line
    original_ending = "\n" if lines[wait_idx].endswith("\n") else ""
    replacement_lines = [line + "\n" for line in WAIT_SPLIT_REPLACEMENT.splitlines()]
    if replacement_lines and original_ending == "":
        replacement_lines[-1] = replacement_lines[-1].rstrip("\n")
    lines = lines[:wait_idx] + replacement_lines + lines[wait_idx + 1:]

    text_after_wait = "".join(lines)
    lines = text_after_wait.splitlines(keepends=True)

    # Step 5: Insert CACHEWRITE_UNSAFE_BLOCK before IVG89_THEN_PROCEED_ANCHOR
    proceed_indices = _count_occurrences(lines, IVG89_THEN_PROCEED_ANCHOR)
    if len(proceed_indices) != 1:
        raise ValueError(
            f"{skill}: IVG89_THEN_PROCEED_ANCHOR found {len(proceed_indices)} times "
            f"(expected exactly 1): {IVG89_THEN_PROCEED_ANCHOR!r}"
        )
    proceed_idx = proceed_indices[0]
    unsafe_lines = [line + "\n" for line in CACHEWRITE_UNSAFE_BLOCK.splitlines()]
    lines = lines[:proceed_idx] + unsafe_lines + lines[proceed_idx:]

    return "".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print planned insertions without writing files.",
    )
    args = parser.parse_args()

    patched = 0
    skipped = 0
    errored = 0

    for skill in ALL_TARGETS:
        path = skill_md_path(skill)
        if not path.exists():
            print(f"ERROR: {skill}: file not found: {path}", file=sys.stderr)
            errored += 1
            continue

        text = path.read_text(encoding="utf-8")
        try:
            result = patch_text(text, skill)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            errored += 1
            continue

        if result is None:
            print(f"Skipped (already patched): {path.relative_to(PKG_DIR.parent.parent)}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"[dry-run] Would patch: {path.relative_to(PKG_DIR.parent.parent)}")
            patched += 1
        else:
            path.write_text(result, encoding="utf-8")
            print(f"Patched: {path.relative_to(PKG_DIR.parent.parent)}")
            patched += 1

    action = "Would patch" if args.dry_run else "Patched"
    print(f"\n{action}: {patched}  Skipped: {skipped}  Errored: {errored}")
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
