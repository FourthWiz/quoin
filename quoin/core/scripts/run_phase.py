#!/usr/bin/env python3
"""run_phase.py - append a phase marker to a light /run task's phases.log.

/run calls this at the start of each step so a benchmark harness can split a
session's time and tokens by phase afterwards. A marker never changes what
/run does: bad input or an unwritable log prints a warning and still exits 0.

Usage
  python3 run_phase.py TASK PHASE [--skipped] [--stage N] [--root DIR]

  TASK is the task folder name under DIR/.workflow_artifacts/ (DIR defaults to
  the current directory). PHASE is one of PHASES below. --stage N prefixes the
  phase with the stage number ("2:implement") for a staged task.

Line format (one per call, appended)
  2026-11-10T10:32:09.120Z discover skipped
  2026-11-10T10:32:15.004Z 2:implement

The script also prints `profile=<QUOIN_RUN_PROFILE>` so /run can read that
benchmark-only switch from the same Bash call (empty when unset).
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

PHASES = (
    "setup", "discover", "frame", "context", "decompose",
    "implement", "verify", "review", "review-fixes", "commit",
)
PROFILE_ENV = "QUOIN_RUN_PROFILE"


def marker_line(phase, skipped=False, stage=None, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now = now.astimezone(datetime.timezone.utc)
    # Milliseconds, like the transcript timestamps the markers are joined with.
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    name = f"{stage}:{phase}" if stage else phase
    return f"{stamp} {name}" + (" skipped" if skipped else "")


def append_marker(root, task, phase, skipped=False, stage=None, now=None):
    """Append one marker; returns the log path."""
    path = Path(root) / ".workflow_artifacts" / task / "phases.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(marker_line(phase, skipped, stage, now) + "\n")
    return path


def _stage(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("stage must be 1 or more")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Append a /run phase marker.")
    parser.add_argument("task")
    parser.add_argument("phase")
    parser.add_argument("--skipped", action="store_true")
    parser.add_argument("--stage", type=_stage, default=None)
    parser.add_argument("--root", default=".")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    print(f"profile={os.environ.get(PROFILE_ENV, '').strip()}")
    task = args.task.strip()
    if not task or "/" in task or task in (".", ".."):
        print(f"run_phase: WARN bad task name {args.task!r}; no marker written", file=sys.stderr)
        return 0
    if args.phase not in PHASES:
        print(f"run_phase: WARN unknown phase {args.phase!r}; no marker written", file=sys.stderr)
        return 0
    try:
        append_marker(args.root, task, args.phase, args.skipped, args.stage)
    except OSError as exc:
        print(f"run_phase: WARN could not write marker: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
