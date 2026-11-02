"""Validate the Quoin cross-runtime benchmark framework.

This is a structure check for benchmark design files. It does not execute live
runtime benchmarks, score outcomes, or infer results.

Usage:
    python3 quoin/benchmarks/scripts/validate_benchmarks.py --project-root .
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_DIR = SCRIPT_DIR.parent
REPO_ROOT = BENCHMARK_DIR.parent.parent

REQUIRED_MODE_IDS = {
    "simple-claude",
    "quoin-claude",
    "simple-codex",
    "quoin-codex",
    "simple-opencode",
    "quoin-opencode",
}

REQUIRED_METRIC_IDS = {
    "task_completion_quality",
    "correctness_and_tests",
    "artifact_quality",
    "context_reuse",
    "time_or_turn_count",
    "cost_if_available",
    "setup_overhead",
}

REQUIRED_SCENARIO_IDS = {
    "fresh-repo-discovery",
    "medium-refactor-plan",
    "scoped-code-change",
    "review-changes",
    "session-handoff-memory-reuse",
    "tool-call-reliability",
    "cross-service-context-synthesis",
}

REQUIRED_SCENARIO_HEADINGS = [
    "## Purpose",
    "## Starting State",
    "## Prompt",
    "## Mode Notes",
    "## Expected Evidence",
    "## Evaluation Notes",
]

FORBIDDEN_RESULT_CLAIMS = [
    "quoin wins",
    "quoin outperforms",
    "quoin is faster",
    "measured improvement",
    "benchmark result:",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _missing(paths: Iterable[Path]) -> List[str]:
    return [str(path) for path in paths if not path.is_file()]


def _load_manifest() -> dict:
    return json.loads(_read(BENCHMARK_DIR / "benchmark-suite.json"))


def check_required_files() -> CheckResult:
    manifest = _load_manifest()
    required = [
        BENCHMARK_DIR / "README.md",
        BENCHMARK_DIR / "benchmark-suite.json",
    ]
    for scenario in manifest.get("scenarios", []):
        required.append(BENCHMARK_DIR / scenario["file"])
    for path in manifest.get("templates", {}).values():
        required.append(BENCHMARK_DIR / path)

    missing = _missing(required)
    if missing:
        return CheckResult("required-files", False, f"missing files: {missing}")
    return CheckResult("required-files", True, "benchmark files are present")


def check_manifest_shape() -> CheckResult:
    manifest = _load_manifest()
    issues = []

    if manifest.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if manifest.get("status") != "design-only":
        issues.append("status must be design-only")
    if "No benchmark results" not in manifest.get("results_policy", ""):
        issues.append("results_policy must say no benchmark results are recorded")

    mode_ids = {mode.get("id") for mode in manifest.get("comparison_modes", [])}
    if mode_ids != REQUIRED_MODE_IDS:
        issues.append(f"comparison modes mismatch: {sorted(mode_ids)}")

    for mode in manifest.get("comparison_modes", []):
        if mode.get("runtime") not in {"claude", "codex", "opencode"}:
            issues.append(f"{mode.get('id')}: runtime must be claude, codex or opencode")
        if not isinstance(mode.get("quoin_enabled"), bool):
            issues.append(f"{mode.get('id')}: quoin_enabled must be boolean")

    metric_ids = {metric.get("id") for metric in manifest.get("metrics", [])}
    if metric_ids != REQUIRED_METRIC_IDS:
        issues.append(f"metrics mismatch: {sorted(metric_ids)}")

    scenario_ids = {scenario.get("id") for scenario in manifest.get("scenarios", [])}
    if scenario_ids != REQUIRED_SCENARIO_IDS:
        issues.append(f"scenarios mismatch: {sorted(scenario_ids)}")

    if issues:
        return CheckResult("manifest-shape", False, "; ".join(issues))
    return CheckResult("manifest-shape", True, "manifest shape is valid")


def check_scenario_docs() -> CheckResult:
    manifest = _load_manifest()
    issues = []
    mode_tokens = [
        "Simple Claude", "Quoin + Claude", "Simple Codex", "Quoin + Codex",
        "Simple OpenCode", "Quoin + OpenCode",
    ]

    for scenario in manifest.get("scenarios", []):
        path = BENCHMARK_DIR / scenario["file"]
        text = _read(path)
        for heading in REQUIRED_SCENARIO_HEADINGS:
            if heading not in text:
                issues.append(f"{scenario['id']}: missing {heading}")
        for token in mode_tokens:
            if token not in text:
                issues.append(f"{scenario['id']}: missing mode note {token}")
        if ".workflow_artifacts/" not in text:
            issues.append(f"{scenario['id']}: missing Quoin artifact guidance")
        lower = text.lower()
        for phrase in FORBIDDEN_RESULT_CLAIMS:
            if phrase in lower:
                issues.append(f"{scenario['id']}: contains result claim {phrase!r}")

    if issues:
        return CheckResult("scenario-docs", False, "; ".join(issues))
    return CheckResult("scenario-docs", True, "scenario docs are well formed")


def check_templates() -> CheckResult:
    manifest = _load_manifest()
    issues = []
    templates = manifest["templates"]
    result = _read(BENCHMARK_DIR / templates["result_template"])
    run_sheet = _read(BENCHMARK_DIR / templates["run_sheet"])
    rubric = _read(BENCHMARK_DIR / templates["scoring_rubric"])

    for heading in ["## Run Identity", "## Outcome Summary", "## Metric Scores", "## Verification", "## Artifacts"]:
        if heading not in result:
            issues.append(f"result template missing {heading}")

    for label in [metric["label"] for metric in manifest["metrics"]]:
        if label not in result and label not in rubric:
            issues.append(f"metric label missing from templates: {label}")

    for token in ["Scenario id:", "Comparison mode:", "Turn count:", "Cost value:"]:
        if token not in run_sheet:
            issues.append(f"run sheet missing {token}")

    if "This is a template, not a result" not in result:
        issues.append("result template must explicitly avoid being a result")
    if "Do not score from general impressions alone" not in rubric:
        issues.append("rubric must require evidence-based scoring")

    if issues:
        return CheckResult("templates", False, "; ".join(issues))
    return CheckResult("templates", True, "templates cover metrics and evidence fields")


def check_docs_reference_runtime_boundaries(project_root: Path) -> CheckResult:
    docs = [
        project_root / "quoin" / "docs" / "runtime-portability.md",
        project_root / "quoin" / "docs" / "runtime-portability-status.md",
        project_root / "quoin" / "docs" / "runtime-parity-matrix.md",
    ]
    missing = []
    for path in docs:
        text = _read(path)
        if "quoin/benchmarks/" not in text:
            missing.append(str(path))
    if missing:
        return CheckResult("runtime-doc-links", False, f"missing benchmark references: {missing}")
    return CheckResult("runtime-doc-links", True, "runtime docs reference benchmark framework")


def check_no_bundled_results() -> CheckResult:
    results_dir = BENCHMARK_DIR / "results"
    if results_dir.exists():
        return CheckResult(
            "no-bundled-results",
            False,
            "benchmark design must stay separate from results; results directory exists",
        )

    text_files = [
        path for path in BENCHMARK_DIR.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json"}
    ]
    hits = []
    for path in text_files:
        lower = _read(path).lower()
        for phrase in FORBIDDEN_RESULT_CLAIMS:
            if phrase in lower:
                hits.append(f"{path}: {phrase}")
    if hits:
        return CheckResult("no-bundled-results", False, "; ".join(hits))
    return CheckResult("no-bundled-results", True, "no benchmark result claims found")


def run_checks(project_root: Path) -> List[CheckResult]:
    return [
        check_required_files(),
        check_manifest_shape(),
        check_scenario_docs(),
        check_templates(),
        check_docs_reference_runtime_boundaries(project_root),
        check_no_bundled_results(),
    ]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    results = run_checks(args.project_root.resolve())
    for result in results:
        status = "ok" if result.ok else "FAIL"
        print(f"{status} {result.name}: {result.detail}")

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
