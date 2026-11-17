#!/usr/bin/env python3
"""discovery_dirty.py - decide from git whether a commit makes discovery stale.

/run calls this after its commit. It reads the commit's name-status list and
the top-level tree of both sides, and marks discovery out of date when the
change touched the project's structure, a dependency manifest or an interface
definition file. The decision is mechanical so it costs no model reasoning.

Usage
  python3 discovery_dirty.py --repo DIR --task TASK [--base REV]
                             [--project-root DIR] [--dry-run]

  --repo          the git checkout holding the commit (default: cwd)
  --base          compare the merge-base of BASE and HEAD with HEAD, instead
                  of HEAD's own commit; pass where the task started
  --project-root  where .workflow_artifacts/ lives (default: cwd)
  --dry-run       print the decision but do not write the marker

When dirty, it appends `<date> <task>: <reasons>` to
<project-root>/.workflow_artifacts/memory/discovery-dirty.

Output: one line, `discovery-dirty: no` or `discovery-dirty: yes (<reasons>)`.
Exit 0 on either decision; on a git error it prints a warning, writes
nothing and still exits 0, so a record step never fails on it. Exit 2 is
reserved for bad arguments.

Counts as dirty
  structure   a top-level directory added or removed (dot-directories,
              tests and docs aside); a package added, removed or renamed
              (an `__init__.py` with status A, D or R)
  manifest    any change to a dependency manifest (lockfiles alone do not count)
  interface   any change to an interface definition file: protobuf, GraphQL,
              OpenAPI/Swagger, a `.d.ts`, or a migration
Manifests and interface files count anywhere; other tests and docs never count.
"""

import argparse
import datetime
import fnmatch
import subprocess
import sys
from pathlib import Path, PurePosixPath

MARKER = Path(".workflow_artifacts") / "memory" / "discovery-dirty"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt",
    "requirements*.in", "Pipfile", "environment.yml", "package.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "Gemfile", "*.gemspec",
    "composer.json", "*.csproj", "Package.swift", "mix.exs", "pubspec.yaml",
)
INTERFACE_FILES = (
    "*.proto", "*.graphql", "*.gql", "openapi*.yaml", "openapi*.yml",
    "openapi*.json", "swagger*.yaml", "swagger*.yml", "swagger*.json",
    "*.d.ts",
)
INTERFACE_DIRS = ("migrations", "migration", "alembic")
TEST_DOC_DIRS = ("test", "tests", "testing", "__tests__", "spec", "docs", "doc")
TEST_DOC_FILES = (
    "*.md", "*.rst", "*.txt", "test_*.py", "*_test.py", "*_test.go",
    "*.test.*", "*.spec.*", "conftest.py",
)


def _match(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def is_manifest(path):
    return _match(PurePosixPath(path).name, MANIFESTS)


def is_test_or_doc(path):
    p = PurePosixPath(path)
    if any(part in TEST_DOC_DIRS for part in p.parts[:-1]):
        return True
    # A manifest such as requirements.txt is never treated as a doc.
    return not is_manifest(path) and _match(p.name, TEST_DOC_FILES)


def is_interface(path):
    p = PurePosixPath(path)
    if _match(p.name, INTERFACE_FILES):
        return True
    return any(part in INTERFACE_DIRS for part in p.parts[:-1])


def parse_name_status(text):
    """Return (status letter, [paths]) from `--name-status -z` output.

    NUL-separated output keeps paths unquoted; a rename or copy carries two paths.
    """
    fields = text.split("\0")
    entries, i = [], 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        count = 2 if status in "RC" else 1
        entries.append((status, fields[i + 1:i + 1 + count]))
        i += 1 + count
    return entries


def classify(entries, top_before, top_after):
    """Return the sorted reasons a change makes discovery stale (empty: clean)."""
    reasons = set()
    # Dot-directories (.github, .vscode) are tooling, not project structure.
    def counted(names):
        return {n for n in names if not n.startswith(".") and n not in TEST_DOC_DIRS}
    for name in counted(top_after) - counted(top_before):
        reasons.add(f"structure: added {name}/")
    for name in counted(top_before) - counted(top_after):
        reasons.add(f"structure: removed {name}/")
    for status, paths in entries:
        status = status[:1]
        for path in paths:
            # Manifests and interface files count wherever they live (spec/openapi.yaml).
            if is_manifest(path):
                reasons.add(f"manifest: {path}")
            elif is_interface(path):
                reasons.add(f"interface: {path}")
            elif is_test_or_doc(path):
                continue
            elif status in "ADR" and PurePosixPath(path).name == "__init__.py":
                reasons.add(f"structure: package {PurePosixPath(path).parent}")
    return sorted(reasons)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _top_dirs(repo, rev):
    out = _git(repo, "ls-tree", "-d", "--name-only", "-z", rev)
    return [name for name in out.split("\0") if name]


def decide(repo, base=None):
    if base is None:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
        base = parents[1] if len(parents) > 1 else EMPTY_TREE
    else:
        if base.startswith("-"):
            raise ValueError(f"bad base {base!r}")
        # Compare against where the task branched, so later commits on a moved
        # base branch (a pulled main) are not counted as this task's changes.
        base = _git(repo, "merge-base", base, "HEAD").strip()
    diff = _git(repo, "diff", "--name-status", "-z", "-M", base, "HEAD")
    return classify(parse_name_status(diff), _top_dirs(repo, base), _top_dirs(repo, "HEAD"))


def write_marker(project_root, task, reasons, today=None):
    path = Path(project_root) / MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    today = today or datetime.date.today().isoformat()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{today} {task}: {'; '.join(reasons)}\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mark discovery stale after a structural commit.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--base", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        reasons = decide(args.repo, args.base)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"discovery_dirty: WARN git failed ({detail.strip()}); no marker written", file=sys.stderr)
        return 0
    if not reasons:
        print("discovery-dirty: no")
        return 0
    print(f"discovery-dirty: yes ({'; '.join(reasons)})")
    if not args.dry_run:
        try:
            write_marker(args.project_root, args.task, reasons)
        except OSError as exc:
            print(f"discovery_dirty: WARN could not write marker ({exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
