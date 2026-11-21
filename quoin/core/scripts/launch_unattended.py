#!/usr/bin/env python3
"""launch_unattended.py - start `quoin run --autonomous` in the background.

Starts the unattended run for a task as a detached process (its own session,
so it outlives the Claude Code turn and session that started it), writes its
output to ``.workflow_artifacts/<task>/unattended.log``, records the pid in
``.workflow_artifacts/<task>/unattended.pid``, and returns at once. A second
launch while the first is still running is refused.

Usage
  python3 launch_unattended.py --task NAME [--project-root DIR]
                               [--model M] [--max-budget-usd N]

Prints one JSON object: ``started`` (bool), ``pid``, ``log``, ``goal_log``
(the per-stage log `quoin run` keeps), ``command``, or ``error``.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch(project_root: str, task: str, model: str | None = None, budget: float | None = None,
           *, which=shutil.which, popen=subprocess.Popen) -> dict:
    root = Path(project_root).resolve()
    task_dir = root / ".workflow_artifacts" / task
    if not task_dir.is_dir():
        return {"started": False, "error": f"no task folder {task_dir}"}
    quoin = which("quoin")
    if not quoin:
        return {"started": False, "error": "the quoin command is not on PATH; install it with `pip install quoin`"}
    pid_file = task_dir / "unattended.pid"
    try:
        old = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        old = None
    if old and _alive(old):
        return {"started": False, "pid": old, "error": f"an unattended run of {task} is already running (pid {old})"}
    command = [quoin, "run", "--autonomous", task, "--project-root", str(root)]
    if model:
        command += ["--model", model]
    if budget is not None:
        command += ["--max-budget-usd", str(budget)]
    log = task_dir / "unattended.log"
    with log.open("ab") as out:
        proc = popen(command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                     start_new_session=True)
    pid_file.write_text(f"{proc.pid}\n")
    return {"started": True, "pid": proc.pid, "log": str(log), "goal_log": str(task_dir / "goal-run.log"),
            "command": " ".join(command)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--task", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-budget-usd", type=float, default=None)
    args = parser.parse_args(argv)
    result = launch(args.project_root, args.task, args.model, args.max_budget_usd)
    print(json.dumps(result))
    return 0 if result.get("started") else 1


if __name__ == "__main__":
    sys.exit(main())
