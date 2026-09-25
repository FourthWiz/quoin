"""CLI entry point: `python -m quoin.opencode_adapter`.

The only subcommand today is `check-manifest`, which runs the drift check
in `manifest.py` against a source tree and reports the result. Used
standalone and by the OpenCode manifest drift-check step in CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from quoin.opencode_adapter import manifest as _manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m quoin.opencode_adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser(
        "check-manifest", help="check the feature manifest against the catalog and compatibility pin"
    )
    check.add_argument(
        "--source-dir",
        default=None,
        help="path to the quoin data source directory (defaults to the resolved install source)",
    )
    return parser


def _resolve_check_source_dir(explicit: Optional[str]) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    # Lazily imported: this is the only caller in this module that needs the
    # CLI's own wheel/editable resolution, and importing quoin.cli eagerly
    # would pull in its heavier dependency surface for every invocation.
    from quoin.cli import _resolve_source_dir

    return _resolve_source_dir(None)


def _run_check_manifest(source_dir_arg: Optional[str]) -> int:
    try:
        source_dir = _resolve_check_source_dir(source_dir_arg)
    except SystemExit as exc:
        # quoin.cli._resolve_source_dir already printed its own diagnostic
        # to stderr and exits 2 on a usage error; propagate its code.
        return exc.code if isinstance(exc.code, int) else 2

    if not source_dir.is_dir():
        print("opencode manifest: source dir %s is not a directory" % source_dir, file=sys.stderr)
        return 2

    try:
        data = _manifest.load_manifest(source_dir)
        catalog = _manifest.load_catalog(source_dir)
        pinned_version = _manifest.read_pinned_version(source_dir)
    except _manifest.ManifestLoadError as exc:
        print("opencode manifest: %s" % exc, file=sys.stderr)
        return 2

    errors = _manifest.check_manifest(data, catalog, pinned_version)
    if errors:
        for err in errors:
            print("opencode manifest: %s" % err, file=sys.stderr)
        return 1

    rows = data.get("catalog_entries", [])
    counts = {}
    for row in rows:
        counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
    per_status = ", ".join("%s=%d" % (status, counts.get(status, 0)) for status in _manifest.STATUSES)
    print("opencode manifest: no drift (%d rows: %s)" % (len(rows), per_status))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "check-manifest":
        return _run_check_manifest(args.source_dir)
    parser.error("unknown command %r" % (args.command,))  # pragma: no cover - argparse enforces choices
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
