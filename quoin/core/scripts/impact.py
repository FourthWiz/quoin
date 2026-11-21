#!/usr/bin/env python3
"""impact.py - which repositories and interfaces a diff touches.

Runs ``git diff --name-only <range>`` in each repository of the project and
matches the changed files against the interface graph (the discovery map's
``interfaces``, or a fresh scan). A change on an interface's provider side
names the consumer repositories to check; a change on the consumer side names
the provider it depends on.

Usage
  python3 impact.py --diff RANGE [--project-root DIR] [--repo NAME ...] [--json]

  RANGE is anything ``git diff`` takes: ``main...HEAD``, ``HEAD~3``, ``HEAD``
  (uncommitted changes). A repository where the range does not resolve is
  left out. Standard library only.
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


def impact(project_root: str | Path, diff_range: str, only: list[str] | None = None) -> dict:
    root = Path(project_root).resolve()
    graph = ifx.load_interfaces(root)
    changed: dict[str, list[str]] = {}
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
        "range": diff_range,
        "changed": changed,
        "interfaces": touched,
        "check": sorted(to_check),
        "repositories": sorted(set(changed) | to_check),
    }


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
    parser.add_argument("--diff", required=True, metavar="RANGE")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--repo", action="append", default=None, help="limit to these repositories")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = impact(args.project_root, args.diff, args.repo)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
