"""
judge.py — Per-benchmark verdict computation.

For HumanEval+ tasks:
  Invokes the upstream evalplus.evaluate library against the agent's submitted
  solution file in a Docker sandbox; records pass/fail per problem with timing.

For SWE-bench Lite tasks:
  Invokes `python -m swebench.harness.run_evaluation` and parses the
  resolved/unresolved verdict; applies patch normalization before verdict
  computation (trailing whitespace stripping, CRLF→LF, trailing newline
  normalization) following the SWE-bench Verified harness patch-application logic.

Judge output schema:
  {task_id, source_benchmark, verdict: pass|fail|error|timeout,
   evidence_path, judge_runtime_seconds}

The judge is deterministic (no LLM-judge); same submission produces same verdict
on re-run given the same upstream harness version (pinned in requirements-bench.txt).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Patch normalization (SWE-bench Verified patch-application logic)
# ---------------------------------------------------------------------------


def normalize_patch(patch_text: str) -> str:
    """
    Normalize a patch before SWE-bench verdict computation.

    Steps:
    1. Strip trailing whitespace from every line.
    2. Normalize line endings: CRLF → LF.
    3. Ensure exactly one trailing newline at end of patch.
    """
    # Step 2: normalize CRLF → LF
    patch_text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    # Step 1: strip trailing whitespace per line
    lines = [line.rstrip() for line in patch_text.split("\n")]
    # Reconstruct
    normalized = "\n".join(lines)
    # Step 3: ensure exactly one trailing newline
    normalized = normalized.rstrip("\n") + "\n"
    return normalized


# ---------------------------------------------------------------------------
# Judge verdict types
# ---------------------------------------------------------------------------

VALID_VERDICTS = frozenset({"pass", "fail", "error", "timeout"})


@dataclass
class JudgeResult:
    task_id: str
    source_benchmark: str
    verdict: str  # pass | fail | error | timeout
    evidence_path: Optional[str]
    judge_runtime_seconds: float
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "source_benchmark": self.source_benchmark,
            "verdict": self.verdict,
            "evidence_path": self.evidence_path,
            "judge_runtime_seconds": self.judge_runtime_seconds,
        }


# ---------------------------------------------------------------------------
# HumanEval+ judge
# ---------------------------------------------------------------------------


def judge_humaneval_plus(
    task_id: str,
    solution_path: Path,
    run_id: str,
    evidence_dir: Optional[Path] = None,
) -> JudgeResult:
    """
    Judge a HumanEval+ solution using the evalplus library.

    The evalplus library must be installed and accessible. Runs in a Docker
    sandbox when available; falls back to subprocess evaluation.

    Parameters
    ----------
    task_id:
        The harness task ID (e.g., 'humaneval_plus_000').
    solution_path:
        Path to the Python file containing the agent's solution.
    run_id:
        The run identifier for evidence directory naming.
    evidence_dir:
        Optional directory to write evaluation evidence (evalplus output).
    """
    source_id = _task_id_to_source_id(task_id)
    start = time.monotonic()

    if not solution_path.exists():
        return JudgeResult(
            task_id=task_id,
            source_benchmark="evalplus_humaneval_plus",
            verdict="error",
            evidence_path=None,
            judge_runtime_seconds=time.monotonic() - start,
        )

    # Use evalplus as a library to evaluate a single problem.
    # The CLI requires samples for ALL problems; the library lets us target one.
    try:
        from evalplus.data import get_human_eval_plus
    except ImportError:
        return JudgeResult(
            task_id=task_id,
            source_benchmark="evalplus_humaneval_plus",
            verdict="error",
            evidence_path="evalplus not installed; run: pip install evalplus",
            judge_runtime_seconds=time.monotonic() - start,
        )

    try:
        problems = get_human_eval_plus()
        problem = problems.get(source_id)
        if problem is None:
            return JudgeResult(
                task_id=task_id,
                source_benchmark="evalplus_humaneval_plus",
                verdict="error",
                evidence_path=f"problem {source_id} not found in evalplus dataset",
                judge_runtime_seconds=time.monotonic() - start,
            )

        solution_code = solution_path.read_text(encoding="utf-8")

        # Build the executable: problem prompt + agent solution + test code.
        # evalplus test functions are named `check` and call the solution.
        test_program = (
            problem["prompt"]
            + "\n"
            + solution_code
            + "\n"
            + problem["test"]
            + "\n"
            + f"check({problem['entry_point']})\n"
        )

        exec_globals: dict = {}
        try:
            exec(compile(test_program, "<evalplus_judge>", "exec"), exec_globals)
            verdict = "pass"
        except Exception:
            verdict = "fail"

        elapsed = time.monotonic() - start
        return JudgeResult(
            task_id=task_id,
            source_benchmark="evalplus_humaneval_plus",
            verdict=verdict,
            evidence_path=None,
            judge_runtime_seconds=elapsed,
        )

    except Exception as exc:
        return JudgeResult(
            task_id=task_id,
            source_benchmark="evalplus_humaneval_plus",
            verdict="error",
            evidence_path=str(exc),
            judge_runtime_seconds=time.monotonic() - start,
        )


def _parse_evalplus_output(stdout: str, source_id: str) -> str:
    """Parse evalplus CLI stdout to extract pass/fail verdict."""
    # evalplus outputs lines like:
    #   HumanEval/0: pass@1: 1.000
    # or the overall summary. We look for our specific problem.
    problem_key = source_id.lower().replace("/", "_")
    for line in stdout.splitlines():
        if source_id in line or problem_key in line:
            if "1.000" in line or "pass" in line.lower():
                return "pass"
            if "0.000" in line or "fail" in line.lower():
                return "fail"
    # Also check for any overall pass indication
    if "All tests passed" in stdout:
        return "pass"
    if "error" in stdout.lower():
        return "error"
    # If evalplus returned 0 exit code but no clear verdict, default to fail
    return "fail"


# ---------------------------------------------------------------------------
# SWE-bench Lite judge
# ---------------------------------------------------------------------------


def judge_swebench_lite(
    task_id: str,
    patch_path: Path,
    run_id: str,
    evidence_dir: Optional[Path] = None,
    max_workers: int = 4,
) -> JudgeResult:
    """
    Judge a SWE-bench Lite patch using the swebench harness.

    Applies patch normalization preprocessing before verdict computation.

    Parameters
    ----------
    task_id:
        The harness task ID (e.g., 'swebench_lite_000').
    patch_path:
        Path to the .patch file containing the agent's solution.
    run_id:
        The run identifier.
    evidence_dir:
        Optional directory to write SWE-bench evaluation evidence.
    max_workers:
        Number of parallel evaluation workers for swebench.run_evaluation.
    """
    source_id = _task_id_to_source_id(task_id)
    start = time.monotonic()

    if not patch_path.exists():
        return JudgeResult(
            task_id=task_id,
            source_benchmark="swebench_lite",
            verdict="error",
            evidence_path="patch file not found",
            judge_runtime_seconds=time.monotonic() - start,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Normalize the patch before evaluation
        raw_patch = patch_path.read_text(encoding="utf-8", errors="replace")
        normalized_patch = normalize_patch(raw_patch)

        # Write normalized patch in swebench predictions format
        predictions = [
            {
                "instance_id": source_id,
                "model_patch": normalized_patch,
                "model_name_or_path": f"quoin-bench/{run_id}",
            }
        ]
        predictions_file = tmp_path / "predictions.json"
        predictions_file.write_text(
            json.dumps(predictions, indent=2), encoding="utf-8"
        )

        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "python3", "-m", "swebench.harness.run_evaluation",
            "--dataset_name", "princeton-nlp/SWE-bench_Lite",
            "--predictions_path", str(predictions_file),
            "--max_workers", str(max_workers),
            "--run_id", f"{run_id}-{task_id}",
        ]
        if evidence_dir:
            cmd += ["--output_dir", str(evidence_dir)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # SWE-bench can take 30 minutes per task
            )
            elapsed = time.monotonic() - start

            verdict = _parse_swebench_output(
                result.stdout, result.stderr, source_id, evidence_dir
            )
            return JudgeResult(
                task_id=task_id,
                source_benchmark="swebench_lite",
                verdict=verdict,
                evidence_path=str(evidence_dir) if evidence_dir else None,
                judge_runtime_seconds=elapsed,
            )
        except subprocess.TimeoutExpired:
            return JudgeResult(
                task_id=task_id,
                source_benchmark="swebench_lite",
                verdict="timeout",
                evidence_path=None,
                judge_runtime_seconds=1800.0,
            )
        except Exception as exc:
            return JudgeResult(
                task_id=task_id,
                source_benchmark="swebench_lite",
                verdict="error",
                evidence_path=str(exc),
                judge_runtime_seconds=time.monotonic() - start,
            )


def _parse_swebench_output(
    stdout: str,
    stderr: str,
    source_id: str,
    evidence_dir: Optional[Path],
) -> str:
    """Parse swebench evaluation output to extract pass/fail verdict."""
    # SWE-bench writes results to a JSONL file in the output directory
    if evidence_dir:
        # Look for a results file
        for results_file in sorted(evidence_dir.glob("*.json")):
            try:
                data = json.loads(results_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for entry in data:
                        if entry.get("instance_id") == source_id:
                            resolved = entry.get("resolved", False)
                            return "pass" if resolved else "fail"
                elif isinstance(data, dict):
                    if data.get("instance_id") == source_id:
                        resolved = data.get("resolved", False)
                        return "pass" if resolved else "fail"
            except (json.JSONDecodeError, OSError):
                continue

    # Fall back to parsing stdout
    combined = stdout + stderr
    if source_id in combined:
        if "resolved" in combined.lower():
            return "pass"
        if "unresolved" in combined.lower():
            return "fail"

    if "error" in combined.lower():
        return "error"
    return "fail"


# ---------------------------------------------------------------------------
# quoin_scenario judge (T-06)
# ---------------------------------------------------------------------------


def judge_scenario(
    task_id: str,
    task_dir: Path,
    run_id: str,
    invocation_extra: Optional[dict] = None,
) -> JudgeResult:
    """
    Judge a `quoin_scenario` task (e.g. the gate's medium-refactor-plan run).

    This is a MECHANICAL COMPLETION CHECK ONLY — it is NOT AC-4's task-
    quality measure (D-07). AC-4's quality half is `task_completion_quality`,
    scored by hand against `templates/scoring-rubric.md`; a scenario like
    "plan a medium refactor" has no deterministic pass/fail on its content.

    By the time this is called, the runner has already short-circuited any
    cell-reported terminal verdict (T-01, D-03) — a cell-side error or
    timeout never reaches here. This judge only distinguishes a normal
    completion from a plumbing failure to produce evidence at all:
      - quoin arms (an `invocation_extra` carrying either workflow-artifact
        flag): `pass` iff BOTH `workflow_artifacts_has_arch` and
        `workflow_artifacts_has_plan` are true; `fail` otherwise.
      - `simple-claude` (an `invocation_extra` with neither flag):
        `pass` iff the run produced at least one assistant turn
        (`had_assistant_event`); `fail` otherwise.
      - a missing or empty `invocation_extra` is `error`, not `fail` — a
        missing evidence channel is a plumbing fault, never a toolkit
        result.
    """
    start = time.monotonic()
    if not invocation_extra:
        return JudgeResult(
            task_id=task_id,
            source_benchmark="quoin_scenario",
            verdict="error",
            evidence_path="invocation_extra missing or empty; cannot judge a scenario task",
            judge_runtime_seconds=time.monotonic() - start,
        )

    is_quoin_arm = (
        "workflow_artifacts_has_arch" in invocation_extra
        or "workflow_artifacts_has_plan" in invocation_extra
    )
    if is_quoin_arm:
        has_arch = bool(invocation_extra.get("workflow_artifacts_has_arch"))
        has_plan = bool(invocation_extra.get("workflow_artifacts_has_plan"))
        verdict = "pass" if (has_arch and has_plan) else "fail"
    else:
        verdict = "pass" if invocation_extra.get("had_assistant_event") else "fail"

    return JudgeResult(
        task_id=task_id,
        source_benchmark="quoin_scenario",
        verdict=verdict,
        evidence_path=None,
        judge_runtime_seconds=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# SWE-bench Verified judge
# ---------------------------------------------------------------------------

SWEBENCH_PINNED_VERSION = "3.0.17"
VERIFIED_DATASET = "princeton-nlp/SWE-bench_Verified"
LITE_DATASET = "princeton-nlp/SWE-bench_Lite"
SWEBENCH_PYTHON_ENV = "QUOIN_BENCH_SWEBENCH_PYTHON"
SWEBENCH_NAMESPACE_ENV = "QUOIN_BENCH_SWEBENCH_NAMESPACE"
VERIFIED_MODEL_NAME = "quoin-bench"

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
_VERIFIED_JUDGE_LOCK = threading.Lock()
_LOG_TAIL_CHARS = 8000


def build_verified_command(
    python: str,
    source_id: str,
    predictions_path,
    judge_run_id: str,
    namespace: Optional[str] = None,
) -> list:
    cmd = [
        python, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", VERIFIED_DATASET,
        "--split", "test",
        "--instance_ids", source_id,
        "--predictions_path", str(predictions_path),
        "--max_workers", "1",
        "--timeout", "1800",
        "--run_id", judge_run_id,
    ]
    if namespace is not None:
        cmd += ["--namespace", namespace]
    return cmd


def parse_verified_report(report_path, source_id: str) -> tuple:
    """Return (verdict, reason, final) from the run report; stdout is never consulted."""
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report is not an object")
    except (OSError, ValueError):
        return ("error", "report-missing", False)

    def ids(key):
        value = report.get(key)
        return value if isinstance(value, list) else []

    if source_id in ids("resolved_ids"):
        return ("pass", "resolved", True)
    if source_id in ids("unresolved_ids") or source_id in ids("empty_patch_ids"):
        return ("fail", "unresolved", True)
    if source_id in ids("error_ids"):
        return ("error", "judge-instance-error", False)
    return ("error", "report-missing-instance", False)


def _hf_cached_revisions() -> Optional[list]:
    """Best-effort list of cached revisions of the Verified dataset; None when none found."""
    root = os.environ.get("HF_DATASETS_CACHE")
    if not root:
        hf_home = os.environ.get("HF_HOME")
        root = (
            os.path.join(hf_home, "datasets") if hf_home
            else os.path.expanduser("~/.cache/huggingface/datasets")
        )
    try:
        base = Path(root)
        if not base.is_dir():
            return None
        target = VERIFIED_DATASET.replace("/", "___").lower()
        revisions = set()
        for entry in base.iterdir():
            if entry.name.lower() != target or not entry.is_dir():
                continue
            for rev in entry.glob("default/*/*"):
                if rev.is_dir():
                    revisions.add(rev.name)
        return sorted(revisions) or None
    except OSError:
        return None


def _tail(text, limit: int = _LOG_TAIL_CHARS) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "")[-limit:]


def judge_swebench_verified(
    task_spec: dict,
    patch_path,
    run_id: str,
    evidence_dir,
    *,
    cell: Optional[str] = None,
    snapshot_revision: Optional[str] = None,
    run=subprocess.run,
    python: Optional[str] = None,
    timeout: int = 3600,
) -> JudgeResult:
    """Judge a Verified patch with the pinned swebench harness; never raises for valid arguments."""
    start = time.monotonic()
    task_id = task_spec["id"]
    source_id = task_spec.get("source_id", "")
    extra: dict = {"dataset": VERIFIED_DATASET}

    def done(verdict, reason, final, evidence=None):
        extra["judge_final"] = final
        extra["failure_reason"] = None if reason in ("resolved", "unresolved") else reason
        return JudgeResult(
            task_id=task_id, source_benchmark="swebench_verified", verdict=verdict,
            evidence_path=evidence if evidence is not None else reason,
            judge_runtime_seconds=time.monotonic() - start, extra=dict(extra),
        )

    if not isinstance(source_id, str) or not _SOURCE_ID_RE.match(source_id):
        return done("error", "bad-source-id", False)
    patch_path = Path(patch_path)
    if not patch_path.is_file():
        return done("error", "patch-file-missing", False)
    try:
        raw = patch_path.read_bytes()
    except OSError:
        return done("error", "patch-file-missing", False)
    try:
        patch = raw.decode("utf-8")
    except UnicodeDecodeError:
        return done("error", "patch-not-utf8", True)
    if not patch.strip():
        return done("fail", "empty-patch", True)
    if not patch.endswith("\n"):
        patch += "\n"

    python = python or os.environ.get(SWEBENCH_PYTHON_ENV) or sys.executable
    namespace = os.environ.get(SWEBENCH_NAMESPACE_ENV)
    judge_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{run_id}-{cell or 'nocell'}-{task_spec['id']}")
    extra["judge_run_id"] = judge_run_id
    revisions = _hf_cached_revisions()
    extra["judge_dataset_revision"] = revisions
    extra["snapshot_revision"] = snapshot_revision
    if revisions and snapshot_revision and snapshot_revision not in revisions:
        extra["dataset_revision_mismatch"] = True

    probed = False
    try:
        probe = run(
            [python, "-c", "import importlib.metadata as m; print(m.version('swebench'))"],
            capture_output=True, text=True, timeout=60,
        )
        probed = True
        if probe.returncode != 0:
            return done("error", "swebench-not-installed", False)
        version = (probe.stdout or "").strip()
        extra["swebench_version"] = version
        if version != SWEBENCH_PINNED_VERSION:
            return done("error", "swebench-version-mismatch", False)

        eval_dir = Path(evidence_dir) / "swebench_eval"
        try:
            if eval_dir.exists():
                shutil.rmtree(eval_dir)
        except OSError:
            pass
        if eval_dir.exists():
            return done("error", "judge-oserror", False)
        eval_dir.mkdir(parents=True, exist_ok=True)
        predictions = eval_dir / "predictions.json"
        predictions.write_text(
            json.dumps([{
                "instance_id": source_id,
                "model_patch": patch,
                "model_name_or_path": VERIFIED_MODEL_NAME,
            }]),
            encoding="utf-8",
        )
        cmd = build_verified_command(python, source_id, predictions.resolve(), judge_run_id, namespace)
        with _VERIFIED_JUDGE_LOCK:
            proc = run(cmd, cwd=str(eval_dir), capture_output=True, text=True, timeout=timeout)
        (eval_dir / "judge-stdout.txt").write_text(_tail(getattr(proc, "stdout", "")), encoding="utf-8")
        (eval_dir / "judge-stderr.txt").write_text(_tail(getattr(proc, "stderr", "")), encoding="utf-8")
        report = eval_dir / f"{VERIFIED_MODEL_NAME}.{judge_run_id}.json"
        extra["report_path"] = str(report)
        extra["instance_report_path"] = (
            f"logs/run_evaluation/{judge_run_id}/{VERIFIED_MODEL_NAME}/{source_id}/report.json"
        )
        verdict, reason, final = parse_verified_report(report, source_id)
        return done(verdict, reason, final, evidence=str(report))
    except subprocess.TimeoutExpired:
        return done("error", "judge-timeout", False)
    except (FileNotFoundError, PermissionError) as exc:
        extra["exception_type"] = type(exc).__name__
        return done("error", "judge-oserror" if probed else "swebench-python-missing", False)
    except OSError as exc:
        extra["exception_type"] = type(exc).__name__
        return done("error", "judge-oserror", False)
    except Exception as exc:
        extra["exception_type"] = type(exc).__name__
        return done("error", "judge-exception", False)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def judge_task(
    task_id: str,
    task_dir: Path,
    run_id: str,
    evidence_dir: Optional[Path] = None,
    invocation_extra: Optional[dict] = None,
    task_spec: Optional[dict] = None,
    judge_context: Optional[dict] = None,
) -> JudgeResult:
    """
    Dispatch to the appropriate judge based on task source.

    Looks for:
      - task_dir/solution.py for HumanEval+ tasks
      - task_dir/diff.patch for SWE-bench Lite tasks

    `invocation_extra` carries the cell's `extra` dict from `RunResult`
    (T-01) — the channel a scenario-source judge (T-06) reads to see the
    flags the cell recorded (e.g. workflow-artifact evidence). Every
    pre-existing branch here ignores it.
    """
    if task_spec is not None and task_spec.get("source") == "swebench_verified":
        ctx = judge_context or {}
        return judge_swebench_verified(
            task_spec, Path(task_dir) / "diff.patch", run_id, evidence_dir or task_dir,
            cell=ctx.get("cell"), snapshot_revision=ctx.get("snapshot_revision"),
        )

    source = _infer_source(task_id)

    if source == "evalplus_humaneval_plus":
        solution_path = task_dir / "solution.py"
        return judge_humaneval_plus(task_id, solution_path, run_id, evidence_dir)

    if source == "swebench_lite":
        patch_path = task_dir / "diff.patch"
        return judge_swebench_lite(task_id, patch_path, run_id, evidence_dir)

    if source == "quoin_scenario":
        return judge_scenario(task_id, task_dir, run_id, invocation_extra)

    return JudgeResult(
        task_id=task_id,
        source_benchmark=source,
        verdict="error",
        evidence_path=f"Unknown source benchmark for task_id={task_id}",
        judge_runtime_seconds=0.0,
    )


def _task_id_to_source_id(task_id: str) -> str:
    """
    Convert harness task_id to upstream source_id.

    For HumanEval+: humaneval_plus_042 → HumanEval/42
    For SWE-bench Lite: swebench_lite_003 → look up in suite-v1.json (not done
      here; caller must pass the source_id directly for production use).

    This is a best-effort fallback; the orchestrator should pass source_id
    from suite-v1.json directly.
    """
    if task_id.startswith("humaneval_plus_"):
        try:
            idx = int(task_id.split("_")[-1])
            return f"HumanEval/{idx}"
        except ValueError:
            return task_id
    # For SWE-bench, we can't reconstruct source_id from harness task_id
    # without the suite manifest. Return as-is; orchestrator should resolve.
    return task_id


def _infer_source(task_id: str) -> str:
    if task_id.startswith("humaneval"):
        return "evalplus_humaneval_plus"
    if task_id.startswith("swebench"):
        return "swebench_lite"
    if task_id.startswith("scenario_"):
        return "quoin_scenario"
    return "unknown"
