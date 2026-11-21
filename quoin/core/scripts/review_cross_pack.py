#!/usr/bin/env python3
"""review_cross_pack.py - the evidence a cross-repository review reads.

Collects, for a task branch that exists in several repositories of a project:
the impact report (interfaces the change touches and the repositories to
check), each touched interface with the current code of its consumers (changed
or not, since a consumer nobody updated is what the review is for), and every
repository's diff against its default branch. Sizes are bounded so the pack
fits a review.

Usage
  python3 review_cross_pack.py --branch NAME [--project-root DIR] [--out FILE]

Prints the pack (markdown), or writes it to --out and prints the path.
Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import impact as imp  # noqa: E402
import interfaces as ifx  # noqa: E402

MAX_FILE_LINES = 300
MAX_CONSUMER_CHARS = 60_000
MAX_DIFF_CHARS = 120_000


def _numbered(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    shown = [f"{i:>5}  {line}" for i, line in enumerate(lines[:MAX_FILE_LINES], 1)]
    if len(lines) > MAX_FILE_LINES:
        shown.append(f"  ... {len(lines) - MAX_FILE_LINES} more lines")
    return "\n".join(shown)


def _diff(repo_dir: Path, branch: str) -> str:
    base = imp.default_branch(repo_dir)
    parts = []
    for args in ([f"{base}...{branch}"] if base and base != branch else []) + (
        ["HEAD"] if imp._git(repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD") == branch else []
    ):
        try:
            out = subprocess.run(["git", "-C", str(repo_dir), "diff", "--unified=5", args, "--"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            parts.append(out.stdout)
    return "\n".join(parts)


def build(project_root: str | Path, branch: str) -> str:
    root = Path(project_root).resolve()
    repo_dirs = {r["name"]: r["dir"] for r in ifx.repositories(root)}
    result = imp.impact(root, None, branch=branch)
    lines = [f"# Cross-repository review pack: {branch}", "", "## Impact", "", imp.render(result), ""]

    lines += ["## Touched interfaces and their consumers", ""]
    budget = MAX_CONSUMER_CHARS
    if not result["interfaces"]:
        lines += ["No interface between repositories is touched.", ""]
    seen: set[tuple[str, str]] = set()
    for item in result["interfaces"]:
        lines += [f"### {item['name']} ({item['kind']}), {item['side']} side changed in {item['repo']}",
                  "", f"Changed: {', '.join(item['files'])}", ""]
        for other in item["check"]:
            for rel in other["paths"]:
                if "*" in rel or (other["repo"], rel) in seen:
                    continue
                seen.add((other["repo"], rel))
                path = repo_dirs.get(other["repo"], root / other["repo"]) / rel
                body = _numbered(path)
                if len(body) > budget:
                    lines += [f"#### {other['repo']}/{rel}", "", "(left out: pack size limit; read the file)", ""]
                    continue
                budget -= len(body)
                lines += [f"#### {other['repo']}/{rel}", "", "```", body, "```", ""]

    lines += ["## Diffs", ""]
    budget = MAX_DIFF_CHARS
    for repo, files in sorted(result["changed"].items()):
        diff = _diff(repo_dirs[repo], branch)
        lines += [f"### {repo} ({len(files)} file{'s' if len(files) != 1 else ''})", ""]
        if len(diff) > budget:
            lines += [f"(diff left out: pack size limit; run `git -C {repo} diff` for it)", ""]
            continue
        budget -= len(diff)
        lines += ["```diff", diff.rstrip("\n"), "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--branch", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    pack = build(args.project_root, args.branch)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(pack, encoding="utf-8")
        print(out)
    else:
        print(pack)
    return 0


if __name__ == "__main__":
    sys.exit(main())
