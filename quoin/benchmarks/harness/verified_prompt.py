"""
verified_prompt.py — Prompt for SWE-bench Verified tasks.

The upstream instance id is deliberately left out so neither arm is handed a
search key for the upstream fix.
"""

from __future__ import annotations

VERIFIED_PROMPT_TEMPLATE_VERSION = "verified-v1"


def build_verified_prompt(task_spec: dict) -> str:
    repo = task_spec.get("repo") or ""
    base_commit = task_spec.get("base_commit") or ""
    description = task_spec.get("description") or ""
    if not repo or not base_commit or not description:
        raise ValueError("a Verified task needs repo, base_commit and description")
    return (
        f"Resolve the following GitHub issue in the {repo} repository.\n\n"
        f"The working tree is checked out at commit {base_commit}. Work only from this "
        f"repository; do not fetch or look up the upstream fix.\n\n"
        f"Issue:\n{description}\n\n"
        f"Make the code changes needed to resolve the issue in this working tree. "
        f"Hidden tests will evaluate your change."
    )
