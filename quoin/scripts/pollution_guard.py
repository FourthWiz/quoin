"""pollution_guard.py — Score the current Claude session transcript for pollution.

CLAUDE-ADAPTER-OWNED — this file reads Claude Code JSONL sessions. Do NOT
import this module from any file in quoin/core/. The portable cost-event
schema is at quoin/core/scripts/cost_event.py. Adding runtime-neutral
functionality belongs there, not here.

Skills that dispatch themselves to a fresh context when the session is heavy
call this once at entry instead of relying on a score written by a prompt hook.

Transcript resolution: --transcript PATH, else --current-uuid, else the
CLAUDE_CODE_SESSION_ID environment variable, else the newest *.jsonl by mtime
in the Claude project dir for --project-root (default: cwd). Resolution reuses
the helpers of session_age_guard.py so both guards pick the same file.

Score: bytes // 1000 + 5 * Agent calls + Read calls + Bash calls, counting
every tool_use item in the content list of each assistant line. Lines that are
not valid JSON are skipped.

Threshold: --threshold N, else env QUOIN_POLLUTION_THRESHOLD, else 5000.

Usage:
  python3 ~/.claude/scripts/pollution_guard.py [--transcript PATH]
      [--current-uuid UUID] [--project-root ABS_PATH] [--threshold N]

Stdout: exactly one line.
  OVER|<score>|<path>   score is at or above the threshold
  OK|<score>|<path>     score is below the threshold
  OK|0|                 anything went wrong (no transcript, unreadable, bad args)

Exit code is 0 on every path; the result is carried only on stdout so a
crash can never be mistaken for an over-threshold reading. Diagnostics go to
stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_THRESHOLD = 5000
_WEIGHTS = {"Agent": 5, "Read": 1, "Bash": 1}


def _note(msg: str) -> None:
    print(f"[pollution-guard] {msg}", file=sys.stderr)


def _threshold(flag: Optional[int]) -> int:
    if flag is not None:
        return flag
    raw = os.environ.get("QUOIN_POLLUTION_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        _note(f"ignoring non-integer QUOIN_POLLUTION_THRESHOLD={raw!r}; using {DEFAULT_THRESHOLD}")
        return DEFAULT_THRESHOLD


def compute_score(path: Path) -> int:
    """Return the pollution score of one transcript file."""
    size = path.stat().st_size
    weighted = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"tool_use"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    weighted += _WEIGHTS.get(item.get("name"), 0)
    return size // 1000 + weighted


def _resolve(args) -> Optional[Path]:
    if args.transcript:
        return Path(args.transcript)
    import session_age_guard as sag

    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    projects_base = Path.home() / ".claude" / "projects"
    project_dir = projects_base / sag._project_hash(project_root)
    uuid = args.current_uuid or sag._env_session_uuid()
    if uuid:
        target = sag._find_uuid_jsonl(projects_base, project_dir, uuid)
        if target is not None:
            return target
        _note(f"no transcript found for session {uuid}")
        return None
    if not project_dir.is_dir():
        _note(f"project dir not found: {project_dir}")
        return None
    files = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run(argv=None) -> str:
    parser = argparse.ArgumentParser(description="Score the session transcript for pollution.")
    parser.add_argument("--transcript")
    parser.add_argument("--current-uuid")
    parser.add_argument("--project-root")
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args(argv)
    path = _resolve(args)
    if path is None:
        return "OK|0|"
    score = compute_score(path)
    status = "OVER" if score >= _threshold(args.threshold) else "OK"
    return f"{status}|{score}|{path}"


def main(argv=None) -> int:
    try:
        line = run(argv)
    except SystemExit:
        line = "OK|0|"
    except Exception as exc:  # noqa: BLE001 - fail-OPEN on every error
        _note(f"{type(exc).__name__}: {exc} — fail-OPEN")
        line = "OK|0|"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
