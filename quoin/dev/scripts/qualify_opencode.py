#!/usr/bin/env python3
"""Add an OpenCode release to the adapter's qualified list.

Dev tooling only: used by the manual OpenCode qualification workflow before it
runs the binary-tier tests against a candidate, and by hand. Stdlib only.

    python3 quoin/dev/scripts/qualify_opencode.py add 1.19.0
    python3 quoin/dev/scripts/qualify_opencode.py latest      # print the newest upstream release

`add` writes these in the source tree and nothing else:

- the release into `adapters/opencode/qualified-versions.json` (kept sorted);
- the `Qualified:` and `Floor:` lines of `adapters/opencode/compatibility.md`
  and the OpenCode row of the compatibility matrix in `release-acceptance.md`;
- a native-config schema subset for the release, copied from the nearest lower
  qualified release when the release has none yet. A copy is a starting point:
  a reviewer rechecks it against the new tag before the change is merged.

Exit status: 0 when the release was added, 3 when it was already qualified,
2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE = REPO_ROOT / "quoin"
RELEASES_API = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
_VERSION = re.compile(r"\d+\.\d+\.\d+")
SCHEMA_TEMPLATE = "opencode-%s-config.subset.schema.json"


def _key(version: str):
    return tuple(int(p) for p in version.split("."))


def add(source: Path, version: str) -> int:
    if not _VERSION.fullmatch(version):
        print("qualify_opencode: %r is not an X.Y.Z release" % version, file=sys.stderr)
        return 2
    adapter = source / "adapters" / "opencode"
    list_path = adapter / "qualified-versions.json"
    data = json.loads(list_path.read_text(encoding="utf-8"))
    qualified = sorted(data["qualified"], key=_key)
    if version in qualified:
        print("qualify_opencode: %s is already qualified" % version)
        return 3
    lower = [v for v in qualified if _key(v) < _key(version)]
    if not lower:
        print("qualify_opencode: %s is older than the floor %s" % (version, qualified[0]), file=sys.stderr)
        return 2
    qualified = sorted(qualified + [version], key=_key)
    data["qualified"] = qualified
    list_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    compat = adapter / "compatibility.md"
    text = compat.read_text(encoding="utf-8")
    text = re.sub(r"^Qualified:.*$", "Qualified: " + ", ".join(qualified), text, count=1, flags=re.M)
    text = re.sub(r"^Floor:.*$", "Floor: " + qualified[0], text, count=1, flags=re.M)
    compat.write_text(text, encoding="utf-8")

    acceptance = adapter / "release-acceptance.md"
    text = acceptance.read_text(encoding="utf-8")
    text = re.sub(
        r"^\| OpenCode \| [^|]+ \|", "| OpenCode | %s |" % ", ".join(qualified), text, count=1, flags=re.M,
    )
    acceptance.write_text(text, encoding="utf-8")

    schemas = adapter / "schemas"
    target = schemas / (SCHEMA_TEMPLATE % version)
    if not target.exists():
        base = max(lower, key=_key)
        body = (schemas / (SCHEMA_TEMPLATE % base)).read_text(encoding="utf-8")
        body = body.replace("native-config:%s" % base, "native-config:%s" % version)
        body = body.replace("OpenCode %s configuration subset" % base, "OpenCode %s configuration subset" % version)
        target.write_text(body, encoding="utf-8")
        print("qualify_opencode: copied the schema subset of %s to %s" % (base, target.name))
    print("qualify_opencode: qualified releases are now %s" % ", ".join(qualified))
    return 0


def latest() -> int:
    request = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URL
        tag = json.load(response).get("tag_name", "")
    version = tag[1:] if tag.startswith("v") else tag
    if not _VERSION.fullmatch(version):
        print("qualify_opencode: the latest release tag %r is not an X.Y.Z release" % tag, file=sys.stderr)
        return 2
    print(version)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser("add", help="add a release to the qualified list")
    add_parser.add_argument("version")
    add_parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    sub.add_parser("latest", help="print the newest upstream OpenCode release")
    args = parser.parse_args(argv)
    if args.command == "add":
        return add(args.source_dir, args.version)
    return latest()


if __name__ == "__main__":
    sys.exit(main())
