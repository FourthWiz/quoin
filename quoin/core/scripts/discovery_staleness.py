#!/usr/bin/env python3
"""Portable-core discovery staleness detector.

Public API:
    staleness_report(project_root, *, now=None) -> dict

CLI:
    python3 discovery_staleness.py [project_root] [--json] [--quiet]

Exit codes:
    0  — fresh or disabled
    2  — invocation error
    10 — discovery stale (older than the threshold, or a discovery-dirty marker exists)
    11 — discovery absent (no discovery artifacts found)
    12 — Serena present-but-stale (discovery fresh, serena marker exists AND is stale)

No third-party imports; stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (overridable via env vars)
# ---------------------------------------------------------------------------

_DEFAULT_DISCOVERY_STALE_DAYS = 7
_DEFAULT_SERENA_STALE_DAYS = 30

# Marker in .workflow_artifacts/memory/ written by /run when a task changed the
# project's structure; its presence makes discovery stale regardless of age.
DISCOVERY_DIRTY_MARKER = "discovery-dirty"

# Memory artifacts (fallback signal sources d)
_MEMORY_ARTIFACT_NAMES = (
    "repos-inventory.md",
    "architecture-overview.md",
    "dependencies-map.md",
    "git-log.md",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    """Read a positive integer from an env var; fall back to default on error."""
    val = os.environ.get(name, "")
    try:
        n = int(val)
        if n > 0:
            return n
    except (ValueError, TypeError):
        pass
    return default


def _is_disabled() -> bool:
    """Return True when the master-off switch is set."""
    return os.environ.get("QUOIN_DISCOVERY_REFRESH_DISABLE", "") == "1"


def _parse_updated_z_safe(ts_str: str) -> datetime | None:
    """Parse an ISO timestamp, normalising trailing Z for Python 3.10 compat.

    Python 3.10 does not accept 'Z' as a UTC suffix in fromisoformat().
    Normalise to '+00:00' first. Returns None on any parse failure (no-signal).
    """
    try:
        normalised = ts_str.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalised)
    except (ValueError, AttributeError):
        return None


def _file_mtime_utc(path: Path) -> datetime | None:
    """Return the mtime of *path* as a UTC-aware datetime, or None."""
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        return None


def _parse_staleness_updated_column(staleness_path: Path) -> datetime | None:
    """Read _staleness.md and return the newest 'Updated' column value as UTC datetime.

    The file has rows like:
      | repo | HEAD | Updated |
    We scan every data row for an ISO-ish timestamp in the Updated column (col 3, index 2).
    Returns None when the file is absent, unreadable, or has no parseable timestamps.
    Never uses a directory mtime.
    """
    try:
        text = staleness_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    newest: datetime | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        # parts[0] == '' (before first |), parts[-1] == '' (after last |)
        # data columns start at index 1
        # We need at least 4 parts: '' | col1 | col2 | col3 | ''
        if len(parts) < 4:
            continue
        # Skip header and separator rows
        col3 = parts[3] if len(parts) > 3 else ""
        if not col3 or col3.startswith("---") or col3.lower() in ("updated", ""):
            continue
        ts = _parse_updated_z_safe(col3)
        if ts is None:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


# ---------------------------------------------------------------------------
# Serena sub-report
# ---------------------------------------------------------------------------

def _serena_subreport(project_root: Path, now: datetime) -> dict:
    """Return Serena marker state dict.

    Keys:
      present_marker (bool) — True when serena-onboarded.md exists
      age_days (float | None) — age of the marker file, or None when absent
      stale (bool) — True when present_marker=True AND age_days > threshold
    """
    threshold_days = _env_int("QUOIN_SERENA_STALE_DAYS", _DEFAULT_SERENA_STALE_DAYS)
    marker_path = project_root / ".workflow_artifacts" / "memory" / "serena-onboarded.md"

    marker_mtime = _file_mtime_utc(marker_path)
    if marker_mtime is None:
        # Absent marker — not yet onboarded, not a banner trigger (Graceful Absence)
        return {
            "present_marker": False,
            "age_days": None,
            "stale": False,
            "threshold_days": threshold_days,
        }

    age_days = (now - marker_mtime).total_seconds() / 86400.0
    stale = age_days > threshold_days
    return {
        "present_marker": True,
        "age_days": age_days,
        "stale": stale,
        "threshold_days": threshold_days,
    }


# ---------------------------------------------------------------------------
# Core staleness report
# ---------------------------------------------------------------------------

def staleness_report(
    project_root: Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Return a staleness report dict for the given project root.

    Return shape:
    {
      "verdict": "fresh" | "stale" | "absent" | "serena_stale",
      "age_days": float | None,
      "threshold_days": int,
      "signal_source": str | None,
      "serena": { "present_marker": bool, "age_days": float|None, "stale": bool, "threshold_days": int },
      "disabled": bool,
    }
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    if _is_disabled():
        return {
            "verdict": "fresh",
            "age_days": None,
            "threshold_days": _env_int("QUOIN_DISCOVERY_STALE_DAYS", _DEFAULT_DISCOVERY_STALE_DAYS),
            "signal_source": None,
            "serena": {
                "present_marker": False,
                "age_days": None,
                "stale": False,
                "threshold_days": _env_int("QUOIN_SERENA_STALE_DAYS", _DEFAULT_SERENA_STALE_DAYS),
            },
            "disabled": True,
        }

    threshold_days = _env_int("QUOIN_DISCOVERY_STALE_DAYS", _DEFAULT_DISCOVERY_STALE_DAYS)
    wa_dir = project_root / ".workflow_artifacts"
    cache_dir = wa_dir / "cache"
    memory_dir = wa_dir / "memory"
    staleness_file = cache_dir / "_staleness.md"
    repo_heads_file = memory_dir / "repo-heads.md"

    # ── Signal precedence ────────────────────────────────────────────────────
    # (a) newest Updated column in _staleness.md
    token: datetime | None = None
    source: str | None = None

    updated_col = _parse_staleness_updated_column(staleness_file)
    if updated_col is not None:
        token = updated_col
        source = "staleness_updated_column"

    # (b) mtime of _staleness.md
    if token is None:
        mtime = _file_mtime_utc(staleness_file)
        if mtime is not None:
            token = mtime
            source = "staleness_file_mtime"

    # (c) mtime of repo-heads.md
    if token is None:
        mtime = _file_mtime_utc(repo_heads_file)
        if mtime is not None:
            token = mtime
            source = "repo_heads_mtime"

    # (d) newest mtime among the 4 memory artifacts
    if token is None:
        newest_artifact: datetime | None = None
        for name in _MEMORY_ARTIFACT_NAMES:
            mtime = _file_mtime_utc(memory_dir / name)
            if mtime is not None:
                if newest_artifact is None or mtime > newest_artifact:
                    newest_artifact = mtime
        if newest_artifact is not None:
            token = newest_artifact
            source = "memory_artifact_mtime"

    # (e) absent
    if token is None:
        serena = _serena_subreport(project_root, now)
        return {
            "verdict": "absent",
            "age_days": None,
            "threshold_days": threshold_days,
            "signal_source": None,
            "serena": serena,
            "disabled": False,
        }

    age_days = (now - token).total_seconds() / 86400.0
    serena = _serena_subreport(project_root, now)

    # ── Combined verdict ─────────────────────────────────────────────────────
    # A task that changed the project's structure leaves a discovery-dirty
    # marker; the next /discover run deletes it. Until then the memory is
    # out of date whatever its age.
    dirty = (memory_dir / DISCOVERY_DIRTY_MARKER).is_file()
    if dirty:
        verdict = "stale"           # exit 10
        source = "discovery_dirty_marker"
    elif age_days > threshold_days:
        verdict = "stale"           # exit 10
    elif serena["present_marker"] and serena["stale"]:
        verdict = "serena_stale"    # exit 12 (present-but-stale only)
    else:
        verdict = "fresh"           # exit 0 (incl. absent-marker)

    return {
        "verdict": verdict,
        "age_days": age_days,
        "threshold_days": threshold_days,
        "signal_source": source,
        "serena": serena,
        "disabled": False,
    }


# ---------------------------------------------------------------------------
# Exit-code mapping
# ---------------------------------------------------------------------------

_VERDICT_TO_EXIT = {
    "fresh": 0,
    "stale": 10,
    "absent": 11,
    "serena_stale": 12,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]

    import argparse

    parser = argparse.ArgumentParser(
        prog="discovery_staleness",
        description="Report whether quoin discovery memory is stale.",
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help="Path to the project root (default: cwd).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the full report as JSON.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all output (exit code only).",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    root = Path(args.project_root).resolve() if args.project_root else Path.cwd()

    try:
        report = staleness_report(root)
    except Exception as exc:  # pragma: no cover — defensive
        if not args.quiet:
            print(f"[discovery_staleness] error: {exc}", file=sys.stderr)
        return 2

    exit_code = _VERDICT_TO_EXIT.get(report["verdict"], 0)

    if not args.quiet:
        if args.as_json:
            print(json.dumps(report, default=str))
        else:
            age = report["age_days"]
            age_str = f"{age:.1f}d" if age is not None else "unknown"
            if report.get("disabled"):
                print(f"[discovery_staleness] disabled (QUOIN_DISCOVERY_REFRESH_DISABLE=1)")
            else:
                print(
                    f"[discovery_staleness] verdict={report['verdict']} "
                    f"age={age_str} "
                    f"threshold={report['threshold_days']}d "
                    f"source={report['signal_source']}"
                )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
