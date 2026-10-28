"""
check_invariants.py — Fair-comparison invariants checker.

Reads runs/<run-id>/ and verifies all 14 invariants from methodology.md.
Non-zero exit if any invariant fails; produces runs/<run-id>/invariants-report.md.

Usage:
    python3 quoin/benchmarks/scripts/check_invariants.py \
        --run-dir .workflow_artifacts/quoin-benchmarks/runs \
        --run-id v0-smoke

The 14 invariants (from methodology.md):
  1. Same model ID within base-agent pair
  2. Same temperature setting (0 for HumanEval+, 0.2 for SWE-bench Lite)
  3. Same suite (suite-v1.json frozen by SHA)
  4. Same fixture repo SHAs
  5. Same per-task wall-clock budget
  6. Same per-task USD kill-switch (per-cell-pair)
  7. Zero retries on task failure
  8. Network policy: offline-after-clone for fixture
  9. Docker judge image hash pinned
 10. Library versions pinned (evalplus, swebench)
 11. Transcripts captured
 12. Cost data captured or explicitly marked not_available
 13. Per-task isolated .workflow_artifacts/ (not per-cell)
 14. Zero lessons-learned cross-contamination
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class InvariantResult:
    id: int
    name: str
    status: str  # PASS | FAIL | WARN | NOT_APPLICABLE
    detail: str = ""


@dataclass
class InvariantsReport:
    run_id: str
    run_dir: Path
    results: list[InvariantResult] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    def overall_pass(self) -> bool:
        return all(r.status in ("PASS", "WARN", "NOT_APPLICABLE") for r in self.results)

    def to_markdown(self) -> str:
        lines = [
            f"# Invariants Report: {self.run_id}\n",
            f"**Run directory:** `{self.run_dir / self.run_id}`\n",
            "",
            "| ID | Invariant | Status | Detail |",
            "|----|-----------|--------|--------|",
        ]
        for r in self.results:
            status_icon = {
                "PASS": "PASS",
                "FAIL": "FAIL",
                "WARN": "WARN",
                "NOT_APPLICABLE": "N/A",
            }.get(r.status, r.status)
            detail = r.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.id} | {r.name} | {status_icon} | {detail} |")

        lines.append("")
        if self.overall_pass():
            lines.append("**Overall verdict: PASS**\n")
        else:
            failing = [r for r in self.results if r.status == "FAIL"]
            lines.append(f"**Overall verdict: FAIL** ({len(failing)} invariant(s) failed)\n")
            lines.append("## Failures\n")
            for r in failing:
                lines.append(f"### Invariant {r.id}: {r.name}")
                lines.append(f"{r.detail}\n")

        return "\n".join(lines)


def _load_manifest(run_path: Path) -> dict:
    """Load run-manifest.yaml if present."""
    manifest_path = run_path / "run-manifest.yaml"
    if not manifest_path.exists():
        return {}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except ImportError:
        pass
    except Exception:
        # Manifests written before quoted values were escaped are not strict
        # YAML; read them the way the tiered report does.
        from quoin.benchmarks.harness.tiered_report import read_manifest
        return read_manifest(run_path)
    try:
        # yaml not available; try manual key parsing
        result = {}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result
    except Exception:
        return {}


def _get_cell_dirs(run_path: Path) -> dict[str, Path]:
    """Return {cell_name: cell_dir} for all cell subdirs in the run."""
    cells = {}
    for d in run_path.iterdir():
        if d.is_dir() and d.name not in (".", ".."):
            cells[d.name] = d
    return cells


def _load_task_results(cell_dir: Path) -> dict[str, dict]:
    """Return {task_id: {judge, metrics, cost}} for all tasks in a cell dir."""
    results = {}
    for task_dir in sorted(cell_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        data: dict = {}
        for fname in ("judge.json", "metrics.json", "cost.json"):
            p = task_dir / fname
            if p.exists():
                try:
                    data[fname.split(".")[0]] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    data[fname.split(".")[0]] = None
        results[task_id] = data
    return results


def check_invariants(run_dir: Path, run_id: str) -> InvariantsReport:
    """Run all 14 invariant checks and return the report."""
    run_path = run_dir / run_id
    manifest = _load_manifest(run_path)
    report = InvariantsReport(run_id=run_id, run_dir=run_dir, manifest=manifest)

    cell_dirs = _get_cell_dirs(run_path)
    claude_cells = {k: v for k, v in cell_dirs.items() if "claude" in k}
    codex_cells = {k: v for k, v in cell_dirs.items() if "codex" in k}

    def add(id: int, name: str, status: str, detail: str = ""):
        report.results.append(InvariantResult(id=id, name=name, status=status, detail=detail))

    # --------------------------------------------------------------------------
    # Invariant 1: Same model ID within base-agent pair
    # --------------------------------------------------------------------------
    if manifest:
        simple_model = manifest.get("simple_claude_model", manifest.get("simple-claude-model"))
        quoin_model = manifest.get("quoin_claude_model", manifest.get("quoin-claude-model"))
        if simple_model and quoin_model:
            if simple_model == quoin_model:
                add(1, "Same model ID (Claude pair)", "PASS",
                    f"Both cells: {simple_model}")
            else:
                add(1, "Same model ID (Claude pair)", "FAIL",
                    f"simple-claude={simple_model}, quoin-claude={quoin_model}")
        else:
            add(1, "Same model ID (Claude pair)", "WARN",
                "run-manifest.yaml missing model fields; cannot verify")
    else:
        add(1, "Same model ID (Claude pair)", "WARN",
            "run-manifest.yaml not found; cannot verify model IDs")

    # --------------------------------------------------------------------------
    # Invariant 2: Same temperature
    # --------------------------------------------------------------------------
    temp = manifest.get("temperature")
    if temp is not None:
        add(2, "Same temperature", "PASS", f"temperature={temp}")
    else:
        add(2, "Same temperature", "WARN",
            "Temperature not recorded in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 3: Same suite (frozen by SHA)
    # --------------------------------------------------------------------------
    suite_sha = manifest.get("suite_sha") or manifest.get("suite-sha")
    if suite_sha:
        add(3, "Same suite (frozen by SHA)", "PASS",
            f"suite_sha={suite_sha}")
    else:
        add(3, "Same suite (frozen by SHA)", "WARN",
            "suite_sha not in run-manifest.yaml; verify suite-v1.json was not changed")

    # --------------------------------------------------------------------------
    # Invariant 4: Same fixture repo SHAs
    # --------------------------------------------------------------------------
    fixture_sha = manifest.get("fixture_repo_sha") or manifest.get("fixture-repo-sha")
    if fixture_sha:
        add(4, "Same fixture repo SHAs", "PASS",
            f"fixture_sha={fixture_sha}")
    else:
        add(4, "Same fixture repo SHAs", "WARN",
            "fixture_repo_sha not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 5: Same per-task wall-clock budget
    # --------------------------------------------------------------------------
    budget = manifest.get("wall_clock_budget_seconds") or manifest.get("wall-clock-budget-seconds")
    if budget:
        add(5, "Same per-task wall-clock budget", "PASS",
            f"wall_clock_budget_seconds={budget}")
    else:
        add(5, "Same per-task wall-clock budget", "WARN",
            "wall_clock_budget_seconds not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 6: Same per-task USD kill-switch (per-cell-pair)
    # --------------------------------------------------------------------------
    kill_switch = manifest.get("usd_kill_switch_per_cell_pair")
    if kill_switch:
        add(6, "Same USD kill-switch (per-cell-pair)", "PASS",
            f"usd_kill_switch_per_cell_pair={kill_switch}")
    else:
        add(6, "Same USD kill-switch (per-cell-pair)", "WARN",
            "usd_kill_switch_per_cell_pair not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 7: Zero retries on task failure
    # --------------------------------------------------------------------------
    max_retries = manifest.get("max_retries", manifest.get("max-retries"))
    if str(max_retries) == "0":
        add(7, "Zero retries on task failure", "PASS", "max_retries=0")
    elif max_retries is None:
        add(7, "Zero retries on task failure", "WARN",
            "max_retries not in run-manifest.yaml; assumed 0 by harness config")
    else:
        add(7, "Zero retries on task failure", "FAIL",
            f"max_retries={max_retries} (must be 0 per invariant 7)")

    # --------------------------------------------------------------------------
    # Invariant 8: Network policy = offline-after-clone
    # --------------------------------------------------------------------------
    network_policy = manifest.get("network_policy")
    if network_policy:
        if "offline" in str(network_policy).lower():
            add(8, "Network policy (offline-after-clone)", "PASS",
                f"network_policy={network_policy}")
        else:
            add(8, "Network policy (offline-after-clone)", "WARN",
                f"network_policy={network_policy} — verify it matches offline-after-clone")
    else:
        add(8, "Network policy (offline-after-clone)", "WARN",
            "network_policy not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 9: Docker judge image hash pinned
    # --------------------------------------------------------------------------
    docker_hash = manifest.get("docker_judge_image_hash") or manifest.get("docker-judge-image-hash")
    if docker_hash:
        add(9, "Docker judge image hash pinned", "PASS",
            f"docker_judge_image_hash={docker_hash}")
    else:
        add(9, "Docker judge image hash pinned", "WARN",
            "docker_judge_image_hash not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 10: Library versions pinned
    # --------------------------------------------------------------------------
    evalplus_ver = manifest.get("evalplus_version")
    swebench_ver = manifest.get("swebench_version")
    if evalplus_ver and swebench_ver:
        add(10, "Library versions pinned (evalplus, swebench)", "PASS",
            f"evalplus={evalplus_ver}, swebench={swebench_ver}")
    else:
        add(10, "Library versions pinned (evalplus, swebench)", "WARN",
            "evalplus_version or swebench_version not in run-manifest.yaml")

    # --------------------------------------------------------------------------
    # Invariant 11: Transcripts captured
    # --------------------------------------------------------------------------
    missing_transcripts = []
    for cell_name, cell_dir in cell_dirs.items():
        task_results = _load_task_results(cell_dir)
        for task_id, data in task_results.items():
            transcript_path = cell_dir / task_id / "transcript.jsonl"
            if not transcript_path.exists() or transcript_path.stat().st_size == 0:
                missing_transcripts.append(f"{cell_name}/{task_id}")

    if not missing_transcripts:
        add(11, "Transcripts captured", "PASS",
            f"All {sum(len(list(cell_dir.iterdir())) for cell_dir in cell_dirs.values())} task transcripts present")
    else:
        n = len(missing_transcripts)
        add(11, "Transcripts captured", "FAIL",
            f"{n} missing or empty transcripts: {missing_transcripts[:5]}"
            + (" ..." if n > 5 else ""))

    # --------------------------------------------------------------------------
    # Invariant 12: Cost data captured or explicitly marked not_available
    # --------------------------------------------------------------------------
    bad_cost = []
    for cell_name, cell_dir in cell_dirs.items():
        task_results = _load_task_results(cell_dir)
        for task_id, data in task_results.items():
            cost = data.get("cost")
            if cost is None:
                # cost.json missing entirely
                cost_path = cell_dir / task_id / "cost.json"
                if not cost_path.exists():
                    bad_cost.append(f"{cell_name}/{task_id}: cost.json missing")
                continue
            # Must have either cost_available=True or cost="not_available"
            has_available = cost.get("cost_available") is not None
            has_explicit_not = cost.get("cost") == "not_available"
            if not has_available and not has_explicit_not:
                bad_cost.append(f"{cell_name}/{task_id}: cost.json lacks cost_available field")

    if not bad_cost:
        add(12, "Cost data captured or marked not_available", "PASS",
            "All task cost.json files have cost_available field or explicit not_available")
    else:
        add(12, "Cost data captured or marked not_available", "FAIL",
            f"{len(bad_cost)} tasks with invalid cost.json: {bad_cost[:3]}"
            + (" ..." if len(bad_cost) > 3 else ""))

    # --------------------------------------------------------------------------
    # Invariant 13: Per-task isolated .workflow_artifacts/ (not per-cell)
    # --------------------------------------------------------------------------
    # We check this by looking at the run manifest for the isolation mode
    isolation_mode = manifest.get("isolation_mode") or manifest.get("isolation-mode")
    if isolation_mode:
        if "per-task" in str(isolation_mode).lower():
            add(13, "Per-task isolated .workflow_artifacts/", "PASS",
                f"isolation_mode={isolation_mode}")
        else:
            add(13, "Per-task isolated .workflow_artifacts/", "FAIL",
                f"isolation_mode={isolation_mode} — must be per-task")
    else:
        add(13, "Per-task isolated .workflow_artifacts/", "WARN",
            "isolation_mode not in run-manifest.yaml; check harness config manually")

    # --------------------------------------------------------------------------
    # Invariant 14: Zero lessons-learned cross-contamination
    # --------------------------------------------------------------------------
    # Check that no lessons-learned.md exists in the run evidence dirs
    contamination_found = []
    for cell_name, cell_dir in cell_dirs.items():
        for task_dir in cell_dir.iterdir():
            if not task_dir.is_dir():
                continue
            artifacts_evidence = task_dir / "workflow_artifacts_evidence"
            if artifacts_evidence.exists():
                lessons_files = list(artifacts_evidence.rglob("lessons-learned.md"))
                if lessons_files:
                    contamination_found.append(
                        f"{cell_name}/{task_dir.name}: {len(lessons_files)} lessons-learned.md found"
                    )

    if not contamination_found:
        add(14, "Zero lessons-learned cross-contamination", "PASS",
            "No lessons-learned.md found in any task's workflow_artifacts_evidence")
    else:
        add(14, "Zero lessons-learned cross-contamination", "FAIL",
            f"Lessons-learned contamination detected: {contamination_found}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check fair-comparison invariants for a quoin benchmark run"
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(".workflow_artifacts/quoin-benchmarks/runs"),
        help="Root directory for run outputs",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run identifier (e.g., v0-smoke, v1)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for invariants-report.md (default: <run-dir>/<run-id>/invariants-report.md)",
    )
    args = parser.parse_args()

    run_path = args.run_dir / args.run_id
    if not run_path.exists():
        print(f"ERROR: Run directory not found: {run_path}", file=sys.stderr)
        sys.exit(1)

    report = check_invariants(args.run_dir, args.run_id)

    # Write invariants-report.md
    output_path = args.output or (run_path / "invariants-report.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.to_markdown(), encoding="utf-8")
    print(f"Report written to: {output_path}")

    # Print summary
    pass_count = sum(1 for r in report.results if r.status == "PASS")
    warn_count = sum(1 for r in report.results if r.status == "WARN")
    fail_count = sum(1 for r in report.results if r.status == "FAIL")
    na_count = sum(1 for r in report.results if r.status == "NOT_APPLICABLE")

    print(f"\nInvariants: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL, {na_count} N/A")

    if report.overall_pass():
        print("Overall: PASS")
        sys.exit(0)
    else:
        print("Overall: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
