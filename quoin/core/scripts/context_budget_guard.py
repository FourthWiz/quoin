#!/usr/bin/env python3
"""context_budget_guard.py — On-demand context-budget guard (IVG-141).

Portable-core, on-demand utilization reader modeled on the Claude adapter's
``session_age_guard.py``. At a heavy phase/task boundary a skill runs this guard
to decide whether the CURRENT session transcript is over a pre-phase budget
(~70%), so it can save a durable boundary checkpoint and hand off to a fresh
session BEFORE a phase adds a full context load and rides into auto-compact.

Portability
-----------
This module is portable-core: the utilization FORMULA and CONSTANTS are
runtime-neutral (byte-identical to ``hooks/_lib.sh::compute_utilization``). The
only Claude-specific surface is transcript RESOLUTION (``~/.claude/projects``),
and that is fully bypassable by passing ``--transcript`` explicitly — a future
Fable adapter (IVG-104) passes its own transcript path. The inline project-hash
regex mirrors the ``get_session_uuid.py`` core precedent (do NOT import a
Claude-adapter module from core).

Usage
-----
  python3 context_budget_guard.py [--threshold-bps N] [--project-root PATH] \
      [--current-uuid UUID] [--transcript PATH]

Threshold precedence: ``--threshold-bps`` if given, else
``QUOIN_PHASE_BOUNDARY_BPS`` (default 7000 bp = 70%).

Opt-out (checked FIRST, before any transcript read):
  ``QUOIN_DISABLE_PHASE_BUDGET=1`` → stdout ``OK|disabled|``, exit 0.

Caller-side behavior knob (NOT read here): ``QUOIN_PHASE_BUDGET_BLOCK=1`` — the
four SKILL wirings switch their reaction to ``OVER`` from the default
checkpoint+advisory+PROCEED to checkpoint + printed fresh-session resume
instruction + STOP. This helper's OK/OVER contract is IDENTICAL regardless of
that knob; no code here branches on it. Documented so the knob surface is
complete.

Stdout: single line ``<status>|<util_bps>|<transcript_path>``
  e.g. OVER|7250|/…/<uuid>.jsonl      (genuinely over budget)
       OK|5100|/…/<uuid>.jsonl        (under budget → proceed)
       OK|disabled|                   (QUOIN_DISABLE_PHASE_BUDGET=1)
       OK|0|                          (fail-OPEN: no transcript / stat error / bad args)

Stderr: human-readable diagnostics (never stdout).

Exit codes (mirror session_age_guard.py):
  1 — util_bps >= threshold (OVER)
  0 — OK (under), disabled, and EVERY fail-OPEN path (no project dir, no jsonl,
      missing uuid file, stat error, bad args)

Design decisions:
  - Stdlib-only: argparse, os, re, sys, pathlib.
  - Fail-OPEN: any error → ``OK|0|``, exit 0 (never block a phase on our failure).
  - Formula BYTE-IDENTICAL to the awk in compute_utilization:
    ``int((bytes / bpt / lim) * 10000)`` (unclamped; Python int() truncates
    toward zero exactly as awk ``printf "%d"`` for positive values).
  - Project hash mirrors Claude harness behavior: replace every char outside
    [A-Za-z0-9-] with '-' (matches get_session_uuid.project_hash /
    session_age_guard._project_hash).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Drive conflict-copy filter (reused verbatim from get_session_uuid.py, ivg-75)
# — a conflict copy "UUID 2.jsonl" newer than the real file must not be picked.
_CONFLICT_RE = re.compile(r" \d{1,3}(\.[^ ]*)?$")


def _project_hash(project_root: Path) -> str:
    """Convert an absolute path to a Claude project hash.

    Mirrors the Claude harness: every character outside [A-Za-z0-9-] becomes
    '-'. Handles paths with spaces / dots / @ / underscores. INLINE by design —
    do NOT import a Claude-adapter module from core (mirrors get_session_uuid.py).
    """
    return re.sub(r'[^A-Za-z0-9-]', '-', str(project_root).rstrip("/"))


def _resolve_transcript(args) -> Path | None:
    """Resolve the transcript file to measure, or None (→ fail-OPEN).

    Precedence: --transcript, else --current-uuid → <hash>/<uuid>.jsonl, else
    newest non-conflict *.jsonl by mtime under ~/.claude/projects/<hash>/.
    """
    # 1. Explicit transcript path wins — the runtime-neutral entry point.
    if args.transcript:
        target = Path(args.transcript)
        if not target.exists():
            print(
                f"[context-budget-guard] --transcript not found: {target} — fail-OPEN",
                file=sys.stderr,
            )
            return None
        return target

    # 2/3. Resolve via project hash under ~/.claude/projects/.
    if args.project_root is None:
        project_root = Path.cwd()
        print(
            f"[context-budget-guard] --project-root not provided; using cwd: {project_root}",
            file=sys.stderr,
        )
    else:
        project_root = Path(args.project_root).resolve()

    project_dir = Path.home() / ".claude" / "projects" / _project_hash(project_root)
    if not project_dir.exists():
        print(
            f"[context-budget-guard] project dir not found: {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        return None

    if args.current_uuid:
        target = project_dir / f"{args.current_uuid}.jsonl"
        if not target.exists():
            print(
                f"[context-budget-guard] uuid jsonl not found: {target} — fail-OPEN",
                file=sys.stderr,
            )
            return None
        return target

    jsonl_files = sorted(
        [p for p in project_dir.glob("*.jsonl") if not _CONFLICT_RE.search(p.name)],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not jsonl_files:
        print(
            f"[context-budget-guard] no jsonl files in {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        return None
    return jsonl_files[0]


def _resolve_threshold(args) -> int:
    """Threshold precedence: --threshold-bps if given, else env (default 7000).

    Defensive: a malformed env value falls back to 7000 (fail-OPEN spirit —
    never crash the guard on a bad knob).
    """
    if args.threshold_bps is not None:
        return args.threshold_bps
    try:
        return int(os.environ.get("QUOIN_PHASE_BOUNDARY_BPS", 7000))
    except (TypeError, ValueError):
        return 7000


def _resolve_constants() -> tuple[float, float]:
    """Return (bpt, lim) from env with the EXACT _lib.sh defaults (8.0 / 150000).

    Defensive parse: a malformed env value falls back to the default.
    """
    try:
        bpt = float(os.environ.get("QUOIN_BYTES_PER_TOKEN") or 8.0)
    except (TypeError, ValueError):
        bpt = 8.0
    try:
        lim = float(os.environ.get("QUOIN_EFFECTIVE_CONTEXT_LIMIT") or 150000)
    except (TypeError, ValueError):
        lim = 150000.0
    return bpt, lim


def main() -> int:
    # Opt-out FIRST — before any arg parse / transcript read.
    if os.environ.get("QUOIN_DISABLE_PHASE_BUDGET") == "1":
        print("OK|disabled|")
        return 0

    parser = argparse.ArgumentParser(
        description="On-demand context-budget guard for phase boundaries (IVG-141)."
    )
    parser.add_argument(
        "--threshold-bps",
        type=int,
        default=None,
        help="Budget threshold in basis-points (0..10000). "
             "Defaults to QUOIN_PHASE_BOUNDARY_BPS (default 7000).",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Absolute path to the project root. Defaults to cwd (with a warning).",
    )
    parser.add_argument(
        "--current-uuid",
        type=str,
        default=None,
        help="UUID of the current session's jsonl (without .jsonl). "
             "If omitted, the most recent non-conflict *.jsonl by mtime is used.",
    )
    parser.add_argument(
        "--transcript",
        type=str,
        default=None,
        help="Explicit transcript path (runtime-neutral entry point). "
             "Overrides project-root/uuid resolution.",
    )

    try:
        args = parser.parse_args()
    except SystemExit:
        # Even on arg-parse errors, fail-OPEN.
        print("OK|0|")
        return 0

    try:
        threshold = _resolve_threshold(args)
        bpt, lim = _resolve_constants()

        target = _resolve_transcript(args)
        if target is None:
            print("OK|0|")
            return 0

        try:
            num_bytes = target.stat().st_size
        except OSError as exc:
            print(
                f"[context-budget-guard] cannot stat {target}: {exc} — fail-OPEN",
                file=sys.stderr,
            )
            print("OK|0|")
            return 0

        # BYTE-IDENTICAL to the awk in compute_utilization:
        #   awk 'BEGIN{ printf "%d\n", (b / bpt / lim) * 10000 }'
        util_bps = int((num_bytes / bpt / lim) * 10000)

        status = "OVER" if util_bps >= threshold else "OK"
        print(f"{status}|{util_bps}|{target}")

        if status == "OVER":
            print(
                f"[context-budget-guard] util {util_bps}bp >= threshold "
                f"{threshold}bp — recommend boundary checkpoint + handoff",
                file=sys.stderr,
            )
            return 1

        print(
            f"[context-budget-guard] util {util_bps}bp < threshold {threshold}bp — OK",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — fail-OPEN on ANY unexpected error
        print(
            f"[context-budget-guard] unexpected error: {exc} — fail-OPEN",
            file=sys.stderr,
        )
        print("OK|0|")
        return 0


if __name__ == "__main__":
    sys.exit(main())
