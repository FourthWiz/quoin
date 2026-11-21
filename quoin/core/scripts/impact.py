#!/usr/bin/env python3
"""impact.py - which repositories and interfaces a diff touches.

Runs ``git diff --name-only <range>`` in each repository of the project and
matches the changed files against the interface graph (the discovery map's
``interfaces``, or a fresh scan). A change on an interface's provider side
names the consumer repositories to check; a change on the consumer side names
the provider it depends on.

Usage
  python3 impact.py --diff RANGE [--project-root DIR] [--repo NAME ...] [--json]
  python3 impact.py --branch NAME [--project-root DIR] [--json]
  python3 impact.py --plan --repos A,B [--project-root DIR] [--json]

  RANGE is anything ``git diff`` takes: ``main...HEAD``, ``HEAD~3``, ``HEAD``
  (uncommitted changes). A repository where the range does not resolve is
  left out. ``--branch`` diffs each repository that has the branch against its
  own default branch, plus uncommitted changes when the branch is checked out
  (a staged task keeps one branch name in every repository).
  ``--plan`` lists the stages of a cross-repository change before any code is
  written: the repositories named plus the consumers of the interfaces they
  provide, providers first. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import interfaces as ifx  # noqa: E402


def changed_files(repo_dir: Path, diff_range: str) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--name-only", diff_range, "--"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def _git(repo_dir: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def default_branch(repo_dir: Path) -> str | None:
    head = _git(repo_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head
    for name in ("main", "master", "trunk", "develop"):
        if _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return None


def branch_ranges(project_root: str | Path, branch: str) -> dict[str, list[str]]:
    """``{repo: changed files}`` for every repository that has ``branch``."""
    root = Path(project_root).resolve()
    out: dict[str, list[str]] = {}
    for repo in ifx.repositories(root):
        if not _git(repo["dir"], "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            continue
        base = default_branch(repo["dir"])
        files = set(changed_files(repo["dir"], f"{base}...{branch}") or []) if base and base != branch else set()
        if _git(repo["dir"], "symbolic-ref", "--quiet", "--short", "HEAD") == branch:
            files.update(changed_files(repo["dir"], "HEAD") or [])
            untracked = _git(repo["dir"], "ls-files", "--others", "--exclude-standard")
            files.update(line for line in (untracked or "").splitlines() if line)
        if files:
            out[repo["name"]] = sorted(files)
    return out


def impact(project_root: str | Path, diff_range: str | None, only: list[str] | None = None,
           *, branch: str | None = None) -> dict:
    root = Path(project_root).resolve()
    graph = ifx.load_interfaces(root)
    changed: dict[str, list[str]] = {}
    if branch is not None:
        changed = {k: v for k, v in branch_ranges(root, branch).items() if not only or k in only}
    else:
        for repo in ifx.repositories(root):
            if only and repo["name"] not in only:
                continue
            files = changed_files(repo["dir"], diff_range)
            if files:
                changed[repo["name"]] = files

    touched = []
    to_check: set[str] = set()
    for item in graph:
        hits = [f for f in changed.get(item["provider"], [])
                if any(ifx.glob_match(f, pattern) for pattern in item["provider_paths"])]
        if hits:
            consumers = [c["repo"] for c in item["consumers"]]
            touched.append({"id": item["id"], "name": item["name"], "kind": item["kind"], "side": "provider",
                            "repo": item["provider"], "files": hits, "check": item["consumers"]})
            to_check.update(r for r in consumers if r not in changed)
        for consumer in item["consumers"]:
            chits = [f for f in changed.get(consumer["repo"], []) if f in consumer["paths"]]
            if chits:
                touched.append({"id": item["id"], "name": item["name"], "kind": item["kind"], "side": "consumer",
                                "repo": consumer["repo"], "files": chits,
                                "check": [{"repo": item["provider"], "paths": item["provider_paths"]}]})
    return {
        "range": diff_range if branch is None else f"branch {branch}",
        "changed": changed,
        "interfaces": touched,
        "check": sorted(to_check),
        "repositories": sorted(set(changed) | to_check),
    }


def plan_stages(project_root: str | Path, repos: list[str]) -> list[dict]:
    """Stages for a change to ``repos``: those plus their interfaces' consumers, providers first."""
    root = Path(project_root).resolve()
    graph = ifx.load_interfaces(root)
    known = {r["name"] for r in ifx.repositories(root)}
    reasons: dict[str, str] = {r: "named in the task" for r in repos if r in known}
    edges: set[tuple[str, str]] = set()
    for item in graph:
        if item["provider"] in reasons:
            for consumer in item["consumers"]:
                reasons.setdefault(consumer["repo"], f"consumes {item['name']} from {item['provider']}")
    for item in graph:
        for consumer in item["consumers"]:
            if item["provider"] in reasons and consumer["repo"] in reasons and consumer["repo"] != item["provider"]:
                edges.add((item["provider"], consumer["repo"]))
    order, pending = [], set(reasons)
    while pending:
        ready = sorted(r for r in pending if not any(a in pending and b == r for a, b in edges))
        if not ready:  # a cycle: keep the remaining ones in name order
            ready = sorted(pending)
        order.append(ready[0])
        pending.discard(ready[0])
    return [{"repo": r, "reason": reasons[r]} for r in order]


def render(result: dict) -> str:
    if not result["changed"]:
        return f"No repository has changes in {result['range']}."
    lines = ["Repositories changed:"]
    for repo, files in sorted(result["changed"].items()):
        lines.append(f"  {repo} ({len(files)} file{'s' if len(files) != 1 else ''})")
    if result["interfaces"]:
        lines.append("Interfaces touched:")
        for item in result["interfaces"]:
            lines.append(f"  {item['name']} ({item['kind']}), {item['side']} side in {item['repo']}: {', '.join(item['files'])}")
            for other in item["check"]:
                lines.append(f"    check {other['repo']}: {', '.join(other['paths'])}")
    else:
        lines.append("No interface between repositories is touched.")
    if result["check"]:
        lines.append(f"Repositories to check: {', '.join(result['check'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--diff", metavar="RANGE")
    mode.add_argument("--branch", metavar="NAME")
    mode.add_argument("--plan", action="store_true")
    parser.add_argument("--repos", default="", help="with --plan: the repositories the task changes, comma-separated")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--repo", action="append", default=None, help="limit to these repositories")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.plan:
        stages = plan_stages(args.project_root, [r.strip() for r in args.repos.split(",") if r.strip()])
        if args.json:
            print(json.dumps(stages, indent=2))
        else:
            for i, stage in enumerate(stages, 1):
                print(f"{i}. {stage['repo']} ({stage['reason']})")
        return 0
    result = impact(args.project_root, args.diff, args.repo, branch=args.branch)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
