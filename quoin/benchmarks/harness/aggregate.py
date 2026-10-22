"""
aggregate.py — Metrics aggregation and Markdown report generation.

Reads all runs/<run-id>/<cell>/<task-id>/{metrics,cost,judge}.json files,
produces:
  runs/<run-id>/summary.json   — machine-readable aggregated metrics
  runs/<run-id>/summary.md     — human-readable Markdown report

Summary.json shape:
  {
    run_id, started_at, finished_at,
    cells: {
      cell_id: {
        n_tasks, pass_at_1,
        mean_cost_usd_or_null, total_cost_usd_or_null,
        mean_wall_clock_s, p50_wall_clock_s, p95_wall_clock_s,
        gate_intervention_count,
        headline_metric_pass_per_dollar_or_null
      }
    }
  }

Statistical methods:
  - Wilson 95% CIs on pass@1 via statsmodels.stats.proportion.proportion_confint
  - McNemar test for within-Claude-pair significance (paired test on per-task outcomes)
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any, Optional

VALID_VERDICTS = frozenset({"pass", "fail", "error", "timeout"})
CODEX_CELLS = frozenset({"simple-codex", "quoin-codex"})
CLAUDE_CELLS = frozenset({"simple-claude", "quoin-claude"})


def _wilson_ci(count: int, nobs: int, alpha: float = 0.05):
    """
    Compute Wilson 95% CI on a proportion.

    Uses statsmodels if available; falls back to normal approximation.
    Returns (lower, upper) as floats in [0, 1].
    """
    if nobs == 0:
        return (0.0, 1.0)
    try:
        from statsmodels.stats.proportion import proportion_confint
        lower, upper = proportion_confint(count, nobs, alpha=alpha, method="wilson")
        return (float(lower), float(upper))
    except ImportError:
        # Normal approximation fallback
        import math
        p = count / nobs
        z = 1.96
        margin = z * math.sqrt(p * (1 - p) / nobs)
        return (max(0.0, p - margin), min(1.0, p + margin))


def _mcnemar_pvalue(results_a: list[str], results_b: list[str]) -> Optional[float]:
    """
    Compute McNemar test p-value on paired pass/fail outcomes.

    results_a, results_b: parallel lists of 'pass'/'fail'/'error'/'timeout'
    Returns p-value or None if statsmodels unavailable or insufficient data.
    """
    if len(results_a) != len(results_b):
        return None
    # Discordant pairs
    b = sum(1 for a, bv in zip(results_a, results_b)
            if a == "pass" and bv != "pass")
    c = sum(1 for a, bv in zip(results_a, results_b)
            if a != "pass" and bv == "pass")

    if b + c == 0:
        return 1.0  # No discordant pairs — cannot reject null

    try:
        from statsmodels.stats.contingency_tables import mcnemar
        table = [[0, b], [c, 0]]
        result = mcnemar(table, exact=False, correction=True)
        return float(result.pvalue)
    except ImportError:
        # Manual chi-square with continuity correction
        import math
        stat = (abs(b - c) - 1) ** 2 / (b + c)
        # chi2 CDF approximation for 1 df
        p = 1.0 - _chi2_cdf_1df(stat)
        return p


def _chi2_cdf_1df(x: float) -> float:
    """Approximate chi-squared CDF for 1 degree of freedom."""
    import math
    if x <= 0:
        return 0.0
    return math.erf(math.sqrt(x / 2))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = (len(sorted_v) - 1) * pct / 100
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (idx - lo)


def aggregate_run(
    run_dir: Path,
    run_id: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    suite_path: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Aggregate all task results for a run.

    Returns the summary dict (also writes summary.json and summary.md).
    """
    run_path = run_dir / run_id
    if not run_path.exists():
        raise FileNotFoundError(f"Run directory not found: {run_path}")

    cells_data: dict[str, dict] = {}

    # Discover cells
    cell_dirs = [d for d in run_path.iterdir() if d.is_dir()]

    for cell_dir in sorted(cell_dirs):
        cell = cell_dir.name
        task_verdicts: list[str] = []
        wall_clock_times: list[float] = []
        costs_usd: list[float] = []
        gate_intervention_count = 0
        task_ids: list[str] = []
        per_task_verdict: dict[str, str] = {}

        task_dirs = [d for d in cell_dir.iterdir() if d.is_dir()]

        for task_dir in sorted(task_dirs):
            task_id = task_dir.name
            task_ids.append(task_id)

            # Load judge.json
            judge_path = task_dir / "judge.json"
            verdict = "error"
            if judge_path.exists():
                try:
                    j = json.loads(judge_path.read_text(encoding="utf-8"))
                    verdict = j.get("verdict", "error")
                except Exception:
                    pass
            task_verdicts.append(verdict)
            per_task_verdict[task_id] = verdict

            # Load metrics.json
            metrics_path = task_dir / "metrics.json"
            if metrics_path.exists():
                try:
                    m = json.loads(metrics_path.read_text(encoding="utf-8"))
                    wc = m.get("wall_clock_seconds", 0.0)
                    wall_clock_times.append(float(wc))
                    gate_intervention_count += int(m.get("gate_intervention_count", 0))
                except Exception:
                    pass

            # Load cost.json (Claude cells only)
            cost_path = task_dir / "cost.json"
            if cost_path.exists():
                try:
                    c = json.loads(cost_path.read_text(encoding="utf-8"))
                    if c.get("cost_available") and c.get("cost_runtime_usd") is not None:
                        costs_usd.append(float(c["cost_runtime_usd"]))
                except Exception:
                    pass

        n_tasks = len(task_verdicts)
        n_pass = sum(1 for v in task_verdicts if v == "pass")
        pass_at_1 = n_pass / n_tasks if n_tasks > 0 else 0.0

        mean_cost = statistics.mean(costs_usd) if costs_usd else None
        total_cost = sum(costs_usd) if costs_usd else None
        mean_wall = statistics.mean(wall_clock_times) if wall_clock_times else 0.0
        p50_wall = _percentile(wall_clock_times, 50)
        p95_wall = _percentile(wall_clock_times, 95)

        # Headline metric: pass/USD (Claude cells only)
        headline = None
        if cell in CLAUDE_CELLS and mean_cost and mean_cost > 0:
            headline = pass_at_1 / mean_cost

        # Wilson CI
        ci_lower, ci_upper = _wilson_ci(n_pass, n_tasks)

        cells_data[cell] = {
            "n_tasks": n_tasks,
            "pass_at_1": pass_at_1,
            "n_pass": n_pass,
            "wilson_ci_95": [ci_lower, ci_upper],
            "mean_cost_usd_or_null": mean_cost,
            "total_cost_usd_or_null": total_cost,
            "mean_wall_clock_s": mean_wall,
            "p50_wall_clock_s": p50_wall,
            "p95_wall_clock_s": p95_wall,
            "gate_intervention_count": gate_intervention_count,
            "headline_metric_pass_per_dollar_or_null": headline,
            "_task_ids": task_ids,
            "_per_task_verdict": per_task_verdict,
        }

    # McNemar test for Claude pair
    mcnemar_p: Optional[float] = None
    if "simple-claude" in cells_data and "quoin-claude" in cells_data:
        simple_ids = cells_data["simple-claude"]["_task_ids"]
        quoin_ids = cells_data["quoin-claude"]["_task_ids"]
        common_ids = [tid for tid in simple_ids if tid in set(quoin_ids)]
        if common_ids:
            simple_verdicts = [
                cells_data["simple-claude"]["_per_task_verdict"].get(tid, "error")
                for tid in common_ids
            ]
            quoin_verdicts = [
                cells_data["quoin-claude"]["_per_task_verdict"].get(tid, "error")
                for tid in common_ids
            ]
            mcnemar_p = _mcnemar_pvalue(simple_verdicts, quoin_verdicts)

    # Build summary
    cells_clean = {
        cell: {k: v for k, v in data.items() if not k.startswith("_")}
        for cell, data in cells_data.items()
    }

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "cells": cells_clean,
        "within_claude_pair_mcnemar_pvalue": mcnemar_p,
    }

    tiered_md = _build_tiered_section(summary, run_path, suite_path)

    # Write summary.json
    summary_json_path = run_path / "summary.json"
    summary_json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Write summary.md
    summary_md = _render_markdown_report(summary, cells_data, run_id)
    if tiered_md:
        summary_md = summary_md + tiered_md
    summary_md_path = run_path / "summary.md"
    summary_md_path.write_text(summary_md, encoding="utf-8")

    return summary


def _build_tiered_section(summary: dict, run_path: Path, suite_path: Optional[Path]) -> str:
    """Add the tiered comparison to `summary` when the run's suite is tiered.

    Returns the Markdown section, or an empty string for any other suite. A
    failure while building, serializing or rendering is recorded in the
    summary instead of raised, so the summary files are always written.
    """
    from . import tiered_report

    manifest = tiered_report.read_manifest(run_path)
    suite_file = None
    if suite_path is not None:
        suite_file = Path(suite_path)
    elif manifest.get("suite_path"):
        suite_file = Path(manifest["suite_path"])
    if suite_file is None or not suite_file.is_file():
        return ""
    try:
        raw = suite_file.read_bytes()
        suite = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        summary["tiered"] = {"error": error}
        return f"\n## Tiered comparison\n\n_Tiered report unavailable: {error}_\n"
    if not isinstance(suite, dict) or not isinstance(suite.get("tiered"), dict):
        return ""
    try:
        sha = hashlib.sha256(raw).hexdigest()[:16]
        tiered = tiered_report.build_tiered_report(run_path, suite, manifest, suite_sha=sha)
        json.dumps(tiered)
        tiered_md = tiered_report.render_tiered_markdown(tiered, _format_md_table)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        summary["tiered"] = {"error": error}
        return f"\n## Tiered comparison\n\n_Tiered report unavailable: {error}_\n"
    summary["tiered"] = tiered
    return tiered_md


def _render_markdown_report(
    summary: dict,
    cells_data: dict,
    run_id: str,
) -> str:
    """Render the human-readable Markdown summary report."""
    lines: list[str] = []
    lines.append(f"# Benchmark Run: {run_id}\n")

    if summary.get("started_at"):
        lines.append(f"**Started:** {summary['started_at']}  ")
    if summary.get("finished_at"):
        lines.append(f"**Finished:** {summary['finished_at']}  ")
    lines.append("")

    # --- Primary: Within-agent-pair comparison ---
    lines.append("## Primary Result: Within-Agent-Pair Comparison\n")
    lines.append(
        "_This section shows the primary finding: quoin's effect on each agent. "
        "Cross-agent comparison (Claude vs Codex) is secondary and model-confounded._\n"
    )

    lines.append("### Claude Pair\n")
    claude_table = _render_cell_table(
        summary, cells_data, ["simple-claude", "quoin-claude"]
    )
    lines.append(claude_table)

    mcnemar_p = summary.get("within_claude_pair_mcnemar_pvalue")
    if mcnemar_p is not None:
        sig = "significant" if mcnemar_p < 0.05 else "NOT significant"
        lines.append(
            f"\n**McNemar paired test (quoin-claude vs simple-claude):** "
            f"p = {mcnemar_p:.4f} ({sig} at α=0.05)\n"
        )
        if mcnemar_p >= 0.05:
            lines.append(
                "_Note: The observed delta is within sampling variance at this suite size. "
                "See methodology.md MDE table for detectable effect sizes._\n"
            )
    else:
        lines.append(
            "\n_McNemar test: insufficient paired data (both cells must run the same tasks)._\n"
        )

    lines.append("### Codex Pair\n")
    lines.append(
        "_Note: Codex cost metric is undefined in v1 "
        "(cost telemetry not available per quoin/adapters/codex/cost.md). "
        "Headline pass/USD is reported for Claude pair only._\n"
    )
    codex_table = _render_cell_table(
        summary, cells_data, ["simple-codex", "quoin-codex"]
    )
    lines.append(codex_table)

    # --- Secondary: Cross-agent view ---
    lines.append("## Secondary / Model-Confounded View\n")
    lines.append(
        "**Do not headline these numbers.** Cross-agent comparison is model-confounded: "
        "differences between Claude and Codex cells reflect model differences, not quoin's effect. "
        "This table is provided for completeness only.\n"
    )
    all_cells = list(summary["cells"].keys())
    lines.append(_render_cell_table(summary, cells_data, all_cells, show_headline=False))

    # --- Rank-order table ---
    lines.append("## Rank-Order Table: Per-Task Pass/Fail\n")
    lines.append(
        "_Rows = cells; Columns = task IDs. "
        "Shows which tasks each cell solved, not just aggregate rates._\n"
    )
    rank_table = _render_rank_order_table(cells_data)
    lines.append(rank_table)

    return "\n".join(lines)


def _render_cell_table(
    summary: dict,
    cells_data: dict,
    cell_names: list[str],
    show_headline: bool = True,
) -> str:
    """Render a Markdown table for a subset of cells."""
    headers = [
        "Cell", "N tasks", "pass@1", "95% CI", "Mean cost USD",
        "Total cost USD", "Mean wall-clock s", "Gate interventions",
    ]
    if show_headline:
        headers.append("Headline pass/USD")
    rows = [headers, ["---"] * len(headers)]

    for cell in cell_names:
        if cell not in summary["cells"]:
            continue
        d = summary["cells"][cell]
        ci = d.get("wilson_ci_95", [None, None])
        ci_str = (
            f"[{ci[0]:.3f}, {ci[1]:.3f}]"
            if ci[0] is not None
            else "n/a"
        )
        mean_cost = d.get("mean_cost_usd_or_null")
        total_cost = d.get("total_cost_usd_or_null")
        headline = d.get("headline_metric_pass_per_dollar_or_null")

        row = [
            cell,
            str(d.get("n_tasks", 0)),
            f"{d.get('pass_at_1', 0):.3f}",
            ci_str,
            f"${mean_cost:.4f}" if mean_cost is not None else "not_available",
            f"${total_cost:.2f}" if total_cost is not None else "not_available",
            f"{d.get('mean_wall_clock_s', 0):.1f}",
            str(d.get("gate_intervention_count", 0)),
        ]
        if show_headline:
            row.append(
                f"{headline:.4f}" if headline is not None else "not_available"
            )
        rows.append(row)

    return _format_md_table(rows)


def _render_rank_order_table(cells_data: dict) -> str:
    """Render a task × cell rank-order table."""
    # Collect all task IDs across all cells
    all_task_ids: list[str] = []
    for data in cells_data.values():
        for tid in data.get("_task_ids", []):
            if tid not in all_task_ids:
                all_task_ids.append(tid)
    all_task_ids.sort()

    cell_names = sorted(cells_data.keys())
    headers = ["Task ID"] + cell_names
    rows = [headers, ["---"] * len(headers)]

    for tid in all_task_ids:
        row = [tid]
        for cell in cell_names:
            verdict = cells_data[cell].get("_per_task_verdict", {}).get(tid, "-")
            symbol = {"pass": "P", "fail": "F", "error": "E", "timeout": "T"}.get(
                verdict, "-"
            )
            row.append(symbol)
        rows.append(row)

    legend = (
        "\n_Legend: P=pass, F=fail, E=error, T=timeout, -=not run_\n"
    )
    return _format_md_table(rows) + legend


def _format_md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    col_widths = [max(len(str(rows[r][c])) for r in range(len(rows))) for c in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [str(v).ljust(col_widths[i]) for i, v in enumerate(row)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
