"""
compare_arms.py — The three-arm comparison emitter (T-11).

`aggregate.aggregate_run` is scoped to one `run_dir/run_id` and cannot see
sibling arms (D-01: the three arms are three separate run-ids sharing no
common directory). This module is therefore a new reader, not a change to
`aggregate.py`.

Output lands at `{run_dir}/{gate_id}-comparison/three-arm-comparison.md` —
a SIBLING of the run directories, never nested inside one:
`aggregate.aggregate_run` discovers cells by iterating directories inside
`run_dir/run_id` (`aggregate.py:130`), so a comparison folder placed there
would be mistaken for a cell.

`harness_verdict` (the mechanical completion check, NOT AC-4's quality
measure — D-07) is derived from `summary.json`'s `n_pass`/`n_tasks`: this
gate's suite is exactly one task, so `n_pass == n_tasks == 1` means that
task passed and `n_pass == 0` means it did not. `summary.json` does not
retain the fail/error distinction at the cell level (`aggregate_run` strips
its internal `_per_task_verdict` before writing), so a failed task reads as
`"fail_or_error"` here rather than a fabricated `"fail"`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

ARMS = ("raw", "main", "candidate")

# Columns this module owns, in table order.
_COLUMNS = (
    "arm", "cell", "run_id", "installed_quoin_commit", "n_tasks",
    "harness_verdict", "total_cost_usd_or_null", "mean_wall_clock_s",
    "turn_count", "gate_intervention_count", "compaction_event_count",
    "task_completion_quality", "config_root",
)

NOT_AVAILABLE = "not_available"
PENDING = "pending"


def _load_summary(run_dir: Path, run_id: Optional[str]) -> Optional[dict]:
    if not run_id:
        return None
    path = Path(run_dir) / run_id / "summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _harness_verdict(cell_data: dict) -> str:
    n_tasks = cell_data.get("n_tasks", 0)
    n_pass = cell_data.get("n_pass", 0)
    if not n_tasks:
        return NOT_AVAILABLE
    return "pass" if n_pass == n_tasks else "fail_or_error"


def _build_row(
    arm: str,
    run_id: Optional[str],
    cell: Optional[str],
    summary: Optional[dict],
    evidence: dict,
) -> dict[str, Any]:
    if summary is None:
        cell_data: dict = {}
    else:
        cell_data = (summary.get("cells") or {}).get(cell or "", {})

    have_summary = summary is not None and bool(cell_data)
    total_cost = cell_data.get("total_cost_usd_or_null")

    quality = evidence.get("task_completion_quality", PENDING)
    if quality is None:
        quality = PENDING

    return {
        "arm": arm,
        "cell": cell or NOT_AVAILABLE,
        "run_id": run_id or NOT_AVAILABLE,
        "installed_quoin_commit": evidence.get("installed_quoin_commit", NOT_AVAILABLE),
        "n_tasks": cell_data.get("n_tasks", NOT_AVAILABLE) if have_summary else NOT_AVAILABLE,
        "harness_verdict": _harness_verdict(cell_data) if have_summary else NOT_AVAILABLE,
        "total_cost_usd_or_null": total_cost if (have_summary and total_cost is not None) else NOT_AVAILABLE,
        "mean_wall_clock_s": cell_data.get("mean_wall_clock_s", NOT_AVAILABLE) if have_summary else NOT_AVAILABLE,
        "turn_count": evidence.get("turn_count", NOT_AVAILABLE),
        "gate_intervention_count": cell_data.get("gate_intervention_count", NOT_AVAILABLE) if have_summary else NOT_AVAILABLE,
        "compaction_event_count": evidence.get("compaction_event_count", NOT_AVAILABLE),
        "task_completion_quality": quality,
        "config_root": evidence.get("config_root", NOT_AVAILABLE),
        "_max_budget_usd_applied": evidence.get("max_budget_usd_applied", NOT_AVAILABLE),
        "_raw_total_cost": total_cost if have_summary else None,
        "_raw_quality": quality,
    }


def _cost_verdict(rows_by_arm: dict[str, dict]) -> str:
    main_cost = rows_by_arm["main"]["_raw_total_cost"]
    candidate_cost = rows_by_arm["candidate"]["_raw_total_cost"]
    if main_cost is None or candidate_cost is None:
        return PENDING
    return "PASS" if candidate_cost <= main_cost else "FAIL"


def _quality_verdict(rows_by_arm: dict[str, dict]) -> str:
    main_quality = rows_by_arm["main"]["_raw_quality"]
    candidate_quality = rows_by_arm["candidate"]["_raw_quality"]
    if main_quality in (None, PENDING) or candidate_quality in (None, PENDING):
        return PENDING
    return "PASS" if candidate_quality >= main_quality else "FAIL"


def compare_arms(
    run_dir: Path,
    gate_id: str,
    arm_run_ids: dict[str, str],
    arm_cells: dict[str, str],
    arm_evidence: Optional[dict[str, dict]] = None,
    raw_arm_note: str = "quoin-free floor, isolated CLAUDE_CONFIG_DIR",
) -> Path:
    """
    Write the three-arm comparison and return the path written.

    `arm_run_ids` / `arm_cells`: `{"raw": ..., "main": ..., "candidate": ...}`.
    A missing entry for an arm degrades that arm's row to `not_available`
    fields rather than raising.

    `arm_evidence`: optional per-arm dict of fields `summary.json` does not
    carry — `installed_quoin_commit`, `turn_count`, `compaction_event_count`,
    `task_completion_quality` (0-4, or omitted/`None` for `pending` — NEVER
    fabricated as `0`), `config_root`, `max_budget_usd_applied`.

    `raw_arm_note`: the one-line, honest statement of what the raw arm
    actually is (D-15) — either the isolated quoin-free floor, or
    `"stock Claude prompt on a quoin-installed machine — NOT a quoin-free
    floor"` when isolation degraded. Recorded, never assumed.
    """
    run_dir = Path(run_dir)
    arm_evidence = arm_evidence or {}

    rows_by_arm: dict[str, dict] = {}
    for arm in ARMS:
        run_id = arm_run_ids.get(arm)
        cell = arm_cells.get(arm)
        summary = _load_summary(run_dir, run_id)
        rows_by_arm[arm] = _build_row(arm, run_id, cell, summary, arm_evidence.get(arm, {}))

    cost_verdict = _cost_verdict(rows_by_arm)
    quality_verdict = _quality_verdict(rows_by_arm)

    lines = [
        f"# Three-arm comparison — {gate_id}",
        "",
        "This is the D-01 cross-arm reader. Each arm is a SEPARATE run-id "
        "(`aggregate.aggregate_run` cannot compare across run-ids); the "
        "within-Claude-pair statistical test in `aggregate.py` is inert per "
        "arm here (one cell each) and is not repeated in this file.",
        "",
        "## Per-arm results",
        "",
        "| " + " | ".join(_COLUMNS) + " |",
        "|" + "---|" * len(_COLUMNS),
    ]
    for arm in ARMS:
        row = rows_by_arm[arm]
        lines.append("| " + " | ".join(str(row[c]) for c in _COLUMNS) + " |")

    lines += [
        "",
        "## AC-4 verdicts",
        "",
        f"- **cost: candidate <= main** — {cost_verdict}",
        f"- **quality: candidate >= main (task_completion_quality)** — {quality_verdict}",
        "",
        "## Notes",
        "",
        f"- **raw arm:** {raw_arm_note}. It is NOT part of either AC-4 "
        "comparison above in either case — only main and candidate are "
        "compared.",
        "- **baseline identity:** `main` is `dd188d87ed2512b80d46a3ee80d333b842bd23b5` "
        "(pre-IVG-258); `candidate` is this stage's branch HEAD (D-13).",
        "- **spend caps applied per arm:** "
        + ", ".join(
            f"{arm}={rows_by_arm[arm]['_max_budget_usd_applied']}" for arm in ARMS
        ),
    ]

    out_dir = run_dir / f"{gate_id}-comparison"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "three-arm-comparison.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
