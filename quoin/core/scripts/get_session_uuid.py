#!/usr/bin/env python3
"""Portable core implementation of session UUID capture for cost ledger.

Returns the current Claude Code session UUID: the CLAUDE_CODE_SESSION_ID
environment variable when valid, else the stem of the newest session JSONL
for the current project.

Public API:
  project_hash(project_path: str) -> str
  get_session_uuid(project_path=None, home=None, phase=None, environ=None) -> str
  main(argv=None) -> int

Exit codes:
  0 — always (fail-open design)

Fallback UUID form: unknown-<phase_slug>-<YYYYMMDD>T<HHMMSS>Z
  where phase_slug replaces '-' with '_' (e.g., end-of-task -> end_of_task)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

# Claude Code exports the running session's id to Bash tool calls. Strict shape
# check: guards against odd values leaking into the cost ledger.
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _env_session_uuid(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the session UUID from CLAUDE_CODE_SESSION_ID, or None.

    Unset/empty -> None silently; a malformed value -> None with a stderr note.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not raw:
        return None
    if not _UUID_RE.match(raw):
        print("[get-session-uuid] ignoring malformed CLAUDE_CODE_SESSION_ID", file=sys.stderr)
        return None
    return raw


# ---------------------------------------------------------------------------
# Hash logic — INLINE COPY from quoin/scripts/cost_from_jsonl.py (adapter-owned).
# Do NOT import from cost_from_jsonl.py — that file is CLAUDE-ADAPTER-OWNED
# and must not be imported from quoin/core/. The function is a one-line regex
# substitution with negligible drift risk. Parity tested by test_get_session_uuid.py.
# ---------------------------------------------------------------------------

def project_hash(project_path: str) -> str:
    """Convert /abs/path/to/project to the ~/.claude/projects/HASH form
    used by Claude Code session JSONL files.
    Empirical rule (verified 2026-04-27 by listing ~/.claude/projects/ on the
    developer machine): replace ANY character that is NOT [A-Za-z0-9-] with '-'.
    This covers '/' -> '-', '.' -> '-', '@' -> '-', '_' -> '-', ' ' -> '-', etc.
    Example: '/Users/ivgo/.../GoogleDrive-ivan.gorban@gmail.com/My Drive/...'
    becomes '-Users-ivgo-...-GoogleDrive-ivan-gorban-gmail-com-My-Drive-...'.
    Note: CLAUDE.md's legacy description 'project path with / replaced by -' is
    a simplification -- the actual on-disk transform is the broader regex rule.
    Path-with-spaces: the project path may contain spaces (e.g., 'My Drive');
    the transform replaces spaces with '-' as well. Quote all path expansions
    in callers to prevent shell word-splitting."""
    return re.sub(r'[^A-Za-z0-9-]', '-', project_path)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _fallback_uuid(phase: str | None) -> str:
    """Return a deterministic fallback UUID in unknown-<phase_slug>-<ISO>Z form.

    Phase dashes are slugified to underscores so the UUID stem is unambiguous
    (e.g., 'end-of-task' -> 'end_of_task'). This keeps the cost_snapshot
    'unknown-*' skip filter working without any changes to that filter.
    """
    phase_slug = (phase or "unknown").replace("-", "_")
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"unknown-{phase_slug}-{ts}"


def get_session_uuid(
    project_path: str | None = None,
    home: str | None = None,
    phase: str | None = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Return the current session UUID.

    Precedence: the CLAUDE_CODE_SESSION_ID environment variable (returned
    as-is, without checking for the file: the harness id is authoritative and
    the transcript may live under a different project hash), else the
    most-recently-modified JSONL under ~/.claude/projects/<hash>/, else a
    synthetic unknown-<phase_slug>-<timestamp> UUID (fail-open).

    Args:
        project_path: Absolute path to the project root. Defaults to cwd.
        home: Home directory override (for testing). Defaults to Path.home().
        phase: Phase name for fallback UUID discrimination (e.g., 'implement').
        environ: Environment mapping override (for testing). Defaults to os.environ.
    """
    try:
        env_uuid = _env_session_uuid(environ)
        if env_uuid:
            return env_uuid
        proj_path = project_path or str(Path.cwd())
        home_path = Path(home) if home else Path.home()
        proj_dir = home_path / ".claude" / "projects" / project_hash(proj_path)
        # Filter out Drive conflict copies (e.g. "UUID 2.jsonl") before sorting
        # by mtime — a conflict copy newer than the real session file would
        # otherwise be picked as the "most recent" UUID. IVG-75 T-06.
        _conflict_re = re.compile(r" \d{1,3}(\.[^ ]*)?$")
        jsonl_files = sorted(
            [p for p in proj_dir.glob("*.jsonl")
             if not _conflict_re.search(p.name)],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonl_files:
            return jsonl_files[0].stem
    except Exception:  # noqa: BLE001
        pass
    return _fallback_uuid(phase)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Accepts --project-path PATH, --phase PHASE, and --print-hash.
    Prints the UUID (or project hash) to stdout.
    Always exits 0 (fail-open).
    """
    parser = argparse.ArgumentParser(
        description="Get the Claude Code session UUID for cost ledger recording.",
        add_help=True,
    )
    parser.add_argument(
        "--project-path",
        default=None,
        metavar="PATH",
        help="Absolute path to the project root (defaults to cwd).",
    )
    parser.add_argument(
        "--phase",
        default=None,
        metavar="PHASE",
        help="Phase name for fallback UUID (e.g., implement, end-of-task).",
    )
    parser.add_argument(
        "--print-hash",
        action="store_true",
        default=False,
        help=(
            "Print the project hash (the ~/.claude/projects/<hash> directory name) "
            "and exit 0. Does NOT perform the JSONL lookup. "
            "Resolves --project-path or falls back to cwd when --project-path is absent."
        ),
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # Even on arg parse errors, print a fallback and exit 0 (fail-open)
        print(_fallback_uuid(None))
        return 0

    if args.print_hash:
        # Resolve path: explicit --project-path if given, else cwd.
        # NEVER pass '' to project_hash — that would produce an empty string
        # (the same class of bug RC-1 fixes). Mirror the existing UUID path:
        # `proj_path = project_path or str(Path.cwd())`.
        proj_path = args.project_path or str(Path.cwd())
        print(project_hash(proj_path))
        return 0

    uuid = get_session_uuid(
        project_path=args.project_path,
        phase=args.phase,
    )
    print(uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
