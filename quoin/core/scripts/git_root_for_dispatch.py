"""git_root_for_dispatch.py — Resolve the nested git root for worktree dispatch.

Used by the WorktreeCreate hook to determine which nested git repo should receive
worktree isolation when the project root is not a git repository.

Exit codes:
  0  — single nested repo resolved; path printed on stdout
  1  — no nested repo found or cwd is already a git root; no output
  2  — multiple nested repos found (multi-repo plan); paths printed on stderr
  3  — malformed/missing input, unreadable plan file, or stale sidecar

CLI modes:
  --sidecar PATH            Read dispatch hint JSON; dispatch internally to
                            plan-mode or cwd-scan-only mode.
  --plan PATH --cwd PATH    Plan-mode: scan plan file for repo references.
  --cwd-scan-only --cwd PATH  Scan immediate children of cwd for .git dirs.

Environment:
  QUOIN_DISABLE_DISPATCH_CWD=1  → exit 1 immediately (opt-out).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ── helpers ──────────────────────────────────────────────────────────────────


def _cwd_is_git_root(cwd: Path) -> bool:
    """Return True if cwd contains a .git entry (directory or file for worktrees)."""
    return (cwd / ".git").exists()


def _walk_to_git_root(start: Path, boundary: Path) -> Path | None:
    """Walk up from start.parent looking for a .git directory.

    Stops when we reach OR pass boundary (inclusive — boundary is checked too).
    Returns the absolute path containing .git, or None.
    """
    cur = start
    while True:
        if (cur / ".git").exists():
            return cur
        if cur == boundary:
            break
        parent = cur.parent
        if parent == cur:
            # filesystem root reached without hitting boundary
            break
        cur = parent
    return None


# ── v3 format detection ───────────────────────────────────────────────────────
# Rule 5.7.1: file is v3 iff the first 50 lines after the closing '---' of the
# YAML frontmatter contain a heading matching ^## For human\s*$


_FOR_HUMAN_RE = re.compile(r"^## For human\s*$", re.MULTILINE)


def _is_v3_format(text: str) -> bool:
    """Detect v3 format per rule 5.7.1 (string comparison only — no LLM)."""
    if not text.startswith("---"):
        return False
    close = text.find("\n---", 3)
    if close == -1:
        return False
    after = text[close + 4:]  # text after the closing '---\n'
    lines = after.split("\n", 50)[:50]
    for line in lines:
        if _FOR_HUMAN_RE.match(line):
            return True
    return False


def _extract_v3_body(text: str) -> str:
    """Extract ## Tasks and ## Procedures section bodies from v3 plan text."""
    sections = []
    for header in ("## Tasks", "## Procedures"):
        idx = text.find(header)
        if idx == -1:
            continue
        # Find the next ## at the same level
        next_h2 = text.find("\n## ", idx + len(header))
        if next_h2 == -1:
            sections.append(text[idx:])
        else:
            sections.append(text[idx:next_h2])
    return "\n".join(sections)


# ── backtick path extractor ────────────────────────────────────────────────────

# Match backtick-delimited tokens with at least one / and a known code extension.
_CODE_EXT = frozenset({
    ".py", ".sh", ".md", ".json", ".yaml", ".yml", ".txt", ".toml",
    ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".cpp", ".c", ".h", ".cs", ".swift", ".kt", ".scala", ".sql",
    ".html", ".css", ".scss", ".less", ".vue",
})

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def _extract_candidate_paths(text: str) -> list[str]:
    """Return relative file paths extracted from backtick tokens in text."""
    candidates = []
    for m in _BACKTICK_RE.finditer(text):
        token = m.group(1).strip()
        if "/" not in token:
            continue
        if token.startswith("/"):
            continue
        # Skip workflow-artifacts paths
        if token.startswith(".workflow_artifacts/"):
            continue
        # Skip __QUOIN_HOME__ and ~ references
        if token.startswith("~") or token.startswith("__QUOIN_HOME__"):
            continue
        suffix = Path(token).suffix.lower()
        if suffix not in _CODE_EXT:
            continue
        candidates.append(token)
    return candidates


# ── resolution algorithms ─────────────────────────────────────────────────────


def _resolve_plan_mode(plan_path: Path, cwd: Path) -> int:
    """proc:R-01 — plan-mode resolution.

    Scans the plan file for backtick-quoted paths, walks up to find .git roots,
    deduplicates, and exits with 0/1/2.
    """
    # STEP 1: opt-out check
    if os.environ.get("QUOIN_DISABLE_DISPATCH_CWD") == "1":
        return 1

    # STEP 2: if cwd is itself a git root, no nested-git problem
    if _cwd_is_git_root(cwd):
        return 1

    # STEP 3: read plan file
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"git_root_for_dispatch: cannot read plan {plan_path}: {e}", file=sys.stderr)
        return 3

    # STEP 4: detect v3 vs v2
    if _is_v3_format(text):
        scan_text = _extract_v3_body(text)
    else:
        # v2: exit 1 (no v2 plans expected in production; plan is not v3)
        return 1

    # STEP 5+6: scan for backtick paths, walk to git root
    repos: dict[Path, str] = {}  # git_root_path -> first source path
    for rel_path_str in _extract_candidate_paths(scan_text):
        abs_path = (cwd / rel_path_str).resolve()
        if not abs_path.exists():
            continue
        git_root = _walk_to_git_root(abs_path.parent, cwd)
        if git_root is None:
            # Also check cwd itself (boundary inclusive)
            git_root = _walk_to_git_root(cwd, cwd)
        if git_root is not None and git_root != cwd:
            # Only count repos that are NESTED (not cwd itself)
            if git_root not in repos:
                repos[git_root] = rel_path_str

    # STEP 7: deduplicate by git_root_path (already done via dict)

    # STEP 8: output
    sorted_repos = sorted(repos.keys(), key=str)
    if len(sorted_repos) == 0:
        return 1
    elif len(sorted_repos) == 1:
        print(str(sorted_repos[0]))
        return 0
    else:
        for repo in sorted_repos:
            print(str(repo), file=sys.stderr)
        return 2


def _resolve_cwd_scan_only(cwd: Path) -> int:
    """proc:R-01b — cwd-scan-only mode.

    Scans immediate children of cwd (depth 1) for .git directories.
    """
    # STEP 1: opt-out check
    if os.environ.get("QUOIN_DISABLE_DISPATCH_CWD") == "1":
        return 1

    # STEP 2: if cwd is itself a git root, no nested-git problem
    if _cwd_is_git_root(cwd):
        return 1

    # STEP 3: scan immediate children
    found = []
    try:
        for child in sorted(cwd.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                found.append(child.resolve())
    except OSError:
        return 1

    # STEP 4: output
    if len(found) == 0:
        return 1
    elif len(found) == 1:
        print(str(found[0]))
        return 0
    else:
        for repo in found:
            print(str(repo), file=sys.stderr)
        return 2


def _resolve_sidecar_mode(sidecar_path: Path) -> int:
    """proc:R-01c — sidecar mode.

    Reads the dispatch hint JSON, rejects stale sidecars, dispatches internally
    to plan-mode or cwd-scan-only mode.
    """
    # STEP 1: read and parse JSON
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"git_root_for_dispatch: cannot read sidecar {sidecar_path}: {e}", file=sys.stderr)
        return 3

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"git_root_for_dispatch: malformed sidecar JSON: {e}", file=sys.stderr)
        return 3

    # STEP 2: check mtime (reject sidecars > 60s old)
    try:
        mtime = sidecar_path.stat().st_mtime
        if time.time() - mtime > 60:
            print("git_root_for_dispatch: sidecar is stale (mtime > 60s)", file=sys.stderr)
            return 3
    except OSError:
        return 3

    # STEP 3: extract fields
    project_root_str = data.get("project_root")
    plan_path_str = data.get("plan_path")

    if not project_root_str:
        print("git_root_for_dispatch: sidecar missing 'project_root'", file=sys.stderr)
        return 3

    cwd = Path(project_root_str)

    # STEP 4: dispatch to plan-mode or cwd-scan-only
    if plan_path_str:
        plan_path = Path(plan_path_str)
        return _resolve_plan_mode(plan_path, cwd)
    else:
        return _resolve_cwd_scan_only(cwd)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the nested git root for worktree dispatch.",
        epilog=(
            "Exit codes: "
            "0=single repo resolved (path on stdout); "
            "1=no nested repo or cwd is git root; "
            "2=multi-repo (paths on stderr); "
            "3=input error/stale sidecar"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sidecar", metavar="PATH", help="Read dispatch hint JSON from PATH")
    group.add_argument("--plan", metavar="PATH", help="Plan file to scan for repo references")
    group.add_argument(
        "--cwd-scan-only",
        action="store_true",
        help="Scan immediate children of --cwd for .git dirs (no plan required)",
    )

    parser.add_argument(
        "--cwd",
        metavar="PATH",
        default=os.getcwd(),
        help="Project root directory (default: os.getcwd())",
    )

    args = parser.parse_args()

    # opt-out at top level
    if os.environ.get("QUOIN_DISABLE_DISPATCH_CWD") == "1":
        return 1

    if args.sidecar:
        return _resolve_sidecar_mode(Path(args.sidecar))
    elif args.plan:
        return _resolve_plan_mode(Path(args.plan), Path(args.cwd))
    else:  # --cwd-scan-only
        return _resolve_cwd_scan_only(Path(args.cwd))


if __name__ == "__main__":
    sys.exit(main())
