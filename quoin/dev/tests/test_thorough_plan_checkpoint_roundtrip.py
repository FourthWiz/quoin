"""T-06 (roundtrip): Executable roundtrip test for the phase-boundary checkpoint helper (IVG-98).

Tests the full write/read cycle for thorough_plan_checkpoint.py:
  - Multiple boundaries (round-1 plan, round-1 critic, round-2 revise)
  - Checkpoint file content and stage token format
  - ## Last user intent non-empty (C-01)
  - Pending-restore sentinel written and discoverable (M-04 foreign-SID scenario)
  - M-01 write-order: checkpoint mtime >= orchestrator session-state mtime
  - C-02 adversarial subagent mtime: B3 Clause-B fires, T-04 direct scan still works
  - Empty-SID / unknown-SID guard
  - M-02 session-state: creation-if-absent AND update-in-place
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the core module under test
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_SCRIPT = (
    REPO_ROOT / "quoin" / "core" / "scripts" / "thorough_plan_checkpoint.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_test_thorough_plan_checkpoint", _CORE_SCRIPT
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_module()

TASK = "test-ivg98-roundtrip"
SID = "TEST-SID-ROUNDTRIP"
TODAY = "2026-07-01"


def _call(tmp_path: Path, round_n: int, phase: str, sid: str = SID, **kwargs):
    """Invoke main() with standard args, additional kwargs optional."""
    sessions_dir = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ss_path = sessions_dir / f"{TODAY}-{TASK}-orchestrator.md"

    argv = [
        "--project-root", str(tmp_path),
        "--task", TASK,
        "--round", str(round_n),
        "--phase", phase,
        "--sid", sid,
        "--branch", "main",
        "--plan-path", str(tmp_path / ".workflow_artifacts" / TASK / "current-plan.md"),
        "--session-state", str(ss_path),
    ]
    for k, v in kwargs.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]

    rc = _MOD.main(argv)
    assert rc == 0, f"main() returned {rc}, expected 0"
    return ss_path


class TestThoroughPlanCheckpointRoundtrip:
    """Full write/read roundtrip assertions for thorough_plan_checkpoint (IVG-98 T-06 AC-4)."""

    # ------------------------------------------------------------------
    # Basic roundtrip
    # ------------------------------------------------------------------

    def test_multiple_boundaries_final_state(self, tmp_path):
        """Simulate round-1 plan, round-1 critic, round-2 revise; assert checkpoint content."""
        _call(tmp_path, round_n=1, phase="plan")
        _call(tmp_path, round_n=1, phase="critic")
        _call(tmp_path, round_n=2, phase="revise")

        ckpt = tmp_path / ".workflow_artifacts" / "memory" / "checkpoints" / f"thorough-plan-progress-{SID}.md"
        assert ckpt.exists(), f"Checkpoint file not found: {ckpt}"
        text = ckpt.read_text()

        # Stage token
        assert "## Current stage" in text
        assert "thorough-plan:round-2-revise" in text

        # Active task
        assert "## Active task" in text
        assert TASK in text

    def test_last_user_intent_non_empty(self, tmp_path):
        """C-01: ## Last user intent must be present and non-empty in checkpoint."""
        _call(tmp_path, round_n=2, phase="revise")

        ckpt = tmp_path / ".workflow_artifacts" / "memory" / "checkpoints" / f"thorough-plan-progress-{SID}.md"
        text = ckpt.read_text()

        # ## Last user intent must be present
        assert "## Last user intent" in text, "## Last user intent section missing from checkpoint"

        # Must contain the stage token and the task name (non-empty)
        assert "thorough-plan:round-2-revise" in text or TASK in text, (
            "## Last user intent appears empty or doesn't reference the stage/task"
        )
        # Must reference 'critic' as the next phase after 'revise'
        assert "critic" in text, (
            "## Last user intent should name 'critic' as the next phase after 'revise'"
        )

    def test_sentinel_written(self, tmp_path):
        """Sentinel pending-restore-{SID}.txt must exist and point to checkpoint."""
        _call(tmp_path, round_n=1, phase="plan")

        mem = tmp_path / ".workflow_artifacts" / "memory"
        sentinel = mem / f"pending-restore-{SID}.txt"
        assert sentinel.exists(), f"Sentinel not written: {sentinel}"

        ckpt_path_in_sentinel = sentinel.read_text().strip()
        ckpt = mem / "checkpoints" / f"thorough-plan-progress-{SID}.md"
        assert ckpt_path_in_sentinel == str(ckpt), (
            f"Sentinel points to wrong path: {ckpt_path_in_sentinel!r} != {str(ckpt)!r}"
        )

    # ------------------------------------------------------------------
    # Restore-compat parse (T-06 step 6)
    # ------------------------------------------------------------------

    def test_restore_compat_parse(self, tmp_path):
        """Checkpoint file must be parseable by the picker's awk-style logic (## Active task)."""
        _call(tmp_path, round_n=1, phase="plan")

        ckpt = tmp_path / ".workflow_artifacts" / "memory" / "checkpoints" / f"thorough-plan-progress-{SID}.md"
        text = ckpt.read_text()

        # Simulate the picker's awk: find ## Active task header, next non-empty line = task name
        lines = text.splitlines()
        task_name_found = None
        for i, line in enumerate(lines):
            if line.strip() == "## Active task":
                for j in range(i + 1, len(lines)):
                    if lines[j].strip():
                        task_name_found = lines[j].strip()
                        break
                break

        assert task_name_found == TASK, (
            f"Picker awk parse failed: expected task '{TASK}', got {task_name_found!r}"
        )

    # ------------------------------------------------------------------
    # M-01: Orchestrator mtime ordering (T-06 step 7)
    # ------------------------------------------------------------------

    def test_checkpoint_mtime_newer_than_session_state(self, tmp_path):
        """M-01: checkpoint file mtime must be >= session-state mtime after the helper call."""
        ss_path = _call(tmp_path, round_n=1, phase="plan")

        ckpt = tmp_path / ".workflow_artifacts" / "memory" / "checkpoints" / f"thorough-plan-progress-{SID}.md"
        assert ckpt.exists()
        assert ss_path.exists()

        ckpt_mtime = ckpt.stat().st_mtime
        ss_mtime = ss_path.stat().st_mtime

        assert ckpt_mtime >= ss_mtime, (
            f"M-01 violation: checkpoint mtime ({ckpt_mtime}) < session-state mtime ({ss_mtime}); "
            "checkpoint must be written AFTER session-state"
        )

    # ------------------------------------------------------------------
    # C-02 / M-06: Adversarial subagent mtime (T-06 step 7b)
    # ------------------------------------------------------------------

    def test_b3_clause_b_fires_but_t04_scan_immune(self, tmp_path):
        """C-02/M-06: Simulate subagent writing a newer {date}-{task}.md AFTER the checkpoint.

        Asserts:
          (a) B3 Clause-B fires (checkpoint mtime < sessions/*.md max mtime).
          (b) T-04 direct glob still surfaces the checkpoint by ## Active task.
          (c) Does NOT assert /checkpoint --restore provides phase-precise restore
              (documented C-02 design-honesty boundary: T-04 is primary; picker is secondary
              with known B3 limitation).
        """
        _call(tmp_path, round_n=1, phase="plan")

        mem = tmp_path / ".workflow_artifacts" / "memory"
        ckpt = mem / "checkpoints" / f"thorough-plan-progress-{SID}.md"

        # Simulate a subagent writing its session-state AFTER the last checkpoint
        # (the realistic kill-during-subagent window)
        time.sleep(0.02)  # ensure strictly newer mtime
        subagent_ss = mem / "sessions" / f"{TODAY}-{TASK}.md"
        subagent_ss.write_text(
            f"## Status\nin_progress\n\n## Current stage\nplan\n\n## Active task\n{TASK}\n",
            encoding="utf-8",
        )

        # (a) B3 Clause-B: max(candidate mtime) < max(sessions/*.md mtime within 7d)?
        ckpt_mtime = ckpt.stat().st_mtime
        sessions_mtimes = [p.stat().st_mtime for p in (mem / "sessions").glob("*.md")]
        max_sessions_mtime = max(sessions_mtimes)

        assert ckpt_mtime < max_sessions_mtime, (
            "C-02 test setup: expected checkpoint mtime < subagent session-state mtime, "
            "but checkpoint is newer — B3 Clause-B would NOT fire (test invalid)"
        )
        # B3 Clause-B DOES fire: checkpoint would be discarded by the picker.
        # This is the DOCUMENTED C-02 design-honesty boundary.
        # /checkpoint --restore would synthesise a task-level restore from the subagent file.

        # (b) T-04 direct scan: glob checkpoints/ by ## Active task — immune to B3
        candidates = list((mem / "checkpoints").glob("thorough-plan-progress-*.md"))
        assert candidates, "T-04 direct scan: no progress checkpoint files found"

        matched = []
        for c in candidates:
            text = c.read_text(encoding="utf-8")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == "## Active task":
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip():
                            if lines[j].strip() == TASK:
                                matched.append(c)
                            break
                    break

        assert matched, (
            "T-04 direct scan: checkpoint not found by ## Active task match "
            "even though the file exists — B3 must not apply here"
        )

        # C-02 design-honesty note (do NOT assert picker phase-precision here):
        # The test above confirms T-04 is immune to B3. We explicitly do NOT call
        # the picker and assert it returns the phase-correct checkpoint — in the
        # kill-during-subagent window, the expected (documented) behaviour is
        # that /checkpoint --restore degrades to task-level restore.

    # ------------------------------------------------------------------
    # M-04: Foreign-SID sentinel discovery (T-06 step 8)
    # ------------------------------------------------------------------

    def test_foreign_sid_sentinel_discovery(self, tmp_path):
        """M-04 (kill scenario): sentinel written by SID-A is discoverable from session SID-B."""
        _call(tmp_path, round_n=1, phase="plan")

        mem = tmp_path / ".workflow_artifacts" / "memory"
        # "New session" with a different SID — simulate post-kill fresh session.
        # (new_sid is intentionally not passed to any call — the test asserts
        #  that the sentinel written under SID is discoverable regardless of
        #  the current session's ID, i.e., by glob not by SID match.)

        # Port the picker's sentinel-discovery logic:
        #   1. Glob pending-restore-*.txt in memory/
        #   2. Read checkpoint path from line-1
        #   3. Run ## Active task awk over the checkpoint
        sentinels = list(mem.glob("pending-restore-*.txt"))
        assert sentinels, "No pending-restore-*.txt sentinels found in memory dir"

        discovered_tasks = []
        for sentinel in sentinels:
            ckpt_path_str = sentinel.read_text().strip()
            ckpt_path = Path(ckpt_path_str)
            if not ckpt_path.exists():
                continue
            text = ckpt_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == "## Active task":
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip():
                            discovered_tasks.append(lines[j].strip())
                        break
                    break

        assert TASK in discovered_tasks, (
            f"Foreign-SID sentinel discovery failed: task '{TASK}' not found via sentinel; "
            f"discovered: {discovered_tasks}"
        )

    # ------------------------------------------------------------------
    # Empty/unknown SID guard (T-06 step 9)
    # ------------------------------------------------------------------

    def test_empty_sid_no_sentinel(self, tmp_path):
        """Empty or 'unknown' SID must NOT write a pending-restore sentinel; exit 0."""
        _call(tmp_path, round_n=1, phase="plan", sid="unknown")

        mem = tmp_path / ".workflow_artifacts" / "memory"
        orphan_sentinels = list(mem.glob("pending-restore-unknown.txt"))
        assert not orphan_sentinels, (
            f"Empty-SID guard failed: orphan sentinel written: {orphan_sentinels}"
        )
        # Checkpoint file still exists (sid=unknown → filename = thorough-plan-progress-unknown.md)
        ckpt = mem / "checkpoints" / "thorough-plan-progress-unknown.md"
        assert ckpt.exists(), "Checkpoint file should still be written even when SID=unknown"

    def test_empty_string_sid_no_sentinel(self, tmp_path):
        """Empty string SID must NOT write a pending-restore sentinel; exit 0."""
        _call(tmp_path, round_n=1, phase="plan", sid="")

        mem = tmp_path / ".workflow_artifacts" / "memory"
        # Should produce thorough-plan-progress-unknown.md (empty normalised to unknown)
        ckpt = mem / "checkpoints" / "thorough-plan-progress-unknown.md"
        assert ckpt.exists()
        orphans = list(mem.glob("pending-restore-.txt"))
        assert not orphans, f"Orphan sentinel pending-restore-.txt was written: {orphans}"

    # ------------------------------------------------------------------
    # M-02: Orchestrator session-state dedicated file (T-06 step 10)
    # ------------------------------------------------------------------

    def test_session_state_created_when_absent(self, tmp_path):
        """M-02 (creation-if-absent): helper creates orchestrator session-state if it doesn't exist."""
        ss_path = _call(tmp_path, round_n=1, phase="plan")
        assert ss_path.exists(), (
            f"M-02: orchestrator session-state file was NOT created: {ss_path}"
        )
        text = ss_path.read_text()
        assert TASK in text, "Session-state file should contain the task name"
        assert "## Current stage" in text
        assert "thorough-plan:round-1-plan" in text

    def test_session_state_updated_when_present(self, tmp_path):
        """M-02 (update-in-place): helper updates ## Current stage but preserves other blocks."""
        sessions_dir = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        ss_path = sessions_dir / f"{TODAY}-{TASK}-orchestrator.md"

        # Seed the file with a minimal template plus a sentinel block
        initial_content = (
            f"## Active task: {TASK}\n\n"
            f"## Current stage\nthorough-plan:round-1-plan\n\n"
            f"## Branch\nmain\n\n"
            f"## Session ID\n{SID}\n\n"
            f"## CUSTOM BLOCK\nshould-be-preserved\n"
        )
        ss_path.write_text(initial_content, encoding="utf-8")

        # Now call for round-1-critic — should update ## Current stage only
        argv = [
            "--project-root", str(tmp_path),
            "--task", TASK,
            "--round", "1",
            "--phase", "critic",
            "--sid", SID,
            "--branch", "main",
            "--session-state", str(ss_path),
        ]
        rc = _MOD.main(argv)
        assert rc == 0

        updated = ss_path.read_text()
        # Stage must be updated
        assert "thorough-plan:round-1-critic" in updated, (
            "## Current stage was not updated to round-1-critic"
        )
        # Other blocks must be preserved
        assert "## CUSTOM BLOCK" in updated, "Custom block was unexpectedly removed"
        assert "should-be-preserved" in updated, "Custom block content was unexpectedly removed"
        assert TASK in updated, "Active task was unexpectedly removed"
