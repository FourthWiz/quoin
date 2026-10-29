#!/usr/bin/env python3
"""
build_preambles.py — Preamble builder for subagent prompt-cache warming.

Generates preamble.md files for 9 spawn-target skills under quoin/adapters/claude/skills/<skill>/.
Each full preamble contains:
  - YAML frontmatter (path, kind, source_files, source_hashes, generated_at, generated_by, total_bytes)
  - [format-kit-§3-slice] marker + the marker-anchored §3 slice of format-kit.md
    (the lines strictly between the '---' delimiters bracketing the
    '## §3 Pick rules for ambiguous content' heading — self-corrects if
    format-kit.md's line numbers shift; see read_format_kit_slice())
  - [glossary] marker + verbatim glossary.md content

Gate skill gets a frontmatter-only stub (~200 bytes) for uniformity.

Exit codes:
  0  success
  3  PREAMBLE OVERSIZE: a generated full preamble exceeds 6144 bytes
  4  MISSING SOURCE: a required source file not found
  5  EMPTY SPAWN_TARGETS: the target dict is empty
  6  --dry-run and --check are mutually exclusive
  7  --check: stale preamble(s) detected (source hash mismatch)
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys

# Note: pyyaml is lazy-imported inside run_check() only. The default build path
# (no flags) emits frontmatter via stdlib string templating, so a fresh
# `bash install.sh` works on systems without pyyaml installed.

# Single source of truth for spawn-target membership.
# "full" = format-kit §3 slice + glossary; "stub" = frontmatter-only note.
SPAWN_TARGETS = {
    "critic": "full",
    "revise": "full",
    "revise-fast": "full",
    "plan": "full",
    "review": "full",
    "architect": "full",
    "gate": "stub",
    "specify": "stub",
    "enrich": "stub",
}

PREAMBLE_SIZE_BUDGET = 6144  # bytes

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent  # project root (parent of quoin/)
QUOIN_DIR = SCRIPT_DIR.parent          # quoin/
SOURCE_FORMAT_KIT = QUOIN_DIR / "memory" / "format-kit.md"
SOURCE_GLOSSARY = QUOIN_DIR / "memory" / "glossary.md"

# Relative paths from REPO_ROOT for frontmatter source_files field
# (use quoin-relative paths for readability, matching audit doc conventions)
SOURCE_REL_FORMAT_KIT = "quoin/memory/format-kit.md"
SOURCE_REL_GLOSSARY = "quoin/memory/glossary.md"

STUB_NOTE = (
    "# Stub preamble — gate has no boilerplate bootstrap reads; "
    "this file is kept for uniformity per spawn-bootstrap audit doc.\n"
)


def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy (D-06) — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def git_hash_object(path: pathlib.Path) -> str:
    """Return the git hash-object SHA for a file (NOT the index — working tree)."""
    try:
        result = subprocess.run(
            ["git", "hash-object", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=_subprocess_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"git hash-object timed out after {_subprocess_timeout()}s for {path} "
            f"(override with QUOIN_SUBPROCESS_TIMEOUT)"
        ) from exc
    return result.stdout.strip()


_SECTION3_HEADING_RE = re.compile(r"^## §3 Pick rules for ambiguous content")


def read_format_kit_slice() -> str:
    """Extract the §3 pick-rules slice from format-kit.md via marker anchoring.

    Finds the line matching '## §3 Pick rules for ambiguous content', then scans
    backward for the nearest preceding bare '---' line and forward for the
    nearest following bare '---' line. The slice is every line strictly between
    those two delimiters. This reproduces the historical fixed-window output
    byte-for-byte (format-kit.md's §3 heading is bracketed by bare '---' lines
    immediately above and below its surrounding table), but self-corrects for
    any future shift in format-kit.md instead of requiring a manual re-count
    each time the file changes near that section (closes the MAJ-1 risk class
    from IVG-128, not just one instance of it). Exits with a MISSING MARKER
    error rather than silently returning an empty or wrong slice if no
    '---'-delimited match is found.
    """
    if not SOURCE_FORMAT_KIT.exists():
        print(f"MISSING SOURCE: {SOURCE_REL_FORMAT_KIT}", file=sys.stderr)
        sys.exit(4)
    lines = SOURCE_FORMAT_KIT.read_text(encoding="utf-8").splitlines(keepends=True)
    heading_idx = None
    for i, line in enumerate(lines):
        if _SECTION3_HEADING_RE.match(line.rstrip("\n")):
            heading_idx = i
            break
    if heading_idx is None:
        print(
            f"MISSING MARKER: '## §3 Pick rules for ambiguous content' heading "
            f"not found in {SOURCE_REL_FORMAT_KIT}",
            file=sys.stderr,
        )
        sys.exit(4)
    start_idx = None
    for i in range(heading_idx - 1, -1, -1):
        if lines[i].rstrip("\n") == "---":
            start_idx = i
            break
    end_idx = None
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            end_idx = i
            break
    if start_idx is None or end_idx is None:
        print(
            f"MISSING MARKER: '---' delimiters bracketing the §3 heading not "
            f"found in {SOURCE_REL_FORMAT_KIT}",
            file=sys.stderr,
        )
        sys.exit(4)
    slice_lines = lines[start_idx + 1 : end_idx]
    return "".join(slice_lines)


def read_glossary() -> str:
    """Read glossary.md verbatim."""
    if not SOURCE_GLOSSARY.exists():
        print(f"MISSING SOURCE: {SOURCE_REL_GLOSSARY}", file=sys.stderr)
        sys.exit(4)
    return SOURCE_GLOSSARY.read_text(encoding="utf-8")


def compose_frontmatter(skill: str, kind: str, source_hashes: dict, total_bytes: int) -> str:
    """
    Compose YAML frontmatter as a string via stdlib templating (no pyyaml required).

    Output is deterministic: keys are emitted in alphabetical order; lists and maps
    use canonical YAML block style. Field set: generated_by, kind, path,
    source_files (list), source_hashes (map), total_bytes.

    `generated_at` is intentionally omitted (Stage 2-alt /review MIN-5/MIN-6 fix:
    drift signal lives in source_hashes, not in a wall-clock timestamp).
    """
    if kind == "full":
        source_files = [SOURCE_REL_FORMAT_KIT, SOURCE_REL_GLOSSARY]
    else:
        source_files = []

    lines = ["---"]
    lines.append("generated_by: build_preambles.py")
    lines.append(f"kind: {kind}")
    lines.append(f"path: __QUOIN_HOME__/skills/{skill}/preamble.md")
    if source_files:
        lines.append("source_files:")
        for path in source_files:
            lines.append(f"- {path}")
    else:
        lines.append("source_files: []")
    if source_hashes:
        lines.append("source_hashes:")
        for path in sorted(source_hashes):
            lines.append(f"  {path}: {source_hashes[path]}")
    else:
        lines.append("source_hashes: {}")
    lines.append(f"total_bytes: {total_bytes}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def build_preamble_body(kind: str, fmt_slice: str, glossary: str) -> str:
    """Build the body content (without frontmatter)."""
    if kind == "full":
        return f"[format-kit-§3-slice]\n{fmt_slice}\n[glossary]\n{glossary}"
    else:
        return STUB_NOTE


def compute_source_hashes(kind: str) -> dict:
    """Compute git hash-object for each source file."""
    if kind != "full":
        return {}
    hashes = {}
    for rel_path, abs_path in [
        (SOURCE_REL_FORMAT_KIT, SOURCE_FORMAT_KIT),
        (SOURCE_REL_GLOSSARY, SOURCE_GLOSSARY),
    ]:
        if not abs_path.exists():
            print(f"MISSING SOURCE: {rel_path}", file=sys.stderr)
            sys.exit(4)
        hashes[rel_path] = git_hash_object(abs_path)
    return hashes


def run_check() -> int:
    """
    --check mode: verify each preamble's source_hashes against current git hash-object.
    Returns exit code 0 (fresh) or 7 (stale).

    pyyaml is lazy-imported here because --check is the only path that needs to
    PARSE existing frontmatter. The default build path emits frontmatter via
    stdlib templating (compose_frontmatter), so `bash install.sh` works on
    systems without pyyaml.
    """
    try:
        import yaml  # lazy: only --check needs a YAML parser
    except ImportError:
        print(
            "build_preambles.py --check requires pyyaml. Install with: pip install pyyaml",
            file=sys.stderr,
        )
        return 7

    stale = []
    for skill, kind in SPAWN_TARGETS.items():
        preamble_path = QUOIN_DIR / "adapters" / "claude" / "skills" / skill / "preamble.md"
        if not preamble_path.exists():
            stale.append(f"{skill}: preamble.md missing")
            continue
        content = preamble_path.read_text(encoding="utf-8")
        # Parse frontmatter
        if not content.startswith("---\n"):
            stale.append(f"{skill}: malformed frontmatter (no opening ---)")
            continue
        end = content.index("\n---\n", 4)
        fm_text = content[4:end]
        try:
            fm = yaml.safe_load(fm_text)
        except yaml.YAMLError as e:
            stale.append(f"{skill}: YAML parse error: {e}")
            continue
        source_hashes = fm.get("source_hashes") or {}
        # Stub: vacuously fresh
        if kind == "stub" or not source_hashes:
            continue
        for rel_path, expected_sha in source_hashes.items():
            # Reconstruct abs path from rel_path (relative to project root = QUOIN_DIR.parent)
            abs_path = QUOIN_DIR.parent / rel_path
            if not abs_path.exists():
                stale.append(f"{skill}: source {rel_path} not found")
                continue
            current_sha = git_hash_object(abs_path)
            if current_sha != expected_sha:
                print(
                    f"Preamble for {skill} is stale: source {rel_path} changed "
                    f"(preamble has {expected_sha}, current is {current_sha}). "
                    f"Run: python3 quoin/scripts/build_preambles.py (or bash install.sh --dev), "
                    f"then commit the regenerated quoin/adapters/claude/skills/*/preamble.md",
                    file=sys.stderr,
                )
                stale.append(f"{skill}: {rel_path} changed")
    if stale:
        return 7
    return 0


def main() -> int:
    if not SPAWN_TARGETS:
        print("EMPTY SPAWN_TARGETS", file=sys.stderr)
        return 5

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  success
  3  PREAMBLE OVERSIZE: generated full preamble exceeds 6144 bytes
  4  MISSING SOURCE: required source file not found
  5  EMPTY SPAWN_TARGETS
  6  --dry-run and --check are mutually exclusive
  7  --check: stale preamble(s) detected

Spawn targets: """ + ", ".join(SPAWN_TARGETS.keys()),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write preamble content to stdout (=== <skill> === sections) instead of disk",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run freshness check (verify source_hashes in preambles match current git hash-object); exits 7 if stale",
    )
    args = parser.parse_args()

    if args.dry_run and args.check:
        print("--dry-run and --check are mutually exclusive", file=sys.stderr)
        return 6

    if args.check:
        return run_check()

    # Read source files once (fail fast if missing)
    fmt_slice = read_format_kit_slice()
    glossary = read_glossary()

    # Compute hashes once (same for all full targets)
    full_hashes = compute_source_hashes("full")

    for skill, kind in SPAWN_TARGETS.items():
        body = build_preamble_body(kind, fmt_slice, glossary)
        source_hashes = full_hashes if kind == "full" else {}
        total_bytes = len(body.encode("utf-8"))

        # Size budget gate (full preambles only — stub is tiny)
        if kind == "full" and total_bytes > PREAMBLE_SIZE_BUDGET:
            print(
                f"PREAMBLE OVERSIZE: {skill}/preamble.md is {total_bytes} bytes (budget: {PREAMBLE_SIZE_BUDGET} bytes)",
                file=sys.stderr,
            )
            return 3

        # Compose frontmatter (needs total_bytes)
        frontmatter = compose_frontmatter(skill, kind, source_hashes, total_bytes)
        full_content = frontmatter + "\n" + body

        if args.dry_run:
            print(f"=== {skill} ===")
            print(full_content)
            continue

        # Atomic write: .tmp + os.replace()
        dest_dir = QUOIN_DIR / "adapters" / "claude" / "skills" / skill
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "preamble.md"
        tmp_path = dest_dir / "preamble.md.tmp"
        tmp_path.write_text(full_content, encoding="utf-8")
        os.replace(tmp_path, dest_path)
        print(f"  wrote {dest_path.relative_to(QUOIN_DIR.parent)} ({len(full_content.encode('utf-8'))} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
