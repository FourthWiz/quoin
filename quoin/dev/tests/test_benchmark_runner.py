"""
test_benchmark_runner.py — Tests for the quoin benchmark harness runner.

Covers (per T-03 acceptance criteria):
  (a) result-directory schema — all 6 files present and parseable
  (b) cost.json field presence
  (c) judge.json verdict shape

Also covers:
  - result_writer.py: write_run_result() and is_result_complete()
  - isolation.py: verify_isolation() and ensure_clean_workflow_artifacts()
  - cost.py: estimate_cost() and load_pricing()
  - aggregate.py: aggregate_run()
  - check_invariants.py: InvariantResult shapes
"""

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so quoin.benchmarks is importable.
# The repo root is 4 levels up from this file:
#   quoin/dev/tests/test_benchmark_runner.py → quoin/ (repo root)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_run_result(
    cell="simple-claude",
    task_id="humaneval_plus_000",
    run_id="test-run",
    verdict="pass",
    cost_available=True,
    cost_runtime_usd=0.05,
):
    from quoin.benchmarks.harness.result_writer import RunResult

    return RunResult(
        cell=cell,
        task_id=task_id,
        run_id=run_id,
        transcript_events=[{"type": "result", "content": "done"}],
        verdict=verdict,
        evidence_path="/tmp/evidence",
        judge_runtime_seconds=1.5,
        wall_clock_seconds=10.0,
        tokens_in=500,
        tokens_out=200,
        gate_intervention_count=0,
        turn_count=3,
        cost_runtime_usd=cost_runtime_usd if cost_available else None,
        cost_estimated_usd=0.048 if cost_available else None,
        cost_delta_usd=0.002 if cost_available else None,
        cost_available=cost_available,
        prompt="Solve HumanEval/0",
        diff_patch="--- a/solution.py\n+++ b/solution.py\n",
    )


# ---------------------------------------------------------------------------
# T-03(a): result directory schema
# ---------------------------------------------------------------------------


class TestResultDirectorySchema:
    def test_all_six_files_written(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result()
        out_dir = write_run_result(result, tmp_path)

        expected_files = [
            "prompt.txt",
            "transcript.jsonl",
            "diff.patch",
            "judge.json",
            "metrics.json",
            "cost.json",
        ]
        for fname in expected_files:
            assert (out_dir / fname).exists(), f"Missing: {fname}"

    def test_result_directory_path_is_canonical(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import (
            write_run_result,
            task_result_dir,
        )

        result = make_run_result(
            cell="quoin-claude", task_id="swebench_lite_000", run_id="v1"
        )
        out_dir = write_run_result(result, tmp_path)
        expected = task_result_dir(tmp_path, "v1", "quoin-claude", "swebench_lite_000")
        assert out_dir == expected

    def test_transcript_jsonl_is_valid(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result()
        result.transcript_events = [
            {"type": "assistant", "content": "Here is my solution"},
            {"type": "result", "cost_usd": 0.05},
        ]
        out_dir = write_run_result(result, tmp_path)

        transcript = (out_dir / "transcript.jsonl").read_text(encoding="utf-8")
        lines = [l for l in transcript.strip().splitlines() if l]
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "type" in parsed

    def test_prompt_written_verbatim(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result()
        result.prompt = "My special prompt\nwith newlines"
        out_dir = write_run_result(result, tmp_path)

        assert (out_dir / "prompt.txt").read_text(encoding="utf-8") == result.prompt

    def test_diff_patch_written(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result()
        result.diff_patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n+hello\n"
        out_dir = write_run_result(result, tmp_path)

        assert "hello" in (out_dir / "diff.patch").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T-03(b): cost.json field presence
# ---------------------------------------------------------------------------


class TestCostJsonFields:
    def test_cost_available_fields_present(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result(cost_available=True, cost_runtime_usd=0.12)
        out_dir = write_run_result(result, tmp_path)

        cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
        assert cost["cost_available"] is True
        assert "cost_runtime_usd" in cost
        assert cost["cost_runtime_usd"] == pytest.approx(0.12)

    def test_cost_not_available_fields(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result(cost_available=False)
        out_dir = write_run_result(result, tmp_path)

        cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
        assert cost["cost_available"] is False
        assert cost["cost"] == "not_available"
        # No cost_runtime_usd should be present or it should be null
        assert "cost_runtime_usd" not in cost

    def test_cost_delta_sanity_check_fields(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result = make_run_result(cost_available=True, cost_runtime_usd=0.05)
        result.cost_estimated_usd = 0.048
        result.cost_delta_usd = 0.002
        out_dir = write_run_result(result, tmp_path)

        cost = json.loads((out_dir / "cost.json").read_text(encoding="utf-8"))
        assert "cost_estimated_usd" in cost
        assert "cost_delta_usd" in cost


# ---------------------------------------------------------------------------
# T-03(c): judge.json verdict shape
# ---------------------------------------------------------------------------


class TestJudgeJsonShape:
    def test_judge_json_schema(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        for verdict in ("pass", "fail", "error", "timeout"):
            result = make_run_result(verdict=verdict)
            out_dir = write_run_result(result, tmp_path)

            judge = json.loads((out_dir / "judge.json").read_text(encoding="utf-8"))
            assert "task_id" in judge
            assert "source_benchmark" in judge
            assert judge["verdict"] == verdict
            assert "judge_runtime_seconds" in judge

    def test_judge_json_source_benchmark_inferred(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import write_run_result

        result_he = make_run_result(task_id="humaneval_plus_042")
        out_he = write_run_result(result_he, tmp_path)
        judge_he = json.loads((out_he / "judge.json").read_text(encoding="utf-8"))
        assert judge_he["source_benchmark"] == "evalplus_humaneval_plus"

        result_sw = make_run_result(task_id="swebench_lite_003")
        out_sw = write_run_result(result_sw, tmp_path)
        judge_sw = json.loads((out_sw / "judge.json").read_text(encoding="utf-8"))
        assert judge_sw["source_benchmark"] == "swebench_lite"


# ---------------------------------------------------------------------------
# is_result_complete()
# ---------------------------------------------------------------------------


class TestIsResultComplete:
    def test_complete_result_passes(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import (
            write_run_result,
            is_result_complete,
            task_result_dir,
        )

        result = make_run_result()
        write_run_result(result, tmp_path)
        t_dir = task_result_dir(tmp_path, result.run_id, result.cell, result.task_id)
        assert is_result_complete(t_dir)

    def test_missing_file_fails(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import (
            write_run_result,
            is_result_complete,
            task_result_dir,
        )

        result = make_run_result()
        write_run_result(result, tmp_path)
        t_dir = task_result_dir(tmp_path, result.run_id, result.cell, result.task_id)
        (t_dir / "judge.json").unlink()
        assert not is_result_complete(t_dir)

    def test_invalid_verdict_fails(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import (
            write_run_result,
            is_result_complete,
            task_result_dir,
        )

        result = make_run_result()
        write_run_result(result, tmp_path)
        t_dir = task_result_dir(tmp_path, result.run_id, result.cell, result.task_id)
        # Corrupt judge.json with invalid verdict
        (t_dir / "judge.json").write_text(
            json.dumps({"task_id": "x", "verdict": "unknown"}), encoding="utf-8"
        )
        assert not is_result_complete(t_dir)

    def test_nonexistent_dir_fails(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import is_result_complete

        assert not is_result_complete(tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# isolation.py tests
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_verify_isolation_clean_dir(self, tmp_path):
        from quoin.benchmarks.harness.isolation import verify_isolation

        result = verify_isolation(tmp_path)
        assert result["overall_ok"]
        assert result["lessons_learned_absent"]
        assert result["sessions_absent"]
        assert result["cache_absent"]

    def test_verify_isolation_detects_lessons_learned(self, tmp_path):
        from quoin.benchmarks.harness.isolation import verify_isolation

        artifacts = tmp_path / ".workflow_artifacts" / "memory"
        artifacts.mkdir(parents=True)
        lessons = artifacts / "lessons-learned.md"
        lessons.write_text("# Lessons\n- something learned", encoding="utf-8")

        result = verify_isolation(tmp_path)
        assert not result["lessons_learned_absent"]
        assert not result["overall_ok"]

    def test_ensure_clean_workflow_artifacts_removes_contamination(self, tmp_path):
        from quoin.benchmarks.harness.isolation import (
            ensure_clean_workflow_artifacts,
            verify_isolation,
        )

        # Create contaminated state
        artifacts = tmp_path / ".workflow_artifacts" / "memory"
        artifacts.mkdir(parents=True)
        (artifacts / "lessons-learned.md").write_text("contamination", encoding="utf-8")

        ensure_clean_workflow_artifacts(tmp_path)

        result = verify_isolation(tmp_path)
        assert result["lessons_learned_absent"]
        assert result["overall_ok"]

    def test_ensure_clean_creates_directory_structure(self, tmp_path):
        from quoin.benchmarks.harness.isolation import ensure_clean_workflow_artifacts

        ensure_clean_workflow_artifacts(tmp_path)

        assert (tmp_path / ".workflow_artifacts").is_dir()
        assert (tmp_path / ".workflow_artifacts" / "memory").is_dir()


# ---------------------------------------------------------------------------
# cost.py tests
# ---------------------------------------------------------------------------


class TestCostEstimation:
    def test_estimate_cost_claude_model(self, tmp_path):
        from quoin.benchmarks.harness.cost import estimate_cost, load_pricing

        # Create a minimal pricing.json for testing
        pricing_data = {
            "models": {
                "claude-opus-4-7-20261001": {
                    "input_per_1m_usd": 15.0,
                    "output_per_1m_usd": 75.0,
                    "cache_write_per_1m_usd": 18.75,
                    "cache_read_per_1m_usd": 1.50,
                }
            }
        }
        pricing_file = tmp_path / "pricing.json"
        pricing_file.write_text(json.dumps(pricing_data), encoding="utf-8")
        pricing = json.loads(pricing_file.read_text(encoding="utf-8"))

        # 1000 input tokens, 500 output tokens
        # Expected: 1000/1M * $15 + 500/1M * $75 = $0.015 + $0.0375 = $0.0525
        cost = estimate_cost(
            model="claude-opus-4-7-20261001",
            tokens_in=1000,
            tokens_out=500,
            pricing=pricing,
        )
        assert cost is not None
        assert isinstance(cost, Decimal)
        assert cost == Decimal("0.052500")  # $15/1M * 1000 + $75/1M * 500

    def test_estimate_cost_codex_returns_none(self):
        from quoin.benchmarks.harness.cost import estimate_cost

        # Codex models must always return None
        for model in ["gpt-5.5", "o4-mini-2025-04-16", "gpt-4", "o1-preview", "codex-davinci"]:
            result = estimate_cost(model=model, tokens_in=1000, tokens_out=500)
            assert result is None, f"Expected None for Codex model {model}, got {result}"

    def test_estimate_cost_unknown_claude_model_returns_none(self):
        from quoin.benchmarks.harness.cost import estimate_cost

        result = estimate_cost(
            model="claude-nonexistent-9999",
            tokens_in=1000,
            tokens_out=500,
            pricing={"models": {}},
        )
        assert result is None

    def test_estimate_cost_with_cache_tokens(self):
        from quoin.benchmarks.harness.cost import estimate_cost

        pricing = {
            "models": {
                "claude-opus-4-7-20261001": {
                    "input_per_1m_usd": 15.0,
                    "output_per_1m_usd": 75.0,
                    "cache_write_per_1m_usd": 18.75,
                    "cache_read_per_1m_usd": 1.50,
                }
            }
        }
        cost = estimate_cost(
            model="claude-opus-4-7-20261001",
            tokens_in=1000,
            tokens_out=500,
            cache_write=2000,
            cache_read=3000,
            pricing=pricing,
        )
        assert cost is not None
        # cache_write: 2000/1M * $18.75 = $0.0375
        # cache_read: 3000/1M * $1.50 = $0.0045
        # total: $0.0525 + $0.0375 + $0.0045 = $0.0945
        assert cost > Decimal("0.09")


# ---------------------------------------------------------------------------
# judge.py patch normalization tests
# ---------------------------------------------------------------------------


class TestPatchNormalization:
    def test_normalize_patch_strips_trailing_whitespace(self):
        from quoin.benchmarks.harness.judge import normalize_patch

        patch = "--- a/foo.py   \n+++ b/foo.py\n@@ -1,1 +1,2 @@\n+hello   \n"
        normalized = normalize_patch(patch)
        assert "   " not in normalized

    def test_normalize_patch_crlf_to_lf(self):
        from quoin.benchmarks.harness.judge import normalize_patch

        patch = "--- a/foo.py\r\n+++ b/foo.py\r\n"
        normalized = normalize_patch(patch)
        assert "\r" not in normalized
        assert "\n" in normalized

    def test_normalize_patch_trailing_newline(self):
        from quoin.benchmarks.harness.judge import normalize_patch

        patch_no_newline = "--- a/foo.py\n+++ b/foo.py"
        normalized = normalize_patch(patch_no_newline)
        assert normalized.endswith("\n")

        patch_multi_newline = "--- a/foo.py\n+++ b/foo.py\n\n\n"
        normalized2 = normalize_patch(patch_multi_newline)
        assert normalized2.endswith("\n")
        assert not normalized2.endswith("\n\n")


# ---------------------------------------------------------------------------
# T-19: Gate auto-approve unit tests
# ---------------------------------------------------------------------------


class TestGateAutoApprove:
    """
    Unit tests for the dual-env-var gate auto-approve guard (T-19).

    These tests verify the CONTRACT specified in gate.md and gate/SKILL.md:
    - Both QUOIN_GATE_AUTO_APPROVE=1 AND QUOIN_BENCHMARK_RUN set → auto-approve
    - Only QUOIN_GATE_AUTO_APPROVE=1 (no QUOIN_BENCHMARK_RUN) → NOT auto-approved
    - Only QUOIN_BENCHMARK_RUN (no QUOIN_GATE_AUTO_APPROVE) → NOT auto-approved
    - Neither → NOT auto-approved

    Implementation note: the actual gate auto-approve check runs inside the
    gate SKILL.md (which is a Claude Code skill, not a Python module). These
    tests verify the DECISION LOGIC as a standalone function that mirrors the
    contract in the SKILL.md specification.
    """

    def _should_auto_approve(self, env: dict) -> bool:
        """
        Mirror the gate auto-approve decision logic from gate SKILL.md Step 4a.

        Returns True iff BOTH QUOIN_GATE_AUTO_APPROVE=1 AND QUOIN_BENCHMARK_RUN
        are set in the given env dict.
        """
        auto_approve = env.get("QUOIN_GATE_AUTO_APPROVE", "")
        benchmark_run = env.get("QUOIN_BENCHMARK_RUN", "")
        return auto_approve == "1" and bool(benchmark_run)

    def test_both_vars_set_triggers_auto_approve(self):
        env = {
            "QUOIN_GATE_AUTO_APPROVE": "1",
            "QUOIN_BENCHMARK_RUN": "v0-smoke",
        }
        assert self._should_auto_approve(env) is True

    def test_only_gate_approve_no_benchmark_run_does_not_auto_approve(self):
        """The gate must block normally when only QUOIN_GATE_AUTO_APPROVE is set."""
        env = {"QUOIN_GATE_AUTO_APPROVE": "1"}
        assert self._should_auto_approve(env) is False

    def test_only_benchmark_run_no_gate_approve_does_not_auto_approve(self):
        env = {"QUOIN_BENCHMARK_RUN": "v0-smoke"}
        assert self._should_auto_approve(env) is False

    def test_neither_var_set_does_not_auto_approve(self):
        env = {}
        assert self._should_auto_approve(env) is False

    def test_auto_approve_value_must_be_exactly_1(self):
        """QUOIN_GATE_AUTO_APPROVE=true should NOT trigger auto-approve."""
        env = {
            "QUOIN_GATE_AUTO_APPROVE": "true",
            "QUOIN_BENCHMARK_RUN": "v1",
        }
        assert self._should_auto_approve(env) is False

        env2 = {
            "QUOIN_GATE_AUTO_APPROVE": "yes",
            "QUOIN_BENCHMARK_RUN": "v1",
        }
        assert self._should_auto_approve(env2) is False

    def test_benchmark_run_can_be_any_nonempty_value(self):
        for run_id in ("v1", "v0-smoke", "test-run-123", "1"):
            env = {
                "QUOIN_GATE_AUTO_APPROVE": "1",
                "QUOIN_BENCHMARK_RUN": run_id,
            }
            assert self._should_auto_approve(env) is True, f"Should approve for run_id={run_id}"

    def test_benchmark_run_empty_string_does_not_approve(self):
        env = {
            "QUOIN_GATE_AUTO_APPROVE": "1",
            "QUOIN_BENCHMARK_RUN": "",
        }
        assert self._should_auto_approve(env) is False

    def test_auto_approve_audit_fields_schema(self):
        """Verify the expected audit log fields when auto-approve fires."""
        # These fields must appear in the gate audit log written in auto-approve mode.
        # This test documents the CONTRACT — the actual writing is done by the gate skill.
        required_auto_approve_audit_fields = {
            "auto_approved",
            "env",
            "gate_encountered_at",
            "benchmark_run_id",
        }
        # Simulate what the gate skill would write
        audit_fields = {
            "auto_approved": True,
            "env": "QUOIN_GATE_AUTO_APPROVE=1 QUOIN_BENCHMARK_RUN=v1",
            "gate_encountered_at": "2026-05-17T10:00:00Z",
            "benchmark_run_id": "v1",
        }
        for field in required_auto_approve_audit_fields:
            assert field in audit_fields, f"Missing required auto-approve audit field: {field}"
        assert audit_fields["auto_approved"] is True


# ---------------------------------------------------------------------------
# T-19: Static-assertion test — gate/SKILL.md text contracts
# ---------------------------------------------------------------------------


class TestGateSkillText:
    """
    Static-assertion tests that verify gate/SKILL.md actually contains the
    T-19 auto-approve plumbing (not just a Python mirror of the logic).

    These tests read the SKILL.md file on disk and assert that the required
    env-var names, audit fields, and logical AND context are all present.
    They complement TestGateAutoApprove (which tests decision logic) by
    verifying the actual gate skill text, in the spirit of test_gate_audit_persistence.py.
    """

    @staticmethod
    def _load_gate_skill() -> str:
        import pathlib
        project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
        gate_skill = project_root / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
        return gate_skill.read_text()

    def test_gate_auto_approve_text_invariants(self):
        """
        SKILL.md must contain the T-19 auto-approve plumbing:
        1. QUOIN_GATE_AUTO_APPROVE appears at least twice (definition + use)
        2. QUOIN_BENCHMARK_RUN appears at least once
        3. auto_approved: true appears in the audit-fields block
        4. Both env vars appear within 10 lines of each other (AND context)
        """
        text = self._load_gate_skill()

        # 1. QUOIN_GATE_AUTO_APPROVE appears at least twice
        count_auto_approve = text.count("QUOIN_GATE_AUTO_APPROVE")
        assert count_auto_approve >= 2, (
            f"gate/SKILL.md: expected QUOIN_GATE_AUTO_APPROVE to appear at least twice "
            f"(definition + use), found {count_auto_approve} occurrence(s)"
        )

        # 2. QUOIN_BENCHMARK_RUN appears at least once
        assert "QUOIN_BENCHMARK_RUN" in text, (
            "gate/SKILL.md: QUOIN_BENCHMARK_RUN env-var name not found — "
            "T-19 dual-guard requirement is missing from SKILL.md"
        )

        # 3. auto_approved: true appears in the audit-fields block
        assert "auto_approved: true" in text, (
            "gate/SKILL.md: 'auto_approved: true' audit field not found — "
            "the gate skill must write this field when auto-approving for benchmark runs"
        )

        # 4. Both env vars appear within 10 lines of each other (AND context)
        lines = text.splitlines()
        auto_approve_lines = [i for i, ln in enumerate(lines) if "QUOIN_GATE_AUTO_APPROVE" in ln]
        benchmark_run_lines = [i for i, ln in enumerate(lines) if "QUOIN_BENCHMARK_RUN" in ln]
        assert auto_approve_lines, "QUOIN_GATE_AUTO_APPROVE not found in any line"
        assert benchmark_run_lines, "QUOIN_BENCHMARK_RUN not found in any line"
        # At least one pair of lines must be within 10 lines of each other
        found_close_pair = any(
            abs(a - b) <= 10
            for a in auto_approve_lines
            for b in benchmark_run_lines
        )
        assert found_close_pair, (
            "gate/SKILL.md: QUOIN_GATE_AUTO_APPROVE and QUOIN_BENCHMARK_RUN do not appear "
            "within 10 lines of each other in any passage — the dual-guard AND context "
            "may be missing or split across the file"
        )


# ---------------------------------------------------------------------------
# Round-4 fix (MAJOR 4): a generic exception AFTER the cell already streamed
# a real transcript.jsonl must not clobber it with an empty ring buffer.
# ---------------------------------------------------------------------------


class TestExceptionPathPreservesStreamedTranscript:
    def test_judge_crash_after_streaming_does_not_truncate_transcript_jsonl(self, tmp_path, monkeypatch):
        """Regression: `run_one_task`'s generic `except Exception` handler
        used to build its `RunResult` with `extra={}` and
        `transcript_events=[]`, regardless of what the cell had already
        streamed to `transcript.jsonl` on disk. `write_run_result`'s own
        `if not result.extra.get("transcript_streamed")` re-open guard
        then never fired (because `extra` was empty), and the ring-buffer
        write clobbered the real, paid transcript with an empty file."""
        import types

        from quoin.benchmarks.harness import runner as runner_mod
        from quoin.benchmarks.harness.config import BudgetSpec, HarnessConfig

        run_dir = tmp_path / "runs"
        streamed_content = '{"type": "assistant"}\n{"type": "result", "total_cost_usd": 0.42}\n'

        def fake_invoke(task_spec, workdir, budget, run_id, run_output_dir=None, **kwargs):
            # Simulate the cell: stream a real transcript.jsonl to its own
            # result dir and report that it did so via `extra`, exactly as
            # simple_claude.invoke / quoin_claude.invoke now do at file-open
            # time. `run_output_dir` must be a named parameter, not folded
            # into **kwargs — the runner only threads it when
            # `inspect.signature` reports it by name (T-15/D-11).
            run_output_dir.mkdir(parents=True, exist_ok=True)
            (run_output_dir / "transcript.jsonl").write_text(streamed_content, encoding="utf-8")
            return {
                "prompt": "x", "transcript_events": [{"type": "assistant"}, {"type": "result"}],
                "diff_patch": "", "cost_available": True, "cost_runtime_usd": 0.42,
                "cost_estimated_usd": None, "cost_delta_usd": None,
                "tokens_in": 10, "tokens_out": 5, "tokens_cache_read": None,
                "tokens_cache_write": None, "turn_count": 1, "gate_intervention_count": 0,
                "verdict": None,  # not a cell-terminal verdict — falls through to judge_task
                "extra": {"transcript_streamed": True},
            }

        fake_adapter = types.SimpleNamespace(invoke=fake_invoke)
        monkeypatch.setattr(runner_mod, "_load_cell_adapter", lambda cell: fake_adapter)

        def crashing_judge(*a, **kw):
            raise RuntimeError("judge blew up after the transcript was already on disk")

        monkeypatch.setattr(runner_mod, "judge_task", crashing_judge)

        config = HarnessConfig(run_dir=run_dir, budget=BudgetSpec(wall_clock_seconds=60))
        result = runner_mod.run_one_task(
            cell="simple-claude", task_spec={"id": "t1", "description": "x"},
            run_id="r1", config=config, fixture_repo=None,
        )

        assert result.verdict == "error"
        assert result.extra.get("transcript_streamed") is True

        out_dir = runner_mod.task_result_dir(run_dir, "r1", "simple-claude", "t1")
        on_disk = (out_dir / "transcript.jsonl").read_text(encoding="utf-8")
        assert on_disk == streamed_content, (
            "the exception path must not truncate an already-streamed transcript"
        )
