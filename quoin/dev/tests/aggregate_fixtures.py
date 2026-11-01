"""Run-directory builders shared by the aggregation and tiered-report tests.

`write_row` writes the three per-task files in the shapes `result_writer`
produces; the two `build_*` helpers lay out complete runs with known values
so tests can assert hand-computed figures.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2] / "benchmarks"
SUITE_V1 = BENCH_DIR / "suite-v1.json"

TIERED_SUITE_NAME = "suite-tiered-test.json"
OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"


def write_row(
    run_path,
    cell,
    task_id,
    *,
    verdict,
    wall,
    tokens=None,
    model_usage=None,
    cost=None,
    tool_errors=None,
    extra=None,
    judge_extra=None,
):
    task_dir = Path(run_path) / cell / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    judge = {
        "task_id": task_id,
        "source_benchmark": "swebench_verified",
        "verdict": verdict,
        "evidence_path": None,
        "judge_runtime_seconds": 0.0,
    }
    judge.update(judge_extra or {})
    (task_dir / "judge.json").write_text(json.dumps(judge, indent=2) + "\n", encoding="utf-8")

    metrics = {
        "task_id": task_id,
        "cell": cell,
        "wall_clock_seconds": wall,
        "turn_count": 1,
        "gate_intervention_count": 0,
    }
    for key, value in (tokens or {}).items():
        metrics[key] = value
    if model_usage is not None:
        metrics["model_usage"] = model_usage
    if tool_errors is not None:
        metrics["tool_error_count"] = tool_errors
        metrics["tool_error_count_available"] = True
    metrics.update(extra or {})
    (task_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

    if cost is not None:
        cost_data = {"cost_available": True, "cost_runtime_usd": cost}
    else:
        cost_data = {"cost_available": False, "cost": "not_available", "reason": "no telemetry"}
    (task_dir / "cost.json").write_text(json.dumps(cost_data, indent=2) + "\n", encoding="utf-8")
    return task_dir


def write_manifest(run_path, **fields):
    lines = []
    for key, value in fields.items():
        if value is None:
            lines.append("%s: null" % key)
        elif isinstance(value, list):
            lines.append("%s:" % key)
            lines.extend("  - %s" % item for item in value)
        else:
            text = str(value)
            if any(c in text for c in (":", "#", "-", "<", ">", "=", "!", "'")):
                text = '"%s"' % text
            lines.append("%s: %s" % (key, text))
    path = Path(run_path) / "run-manifest.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_v1_run(tmp, run_id="run-v1"):
    """Three cells over four suite-v1 ids; returns (run_dir, run_id, suite_path)."""
    run_dir = Path(tmp)
    run_path = run_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    ids = ["humaneval_plus_000", "humaneval_plus_001", "swebench_lite_000", "swebench_lite_001"]
    plan = {
        "simple-claude": ["pass", "fail", "pass", "timeout"],
        "quoin-claude": ["pass", "pass", "error", "pass"],
        "simple-codex": ["fail", "pass", "pass", "pass"],
    }
    for cell, verdicts in plan.items():
        for i, (task_id, verdict) in enumerate(zip(ids, verdicts)):
            claude = cell.endswith("claude")
            write_row(
                run_path, cell, task_id, verdict=verdict, wall=10.0 * (i + 1),
                tokens={"tokens_in": 100 * (i + 1), "tokens_out": 50 * (i + 1)},
                cost=(0.5 * (i + 1)) if claude and verdict != "error" else None,
            )
    write_manifest(run_path, run_id=run_id, suite_path=SUITE_V1, max_retries=0)
    return run_dir, run_id, SUITE_V1


def _tiered_tasks():
    tasks = []
    for task_id, tier in (("e1", "easy"), ("e2", "easy"), ("m1", "medium"), ("m2", "medium"), ("l1", "large")):
        tasks.append({"id": task_id, "tier": tier, "source": "swebench_verified",
                      "source_id": "org__repo-%s" % task_id})
    return tasks


def write_tiered_suite(path):
    suite = {
        "schema_version": 2,
        "tiered": {"tiers": {"easy": {"n": 2}, "medium": {"n": 2}, "large": {"n": 1}}},
        "tasks": _tiered_tasks(),
    }
    Path(path).write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    return Path(path)


def suite_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _usage_tokens(out):
    return {"tokens_in": out * 2, "tokens_out": out, "tokens_cache_read": out * 5,
            "tokens_cache_write": out * 3}


def _bucket(out, cost):
    return {"inputTokens": out * 2, "outputTokens": out, "cacheReadInputTokens": out * 5,
            "cacheCreationInputTokens": out * 3, "costUSD": cost}


def build_tiered_run(tmp, run_id="run-tiered", **manifest_overrides):
    """The five-task comparison fixture; returns (run_dir, run_id, suite_path).

    Plain rows carry usage-style tokens only; quoin rows carry per-model usage
    (e1 spans two models). Task l1 has no quoin directory.
    """
    run_dir = Path(tmp)
    run_path = run_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    suite_path = write_tiered_suite(run_dir / TIERED_SUITE_NAME)

    plain = [
        ("e1", "pass", 100, 1.00, 10), ("e2", "fail", 200, 2.00, 20),
        ("m1", "timeout", 300, 3.00, 30), ("m2", "pass", 400, 4.00, 40),
        ("l1", "budget_stopped", 500, 5.50, 50),
    ]
    for task_id, verdict, out, usd, wall in plain:
        write_row(run_path, "simple-claude", task_id, verdict=verdict, wall=wall,
                  tokens=_usage_tokens(out), cost=usd, tool_errors=1)

    write_row(run_path, "quoin-claude", "e1", verdict="pass", wall=15,
              model_usage={OPUS: _bucket(150, 1.2), SONNET: _bucket(50, 0.3)}, cost=1.50, tool_errors=2)
    write_row(run_path, "quoin-claude", "e2", verdict="pass", wall=25,
              model_usage={OPUS: _bucket(300, 2.5)}, cost=2.50, tool_errors=0)
    write_row(run_path, "quoin-claude", "m1", verdict="pass", wall=35,
              model_usage={OPUS: _bucket(600, 3.5)}, cost=3.50, tool_errors=3)
    write_row(run_path, "quoin-claude", "m2", verdict="error", wall=45)

    fields = dict(
        run_id=run_id, suite_path=suite_path, suite_sha=suite_sha(suite_path),
        simple_claude_model=OPUS, quoin_claude_model=OPUS, max_retries=0,
    )
    fields.update(manifest_overrides)
    write_manifest(run_path, **fields)
    return run_dir, run_id, suite_path
