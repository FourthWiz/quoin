"""Tests for quoin/core/scripts/status_graph.py — phase detection, ASCII render,
active-task selection, and CLI flags.

All tests are deterministic (no LLM calls, no live git). Fixtures build synthetic
.workflow_artifacts/ structures in tmp directories.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Load the core script directly (not via wrapper, to avoid path assumptions).
# Register in sys.modules before exec so @dataclass can find the module namespace
# (required for Python 3.8 compatibility — see CPython issue with dynamic loading).
_CORE_PATH = (
    Path(__file__).resolve().parents[2] / "core" / "scripts" / "status_graph.py"
)
import importlib.util as _ilu
_SPEC = _ilu.spec_from_file_location("_quoin_core_status_graph_test", _CORE_PATH)
_SG = _ilu.module_from_spec(_SPEC)
sys.modules["_quoin_core_status_graph_test"] = _SG
_SPEC.loader.exec_module(_SG)

detect_phase = _SG.detect_phase
render_graph = _SG.render_graph
pick_active_task = _SG.pick_active_task
PhaseResult = _SG.PhaseResult
main = _SG.main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(root: Path, name: str, files: list[str]) -> Path:
    """Create a synthetic task dir under root/.workflow_artifacts/NAME with given files."""
    task_dir = root / ".workflow_artifacts" / name
    task_dir.mkdir(parents=True, exist_ok=True)
    for fname in files:
        f = task_dir / fname
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {fname} placeholder\n")
    return task_dir


# ---------------------------------------------------------------------------
# Phase detection — one fixture per phase
# ---------------------------------------------------------------------------

class TestDetectPhase:
    def test_empty_dir_is_discover(self, tmp_path):
        task_dir = tmp_path / ".workflow_artifacts" / "my-task"
        task_dir.mkdir(parents=True)
        r = detect_phase(task_dir)
        assert r.phase == "discover"
        assert r.critic_rounds == 0

    def test_architecture_only(self, tmp_path):
        task_dir = make_task(tmp_path, "t", ["architecture.md"])
        r = detect_phase(task_dir)
        assert r.phase == "architecture"

    def test_plan_only_is_planning(self, tmp_path):
        task_dir = make_task(tmp_path, "t", ["current-plan.md"])
        r = detect_phase(task_dir)
        assert r.phase == "planning"
        assert r.critic_rounds == 0

    def test_plan_plus_critic_round_2(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "critic-response-1.md",
            "critic-response-2.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "planning"
        assert r.critic_rounds == 2

    def test_gate_post_plan_is_plan_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-plan-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "plan-gated"

    def test_gate_plan_prefix_is_plan_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-plan-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "plan-gated"

    def test_gate_implement_is_implement_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-plan-2026-05-31.md",
            "gate-implement-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "implement-gated"

    def test_gate_post_implement_is_implement_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-plan-2026-05-31.md",
            "gate-post-implement-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "implement-gated"

    def test_review_n_is_review(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-implement-2026-05-31.md",
            "review-1.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "review"
        assert r.review_rounds == 1

    def test_gate_review_is_review_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-implement-2026-05-31.md",
            "review-1.md",
            "gate-review-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "review-gated"

    def test_gate_post_review_is_review_gated(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-implement-2026-05-31.md",
            "review-1.md",
            "gate-post-review-2026-05-31.md",
        ])
        r = detect_phase(task_dir)
        assert r.phase == "review-gated"

    def test_finalized_path_is_done(self, tmp_path):
        # A task under finalized/ should always return "done"
        task_dir = tmp_path / ".workflow_artifacts" / "finalized" / "old-task"
        task_dir.mkdir(parents=True)
        (task_dir / "current-plan.md").write_text("# plan\n")
        r = detect_phase(task_dir)
        assert r.phase == "done"

    def test_pre_gate_implement_shows_plan_gated(self, tmp_path):
        """Pre-implement (gate not yet run) task should report plan-gated, not implement.

        This is documented degraded behavior (D-03): a task mid-implement but
        without a gate artifact is indistinguishable from plan-gated.
        """
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md",
            "gate-post-plan-2026-05-31.md",
            # No gate-implement-* or gate-post-implement-* yet
        ])
        r = detect_phase(task_dir)
        assert r.phase == "plan-gated"  # degraded behavior — pinned not accidental

    def test_multi_stage_with_stage_subfolder(self, tmp_path):
        """Stage resolution: task root has only cost-ledger; stage-1/ has plan."""
        artifacts = tmp_path / ".workflow_artifacts" / "my-task"
        artifacts.mkdir(parents=True)
        (artifacts / "cost-ledger.md").write_text("# cost\n")
        stage1 = artifacts / "stage-1"
        stage1.mkdir()
        (stage1 / "current-plan.md").write_text("# plan\n")
        # Detect from the stage-1 dir directly
        r = detect_phase(stage1)
        assert r.phase == "planning"


# ---------------------------------------------------------------------------
# render_graph
# ---------------------------------------------------------------------------

_PIPELINE_NODES = ["discover", "architect", "thorough_plan", "implement", "review", "end_of_task"]
_ALLOWED_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _-[]^>().,:#!?/\n→")


class TestRenderGraph:
    def _make_result(self, phase: str, crit: int = 0, rev: int = 0) -> PhaseResult:
        return PhaseResult(phase=phase, critic_rounds=crit, review_rounds=rev)

    def test_full_graph_contains_all_nodes(self):
        r = self._make_result("planning", crit=2)
        graph = render_graph(r, "my-task")
        for node in _PIPELINE_NODES:
            assert node in graph, f"node '{node}' missing from graph"

    def test_full_graph_marks_active_node(self):
        r = self._make_result("planning")
        graph = render_graph(r, "my-task")
        assert "[thorough_plan]" in graph

    def test_full_graph_you_are_here(self):
        r = self._make_result("plan-gated")
        graph = render_graph(r, "my-task")
        assert "you are here" in graph

    def test_full_graph_single_marker(self):
        for phase in ["discover", "planning", "plan-gated", "implement-gated", "review", "done"]:
            r = self._make_result(phase)
            graph = render_graph(r, "t")
            markers = graph.count("[")  # bracketed active node
            assert markers >= 1, f"no active marker for phase={phase}"

    def test_full_graph_no_tab_chars(self):
        r = self._make_result("implement-gated")
        graph = render_graph(r, "my-task")
        assert "\t" not in graph

    def test_full_graph_ascii_safe(self):
        r = self._make_result("review-gated")
        graph = render_graph(r, "my-task")
        for ch in graph:
            assert ord(ch) < 128 or ch == "→", f"non-ASCII char {repr(ch)} in full graph"

    def test_critic_rounds_shown_in_planning(self):
        r = self._make_result("planning", crit=3)
        graph = render_graph(r, "my-task")
        assert "round 3" in graph or "critic" in graph

    def test_compact_max_line_width(self):
        r = self._make_result("planning")
        graph = render_graph(r, "a" * 40, compact=True)
        for line in graph.splitlines():
            assert len(line) <= 40, f"compact line too long ({len(line)}): {repr(line)}"

    def test_compact_contains_active_marker(self):
        r = self._make_result("plan-gated")
        graph = render_graph(r, "my-task", compact=True)
        assert ">>>" in graph

    def test_compact_contains_all_nodes(self):
        r = self._make_result("review")
        graph = render_graph(r, "my-task", compact=True)
        for node in _PIPELINE_NODES:
            assert node in graph


# ---------------------------------------------------------------------------
# Active-task selection
# ---------------------------------------------------------------------------

class TestPickActiveTask:
    def test_no_artifacts_returns_none(self, tmp_path):
        (tmp_path / ".workflow_artifacts").mkdir()
        assert pick_active_task(tmp_path) is None

    def test_single_task_returned(self, tmp_path):
        make_task(tmp_path, "alpha", ["current-plan.md"])
        result = pick_active_task(tmp_path)
        assert result is not None
        assert result.name == "alpha"

    def test_most_recent_task_selected(self, tmp_path):
        """When multiple tasks exist, the one with the newest artifact mtime is selected."""
        make_task(tmp_path, "old-task", ["architecture.md"])
        import time; time.sleep(0.02)  # ensure distinct mtime
        make_task(tmp_path, "new-task", ["current-plan.md"])
        result = pick_active_task(tmp_path)
        assert result is not None
        assert result.name == "new-task"

    def test_excludes_memory_dir(self, tmp_path):
        mem = tmp_path / ".workflow_artifacts" / "memory"
        mem.mkdir(parents=True)
        (mem / "some-file.md").write_text("data\n")
        assert pick_active_task(tmp_path) is None

    def test_excludes_cache_dir(self, tmp_path):
        cache = tmp_path / ".workflow_artifacts" / "cache"
        cache.mkdir(parents=True)
        (cache / "_index.md").write_text("data\n")
        assert pick_active_task(tmp_path) is None

    def test_excludes_finalized_dir(self, tmp_path):
        fin = tmp_path / ".workflow_artifacts" / "finalized" / "done-task"
        fin.mkdir(parents=True)
        (fin / "current-plan.md").write_text("data\n")
        # finalized/ directory is itself excluded (not a task dir at top level)
        # pick_active_task excludes "finalized" by name
        assert pick_active_task(tmp_path) is None

    def test_excludes_trash_dir(self, tmp_path):
        """IVG-262: newest-mtime trash/ dir must never be picked as the active task."""
        make_task(tmp_path, "old-task", ["current-plan.md"])
        trash = tmp_path / ".workflow_artifacts" / "trash" / "2026-09-27" / "old-task"
        trash.mkdir(parents=True)
        import time; time.sleep(0.02)  # ensure trash/ has the newest mtime
        (trash / "current-plan.md").write_text("data\n")
        result = pick_active_task(tmp_path)
        assert result is not None
        assert result.name == "old-task"

    def test_excludes_security_review_dir_even_with_newest_mtime(self, tmp_path):
        """IVG-128 D-07/MIN-3: the standalone security-review dir must never be
        mistaken for the active task, even when it has the most recent mtime
        of any dir under .workflow_artifacts/.
        """
        make_task(tmp_path, "real-task", ["current-plan.md"])
        import time; time.sleep(0.02)  # ensure security-review/ mtime is strictly newer
        sec = tmp_path / ".workflow_artifacts" / "security-review"
        sec.mkdir(parents=True)
        (sec / "security-review-1.md").write_text("data\n")
        result = pick_active_task(tmp_path)
        assert result is not None
        assert result.name == "real-task", (
            f"pick_active_task() returned {result.name!r}; expected 'real-task' — "
            "the standalone .workflow_artifacts/security-review/ dir must be excluded "
            "from mtime-based active-task discovery regardless of recency."
        )

    def test_stray_top_level_file_excluded(self, tmp_path):
        """Non-directory entries under .workflow_artifacts/ must be ignored."""
        artifacts = tmp_path / ".workflow_artifacts"
        artifacts.mkdir()
        (artifacts / "QUICKSTART.md").write_text("stray\n")  # stray top-level file
        (artifacts / "current-plan.md").write_text("stray\n")
        assert pick_active_task(tmp_path) is None  # no qualifying task dirs

    def test_empty_task_dir_excluded(self, tmp_path):
        """A task dir with no non-empty artifacts does not qualify."""
        task_dir = tmp_path / ".workflow_artifacts" / "ghost-task"
        task_dir.mkdir(parents=True)
        (task_dir / "empty.md").write_text("")  # empty file
        assert pick_active_task(tmp_path) is None


# ---------------------------------------------------------------------------
# CLI — main()
# ---------------------------------------------------------------------------

class TestCLI:
    def test_zero_task_message(self, tmp_path, capsys):
        (tmp_path / ".workflow_artifacts").mkdir()
        rc = main(["--project-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert "No active task" in out
        assert rc == 0

    def test_task_flag_picks_named_task(self, tmp_path, capsys):
        make_task(tmp_path, "my-work", ["current-plan.md"])
        rc = main(["--project-root", str(tmp_path), "--task", "my-work"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "my-work" in out

    def test_json_output_valid(self, tmp_path, capsys):
        make_task(tmp_path, "json-task", ["current-plan.md"])
        rc = main(["--project-root", str(tmp_path), "--task", "json-task", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert "phase" in data
        assert "task" in data
        assert "critic_rounds" in data
        assert "review_rounds" in data
        assert "task_dir" in data

    def test_compact_flag_max_width(self, tmp_path, capsys):
        make_task(tmp_path, "compact-task", ["current-plan.md"])
        rc = main(["--project-root", str(tmp_path), "--task", "compact-task", "--compact"])
        out = capsys.readouterr().out
        assert rc == 0
        for line in out.splitlines():
            assert len(line) <= 40, f"compact line too wide: {repr(line)}"

    def test_unknown_project_root_exits_1(self, tmp_path, capsys):
        empty = tmp_path / "no-artifacts"
        empty.mkdir()
        rc = main(["--project-root", str(empty)])
        assert rc == 1


# ---------------------------------------------------------------------------
# --emit-nodes output
# ---------------------------------------------------------------------------

build_nodes = _SG.build_nodes

class TestEmitNodes:
    def _nodes(self, task_dir: Path, **kwargs) -> list[dict]:
        result = detect_phase(task_dir, **kwargs)
        return build_nodes(result)

    def test_planning_phase_active_node_is_thorough_plan(self, tmp_path):
        task_dir = make_task(tmp_path, "t", ["current-plan.md"])
        nodes = self._nodes(task_dir)
        by_name = {n["node"]: n for n in nodes}
        assert by_name["discover"]["state"] == "done"
        assert by_name["architect"]["state"] == "done"
        assert by_name["thorough_plan"]["state"] == "active"
        assert by_name["implement"]["state"] == "future"
        assert by_name["review"]["state"] == "future"
        assert by_name["end_of_task"]["state"] == "future"

    def test_discover_phase_all_future_except_active(self, tmp_path):
        task_dir = tmp_path / ".workflow_artifacts" / "empty-task"
        task_dir.mkdir(parents=True)
        nodes = self._nodes(task_dir)
        by_name = {n["node"]: n for n in nodes}
        assert by_name["discover"]["state"] == "active"
        assert by_name["architect"]["state"] == "future"

    def test_done_phase_all_nodes_done(self, tmp_path):
        finalized = tmp_path / ".workflow_artifacts" / "finalized" / "old-task"
        finalized.mkdir(parents=True)
        (finalized / "current-plan.md").write_text("# done\n")
        result = detect_phase(finalized)
        assert result.phase == "done"
        nodes = build_nodes(result)
        assert all(n["state"] == "done" for n in nodes)

    def test_critic_rounds_adornment(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md", "critic-response-1.md", "critic-response-2.md",
        ])
        nodes = self._nodes(task_dir)
        by_name = {n["node"]: n for n in nodes}
        assert by_name["thorough_plan"]["state"] == "active"
        assert by_name["thorough_plan"]["critic_rounds"] == 2

    def test_critic_rounds_zero_not_in_output(self, tmp_path):
        task_dir = make_task(tmp_path, "t", ["current-plan.md"])
        nodes = self._nodes(task_dir)
        by_name = {n["node"]: n for n in nodes}
        assert "critic_rounds" not in by_name["thorough_plan"]

    def test_review_rounds_adornment(self, tmp_path):
        task_dir = make_task(tmp_path, "t", [
            "current-plan.md", "critic-response-1.md", "review-1.md", "review-2.md",
        ])
        nodes = self._nodes(task_dir)
        by_name = {n["node"]: n for n in nodes}
        assert by_name["review"]["state"] == "active"
        assert by_name["review"]["review_rounds"] == 2

    def test_json_without_emit_nodes_has_no_nodes_key(self, tmp_path, capsys):
        make_task(tmp_path, "t", ["current-plan.md"])
        rc = main(["--project-root", str(tmp_path), "--task", "t", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert "nodes" not in data

    def test_emit_nodes_adds_nodes_key(self, tmp_path, capsys):
        make_task(tmp_path, "t", ["current-plan.md"])
        rc = main(["--project-root", str(tmp_path), "--task", "t", "--emit-nodes"])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert "nodes" in data
        assert len(data["nodes"]) == 6
        assert all("node" in n and "state" in n for n in data["nodes"])

    def test_pipeline_length_and_names(self, tmp_path):
        task_dir = make_task(tmp_path, "t", ["current-plan.md"])
        nodes = self._nodes(task_dir)
        expected = ["discover", "architect", "thorough_plan", "implement", "review", "end_of_task"]
        assert [n["node"] for n in nodes] == expected
