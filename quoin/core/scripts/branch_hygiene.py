#!/usr/bin/env python3
"""Portable core implementation of branch hygiene checks.

Public API:
  check_repo(repo_path: Path) -> RepoResult
  discover_repos(project_root: Path) -> list[Path]
  main(argv: list[str] | None = None) -> int

Exit codes:
  0 — no repo has task commits on a protected branch (clean)
  1 — at least one repo has has_task_commits=True (actionable violation)
  2 — malformed/missing CLI input (argparse error)
  3 — git unavailable / discovery returned empty / all repos errored (fail-OPEN)

Env:
  QUOIN_PROTECTED_BRANCHES — csv, default "main,master"
  QUOIN_DISABLE_BRANCH_HYGIENE=1 — exit 0 immediately (global opt-out)
  QUOIN_SUBPROCESS_TIMEOUT — seconds, default 30; bounds every git subprocess run
      by this module (see _subprocess_timeout()).
  QUOIN_DISABLE_CHILD_REPO_SCAN=1 — skip the depth-1 child-.git discovery scan in
      discover_repos(); single-repo view only. Distinct from QUOIN_DISABLE_DISPATCH_CWD
      (a different concern scoped to dispatch-site detection, see D-08).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RepoResult:
    repo: str                  # absolute path as string
    current_branch: str | None # None when detached HEAD
    on_protected: bool         # branch in protected set
    commits_ahead: int         # count of commits HEAD is ahead of upstream; 0 when no upstream
    has_task_commits: bool     # on_protected AND commits_ahead > 0 — the gate-FAIL signal
    head_sha: str | None       # valid SHA even when detached; None only if rev-parse fails
    upstream: str | None       # tracking branch ref, or None
    dirty: bool                # working-tree dirty (best-effort)
    error: str | None          # first subprocess error encountered, or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "current_branch": self.current_branch,
            "on_protected": self.on_protected,
            "commits_ahead": self.commits_ahead,
            "has_task_commits": self.has_task_commits,
            "head_sha": self.head_sha,
            "upstream": self.upstream,
            "dirty": self.dirty,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy (D-06) — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _run(args: list[str]) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_subprocess_timeout(),
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except Exception as exc:  # noqa: BLE001
        return "", str(exc), 1


def _protected_set() -> set[str]:
    raw = os.environ.get("QUOIN_PROTECTED_BRANCHES", "main,master")
    return {b.strip() for b in raw.split(",") if b.strip()}


def check_repo(repo_path: Path) -> RepoResult:
    """Check a single git repository for branch hygiene violations.

    Runs only read-only git plumbing — never modifies the repo.

    MIN-3: head_sha is populated even when detached (rev-parse HEAD resolves
    for detached HEAD); head_sha is None only if rev-parse itself fails.
    current_branch is None for detached HEAD; these are independent fields.

    MIN-2: rev-list --count @{u}..HEAD is NEVER invoked when upstream is None.
    The guard is lexical (inside the `if upstream:` block), not just runtime.
    """
    repo = str(repo_path.resolve())
    error: str | None = None

    # --- current branch ---
    stdout, stderr, rc = _run(["git", "-C", repo, "branch", "--show-current"])
    if rc != 0:
        error = stderr or f"git branch --show-current failed (rc={rc})"
        # Cannot determine branch — safe-default: treat as not on protected
        return RepoResult(
            repo=repo,
            current_branch=None,
            on_protected=False,
            commits_ahead=0,
            has_task_commits=False,
            head_sha=None,
            upstream=None,
            dirty=False,
            error=error,
        )
    current_branch: str | None = stdout if stdout else None  # empty string = detached HEAD

    protected_set = _protected_set()
    on_protected = (current_branch is not None and current_branch in protected_set)

    # --- head_sha (MIN-3: always attempt; valid even when detached) ---
    sha_stdout, sha_stderr, sha_rc = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    head_sha: str | None = sha_stdout if sha_rc == 0 and sha_stdout else None
    if sha_rc != 0 and not error:
        error = sha_stderr or f"git rev-parse HEAD failed (rc={sha_rc})"

    # --- upstream (MIN-2: guard rev-list strictly inside this block) ---
    upstream: str | None = None
    commits_ahead: int = 0

    ups_stdout, ups_stderr, ups_rc = _run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    if ups_rc == 0 and ups_stdout:
        upstream = ups_stdout
        # MIN-2: rev-list ONLY runs when upstream is not None (lexically inside this block)
        if upstream:
            rev_stdout, rev_stderr, rev_rc = _run(
                ["git", "-C", repo, "rev-list", "--count", "@{u}..HEAD"]
            )
            if rev_rc == 0:
                try:
                    commits_ahead = int(rev_stdout)
                except ValueError:
                    if not error:
                        error = f"rev-list --count returned non-integer: {rev_stdout!r}"
            else:
                if not error:
                    error = rev_stderr or f"git rev-list --count @{{u}}..HEAD failed (rc={rev_rc})"

    has_task_commits = on_protected and commits_ahead > 0

    # --- dirty (best-effort) ---
    dirty_stdout, _, dirty_rc = _run(["git", "-C", repo, "status", "--porcelain"])
    dirty = dirty_rc == 0 and bool(dirty_stdout)

    return RepoResult(
        repo=repo,
        current_branch=current_branch,
        on_protected=on_protected,
        commits_ahead=commits_ahead,
        has_task_commits=has_task_commits,
        head_sha=head_sha,
        upstream=upstream,
        dirty=dirty,
        error=error,
    )


# Directories to exclude from depth-1 scan (D-03)
_EXCLUDE_NAMES: frozenset[str] = frozenset({
    ".workflow_artifacts",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
})


def discover_repos(project_root: Path) -> list[Path]:
    """Discover git repositories under project_root (depth-1 scan + cwd-as-repo).

    FRESH implementation — does NOT reuse _resolve_cwd_scan_only from
    git_root_for_dispatch.py (see D-03 in the plan; that function is
    structurally incompatible: short-circuits on cwd-as-repo case).

    Behavior:
    - If project_root/.git exists, include project_root.resolve() (cwd-as-repo).
    - Iterate depth-1 children; for each is_dir() child whose name is not in
      _EXCLUDE_NAMES AND (child/.git).exists(), include child.resolve().
    - Dedup by canonical string (.resolve()) so cwd-as-repo + depth-1 self-match
      collapse to one entry (MIN-1).
    - Returns sorted, deduplicated absolute Path list.
    - On OSError, returns [] (never raises).

    D-08 / T-08: when QUOIN_DISABLE_CHILD_REPO_SCAN=1, the depth-1 per-child
    .git stat loop is skipped entirely and this returns a single-repo view
    ([root] if root/.git exists, else []). This is a DISTINCT knob from
    QUOIN_DISABLE_DISPATCH_CWD (git_root_for_dispatch._resolve_cwd_scan_only) —
    disabling dispatch-cwd scanning must never silently narrow /gate's
    multi-repo sibling discovery. Default (unset) is byte-identical to the
    pre-existing behavior below.
    """
    if os.environ.get("QUOIN_DISABLE_CHILD_REPO_SCAN") == "1":
        try:
            root = project_root.resolve()
        except OSError:
            return []
        return [root] if (root / ".git").exists() else []

    repos: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        canonical = str(p.resolve())
        if canonical not in seen:
            seen.add(canonical)
            repos.append(p.resolve())

    try:
        root = project_root.resolve()

        # cwd-as-repo pass (AC-5: include project_root itself if it has .git)
        if (root / ".git").exists():
            _add(root)

        try:
            children = sorted(root.iterdir())
        except OSError:
            children = []

        for child in children:
            if child.name in _EXCLUDE_NAMES:
                continue
            if child.is_dir() and (child / ".git").exists():
                _add(child)

    except OSError:
        return []

    return sorted(repos, key=lambda p: str(p))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _check_one(repo_path: Path) -> tuple[RepoResult, int]:
    """Check a single repo and return (result, exit_code)."""
    result = check_repo(repo_path)
    if result.error and not result.head_sha and not result.current_branch:
        # All fields at safe-default and an error — likely not a git repo
        return result, 3
    if result.has_task_commits:
        return result, 1
    return result, 0


def _check_project(project_root: Path) -> tuple[list[RepoResult], int]:
    """Discover repos under project_root, check each, return (results, exit_code)."""
    repos = discover_repos(project_root)
    if not repos:
        return [], 3

    results: list[RepoResult] = []
    for repo in repos:
        result = check_repo(repo)
        results.append(result)

    any_on_protected = any(r.on_protected for r in results)
    any_task_commits = any(r.has_task_commits for r in results)

    if any_task_commits:
        return results, 1
    # If every result has an error and none are clean, be conservative
    all_errors = all(r.error is not None for r in results)
    if all_errors:
        return results, 3
    return results, 0


def _format_text(result: RepoResult) -> str:
    """One-line text representation per result.

    Format: {flag} {repo_abspath} branch={current_branch_or_DETACHED} ahead={commits_ahead}
    """
    branch_str = result.current_branch if result.current_branch is not None else "DETACHED"
    if result.has_task_commits:
        flag = "VIOLATION"
    elif result.on_protected:
        flag = "on-protected"
    else:
        flag = "ok"
    return f"{flag} {result.repo} branch={branch_str} ahead={result.commits_ahead}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns exit code:
      0 — clean
      1 — task commits on protected branch (violation)
      2 — argparse error
      3 — git undeterminable / discovery empty
    """
    # Env opt-out
    if os.environ.get("QUOIN_DISABLE_BRANCH_HYGIENE", "").strip() == "1":
        print(json.dumps({"disabled": True}))
        return 0

    parser = argparse.ArgumentParser(
        description="Check git repositories for branch hygiene violations.",
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--repo",
        type=Path,
        metavar="PATH",
        help="Check a single repository at PATH.",
    )
    mode.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        default=None,
        help="Discover and check all repos under PATH (default: cwd).",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    fmt = args.format

    if args.repo is not None:
        result, exit_code = _check_one(args.repo)
        if fmt == "text":
            print(_format_text(result))
        else:
            print(json.dumps(result.to_dict(), indent=2))
        return exit_code

    else:
        # --project-root mode
        root = args.project_root if args.project_root is not None else Path.cwd()
        results, exit_code = _check_project(root)
        any_on_protected = any(r.on_protected for r in results)
        any_task_commits = any(r.has_task_commits for r in results)

        if fmt == "text":
            for r in results:
                print(_format_text(r))
        else:
            print(json.dumps(
                {
                    "repos": [r.to_dict() for r in results],
                    "any_on_protected": any_on_protected,
                    "any_task_commits": any_task_commits,
                },
                indent=2,
            ))
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
