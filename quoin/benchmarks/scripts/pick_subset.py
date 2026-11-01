"""
pick_subset.py — Deterministic subset selector for quoin benchmark suite v1.

Reads the upstream EvalPlus HumanEval+ manifest and SWE-bench Lite manifest,
applies a seeded shuffle, and writes suite-v1.json with the selected subset.

Usage:
    python3 quoin/benchmarks/scripts/pick_subset.py --seed 1729

Output is byte-for-byte reproducible from the same seed and the same upstream
manifests. If the upstream manifests change (new problems added, IDs renumbered),
re-run with the same seed; compare output SHA against the previous suite-v1.json
to detect any drift.

The script writes output to stdout (JSON) by default, or to --output if given.

Tiered mode (--suite tiered-small) builds a nine-task suite from a vendored
snapshot of SWE-bench Verified, stratified by the upstream difficulty label.
It reads only vendored files and never opens a network connection. The
snapshot itself is rebuilt by a maintainer with --refresh-verified-snapshot
from a parquet file downloaded at a pinned dataset revision.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import random
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Upstream manifest stubs
# When running live against the actual evalplus / swebench packages, replace
# these stubs with real data loader calls:
#   from evalplus.data import get_human_eval_plus
#   from swebench.harness.utils import load_swebench_dataset
# For the v1 deterministic suite the actual list of IDs is fixed here to
# guarantee byte-for-byte reproducibility without network access.
# ---------------------------------------------------------------------------

HUMANEVAL_PLUS_IDS = [f"HumanEval/{i}" for i in range(164)]

SWEBENCH_LITE_PROBLEM_IDS = [
    "astropy__astropy-12907",
    "astropy__astropy-13033",
    "astropy__astropy-13236",
    "astropy__astropy-13390",
    "astropy__astropy-14096",
    "astropy__astropy-14182",
    "astropy__astropy-14309",
    "astropy__astropy-14365",
    "astropy__astropy-6938",
    "astropy__astropy-7746",
    "django__django-10097",
    "django__django-10606",
    "django__django-10924",
    "django__django-11019",
    "django__django-11099",
    "django__django-11133",
    "django__django-11422",
    "django__django-11564",
    "django__django-11742",
    "django__django-11815",
    "django__django-11999",
    "django__django-12286",
    "django__django-12453",
    "matplotlib__matplotlib-18869",
    "matplotlib__matplotlib-23299",
    "psf__requests-2317",
    "psf__requests-3362",
    "pylint-dev__pylint-5859",
    "pydata__xarray-4094",
    "scikit-learn__scikit-learn-10508",
    "scikit-learn__scikit-learn-13142",
    "sphinx-doc__sphinx-8627",
    "sympy__sympy-14396",
    "sympy__sympy-15346",
    "sympy__sympy-15670",
    "sympy__sympy-16503",
    "sympy__sympy-17022",
    "sympy__sympy-17139",
    "sympy__sympy-17630",
    "sympy__sympy-18087",
]

DIFFICULTY_BANDS = {
    "easy": list(range(0, 30)),
    "medium": list(range(30, 100)),
    "hard": list(range(100, 164)),
}

EXPECTED_MINUTES_BY_DIFFICULTY = {
    "easy": 3,
    "medium": 5,
    "hard": 8,
}


def classify_difficulty(idx: int) -> str:
    if idx < 30:
        return "easy"
    if idx < 100:
        return "medium"
    return "hard"


def pick_humaneval_plus(n: int, seed: int) -> list[dict]:
    """Select n HumanEval+ tasks reproducibly from the full set."""
    rng = random.Random(seed)
    sorted_ids = sorted(HUMANEVAL_PLUS_IDS)
    shuffled = list(sorted_ids)
    rng.shuffle(shuffled)
    selected = shuffled[:n]
    # Sort selected by numeric ID for stable output order
    selected.sort(key=lambda x: int(x.split("/")[1]))

    tasks = []
    for rank, source_id in enumerate(selected):
        idx = int(source_id.split("/")[1])
        difficulty = classify_difficulty(idx)
        tasks.append(
            {
                "id": f"humaneval_plus_{rank:03d}",
                "source": "evalplus_humaneval_plus",
                "source_id": source_id,
                "difficulty_band": difficulty,
                "deterministic_seed": seed,
                "expected_minutes_p50": EXPECTED_MINUTES_BY_DIFFICULTY[difficulty],
            }
        )
    return tasks


def pick_swebench_lite(n: int, seed: int) -> list[dict]:
    """Select n SWE-bench Lite tasks reproducibly."""
    rng = random.Random(seed + 1)  # distinct sub-seed for each benchmark
    sorted_ids = sorted(SWEBENCH_LITE_PROBLEM_IDS)
    shuffled = list(sorted_ids)
    rng.shuffle(shuffled)
    selected = shuffled[:n]
    selected.sort()

    tasks = []
    for rank, source_id in enumerate(selected):
        tasks.append(
            {
                "id": f"swebench_lite_{rank:03d}",
                "source": "swebench_lite",
                "source_id": source_id,
                "difficulty_band": "hard",
                "deterministic_seed": seed,
                "expected_minutes_p50": 15,
            }
        )
    return tasks


def build_suite(
    n_humaneval: int,
    n_swebench: int,
    seed: int,
) -> dict:
    humaneval_tasks = pick_humaneval_plus(n_humaneval, seed)
    swebench_tasks = pick_swebench_lite(n_swebench, seed)
    tasks = humaneval_tasks + swebench_tasks

    suite = {
        "schema_version": 2,
        "description": (
            f"Quoin benchmark v1 task suite — {n_humaneval} EvalPlus HumanEval+ tasks"
            f" + {n_swebench} SWE-bench Lite instances."
            f" Generated deterministically via pick_subset.py --seed {seed}."
            " Do not edit manually; regenerate from pick_subset.py."
        ),
        "generated_by": "quoin/benchmarks/scripts/pick_subset.py",
        "seed": seed,
        "provenance_file": "quoin/benchmarks/suite-v1-provenance.md",
        "tasks": tasks,
    }
    return suite


# ---------------------------------------------------------------------------
# Tiered suite over SWE-bench Verified
# ---------------------------------------------------------------------------

VERIFIED_DATASET = "princeton-nlp/SWE-bench_Verified"

KNOWN_VERIFIED_DIFFICULTIES = (
    "<15 min fix",
    "15 min - 1 hour",
    "1-4 hours",
    ">4 hours",
)

# One wall clock for every tier and both arms: a spend cap of $90 at the
# observed spend rate, with 1.5x headroom, so the wall clock does not bind first.
TIERED_SMALL_WALL_CLOCK_SECONDS = 19200

# Estimates are advisory and fixed before any result exists. The ">4 hours"
# label has no tier and is therefore never selected.
TIERED_SMALL_SPEC = {
    "easy": {
        "label": "<15 min fix",
        "n": 4,
        "sub_seed_offset": 101,
        "wall_clock_seconds": TIERED_SMALL_WALL_CLOCK_SECONDS,
        "est_tokens_in": 250000,
        "est_tokens_out": 12000,
    },
    "medium": {
        "label": "15 min - 1 hour",
        "n": 3,
        "sub_seed_offset": 102,
        "wall_clock_seconds": TIERED_SMALL_WALL_CLOCK_SECONDS,
        "est_tokens_in": 500000,
        "est_tokens_out": 25000,
    },
    "large": {
        "label": "1-4 hours",
        "n": 2,
        "sub_seed_offset": 103,
        "wall_clock_seconds": TIERED_SMALL_WALL_CLOCK_SECONDS,
        "est_tokens_in": 900000,
        "est_tokens_out": 45000,
    },
}

# Derived in suite-tiered-small-provenance.md: the cap is the largest repriced
# cost of a comparable full-pipeline task under $100, rounded up to $5; the
# worst case is cap x 9 tasks x 2 arms; the ceiling adds 10% and rounds up.
TIERED_SMALL_COST = {
    "worst_case_target_usd": 1620,
    "worst_case_ceiling_usd": 1790,
    "documented_cap_usd": 90,
}


# tiered-balanced: 40 tasks (10 easy, 15 medium, 15 large). The token estimates
# are reused from tiered-small per tier. They are advisory only; the quoin cell
# budgets on wall_clock_seconds, so a new figure would have no consumer.
# The wall clock keeps the 1.5x headroom rule over the observed spend rate at the
# $35 per-session cap.
TIERED_BALANCED_WALL_CLOCK_SECONDS = 17400

TIERED_BALANCED_SPEC = {
    "easy": {
        "label": "<15 min fix",
        "n": 10,
        "sub_seed_offset": 101,
        "wall_clock_seconds": TIERED_BALANCED_WALL_CLOCK_SECONDS,
        "est_tokens_in": TIERED_SMALL_SPEC["easy"]["est_tokens_in"],
        "est_tokens_out": TIERED_SMALL_SPEC["easy"]["est_tokens_out"],
    },
    "medium": {
        "label": "15 min - 1 hour",
        "n": 15,
        "sub_seed_offset": 102,
        "wall_clock_seconds": TIERED_BALANCED_WALL_CLOCK_SECONDS,
        "est_tokens_in": TIERED_SMALL_SPEC["medium"]["est_tokens_in"],
        "est_tokens_out": TIERED_SMALL_SPEC["medium"]["est_tokens_out"],
    },
    "large": {
        "label": "1-4 hours",
        "n": 15,
        "sub_seed_offset": 103,
        "wall_clock_seconds": TIERED_BALANCED_WALL_CLOCK_SECONDS,
        "est_tokens_in": TIERED_SMALL_SPEC["large"]["est_tokens_in"],
        "est_tokens_out": TIERED_SMALL_SPEC["large"]["est_tokens_out"],
    },
}

# Cap $35 per session x 40 tasks x 2 arms = $2800 worst case; the ceiling adds
# 10% and rounds up to a multiple of $10.
TIERED_BALANCED_COST = {
    "worst_case_target_usd": 2800,
    "worst_case_ceiling_usd": 3170,
    "documented_cap_usd": 35,
}

TIERED_BALANCED_REVIEW = {
    "pairwise_review_cap_usd": 2,
    "pairwise_review_total_cap_usd": 80,
}


PROMPT_TEMPLATE_VERSION = "verified-v1"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SNAPSHOT_PATH = _DATA_DIR / "swebench-verified-snapshot.json"
DEFAULT_PROBLEMS_PATH = _DATA_DIR / "swebench-verified-problems.json"
DEFAULT_PROBLEMS_PATH_BALANCED = _DATA_DIR / "swebench-verified-problems-tiered-balanced.json"

# The tiered-small entry holds the module dicts themselves, so edits made to
# them in place stay visible through the registry.
TIERED_SUITES = {
    "tiered-small": {
        "tiers": TIERED_SMALL_SPEC,
        "cost": TIERED_SMALL_COST,
        "provenance_file": "quoin/benchmarks/suite-tiered-small-provenance.md",
        "problems_path": DEFAULT_PROBLEMS_PATH,
        "extra_tiered_fields": {},
    },
    "tiered-balanced": {
        "tiers": TIERED_BALANCED_SPEC,
        "cost": TIERED_BALANCED_COST,
        "provenance_file": "quoin/benchmarks/suite-tiered-balanced-provenance.md",
        "problems_path": DEFAULT_PROBLEMS_PATH_BALANCED,
        "extra_tiered_fields": dict(TIERED_BALANCED_REVIEW),
        "records_problems_file": True,
    },
}

SNAPSHOT_FIELDS = ["instance_id", "repo", "base_commit", "difficulty"]
VERIFIED_SOURCE_FILE = "data/test-00000-of-00001.parquet"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class TieredSuiteError(Exception):
    """Raised when the vendored snapshot or problems file is unusable."""


def render_suite(suite: dict) -> str:
    return json.dumps(suite, indent=2, ensure_ascii=False) + "\n"


def _write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _count_difficulties(rows) -> dict:
    counts = collections.Counter(r["difficulty"] for r in rows)
    return {label: counts[label] for label in sorted(counts)}


def load_verified_snapshot(path) -> dict:
    path = Path(path)
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TieredSuiteError(f"cannot read snapshot {path}: {exc}")
    for key in ("dataset", "revision", "row_count", "difficulty_counts", "rows"):
        if key not in snap:
            raise TieredSuiteError(f"snapshot {path} is missing '{key}'")
    if not _SHA40.match(str(snap["revision"])):
        raise TieredSuiteError("snapshot revision is not a 40-hex commit SHA")
    rows = snap["rows"]
    if not isinstance(rows, list):
        raise TieredSuiteError("snapshot 'rows' must be a list")
    if snap["row_count"] != len(rows):
        raise TieredSuiteError(
            f"snapshot row_count {snap['row_count']} != {len(rows)} rows"
        )
    for idx, r in enumerate(rows):
        if not isinstance(r, dict) or sorted(r.keys()) != sorted(SNAPSHOT_FIELDS):
            raise TieredSuiteError(
                f"snapshot row {idx} must be an object with exactly the keys {SNAPSHOT_FIELDS}"
            )
        if not all(isinstance(v, str) for v in r.values()):
            raise TieredSuiteError(f"snapshot row {idx} has a non-string value")
        if not _INSTANCE_ID.match(r["instance_id"]):
            raise TieredSuiteError(f"snapshot row {idx} has a malformed instance_id")
        if not _REPO_NAME.match(r["repo"]) or ".." in r["repo"]:
            raise TieredSuiteError(f"snapshot row {idx} has a malformed repo")
    ids = [r.get("instance_id") for r in rows]
    if ids != sorted(set(ids)):
        raise TieredSuiteError("snapshot rows must be sorted and unique by instance_id")
    for r in rows:
        if r.get("difficulty") not in KNOWN_VERIFIED_DIFFICULTIES:
            raise TieredSuiteError(
                f"unknown difficulty label {r.get('difficulty')!r} for {r.get('instance_id')}"
            )
        if not _SHA40.match(str(r.get("base_commit", ""))):
            raise TieredSuiteError(f"bad base_commit for {r.get('instance_id')}")
    if snap["difficulty_counts"] != _count_difficulties(rows):
        raise TieredSuiteError("snapshot difficulty_counts does not match its rows")
    return snap


def load_verified_problems(path, revision: str) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TieredSuiteError(f"cannot read problems file {path}: {exc}")
    if data.get("revision") != revision:
        raise TieredSuiteError(
            f"problems revision {data.get('revision')!r} does not match snapshot revision {revision!r}"
        )
    problems = data.get("problems")
    if not isinstance(problems, dict):
        raise TieredSuiteError("problems file has no 'problems' mapping")
    for key, value in problems.items():
        if not isinstance(value, str) or not value:
            raise TieredSuiteError(f"problems entry {key!r} is not a non-empty string")
    return problems


def select_tiered(rows, seed: int, spec=None) -> dict:
    if spec is None:
        spec = TIERED_SMALL_SPEC
    by_label = collections.defaultdict(list)
    for r in rows:
        if r["difficulty"] not in KNOWN_VERIFIED_DIFFICULTIES:
            raise TieredSuiteError(f"unknown difficulty label {r['difficulty']!r}")
        by_label[r["difficulty"]].append(r["instance_id"])
    chosen = {}
    for tier, tier_spec in spec.items():
        pool = sorted(by_label.get(tier_spec["label"], []))
        if len(pool) < tier_spec["n"]:
            raise TieredSuiteError(
                f"tier {tier} needs {tier_spec['n']} instances but only {len(pool)} have label {tier_spec['label']!r}"
            )
        rng = random.Random(seed + tier_spec["sub_seed_offset"])
        shuffled = list(pool)
        rng.shuffle(shuffled)
        chosen[tier] = sorted(shuffled[: tier_spec["n"]])
    return chosen


def build_tiered_suite(snapshot_path, problems_path, seed: int, suite: str = "tiered-small") -> dict:
    entry = TIERED_SUITES[suite]
    spec_map = entry["tiers"]
    cost = entry["cost"]
    snapshot_path = Path(snapshot_path)
    snap = load_verified_snapshot(snapshot_path)
    problems = load_verified_problems(problems_path, snap["revision"])
    chosen = select_tiered(snap["rows"], seed, spec_map)
    selected = set()
    for ids in chosen.values():
        selected.update(ids)
    missing = sorted(selected - set(problems))
    extra = sorted(set(problems) - selected)
    if missing or extra:
        raise TieredSuiteError(
            f"problems file does not match the selection (missing: {missing}; extra: {extra}); "
            "run --refresh-verified-snapshot to regenerate it"
        )
    by_id = {r["instance_id"]: r for r in snap["rows"]}
    tasks = []
    for tier, spec in spec_map.items():
        for rank, iid in enumerate(chosen[tier]):
            row = by_id[iid]
            tasks.append(
                {
                    "id": f"verified_{tier}_{rank:03d}",
                    "tier": tier,
                    "source": "swebench_verified",
                    "source_id": iid,
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "difficulty": row["difficulty"],
                    "est_tokens_in": spec["est_tokens_in"],
                    "est_tokens_out": spec["est_tokens_out"],
                    "wall_clock_seconds": spec["wall_clock_seconds"],
                    "description": problems[iid],
                }
            )
    n_total = len(tasks)
    tiered = {
        "dataset": VERIFIED_DATASET,
        "dataset_revision": snap["revision"],
        "split": "test",
        "snapshot_file": "quoin/benchmarks/data/swebench-verified-snapshot.json",
        "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "seed": seed,
        "sub_seeds": {t: s["sub_seed_offset"] for t, s in spec_map.items()},
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "worst_case_target_usd": cost["worst_case_target_usd"],
        "worst_case_ceiling_usd": cost["worst_case_ceiling_usd"],
        "documented_cap_usd": cost["documented_cap_usd"],
        "requires_budget_cap": True,
        "paired_run": True,
        "tiers": {
            t: {
                "label": s["label"],
                "n": s["n"],
                "wall_clock_seconds": s["wall_clock_seconds"],
                "est_tokens_in": s["est_tokens_in"],
                "est_tokens_out": s["est_tokens_out"],
            }
            for t, s in spec_map.items()
        },
    }
    for key, value in entry["extra_tiered_fields"].items():
        tiered[key] = value
    if entry.get("records_problems_file"):
        problems_file = Path(problems_path)
        tiered["problems_file"] = "quoin/benchmarks/data/" + problems_file.name
        tiered["problems_sha256"] = hashlib.sha256(problems_file.read_bytes()).hexdigest()
    return {
        "schema_version": 2,
        "description": (
            f"Quoin benchmark {suite} task suite: {n_total} SWE-bench Verified"
            " instances stratified by upstream difficulty label (easy, medium, large)."
            f" Generated deterministically via pick_subset.py --suite {suite} --seed {seed}"
            " from a vendored snapshot."
            " Do not edit manually; regenerate from pick_subset.py."
        ),
        "generated_by": "quoin/benchmarks/scripts/pick_subset.py",
        "seed": seed,
        "provenance_file": entry["provenance_file"],
        "tiered": tiered,
        "tasks": tasks,
    }


def snapshot_from_rows(
    rows, *, revision, source_file, source_sha256, source_size, fetched_on
) -> dict:
    kept = []
    seen = set()
    for r in rows:
        iid = r["instance_id"]
        if iid in seen:
            raise TieredSuiteError(f"duplicate instance_id {iid}")
        seen.add(iid)
        if r["difficulty"] not in KNOWN_VERIFIED_DIFFICULTIES:
            raise TieredSuiteError(
                f"unknown difficulty label {r['difficulty']!r} for {iid}"
            )
        kept.append({k: r[k] for k in SNAPSHOT_FIELDS})
    kept.sort(key=lambda r: r["instance_id"])
    return {
        "dataset": VERIFIED_DATASET,
        "revision": revision,
        "split": "test",
        "source_file": source_file,
        "source_file_sha256": source_sha256,
        "source_file_size": source_size,
        "fetched_on": fetched_on,
        "generated_by": "quoin/benchmarks/scripts/pick_subset.py --refresh-verified-snapshot",
        "fields": list(SNAPSHOT_FIELDS),
        "row_count": len(kept),
        "difficulty_counts": _count_difficulties(kept),
        "rows": kept,
    }


def problems_from_rows(rows, selected_ids, *, revision) -> dict:
    text_by_id = {r["instance_id"]: r.get("problem_statement") for r in rows}
    problems = {}
    for iid in sorted(selected_ids):
        text = text_by_id.get(iid)
        if not text:
            raise TieredSuiteError(f"empty or missing problem_statement for {iid}")
        problems[iid] = text
    return {
        "dataset": VERIFIED_DATASET,
        "revision": revision,
        "field": "problem_statement",
        "note": "verbatim upstream text for the selected instances only; hints_text excluded",
        "problems": problems,
    }


def _read_parquet_rows(parquet_file):
    """Return (rows, raw_bytes) for the vendored columns plus problem_statement."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print(
            "ERROR: --refresh-verified-snapshot needs pyarrow. Install it in a throwaway"
            " environment, for example: uv venv <dir> --python 3.12 &&"
            " uv pip install --python <dir>/bin/python pyarrow",
            file=sys.stderr,
        )
        sys.exit(2)
    parquet_file = Path(parquet_file)
    raw = parquet_file.read_bytes()
    columns = SNAPSHOT_FIELDS + ["problem_statement"]
    try:
        table = pq.read_table(str(parquet_file), columns=columns)
    except Exception as exc:
        raise TieredSuiteError(f"cannot read required columns from {parquet_file}: {exc}")
    data = table.to_pydict()
    rows = [dict(zip(columns, vals)) for vals in zip(*(data[c] for c in columns))]
    return rows, raw


def refresh_verified_snapshot(parquet_file, revision, fetched_on, seed, snapshot_path, problems_paths) -> None:
    """Rewrite the snapshot, then every registered suite's problems file.

    problems_paths maps suite name to the file written for it.
    """
    rows, raw = _read_parquet_rows(parquet_file)
    snap = snapshot_from_rows(
        rows,
        revision=revision,
        source_file=VERIFIED_SOURCE_FILE,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_size=len(raw),
        fetched_on=fetched_on,
    )
    outputs = []
    for name, entry in TIERED_SUITES.items():
        chosen = select_tiered(snap["rows"], seed, entry["tiers"])
        selected = [iid for ids in chosen.values() for iid in ids]
        problems = problems_from_rows(rows, selected, revision=revision)
        outputs.append((problems_paths[name], problems, len(selected)))
    _write_text(snapshot_path, render_suite(snap))
    print(f"Wrote {snapshot_path} ({snap['row_count']} rows)")
    for path, problems, count in outputs:
        _write_text(path, render_suite(problems))
        print(f"Wrote {path} ({count} problems)")


def refresh_problems_only(parquet_file, revision, seed, suite, snapshot_path, problems_path) -> None:
    """Write one suite's problems file without touching the snapshot.

    Refuses unless the parquet bytes and revision match the committed snapshot,
    so a different upstream release cannot slip in statements that disagree
    with the committed selection.
    """
    snap = load_verified_snapshot(snapshot_path)
    rows, raw = _read_parquet_rows(parquet_file)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != snap.get("source_file_sha256"):
        raise TieredSuiteError(
            f"parquet sha256 {digest} does not match the snapshot's source_file_sha256"
        )
    if revision != snap["revision"]:
        raise TieredSuiteError(
            f"--revision {revision} does not match the snapshot revision {snap['revision']}"
        )
    chosen = select_tiered(snap["rows"], seed, TIERED_SUITES[suite]["tiers"])
    selected = [iid for ids in chosen.values() for iid in ids]
    problems = problems_from_rows(rows, selected, revision=revision)
    _write_text(problems_path, render_suite(problems))
    print(f"Wrote {problems_path} ({len(selected)} problems)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate deterministic quoin benchmark suites"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1729,
        help="Random seed for deterministic shuffle (default: 1729)",
    )
    parser.add_argument(
        "--n-humaneval",
        type=int,
        default=None,
        help="Number of HumanEval+ tasks to select (default: 100)",
    )
    parser.add_argument(
        "--n-swebench",
        type=int,
        default=None,
        help="Number of SWE-bench Lite tasks to select (default: 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: stdout)",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        default=None,
        help="Verify that --output matches an existing file byte-for-byte",
    )
    parser.add_argument(
        "--suite",
        choices=sorted(TIERED_SUITES),
        default=None,
        help="Generate a tiered suite from the vendored SWE-bench Verified snapshot (default: v1)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help="Vendored SWE-bench Verified snapshot path",
    )
    parser.add_argument(
        "--problems",
        type=Path,
        default=None,
        help="Vendored problem statements path (default: the suite's own file)",
    )
    parser.add_argument(
        "--refresh-verified-snapshot",
        action="store_true",
        help="Maintainer mode: rebuild the vendored files from a downloaded parquet file",
    )
    parser.add_argument(
        "--problems-only",
        action="store_true",
        help="With --refresh-verified-snapshot and --suite: rewrite only that suite's problems file",
    )
    parser.add_argument("--parquet-file", type=Path, default=None)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--fetched-on", default=None, help="YYYY-MM-DD (default: today, UTC)")
    args = parser.parse_args(argv)

    if args.problems_only and not args.refresh_verified_snapshot:
        print("ERROR: --problems-only requires --refresh-verified-snapshot", file=sys.stderr)
        sys.exit(2)
    if args.problems_only and args.suite is None:
        print("ERROR: --problems-only needs --suite to name the problems file to refresh", file=sys.stderr)
        sys.exit(2)

    if args.refresh_verified_snapshot:
        if args.parquet_file is None or args.revision is None:
            print("ERROR: --refresh-verified-snapshot requires --parquet-file and --revision", file=sys.stderr)
            sys.exit(2)
        if not _SHA40.match(args.revision):
            print("ERROR: --revision must be a full 40-character commit SHA", file=sys.stderr)
            sys.exit(2)
        if args.suite is not None and not args.problems_only:
            print(
                "ERROR: --refresh-verified-snapshot with --suite needs --problems-only;"
                " omit --suite to refresh the snapshot and every suite's problems file",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.suite is None and args.problems is not None:
            print(
                "ERROR: --problems names one file; a full refresh writes one per suite."
                " Use --suite with --problems-only to choose the output path",
                file=sys.stderr,
            )
            sys.exit(2)
        fetched_on = args.fetched_on or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        try:
            if args.problems_only:
                refresh_problems_only(
                    args.parquet_file, args.revision, args.seed, args.suite, args.snapshot,
                    args.problems or TIERED_SUITES[args.suite]["problems_path"],
                )
            else:
                refresh_verified_snapshot(
                    args.parquet_file, args.revision, fetched_on, args.seed, args.snapshot,
                    {name: e["problems_path"] for name, e in TIERED_SUITES.items()},
                )
        except (TieredSuiteError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        return

    if args.suite in TIERED_SUITES:
        if args.n_humaneval is not None or args.n_swebench is not None:
            print(f"ERROR: --n-humaneval and --n-swebench are not valid with --suite {args.suite}", file=sys.stderr)
            sys.exit(2)
        try:
            suite = build_tiered_suite(
                args.snapshot,
                args.problems or TIERED_SUITES[args.suite]["problems_path"],
                args.seed,
                args.suite,
            )
        except TieredSuiteError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        n_humaneval = 100 if args.n_humaneval is None else args.n_humaneval
        n_swebench = 20 if args.n_swebench is None else args.n_swebench
        suite = build_suite(n_humaneval, n_swebench, args.seed)
    output_json = render_suite(suite)

    if args.verify:
        existing = args.verify.read_text(encoding="utf-8")
        if existing == output_json:
            print(f"VERIFY OK: {args.verify} matches generated output (seed={args.seed})")
            sys.exit(0)
        else:
            print(
                f"VERIFY FAIL: {args.verify} does not match generated output",
                file=sys.stderr,
            )
            existing_hash = hashlib.sha256(existing.encode()).hexdigest()[:12]
            new_hash = hashlib.sha256(output_json.encode()).hexdigest()[:12]
            print(f"  existing sha256[:12] = {existing_hash}", file=sys.stderr)
            print(f"  generated sha256[:12] = {new_hash}", file=sys.stderr)
            sys.exit(1)

    if args.output:
        _write_text(args.output, output_json)
        sha = hashlib.sha256(output_json.encode()).hexdigest()
        print(f"Written to {args.output} (sha256={sha})")
    else:
        sys.stdout.write(output_json)


if __name__ == "__main__":
    main()
