#!/usr/bin/env python3
"""Census of SKILL.md frontmatter `description:` byte sizes (IVG-164 S-4).

Metric: YAML-parse each skill's frontmatter block, take the `description`
value, and count `len(value.encode("utf-8"))` — UTF-8 BYTES, not characters
(multi-byte characters count as their byte length; the census never
normalizes or flags non-ASCII).

Mean rounding rule: round-half-up to the nearest integer, i.e.
`math.floor(total / n + 0.5)` — so 13470/32 = 420.9375 prints as mean=421.
This is a deliberate, reproducible rule, not truncation.

Rows are sorted by byte size descending, with skill name ascending as the
secondary key so ties print in a deterministic, diffable order.

Quoting-hazard guard: for every skill, the raw `description:` line's value
token (including its two `"` delimiters) must be exactly 2 bytes longer than
the parsed value — any other delta means an escape sequence or block-scalar
switch crept in, and the census fails loudly (exit 3).

The census reads the Claude adapter tree (`quoin/adapters/claude/skills/`).

NOTE (phrase parsing): trigger phrases inside descriptions may contain
internal apostrophes (8 of 32 skills do). NO code in this script or its
tests may extract phrases with quote-delimiter parsing; use frozen literals
or `', '`-boundary splits instead.

Exit codes: 0 ok; 3 quoting-hazard guard tripped.

Dev tooling only — NOT deployed (lives under quoin/dev/scripts/, outside
the DEPLOYED_SCRIPTS roster by design; see plan D-01).
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
from pathlib import Path

import yaml

# Source root: this file lives at <git-root>/quoin/dev/scripts/, so the
# source package root (containing adapters/) is parents[2].
SRC_ROOT = Path(__file__).resolve().parents[2]
TREES = {
    "adapter": SRC_ROOT / "adapters" / "claude" / "skills",
}


def _frontmatter_block(text: str, path: Path) -> str:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"census: no frontmatter open marker in {path}")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    raise SystemExit(f"census: unterminated frontmatter in {path}")


def _raw_description_token(block: str, path: Path) -> str:
    for line in block.split("\n"):
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit(f"census: no description: line in {path}")


def census_tree(tree_root: Path) -> dict[str, int]:
    """Return {skill_name: description_utf8_bytes} for one tree."""
    result: dict[str, int] = {}
    for skill_dir in sorted(p for p in tree_root.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        block = _frontmatter_block(text, skill_md)
        data = yaml.safe_load(block)
        if not isinstance(data, dict) or "description" not in data:
            raise SystemExit(f"census: no description key in {skill_md}")
        value = data["description"]
        if not isinstance(value, str):
            raise SystemExit(f"census: non-string description in {skill_md}")
        raw_token = _raw_description_token(block, skill_md)
        delta = len(raw_token.encode("utf-8")) - len(value.encode("utf-8"))
        if delta != 2:
            print(
                f"census: quoting-hazard guard tripped for {skill_md}: "
                f"raw-minus-parsed delta is {delta}, expected 2 "
                "(escape sequence or block-scalar switch?)",
                file=sys.stderr,
            )
            raise SystemExit(3)
        result[skill_dir.name] = len(value.encode("utf-8"))
    return result


def mean_half_up(total: int, n: int) -> int:
    """Round-half-up mean: math.floor(total/n + 0.5)."""
    if n == 0:
        return 0
    return math.floor(total / n + 0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", metavar="PATH", help="write census JSON to PATH")
    parser.add_argument(
        "--text", action="store_true",
        help="print per-skill rows + summary (default output mode)",
    )
    parser.add_argument(
        "--baseline", metavar="PATH", help="write a baseline snapshot JSON to PATH"
    )
    parser.add_argument(
        "--compare", metavar="PATH",
        help="compare against a baseline snapshot JSON at PATH (prints pct-vs-baseline)",
    )
    parser.add_argument(
        "--adapter-root", default=str(TREES["adapter"]), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    sizes = census_tree(Path(args.adapter_root))
    total = sum(sizes.values())
    n = len(sizes)
    mean = mean_half_up(total, n)
    rows = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))

    payload = {
        "metric": "unquoted description value bytes, UTF-8",
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "tree": "adapter",
        "total": total,
        "skills": n,
        "mean": mean,
        "mean_rounding": "round-half-up: math.floor(total/n + 0.5)",
        "per_skill": dict(rows),
    }

    # text output (default; --json alone suppresses it unless --text given)
    if args.text or not args.json:
        for name, size in rows:
            print(f"{size:5d}  {name}")
        print(f"total={total} skills={n} mean={mean}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        base_total = base["total"]
        delta = base_total - total
        pct = (delta / base_total * 100.0) if base_total else 0.0
        print(
            f"baseline_total={base_total} current_total={total} "
            f"saved={delta} pct_vs_baseline={pct:.2f}%"
        )
        payload["baseline_total"] = base_total
        payload["saved_bytes"] = delta
        payload["pct_vs_baseline"] = round(pct, 2)

    if args.baseline:
        Path(args.baseline).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
