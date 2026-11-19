#!/usr/bin/env python3
"""run_record.py - record a finished light /run step in one scripted call.

/run calls this right after its commit, chained with `&&`. It does the
bookkeeping the skill would otherwise write out by hand:

  * appends `## Verification` and `## Commit` (branch and short commit, read
    from git) to .workflow_artifacts/<task>/brief.md, first writing the short
    r0 brief (`# <task>`, `## Request`, `## Scope` with `route: r0`) when the
    task has none, since r0 writes no brief up front;
  * marks discovery out of date when the commit changed the project's
    structure, a dependency manifest or an interface file (the rules live in
    discovery_dirty.py). The benchmark-only QUOIN_RUN_PROFILE=bench-lean
    skips this step.

Usage
  python3 run_record.py --task TASK --verify TEXT [--request TEXT]
                        [--repo DIR] [--base REV] [--project-root DIR]
                        [--stage N] [--no-commit]

  --verify        the test command and its result, e.g. "pytest tests/x.py: pass"
  --request       the request or issue id, used only when the brief is new
  --repo          the git checkout holding the commit (default: cwd)
  --base          where the task or stage started (for the discovery check)
  --project-root  where .workflow_artifacts/ lives (default: cwd)
  --stage N       record into the stage brief, <task>/stage-N/brief.md
  --no-commit     the run ended without a commit: record only verification

Output: one line per step. Like the phase marker, a record never fails the
run: every problem prints a warning and the script still exits 0.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROFILE_ENV = "QUOIN_RUN_PROFILE"
LEAN_PROFILE = "bench-lean"


def _load_discovery_dirty():
    path = Path(__file__).resolve().with_name("discovery_dirty.py")
    spec = importlib.util.spec_from_file_location("_quoin_run_record_discovery_dirty", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def brief_text(task, request, verify, branch=None, commit=None, existing=None, header=True):
    """The text to write: the new r0 brief plus sections, or sections to append."""
    parts = []
    if existing is None and header:
        parts.append(f"# {task}\n## Request\n{request or '(not recorded)'}\n## Scope\nroute: r0\n")
    elif not existing.endswith("\n"):
        parts.append("\n")
    parts.append(f"## Verification\n{verify}\n")
    if commit:
        parts.append(f"## Commit\nbranch: {branch}\ncommit: {commit}\n")
    return "".join(parts)


def record_brief(project_root, task, request, verify, branch=None, commit=None, stage=None):
    folder = Path(project_root) / ".workflow_artifacts" / task
    path = (folder / f"stage-{stage}" if stage else folder) / "brief.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    # A missing stage brief gets only the sections: the r0 header would mislabel it.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(brief_text(task, request, verify, branch, commit, existing, header=not stage))
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record a finished /run step.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--verify", required=True)
    parser.add_argument("--request", default=None)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default=None)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--stage", type=int, default=None)
    parser.add_argument("--no-commit", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0

    task = args.task.strip()
    if not task or "/" in task or task in (".", ".."):
        print(f"run_record: WARN bad task name {args.task!r}; nothing recorded", file=sys.stderr)
        return 0

    branch = commit = None
    if not args.no_commit:
        try:
            branch = _git(args.repo, "rev-parse", "--abbrev-ref", "HEAD")
            branch = "(detached)" if branch == "HEAD" else branch
            commit = _git(args.repo, "rev-parse", "--short", "HEAD")
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            print(f"run_record: WARN git failed ({detail.strip()}); commit not recorded", file=sys.stderr)
    try:
        record_brief(args.project_root, task, args.request, args.verify, branch, commit, args.stage)
        print("brief: recorded" + (f" (commit {commit})" if commit else " (no commit)"))
    except OSError as exc:
        print(f"run_record: WARN could not write the brief ({exc})", file=sys.stderr)

    if not commit or os.environ.get(PROFILE_ENV, "").strip() == LEAN_PROFILE:
        return 0
    dirty_args = ["--repo", args.repo, "--task", task, "--project-root", args.project_root]
    if args.base:
        dirty_args += ["--base", args.base]
    try:
        _load_discovery_dirty().main(dirty_args)
    except (Exception, SystemExit) as exc:  # a record step never fails the run
        print(f"run_record: WARN discovery check failed ({exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
