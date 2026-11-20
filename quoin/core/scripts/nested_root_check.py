"""nested_root_check.py — detect accidental nested/duplicate .workflow_artifacts roots.

A quoin project is assumed to have exactly ONE .workflow_artifacts/ root, at the
project root. Subproject directories that create their own .workflow_artifacts/
break /checkpoint --restore and artifact discovery (IVG-119). This scanner walks a
project root and flags every directory literally named `.workflow_artifacts` whose
parent is NOT the project root.

Portable-core: stdlib only, no `import quoin`. The one same-dir core import
(`path_resolve._find_nested_ancestor`) is used ONLY for the "project_root is itself
nested" advisory in text output.

Exit codes (CLI):
  0 — clean, or globally disabled (QUOIN_DISABLE_NESTED_ROOT_CHECK=1)
  1 — one or more accidental nested roots found
  2 — argparse / invocation error
  3 — undeterminable (fail-OPEN: unreadable/garbage tree)
"""

import argparse
import json
import os
import sys
from pathlib import Path

from path_resolve import _find_nested_ancestor


# Copied (NOT imported) from branch_hygiene._EXCLUDE_NAMES minus `.workflow_artifacts`
# — that name is exactly what we hunt for, so it must NOT be in the descent-prune set.
# Copying a trivial literal avoids a cross-file coupling (D-03).
# `.workspaces` is an IVG-158 addition BEYOND that copy: a workspace's per-repo
# worktree has a `.git` FILE (not a pruned `.git` dir), so without pruning
# `.workspaces` the full-recursive os.walk descends into every worktree of every
# repo (perf blow-up) and could false-positive on any stray nested
# `.workflow_artifacts` a worktree happens to contain (R-09).
_PRUNE_NAMES: frozenset = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
    ".workspaces",
})

# Test-fixture subtrees are pruned unconditionally: a `.workflow_artifacts` under a
# `dev/tests/fixtures` tree is a synthetic test asset, never a real project root.
_FIXTURES_SUFFIX = os.path.join("dev", "tests", "fixtures")

_MARKER = ".quoin-nested-ok"
_ENV_EXCLUDE = "QUOIN_NESTED_ROOT_EXCLUDE"
_ENV_DISABLE = "QUOIN_DISABLE_NESTED_ROOT_CHECK"


def _env_excludes() -> list:
    raw = os.environ.get(_ENV_EXCLUDE, "")
    return [s for s in (part.strip() for part in raw.split(",")) if s]


def find_descendant_roots(project_root, *, excludes=None, include_finalized=True) -> list:
    """Return sorted list of Paths to accidental nested `.workflow_artifacts` roots.

    A finding is any directory literally named `.workflow_artifacts` whose parent is
    NOT `project_root`. Excluded from findings:
      - anything under a `dev/tests/fixtures` subtree (pruned during descent)
      - any path containing a substring in `excludes` (env-configurable)
      - a root carrying a `.quoin-nested-ok` marker file (blessed)
      - findings under `finalized/` when `include_finalized` is False

    Fail-OPEN: os errors on a subtree skip that subtree; a total walk failure
    propagates as OSError for the caller to map to exit 3.
    """
    project_root = Path(project_root).resolve()
    excludes = list(excludes) if excludes is not None else []
    excludes = excludes + _env_excludes()

    findings = []

    def _on_error(_exc):
        # Per-subtree OSError → skip silently (fail-OPEN).
        return None

    for dirpath, dirnames, _filenames in os.walk(project_root, topdown=True, onerror=_on_error):
        dpath = Path(dirpath)

        # A directory literally named `.workflow_artifacts` whose PARENT is not the
        # project root is an accidental nested root. The canonical root's parent IS
        # the project root, so it is never flagged.
        if dpath.name == ".workflow_artifacts" and dpath.parent != project_root:
            if _keep_finding(dpath, excludes=excludes, include_finalized=include_finalized):
                findings.append(dpath)

        # Prune subtrees we must not descend into (perf + fixture correctness).
        pruned = []
        for name in dirnames:
            child = dpath / name
            if name in _PRUNE_NAMES:
                continue
            if name == "fixtures" and str(child).endswith(_FIXTURES_SUFFIX):
                continue
            pruned.append(name)
        dirnames[:] = pruned

    return sorted(set(findings), key=lambda p: str(p))


def _keep_finding(nested: Path, *, excludes: list, include_finalized: bool) -> bool:
    """True if a nested `.workflow_artifacts` path should be reported as a finding."""
    s = str(nested)
    if any(sub in s for sub in excludes):
        return False
    if not include_finalized and (os.sep + "finalized" + os.sep) in s:
        return False
    try:
        if (nested / _MARKER).exists():
            return False
    except OSError:
        # Unreadable marker check → do not suppress (report the finding).
        pass
    return True


def _corrective_action(nested: Path, project_root: Path) -> str:
    canonical = project_root / ".workflow_artifacts"
    return (
        f"canonical root is {canonical}; move/remove {nested}, "
        f"or bless with a {_MARKER} marker file / {_ENV_EXCLUDE}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nested_root_check.py",
        description=(
            "Detect accidental nested/duplicate .workflow_artifacts roots "
            "under a project root (IVG-119)."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=None,
        metavar="PATH",
        help="Project root to scan (default: cwd).",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=("text", "json"),
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--exclude-finalized",
        action="store_true",
        default=False,
        help="Suppress findings under a finalized/ archive subtree.",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if os.environ.get(_ENV_DISABLE) == "1":
        # Global opt-out.
        if args.format == "json":
            print(json.dumps({"disabled": True, "findings": []}))
        else:
            print("nested_root_check: disabled via " + _ENV_DISABLE)
        return 0

    project_root = Path(args.project_root or Path.cwd()).resolve()

    try:
        findings = find_descendant_roots(
            project_root,
            include_finalized=not args.exclude_finalized,
        )
    except OSError as exc:
        # Total walk failure → fail-OPEN, exit 3.
        print(f"nested_root_check: undeterminable — {exc}", file=sys.stderr)
        return 3

    # Advisory: is the project root ITSELF nested under an ancestor root?
    ancestor = None
    try:
        ancestor = _find_nested_ancestor(project_root)
    except OSError:
        ancestor = None

    if args.format == "json":
        payload = {
            "project_root": str(project_root),
            "nested_ancestor": str(ancestor) if ancestor else None,
            "findings": [str(p) for p in findings],
        }
        print(json.dumps(payload))
        return 1 if findings else 0

    if ancestor is not None:
        print(
            f"nested_root_check: NOTE — project root {project_root} is itself nested "
            f"inside ancestor {ancestor} that also has .workflow_artifacts/."
        )
    if not findings:
        print(f"nested_root_check: OK — single .workflow_artifacts root at {project_root}")
        return 0

    print(f"nested_root_check: found {len(findings)} accidental nested root(s):")
    for nested in findings:
        print(f"  - {nested}")
        print(f"      → {_corrective_action(nested, project_root)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
