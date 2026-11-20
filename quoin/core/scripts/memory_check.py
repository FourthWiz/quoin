#!/usr/bin/env python3
"""quoin/core/scripts/memory_check.py — referential-integrity checker for quoin auto-memory.

Verifies that every Markdown link in MEMORY.md resolves to a sibling fact-file, and
that every sibling fact-file is referenced by at least one MEMORY.md link.

Public API:
  FORWARD_LINKS_ARE_ERRORS: bool — D-S2-2 single point of change (see below)
  LINK_RE: re.Pattern — compiled regex that captures Markdown link targets
  DEFAULT_PATTERN_FILE: str — bare name of the pattern config file (S-3)
  parse_links(memory_md_text: str) -> list[str]
  find_fact_files(memory_dir: Path) -> set[str]
  load_patterns(pattern_file: Path | None) -> dict  # S-3 pattern loader
  classify(name: str, patterns: dict) -> str         # S-3 pattern classifier
  check(memory_dir: Path, allow_forward_links: bool = ..., pattern_file: Path | None = None) -> dict
  check_index_pointers(memory_dir: Path) -> list   # D-S2-4 stub (inert until S-1)
  main(argv: list[str] | None = None) -> int

Exit codes:
  0 — clean (no integrity errors)
  1 — integrity errors found
  2 — usage/IO error (missing MEMORY.md, unreadable dir, bad args)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# D-S2-2: single flip-point for forward-link policy.
# A "forward link" is a MEMORY.md link whose target file does not yet exist.
# When True (default), forward links are treated as errors (reported under
# "dangling"). Flip this constant or use --allow-forward-links per-run to relax.
FORWARD_LINKS_ARE_ERRORS = True  # D-S2-2: single point of change

#: Captures Markdown inline link targets: [text](target).
#: Filters to .md targets; URL targets (containing "://") are skipped by check().
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Files to exclude from the "fact files" set even if they have .md extension.
_INDEX_FILENAMES: frozenset[str] = frozenset({"MEMORY.md", "MEMORY-INDEX.md"})

# S-3: bare name of the pattern config file — resolution is done by callers.
DEFAULT_PATTERN_FILE = "memory-maintenance.yaml"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def parse_links(memory_md_text: str) -> list[str]:
    """Return ordered list of Markdown link targets from *memory_md_text*.

    Includes all targets matched by LINK_RE; URL filtering is done in check().
    Duplicate targets are preserved (order matters for reporting).
    """
    return LINK_RE.findall(memory_md_text)


def find_fact_files(memory_dir: Path) -> set[str]:
    """Return the set of sibling *.md file names in *memory_dir*.

    Excludes MEMORY.md itself and any future MEMORY-INDEX.md (D-S2-4 forward-compat).
    Names only, not full paths.
    """
    return {
        p.name
        for p in memory_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and p.name not in _INDEX_FILENAMES
    }


def load_patterns(pattern_file: "Path | None") -> dict:
    """Load the memory-maintenance pattern config from *pattern_file*.

    Parses the memory-maintenance.yaml YAML subset using a stdlib-only line parser
    (no PyYAML dependency — D-02). Tolerates blank lines and ``#`` comments.

    Supported top-level keys: version, archived, read_only, ignore.
    Each list is a sequence of ``- "glob"`` lines.

    Args:
        pattern_file: Path to the pattern YAML file, or None.

    Returns:
        dict with keys ``archived``, ``read_only``, ``ignore`` (each a list of str).
        Returns all-empty lists when ``pattern_file`` is None, not found, or unreadable.
        A missing or unreadable file is advisory only — never an error.
    """
    empty: dict = {"archived": [], "read_only": [], "ignore": []}
    if pattern_file is None:
        return empty
    try:
        text = Path(pattern_file).read_text(encoding="utf-8")
    except (OSError, IOError):
        return empty

    result: dict = {"archived": [], "read_only": [], "ignore": []}
    current_key: "str | None" = None

    for raw_line in text.splitlines():
        comment_pos = raw_line.find("#")
        line = raw_line[:comment_pos].rstrip() if comment_pos >= 0 else raw_line.rstrip()

        if not line.strip():
            continue

        # Top-level key (no leading whitespace, ends with colon)
        if not line[0].isspace() and line.rstrip().endswith(":"):
            key = line.rstrip().rstrip(":").strip()
            if key in result:
                current_key = key
            else:
                current_key = None  # unknown key — skip items
            continue

        # List item:  - "glob"  or  - glob
        stripped = line.lstrip()
        if stripped.startswith("- ") and current_key is not None:
            value = stripped[2:].strip().strip('"').strip("'")
            if value:
                result[current_key].append(value)

    return result


def classify(name: str, patterns: dict) -> str:
    """Classify a bare file name against the pattern config.

    Precedence (first match wins): ignore > archived > read_only > active.

    Args:
        name: bare file name (no directory component).
        patterns: dict from load_patterns().

    Returns:
        One of: ``"ignore"``, ``"archived"``, ``"read_only"``, ``"active"``.
    """
    for glob in patterns.get("ignore", []):
        if fnmatch.fnmatch(name, glob):
            return "ignore"
    for glob in patterns.get("archived", []):
        if fnmatch.fnmatch(name, glob):
            return "archived"
    for glob in patterns.get("read_only", []):
        if fnmatch.fnmatch(name, glob):
            return "read_only"
    return "active"


def check(
    memory_dir: Path,
    allow_forward_links: bool = not FORWARD_LINKS_ARE_ERRORS,
    pattern_file: "Path | None" = None,
) -> dict:
    """Check referential integrity of the auto-memory directory.

    Returns a dict:
      {
        "dangling":  [str, ...],  # link targets with no matching sibling file
        "orphans":   [str, ...],  # fact-files not referenced by any link
        "forward":   [],          # reserved for S-1/S-3 (always empty in S-2)
        "archived":  [str, ...],  # fact-files classified as archived (info only)
        "read_only": [str, ...],  # fact-files classified as read-only (info only)
        "ok":        bool,        # True iff no errors (see policy below)
      }

    Policy (S-3 / D-05):
      - Orphans are always errors unless the file is ``archived`` or ``read_only``.
      - Dangling links are errors unless ``allow_forward_links=True`` or the link
        target is classified as ``archived``.
      - ``ignore``-classified files are removed from the fact-file set entirely.
      - ``archived`` / ``read_only`` info keys are present but empty when
        ``pattern_file`` is None (backward-compatible).
      - ``forward`` key is always [].
    """
    memory_md = memory_dir / "MEMORY.md"
    text = memory_md.read_text(encoding="utf-8")

    raw_targets = parse_links(text)
    link_targets: list[str] = [
        t for t in raw_targets
        if t.endswith(".md") and "://" not in t
    ]

    fact_files = find_fact_files(memory_dir)

    patterns = load_patterns(pattern_file)

    # ignore: remove from fact-file set entirely (never reported)
    ignored = {f for f in fact_files if classify(f, patterns) == "ignore"}
    fact_files -= ignored

    # archived / read_only: classify remaining files
    archived_set = {f for f in fact_files if classify(f, patterns) == "archived"}
    readonly_set = {f for f in fact_files if classify(f, patterns) == "read_only"}

    linked_set = set(link_targets)

    # Dangling: link target has no matching sibling file AND not archived
    dangling: list[str] = [
        t for t in link_targets
        if t not in fact_files and classify(t, patterns) != "archived"
    ]

    # Orphans: fact-file not referenced by any link
    # CRIT-1 fix (D-05): subtract BOTH archived AND read_only — neither is an orphan error
    orphans: list[str] = sorted((fact_files - linked_set) - archived_set - readonly_set)

    ok = (not orphans) and (allow_forward_links or not dangling)

    return {
        "dangling": dangling,
        "orphans": orphans,
        "forward": [],  # S-1/S-3 forward-compat stub
        "archived": sorted(archived_set),
        "read_only": sorted(readonly_set),
        "ok": ok,
    }


def check_index_pointers(memory_dir: Path) -> list:
    """Stub for S-1 forward-compat (D-S2-4).

    When S-1 ships a MEMORY-INDEX.md, this function will validate that every
    index pointer resolves. Until then, returns [] unconditionally.
    """
    # S-1 hook point: when MEMORY-INDEX.md exists, validate its pointers.
    # For S-2, no S-1 index exists yet — return empty list (inert).
    if not (memory_dir / "MEMORY-INDEX.md").exists():
        return []
    return []  # S-1 not yet implemented


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_check",
        description=(
            "Check referential integrity of a quoin auto-memory directory. "
            "Verifies every MEMORY.md link resolves to a sibling fact-file "
            "and every sibling fact-file is referenced by MEMORY.md. "
            "Report-only: never deletes or rewrites files."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help=(
            "Memory directory to check (must contain MEMORY.md). "
            "Default: $CLAUDE_MEMORY_DIR if set, else current directory "
            "if it contains MEMORY.md."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            'Output result as JSON: {"dangling":[...],"orphans":[...],'
            '"forward":[],"ok":bool} (for scripting/tests).'
        ),
    )
    parser.add_argument(
        "--allow-forward-links",
        action="store_true",
        default=False,
        help=(
            "Treat MEMORY.md links to missing files as warnings, not errors. "
            "Overrides the FORWARD_LINKS_ARE_ERRORS constant for this run."
        ),
    )
    parser.add_argument(
        "--patterns",
        default=None,
        metavar="PATH",
        help=(
            "Path to a memory-maintenance.yaml pattern file. When omitted, "
            f"auto-discovers '{DEFAULT_PATTERN_FILE}' beside MEMORY.md or "
            "under $CLAUDE_MEMORY_DIR; if absent, no patterns are applied."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code (0=clean, 1=errors, 2=usage/IO error)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.path:
        memory_dir = Path(args.path).resolve()
    else:
        import os
        env_dir = os.environ.get("CLAUDE_MEMORY_DIR")
        if env_dir:
            memory_dir = Path(env_dir).resolve()
        elif (Path.cwd() / "MEMORY.md").exists():
            memory_dir = Path.cwd()
        else:
            print(
                "error: no memory directory found. "
                "Pass PATH, set $CLAUDE_MEMORY_DIR, or run from a dir containing MEMORY.md.",
                file=sys.stderr,
            )
            return 2

    memory_md = memory_dir / "MEMORY.md"

    if not memory_dir.is_dir():
        print(f"error: not a directory: {memory_dir}", file=sys.stderr)
        return 2
    if not memory_md.exists():
        print(f"error: MEMORY.md not found in {memory_dir}", file=sys.stderr)
        return 2

    # Resolve pattern file: explicit > auto-discover beside MEMORY.md > env > none
    pattern_path: "Path | None" = None
    if args.patterns:
        pattern_path = Path(args.patterns).resolve()
    else:
        import os
        # Auto-discover: look beside MEMORY.md first
        candidate = memory_dir / DEFAULT_PATTERN_FILE
        if candidate.exists():
            pattern_path = candidate
        else:
            env_dir = os.environ.get("CLAUDE_MEMORY_DIR")
            if env_dir:
                candidate = Path(env_dir).resolve() / DEFAULT_PATTERN_FILE
                if candidate.exists():
                    pattern_path = candidate

    try:
        result = check(
            memory_dir,
            allow_forward_links=args.allow_forward_links,
            pattern_file=pattern_path,
        )
    except (OSError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result))
        return 0 if result["ok"] else 1

    issues = 0

    if result["dangling"]:
        print("Dangling links (MEMORY.md links to missing files):")
        for target in result["dangling"]:
            print(f"  - {target}")
        issues += len(result["dangling"])

    if result["orphans"]:
        print("Orphaned fact-files (not referenced by MEMORY.md):")
        for name in result["orphans"]:
            print(f"  - {name}")
        issues += len(result["orphans"])

    # S-3: info-only sections (never change exit code)
    if result.get("archived"):
        print(f"Archived ({len(result['archived'])}):")
        for name in result["archived"]:
            print(f"  - {name}")

    if result.get("read_only"):
        print(f"Read-only ({len(result['read_only'])}):")
        for name in result["read_only"]:
            print(f"  - {name}")

    if result["ok"]:
        print("Memory integrity OK.")
        return 0
    else:
        print(f"{issues} integrity issue(s) found.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
