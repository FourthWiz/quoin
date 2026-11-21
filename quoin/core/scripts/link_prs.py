#!/usr/bin/env python3
"""link_prs.py - link the pull requests of one cross-repository task.

Given the PR URLs of a task (one per repository), adds a "Related pull
requests" section to each PR body listing the others, through ``gh pr view``
and ``gh pr edit``. Rerunning replaces the section instead of adding another.

Usage
  python3 link_prs.py URL URL [URL ...]

Prints one JSON object: ``{"linked": [urls], "failed": [{url, error}]}``.
Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

HEADING = "## Related pull requests"
_SECTION = re.compile(rf"\n*{re.escape(HEADING)}\n.*?(?=\n## |\Z)", re.S)
_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)$")


def _gh(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=60)


def label(url: str) -> str:
    match = _URL.match(url)
    return f"{match.group(2)}#{match.group(3)}" if match else url


def with_section(body: str, others: list[str]) -> str:
    section = HEADING + "\n\n" + "\n".join(f"- {label(u)}: {u}" for u in others) + "\n"
    body = _SECTION.sub("", body or "").rstrip()
    return f"{body}\n\n{section}" if body else section


def link(urls: list[str]) -> dict:
    linked, failed = [], []
    for url in urls:
        others = [u for u in urls if u != url]
        view = _gh("pr", "view", url, "--json", "body", "-q", ".body")
        if view.returncode != 0:
            failed.append({"url": url, "error": view.stderr.strip() or "gh pr view failed"})
            continue
        edit = _gh("pr", "edit", url, "--body-file", "-", stdin=with_section(view.stdout, others))
        if edit.returncode != 0:
            failed.append({"url": url, "error": edit.stderr.strip() or "gh pr edit failed"})
            continue
        linked.append(url)
    return {"linked": linked, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    urls = [u for u in (argv if argv is not None else sys.argv[1:]) if u.strip()]
    bad = [u for u in urls if not _URL.match(u)]
    if len(urls) < 2 or bad:
        print(json.dumps({"error": "give two or more GitHub PR URLs", "bad": bad}))
        return 2
    print(json.dumps(link(urls)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
