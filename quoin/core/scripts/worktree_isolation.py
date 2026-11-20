#!/usr/bin/env python3
"""worktree_isolation — worktree-dispatch capability decider + probe-sentinel reader/writer.

Sibling of dispatch_config.py (IVG-90). Mirrors its config → file → sentinel → default
precedence, but decides whether a source-mutating skill should ATTEMPT worktree isolation
(`isolation: "worktree"`) or SKIP it and dispatch plainly.

Two CLI modes:
  --decide                      Read config + probe sentinel → print "attempt" or "skip".
                                With --verbose, print the reason on a second line
                                (reason ∈ {config, probe, default}).
  --write-probe --result works|broken
                                Atomically overwrite the project probe sentinel.

Precedence (highest first):
  1. env QUOIN_WORKTREE_ISOLATION  ("on" → attempt, "off" → skip; empty/garbage → unset)
  2. ~/.config/quoin/dispatch.json key "worktree_isolation" (same value grammar)
  3. project sentinel .workflow_artifacts/memory/worktree-probe.txt ("works" → attempt)
  4. DEFAULT skip  (isolation is opt-in — D-04; "broken"/unknown/any error all → skip)

Fail-OPEN discipline: any error in --decide → print "skip", exit 0 (never pay the failed
worktree round-trip on error). Any error in --write-probe → silent skip, exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Config layer  (env > ~/.config/quoin/dispatch.json)
# ---------------------------------------------------------------------------


def read_config() -> Optional[str]:
    """Return the raw worktree_isolation config value, or None if unset.

    Precedence: env QUOIN_WORKTREE_ISOLATION > ~/.config/quoin/dispatch.json value > None.
    An empty env string is treated as unset (fall through to the file).
    """
    env_val = os.environ.get("QUOIN_WORKTREE_ISOLATION", "")
    if env_val:  # non-empty → treat as set
        return env_val

    cfg_path = Path(os.path.expanduser("~/.config/quoin/dispatch.json"))
    try:
        file_cfg = json.loads(cfg_path.read_text())
        val = file_cfg.get("worktree_isolation")
        if val is not None:
            return val
    except Exception:
        # Missing file / unreadable / malformed JSON / missing key → contribute nothing.
        pass
    return None


def config_verdict(val: Optional[str]) -> str:
    """Return "attempt" | "skip" | "unset" for a raw config value.

      "on"  (case-insensitive) → attempt
      "off" (case-insensitive) → skip
      None / empty / anything else → unset  (fall through to the probe sentinel)
    """
    if not val:  # None or empty string
        return "unset"
    norm = val.strip().lower()
    if norm == "on":
        return "attempt"
    if norm == "off":
        return "skip"
    return "unset"


# ---------------------------------------------------------------------------
# Project-root resolution + probe sentinel layer
# ---------------------------------------------------------------------------


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from start to find a directory containing .workflow_artifacts/.

    Self-contained copy of the repo's project-root walk-up convention
    (cf. dispatch_config.find_project_root / status_graph._find_project_root).
    Do NOT cross-import — the "copy, do not cross-import" convention avoids
    installer DEPLOYED/CORE churn (lesson 2026-06-17).
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


def probe_path(root: Path) -> Path:
    """Return the project probe-sentinel path."""
    return root / ".workflow_artifacts" / "memory" / "worktree-probe.txt"


def read_probe() -> str:
    """Return "works" | "broken" | "unknown".

    unknown is returned on:
      - no project root found
      - sentinel missing / unreadable / anything other than exactly "works" or "broken"
    """
    root = find_project_root()
    if root is None:
        return "unknown"
    try:
        content = probe_path(root).read_text().strip()
    except Exception:
        return "unknown"
    if content == "works":
        return "works"
    if content == "broken":
        return "broken"
    # malformed sentinel (empty, multi-token, junk) → unknown
    return "unknown"


def write_probe(result: str) -> None:
    """Atomically overwrite the probe sentinel with result ("works" or "broken").

    Whole-file overwrite via atomic rename (tmp → final). No read-modify-write.
    Any exception → silent skip. Fail-OPEN.

    Note (MIN-3): worktree-probe.txt is declared OUT OF /sleep --purge --sentinels
    scope. If it is ever purged anyway, the decider reverts to safe DEFAULT-skip; only
    a cached "works" result is lost and the next opt-in run re-probes.
    """
    if result not in {"works", "broken"}:
        return
    root = find_project_root()
    if root is None:
        return
    cp = probe_path(root)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(cp) + ".tmp")
        tmp.write_text(result)
        os.replace(tmp, cp)
    except Exception:
        # silent skip — fail-OPEN
        pass


# ---------------------------------------------------------------------------
# --decide logic + main()
# ---------------------------------------------------------------------------


def decide() -> Tuple[str, str]:
    """Return (verdict, reason) where verdict ∈ {"attempt","skip"} and reason ∈ {"config","probe","default"}.

    Decision tree (proc:P-01):
      1. config "on"  → ("attempt", "config");  config "off" → ("skip", "config")
      2. config unset → read probe sentinel:
         - "works"                       → ("attempt", "probe")
         - "broken" / "unknown" / error  → ("skip",    "default")   (D-04: default-skip, opt-in)
    """
    cv = config_verdict(read_config())
    if cv == "attempt":
        return ("attempt", "config")
    if cv == "skip":
        return ("skip", "config")

    # cv == "unset" → consult the probe sentinel
    if read_probe() == "works":
        return ("attempt", "probe")
    # broken OR unknown OR any error → default-skip (isolation is opt-in)
    return ("skip", "default")


def main(argv=None) -> int:  # type: ignore[assignment]
    """CLI entry point. Returns exit code (always 0 — fail-OPEN discipline)."""
    args_list = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="worktree_isolation",
        description="Worktree-dispatch capability decider + probe-sentinel reader/writer.",
    )
    parser.add_argument("--decide", action="store_true", help="Read config+probe and print verdict.")
    parser.add_argument("--write-probe", action="store_true", help="Write the project probe sentinel.")
    parser.add_argument("--result", choices=["works", "broken"], help="Result to write (--write-probe only).")
    parser.add_argument("--verbose", action="store_true", help="With --decide: print reason on second line.")

    has_decide = "--decide" in args_list

    try:
        ns = parser.parse_args(args_list)
    except SystemExit:
        # argparse error or --help → fail-OPEN for --decide, silent otherwise
        if has_decide:
            print("skip")
        return 0

    if ns.decide:
        try:
            verdict, reason = decide()
            print(verdict)
            if ns.verbose:
                print(reason)
        except Exception:
            # Whole-body fail-OPEN: any exception → skip (never pay the failed attempt)
            print("skip")
        return 0

    if ns.write_probe:
        if ns.result:
            write_probe(ns.result)
        # Always exit 0 (write errors already swallowed in write_probe)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
