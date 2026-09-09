#!/usr/bin/env python3
# CLAUDE-ADAPTER-OWNED — this file reads Claude Code JSONL sessions and/or
# Claude model pricing. Do NOT import this module from any file in
# quoin/core/. The portable cost-event schema is at
# quoin/core/scripts/cost_event.py. Adding runtime-neutral functionality
# belongs there, not here.
# cost_from_jsonl.py — local fallback for `ccusage session ... --json`.
# Pure stdlib. Walks ~/.claude/projects/{project-hash}/{uuid}.jsonl.
# Per the parent architecture's stage-two spec
# (.workflow_artifacts/quoin-foundation/architecture.md lines 92-123) and the
# parent's resolved open-question two: this is FALLBACK ONLY in stage 2.
#
# Pricing source: https://platform.claude.com/docs/en/about-claude/pricing,
# fetched and cross-checked against every entry below on each `verified` date.
# When prices drift in production (per lesson 2026-04-22), append a row to
# .workflow_artifacts/memory/lessons-learned.md and bump LAST_UPDATED.
#
# Every PRICES entry below carries its own "src" (the pricing page URL) and
# "verified" (ISO date of the fetch that confirmed it) — see IVG-260. Do not
# add an entry without both fields; the provenance test enforces this.
#
# cache_create is the 5-minute-TTL write rate (1.25x input on most models);
# cache_read is 0.1x input on most models — except claude-fable-5-1 and the
# Mythos 5.x family, which use 0.025x input for cache_read instead.
#
# Known gaps in this table (not per-model, and not fixed by IVG-260):
#   - 1-hour-TTL cache writes are 2x input, not the 5-minute 1.25x rate. This
#     single-field table has no TTL dimension, so ALL slugs understate
#     1-hour-TTL writes.
#   - Claude Opus 5 / Opus 4.8 fast mode bills at $10.00/$50.00 rather than
#     $5.00/$25.00. This table has no speed dimension, so fast-mode sessions
#     on those two models are under-priced by this table.
#   - claude-sonnet-5's introductory $2.00/$10.00 rate is now the standard,
#     permanent rate (the previously scheduled increase to $3.00/$15.00 on
#     2026-09-01 will not occur) — no discount-expiry caveat applies.
#   - This table has no long-context dimension, so a 1M-context session is
#     priced at the base slug's standard rates.
#
# This table is the authoritative Claude pricing source for quoin's cost
# tooling. The benchmark harness keeps a second, deliberately narrower table at
# quoin/benchmarks/harness/pricing.json, covering only the models the benchmark
# suite pins, so the harness reads as a standalone artifact. The two tables are
# not merged, but they must agree: a test cross-checks every model in the
# harness table against this one and fails on any drifted rate. When a shared
# model's price changes, update both.
LAST_UPDATED = "2026-09-08"
_PRICING_SRC = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICES = {  # USD per 1M tokens
    "claude-opus-4-7":            {"input":  5.00, "output": 25.00,
                                   "cache_create":  6.25, "cache_read":  0.50,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-opus-4-8":            {"input":  5.00, "output": 25.00,
                                   "cache_create":  6.25, "cache_read":  0.50,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-opus-4-6":            {"input":  5.00, "output": 25.00,
                                   "cache_create":  6.25, "cache_read":  0.50,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-opus-4-5":            {"input":  5.00, "output": 25.00,
                                   "cache_create":  6.25, "cache_read":  0.50,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-sonnet-4-6":          {"input":  3.00, "output": 15.00,
                                   "cache_create":  3.75, "cache_read":  0.30,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-sonnet-4-5":          {"input":  3.00, "output": 15.00,
                                   "cache_create":  3.75, "cache_read":  0.30,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-haiku-4-5-20251001":  {"input":  1.00, "output":  5.00,
                                   "cache_create":  1.25, "cache_read":  0.10,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    # claude-haiku-4-5: bare catalog alias of claude-haiku-4-5-20251001, added
    # defensively (IVG-249 T-03) — a bare alias is added only when the same
    # family already has a dated slug priced in this table, which this one does.
    "claude-haiku-4-5":           {"input":  1.00, "output":  5.00,
                                   "cache_create":  1.25, "cache_read":  0.10,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-fable-5":             {"input": 10.00, "output": 50.00,
                                   "cache_create": 12.50, "cache_read":  1.00,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    # claude-fable-5-1: cache_read is 0.025x input on this model (not the
    # usual 0.1x) — do not derive it from the standard multiplier.
    "claude-fable-5-1":           {"input": 10.00, "output": 50.00,
                                   "cache_create": 12.50, "cache_read":  0.25,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-opus-5":              {"input":  5.00, "output": 25.00,
                                   "cache_create":  6.25, "cache_read":  0.50,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
    "claude-sonnet-5":            {"input":  2.00, "output": 10.00,
                                   "cache_create":  2.50, "cache_read":  0.20,
                                   "src": _PRICING_SRC, "verified": "2026-09-08"},
}

import argparse
import glob
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone


# Non-model sentinel values that can appear in a transcript's message.model
# field. These are never real model IDs and are skipped before both the
# price lookup and per-model aggregation (IVG-260 D-07).
NON_MODEL_SENTINELS = frozenset({"<synthetic>"})

# Fallback-only normalization rewrites, applied in order, repeatedly, until
# the string stops changing. Exact PRICES lookup always runs first and wins;
# these only fire on an exact-lookup miss (IVG-260 D-02). Do NOT add a
# prefix-scan or family-inference rule here — see resolve_prices' docstring.
MODEL_ID_NORMALIZERS = (
    (re.compile(r"\[1m\]$"), ""),
    (re.compile(r"-\d{8}$"), ""),
)


def is_costable(model) -> bool:
    """True iff `model` is a non-empty string that isn't a non-model sentinel.
    Reproduces today's `if model:` guard byte-for-byte for the empty-string
    case, and additionally excludes NON_MODEL_SENTINELS."""
    return bool(model) and model not in NON_MODEL_SENTINELS


def resolve_prices(model):
    """Return the PRICES entry for `model`, or None if not costable/priced.

    Exact match always wins first. Only on a miss does this apply
    MODEL_ID_NORMALIZERS (stripping a trailing `[1m]` suffix, then a
    trailing `-` plus 8 digits) repeatedly until the string stops changing,
    then perform exactly one more exact PRICES lookup. A rewrite output that
    is not itself an exact key is a miss — this is NOT a prefix scan.

    Do NOT copy quoin/benchmarks/harness/cost.py:111's mechanism onto this
    path: that function does a prefix SCAN over the pricing table's keys
    (`model.startswith(key.rsplit("-", 1)[0])` tested per key), not a
    rewrite of the model string. A scan-derived match here would make an
    unrecognized slug silently price at a related model's rate and kill the
    unknown-model signal — exactly what IVG-260 D-02 exists to prevent.
    """
    if not is_costable(model):
        return None
    exact = PRICES.get(model)
    if exact is not None:
        return exact
    normalized = model
    while True:
        changed = False
        for pattern, replacement in MODEL_ID_NORMALIZERS:
            new_normalized = pattern.sub(replacement, normalized)
            if new_normalized != normalized:
                normalized = new_normalized
                changed = True
        if not changed:
            break
    return PRICES.get(normalized)


def is_priced(model) -> bool:
    """True iff resolve_prices(model) finds a price entry (exact or
    normalized)."""
    return resolve_prices(model) is not None


def project_hash(project_path: str) -> str:
    """Convert /abs/path/to/project to the ~/.claude/projects/HASH form
    used by Claude Code session JSONL files.
    Empirical rule (verified 2026-04-27 by listing ~/.claude/projects/ on the
    developer machine): replace ANY character that is NOT [A-Za-z0-9-] with '-'.
    This covers '/' → '-', '.' → '-', '@' → '-', '_' → '-', ' ' → '-', etc.
    Example: '/Users/ivgo/.../GoogleDrive-ivan.gorban@gmail.com/My Drive/...'
    becomes '-Users-ivgo-...-GoogleDrive-ivan-gorban-gmail-com-My-Drive-...'.
    Note: CLAUDE.md's legacy description 'project path with / replaced by -' is
    a simplification — the actual on-disk transform is the broader regex rule.
    Path-with-spaces: the project path may contain spaces (e.g., 'My Drive');
    the transform replaces spaces with '-' as well. Quote all path expansions
    in callers to prevent shell word-splitting."""
    return re.sub(r'[^A-Za-z0-9-]', '-', project_path)


def jsonl_path_for(uuid: str, proj_hash: str,
                   home: pathlib.Path = None) -> pathlib.Path:
    home = home or pathlib.Path.home()
    return home / ".claude" / "projects" / proj_hash / f"{uuid}.jsonl"


def cost_for_entry(model: str, usage: dict) -> tuple:
    """Returns (costUSD, totalTokens) for a single message.
    Unknown model values return (0.0, total_tokens) with no stderr side-effect
    — the caller (parse_session) handles unknown-model dedup and warning."""
    prices = resolve_prices(model)
    in_tok  = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    cc_tok  = usage.get("cache_creation_input_tokens", 0) or 0
    cr_tok  = usage.get("cache_read_input_tokens", 0) or 0
    total_tok = in_tok + out_tok + cc_tok + cr_tok
    if not prices:
        return (0.0, total_tok)
    cost = (in_tok    * prices["input"]
          + out_tok   * prices["output"]
          + cc_tok    * prices["cache_create"]
          + cr_tok    * prices["cache_read"]) / 1_000_000.0
    return (cost, total_tok)


def parse_session(path: pathlib.Path) -> dict:
    """Return {sessionId, totalCost, totalTokens, entries:[{model, costUSD, tokens}, ...],
    unknown_models, priceable, sentinel_rows}.
    unknown_models is the sorted, deduped list of model slugs seen that are not
    priced (via resolve_prices, exact-or-normalized). A non-model sentinel
    value (e.g. "<synthetic>") is skipped before both the price lookup and
    the per-model aggregation — it never reaches entries, unknown_models, or
    the warning branch; sentinel_rows counts how many such rows were seen
    (IVG-260 D-07). priceable is True iff (entries is non-empty OR
    sentinel_rows > 0) AND unknown_models is empty (IVG-260 D-08) — without
    the sentinel_rows clause, a session containing only sentinel rows would
    incorrectly report priceable=False since entries would be empty.
    Aggregates per-MESSAGE rows: each row's 'message' object may contain
    'model' and 'usage' (input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens). Per architecture I-04 (line 306), missing fields
    are tolerated (treated as 0); never crash. Unknown 'message.model' values
    are recorded with costUSD=0 and a one-line stderr warning (deduplicated
    per unique model value).

    Row counting: Claude Code JSONL files contain ALL assistant rows (including
    those that appear to be duplicates from history snapshots). We count every
    row that has a 'message' with 'usage' — this matches ccusage v18.0.11's
    behavior exactly (verified 2026-04-27 by parity testing against 3 real sessions).
    Do NOT deduplicate by message.id — ccusage does not deduplicate either.
    See also: quoin/skills/cost_snapshot/SKILL.md (Pricing parity note);
    quoin/scripts/tests/test_cost_parity_with_ccusage.py (regression test)."""
    session_id = path.stem  # UUID from filename
    per_model_cost = {}    # model -> float
    per_model_tok  = {}    # model -> int
    warned_models  = set()
    sentinel_rows  = 0

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                print(f"cost_from_jsonl: skipping malformed line {line_no} in {path}",
                      file=sys.stderr)
                continue

            msg = row.get("message")
            if not msg or not isinstance(msg, dict):
                # Control row with no message — skip, no cost.
                continue

            model = msg.get("model") or ""
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}

            if not is_costable(model):
                # A non-model sentinel (e.g. "<synthetic>") is dropped here,
                # before the price lookup and before aggregation — it must
                # never reach entries, unknown_models, or the warning below
                # (IVG-260 D-07). A truthy-but-sentinel model is counted; a
                # genuinely modelless row (model == "") is not.
                if model:
                    sentinel_rows += 1
                continue

            if not is_priced(model) and model not in warned_models:
                print(f"cost_from_jsonl: unknown model '{model}' — cost set to 0",
                      file=sys.stderr)
                warned_models.add(model)

            cost, tok = cost_for_entry(model, usage)
            per_model_cost[model] = per_model_cost.get(model, 0.0) + cost
            per_model_tok[model]  = per_model_tok.get(model, 0) + tok

    total_cost   = sum(per_model_cost.values())
    total_tokens = sum(per_model_tok.values())
    entries = [
        {"model": m, "costUSD": per_model_cost[m], "tokens": per_model_tok[m]}
        for m in per_model_cost
    ]

    unknown_models = sorted(warned_models)
    return {
        "sessionId":   session_id,
        "totalCost":   total_cost,
        "totalTokens": total_tokens,
        "entries":     entries,
        "unknown_models": unknown_models,
        "priceable":   (bool(entries) or sentinel_rows > 0) and not unknown_models,
        "sentinel_rows": sentinel_rows,
    }


def _parse_first_timestamp(path: pathlib.Path):
    """Return a datetime (UTC) from the first parseable 'timestamp' field in a
    JSONL file, or None if none found. Comparison is in UTC; --since YYYY-MM-DD
    is interpreted as YYYY-MM-DDT00:00:00Z."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                ts_str = row.get("timestamp")
                if ts_str and isinstance(ts_str, str):
                    try:
                        # Accept ISO 8601 with or without timezone.
                        ts_str_clean = ts_str.replace("Z", "+00:00")
                        return datetime.fromisoformat(ts_str_clean).replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        continue
    except (IOError, OSError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        prog="cost_from_jsonl.py",
        description="Local fallback for 'ccusage session ... --json'. "
                    "Pure stdlib. Reads ~/.claude/projects/<hash>/<uuid>.jsonl.",
    )
    sub = parser.add_subparsers(dest="command")

    sess_parser = sub.add_parser("session", help="Cost for a session")
    id_group = sess_parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("-i", dest="uuid", metavar="UUID",
                          help="Session UUID to look up")
    id_group.add_argument("--since", metavar="YYYY-MM-DD",
                          help="All sessions on or after this date (UTC)")
    sess_parser.add_argument("--json", action="store_true",
                             help="Emit JSON output (accepted for ccusage CLI compat; "
                                  "always active — output is always JSON)")
    sess_parser.add_argument("--project-path", metavar="PATH",
                             default=None,
                             help="Override project path (default: cwd). "
                                  "Used in tests to pin the hash.")

    args = parser.parse_args()

    if args.command != "session":
        parser.print_help()
        sys.exit(0)

    project_path = args.project_path if args.project_path else os.getcwd()
    proj_hash = project_hash(project_path)

    if args.uuid:
        # Per-UUID mode
        jsonl = jsonl_path_for(args.uuid, proj_hash)
        if not jsonl.exists():
            print(
                f"cost_from_jsonl: UUID {args.uuid!r} not found "
                f"in ~/.claude/projects/{proj_hash}/",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            result = parse_session(jsonl)
        except (IOError, OSError) as exc:
            print(f"cost_from_jsonl: error reading {jsonl}: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result))
        sys.exit(0)

    else:
        # --since mode: glob all *.jsonl under the project hash dir
        since_str = args.since
        try:
            since_dt = datetime.strptime(since_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            print(f"cost_from_jsonl: invalid --since date {since_str!r} "
                  "(expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)

        proj_dir = pathlib.Path.home() / ".claude" / "projects" / proj_hash
        pattern = str(proj_dir / "*.jsonl")
        jsonl_files = sorted(glob.glob(pattern))

        results = []
        for fpath in jsonl_files:
            p = pathlib.Path(fpath)
            ts = _parse_first_timestamp(p)
            if ts is None:
                print(f"cost_from_jsonl: no parseable timestamp in {p} — skipping",
                      file=sys.stderr)
                continue
            if ts >= since_dt:
                try:
                    results.append(parse_session(p))
                except (IOError, OSError) as exc:
                    print(f"cost_from_jsonl: error reading {p}: {exc}",
                          file=sys.stderr)
                    continue

        # Results are in filesystem iteration order (glob). For ccusage-emitted
        # files the filename embeds an ISO timestamp prefix, so filename order
        # approximates chronological order. No explicit re-sort is applied here.
        print(json.dumps(results))
        sys.exit(0)


if __name__ == "__main__":
    main()
