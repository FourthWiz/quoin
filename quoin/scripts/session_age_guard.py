"""session_age_guard.py — Check whether the current Claude session is too old.

CLAUDE-ADAPTER-OWNED — this file reads Claude Code JSONL sessions and/or
Claude model pricing. Do NOT import this module from any file in
quoin/core/. The portable cost-event schema is at
quoin/core/scripts/cost_event.py. Adding runtime-neutral functionality
belongs there, not here.

Current-session resolution: --current-uuid, else the CLAUDE_CODE_SESSION_ID
environment variable, else the newest *.jsonl by mtime in the project dir.

Usage:
  python3 ~/.claude/scripts/session_age_guard.py [--threshold-hours 6.0] \
      [--project-root ABS_PATH] [--current-uuid UUID]

Exit codes:
  0 — session age below threshold OR no jsonl found (fail-OPEN)
  1 — session age >= threshold (caller decides whether to refuse / warn)

Stdout: single line: <status>|<age_hours>|<jsonl_path>
  e.g. OK|1.42|/path/to/abc.jsonl
       OVER|7.10|/path/to/abc.jsonl
       OK|0.00|  (no jsonl found — fail-OPEN)

Stderr: human-readable diagnostic

Design decisions:
  - Stdlib-only: pathlib, sys, argparse, os, time, re (no external deps)
  - Prefer st_birthtime (macOS APFS true creation time) over st_ctime
    (inode-change time on macOS — bumped by chmod/rename, not birth)
  - Fail-OPEN: missing project dir → exit 0, not 1
  - Project hash mirrors Claude harness behavior:
    replace ALL non-[A-Za-z0-9-] chars with - (not just /)
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# Claude Code exports the running session's id to Bash tool calls. Strict shape
# check: the value is joined into a path and a glob pattern.
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _env_session_uuid(environ=None) -> Optional[str]:
    """Return the session UUID from CLAUDE_CODE_SESSION_ID, or None.

    Unset/empty -> None silently; a malformed value -> None with a stderr note.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not raw:
        return None
    if not _UUID_RE.match(raw):
        print(
            "[session-age-guard] ignoring malformed CLAUDE_CODE_SESSION_ID",
            file=sys.stderr,
        )
        return None
    return raw


def _find_uuid_jsonl(projects_base: Path, project_dir: Path, uuid: str) -> Optional[Path]:
    """Locate <uuid>.jsonl: the project dir first, then any single project dir.

    The cross-project lookup covers a session whose cwd hash differs from the
    project root's hash (worktrees, subdirectories). Never recursive.
    """
    name = f"{uuid}.jsonl"
    if project_dir.exists():
        candidate = project_dir / name
        if candidate.exists():
            return candidate
    try:
        for candidate in projects_base.glob(f"*/{name}"):
            return candidate
    except OSError:
        pass
    return None



def _project_hash(project_root: Path) -> str:
    """Convert an absolute path to a Claude project hash.

    Mirrors the Claude harness behavior: every character outside [A-Za-z0-9-]
    is replaced with -.  This correctly handles paths containing spaces, dots,
    @-signs, underscores, etc. — which a simple "/" → "-" replacement misses.
    E.g. /Users/x/My Drive/Proj_one → -Users-x-My-Drive-Proj-one
    """
    return re.sub(r'[^A-Za-z0-9-]', '-', str(project_root).rstrip("/"))


def _file_birth_time(stat_result) -> float:
    """Return creation/birth time as seconds since epoch.

    macOS APFS: use st_birthtime (true file birth).
    st_birthtime == 0 means birth time predates volume support — fall back to st_mtime.
    Linux/other: use st_ctime (closest available approximation).
    """
    birth = getattr(stat_result, "st_birthtime", None)
    if birth is not None:
        # macOS path
        if birth == 0:
            # Sentinel: volume doesn't track birth time — use mtime
            return stat_result.st_mtime
        return birth
    # Non-macOS: st_ctime is inode-change time, best available approximation
    return stat_result.st_ctime


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether the current Claude session is too old."
    )
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=6.0,
        help="Age threshold in hours (default: 6.0). Sessions older than this exit 1.",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Absolute path to the project root. Defaults to cwd (with a warning).",
    )
    parser.add_argument(
        "--current-uuid",
        type=str,
        default=None,
        help="UUID of the current session's jsonl file (without .jsonl). "
             "If omitted, CLAUDE_CODE_SESSION_ID is used; the most recent "
             "*.jsonl by mtime is a fallback only when that is unset or malformed.",
    )
    args = parser.parse_args()

    # Resolve project root
    if args.project_root is None:
        project_root = Path.cwd()
        print(
            f"[session-age-guard] --project-root not provided; using cwd: {project_root}",
            file=sys.stderr,
        )
    else:
        project_root = Path(args.project_root).resolve()

    # Derive project hash and locate the projects directory
    project_hash = _project_hash(project_root)
    projects_base = Path.home() / ".claude" / "projects"
    project_dir = projects_base / project_hash

    env_uuid = None if args.current_uuid else _env_session_uuid()

    if not project_dir.exists() and not env_uuid:
        print(
            f"[session-age-guard] project dir not found: {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        print("OK|0.00|")
        return 0

    # Find the target jsonl
    if env_uuid:
        target = _find_uuid_jsonl(projects_base, project_dir, env_uuid)
        if target is None:
            print(
                f"[session-age-guard] env session jsonl not found for {env_uuid} "
                f"under {projects_base} — fail-OPEN",
                file=sys.stderr,
            )
            print("OK|0.00|")
            return 0
        jsonl_files = [target]
    elif args.current_uuid:
        if not project_dir.exists():
            print(
                f"[session-age-guard] project dir not found: {project_dir} — fail-OPEN",
                file=sys.stderr,
            )
            print("OK|0.00|")
            return 0
        target = project_dir / f"{args.current_uuid}.jsonl"
        if not target.exists():
            print(
                f"[session-age-guard] uuid jsonl not found: {target} — fail-OPEN",
                file=sys.stderr,
            )
            print("OK|0.00|")
            return 0
        jsonl_files = [target]
    else:
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

    if not jsonl_files:
        print(
            f"[session-age-guard] no jsonl files in {project_dir} — fail-OPEN",
            file=sys.stderr,
        )
        print("OK|0.00|")
        return 0

    target_file = jsonl_files[0]

    # Compute age
    try:
        stat = target_file.stat()
    except OSError as exc:
        print(
            f"[session-age-guard] cannot stat {target_file}: {exc} — fail-OPEN",
            file=sys.stderr,
        )
        print("OK|0.00|")
        return 0

    birth = _file_birth_time(stat)
    now = time.time()
    age_hours = (now - birth) / 3600.0

    status = "OVER" if age_hours >= args.threshold_hours else "OK"
    print(f"{status}|{age_hours:.2f}|{target_file}")

    if status == "OVER":
        print(
            f"[session-age-guard] session age {age_hours:.2f}h >= threshold "
            f"{args.threshold_hours}h — recommend fresh session",
            file=sys.stderr,
        )
        return 1

    print(
        f"[session-age-guard] session age {age_hours:.2f}h < threshold "
        f"{args.threshold_hours}h — OK",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
