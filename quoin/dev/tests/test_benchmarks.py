import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = REPO_ROOT / "quoin" / "benchmarks"


def read_rel(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_benchmark_framework_files_exist():
    manifest = json.loads(read_rel("quoin/benchmarks/benchmark-suite.json"))
    expected = [
        "quoin/benchmarks/README.md",
        "quoin/benchmarks/benchmark-suite.json",
        "quoin/benchmarks/scripts/validate_benchmarks.py",
    ]
    expected.extend(f"quoin/benchmarks/{scenario['file']}" for scenario in manifest["scenarios"])
    expected.extend(f"quoin/benchmarks/{path}" for path in manifest["templates"].values())

    missing = [path for path in expected if not (REPO_ROOT / path).is_file()]
    assert not missing, f"Missing benchmark files: {missing}"


def test_benchmark_manifest_modes_metrics_and_scenarios():
    manifest = json.loads(read_rel("quoin/benchmarks/benchmark-suite.json"))

    assert manifest["status"] == "design-only"
    assert "No benchmark results" in manifest["results_policy"]

    assert {mode["id"] for mode in manifest["comparison_modes"]} == {
        "simple-claude",
        "quoin-claude",
        "simple-codex",
        "quoin-codex",
        "simple-opencode",
        "quoin-opencode",
    }
    assert {
        (mode["runtime"], mode["quoin_enabled"])
        for mode in manifest["comparison_modes"]
    } == {
        ("claude", False),
        ("claude", True),
        ("codex", False),
        ("codex", True),
        ("opencode", False),
        ("opencode", True),
    }

    assert {metric["id"] for metric in manifest["metrics"]} == {
        "task_completion_quality",
        "correctness_and_tests",
        "artifact_quality",
        "context_reuse",
        "time_or_turn_count",
        "cost_if_available",
        "setup_overhead",
    }
    assert {scenario["id"] for scenario in manifest["scenarios"]} == {
        "fresh-repo-discovery",
        "medium-refactor-plan",
        "scoped-code-change",
        "review-changes",
        "session-handoff-memory-reuse",
        "tool-call-reliability",
        "cross-service-context-synthesis",
    }


def test_benchmark_docs_separate_design_from_results():
    readme = read_rel("quoin/benchmarks/README.md").lower()
    result_template = read_rel("quoin/benchmarks/templates/result-template.md")

    assert "design only" in readme
    assert "does not contain measured results" in readme
    assert "This is a template, not a result" in result_template
    assert not (BENCHMARK_DIR / "results").exists()

    combined = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in BENCHMARK_DIR.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json"}
    )
    forbidden_claims = [
        "quoin wins",
        "quoin outperforms",
        "quoin is faster",
        "measured improvement",
        "benchmark result:",
    ]
    for claim in forbidden_claims:
        assert claim not in combined


def test_benchmark_scenarios_are_well_formed():
    manifest = json.loads(read_rel("quoin/benchmarks/benchmark-suite.json"))
    required_headings = [
        "## Purpose",
        "## Starting State",
        "## Prompt",
        "## Mode Notes",
        "## Expected Evidence",
        "## Evaluation Notes",
    ]
    required_modes = [
        "Simple Claude", "Quoin + Claude", "Simple Codex", "Quoin + Codex",
        "Simple OpenCode", "Quoin + OpenCode",
    ]

    for scenario in manifest["scenarios"]:
        text = read_rel(f"quoin/benchmarks/{scenario['file']}")
        for heading in required_headings:
            assert heading in text, f"{scenario['id']} missing {heading}"
        for mode in required_modes:
            assert mode in text, f"{scenario['id']} missing {mode}"
        assert ".workflow_artifacts/" in text


def test_runtime_docs_reference_benchmark_framework():
    for path in [
        "quoin/docs/runtime-portability.md",
        "quoin/docs/runtime-portability-status.md",
        "quoin/docs/runtime-parity-matrix.md",
    ]:
        assert "quoin/benchmarks/" in read_rel(path)


def test_benchmark_validator_passes():
    completed = subprocess.run(
        [
            sys.executable,
            "quoin/benchmarks/scripts/validate_benchmarks.py",
            "--project-root",
            ".",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
