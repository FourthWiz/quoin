"""Tests for quoin/core/scripts/task_bookkeeping.py — task-folder bookkeeping
classifier: root resolution, thresholds, activity/fingerprint, classification
rules (single- and multi-stage), the PR probe, and the apply action.

All tests are deterministic: no live git, no live gh. The PR probe is either
disabled (--no-gh) or driven by a monkeypatched subprocess.run / shutil.which,
or an injected pr_lookup callable.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CORE_PATH = (
    Path(__file__).resolve().parents[2] / "core" / "scripts" / "task_bookkeeping.py"
)
_SPEC = _ilu.spec_from_file_location("_quoin_core_task_bookkeeping_test", _CORE_PATH)
_TB = _ilu.module_from_spec(_SPEC)
sys.modules["_quoin_core_task_bookkeeping_test"] = _TB
_SPEC.loader.exec_module(_TB)

scan_candidates = _TB.scan_candidates
activity = _TB.activity
is_stub = _TB.is_stub
not_a_task_shape = _TB.not_a_task_shape
multi_stage_shape = _TB.multi_stage_shape
load_eot = _TB.load_eot
SessionIndex = _TB.SessionIndex
EotState = _TB.EotState
parse_stage_ids = _TB.parse_stage_ids
classify = _TB.classify
apply_action = _TB.apply_action
main = _TB.main
PrInfo = _TB.PrInfo


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _mk(root: Path, task: str, files: dict = None, dirs: list = None) -> Path:
    task_dir = root / ".workflow_artifacts" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    for d in dirs or []:
        (task_dir / d).mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        p = task_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return task_dir


def _touch_all(task_dir: Path, epoch: float):
    for dirpath, dirnames, filenames in os.walk(str(task_dir)):
        for f in filenames:
            os.utime(os.path.join(dirpath, f), (epoch, epoch))
    os.utime(str(task_dir), (epoch, epoch))


NOW = time.mktime((2026, 9, 27, 12, 0, 0, 0, 0, -1))
DAY = 86400.0


# ---------------------------------------------------------------------------
# T-01: root resolution, thresholds, scan_candidates, activity
# ---------------------------------------------------------------------------

def test_resolve_root_cwd_hit(tmp_path, monkeypatch):
    (tmp_path / ".workflow_artifacts").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _TB._resolve_root(None) == tmp_path


def test_resolve_root_miss_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main(["classify", "--format", "json"])
    assert code == 2


def test_threshold_precedence_flag_over_env(monkeypatch):
    monkeypatch.setenv("QUOIN_CLEANUP_TASK_STALE_DAYS", "99")
    assert _TB._resolve_threshold("5", "QUOIN_CLEANUP_TASK_STALE_DAYS", 14) == 5


def test_threshold_env_over_default(monkeypatch):
    monkeypatch.setenv("QUOIN_CLEANUP_TASK_STALE_DAYS", "7")
    assert _TB._resolve_threshold(None, "QUOIN_CLEANUP_TASK_STALE_DAYS", 14) == 7


def test_threshold_bad_value_falls_back(monkeypatch, capsys):
    assert _TB._resolve_threshold("abc", "QUOIN_CLEANUP_TASK_STALE_DAYS", 14) == 14
    assert "invalid" in capsys.readouterr().err
    assert _TB._resolve_threshold("-3", "QUOIN_CLEANUP_TASK_STALE_DAYS", 14) == 14


def test_scan_candidates_excludes_reserved_names(tmp_path):
    wa = tmp_path / ".workflow_artifacts"
    for name in ("memory", "cache", "finalized", "security-review", "trash", "real-task"):
        (wa / name).mkdir(parents=True)
    (wa / "a-plain-file.md").write_text("x", encoding="utf-8")
    found = {p.name for p in scan_candidates(wa)}
    assert found == {"real-task"}


def test_activity_ignores_ds_store_and_nested_finalized(tmp_path):
    task_dir = _mk(tmp_path, "t1", files={"cost-ledger.md": "x"})
    (task_dir / ".DS_Store").write_text("junk", encoding="utf-8")
    (task_dir / "finalized").mkdir()
    (task_dir / "finalized" / "old.md").write_text("stale evidence", encoding="utf-8")
    _touch_all(task_dir, NOW - 100 * DAY)
    os.utime(str(task_dir / "cost-ledger.md"), (NOW - 1 * DAY, NOW - 1 * DAY))
    last, count = activity(task_dir)
    assert count == 1  # only cost-ledger.md counted
    assert abs(last - (NOW - 1 * DAY)) < 2


def test_activity_empty_folder_uses_dir_mtime(tmp_path):
    task_dir = _mk(tmp_path, "empty1")
    os.utime(str(task_dir), (NOW - 5 * DAY, NOW - 5 * DAY))
    last, count = activity(task_dir)
    assert count == 0
    assert abs(last - (NOW - 5 * DAY)) < 2


def test_wrapper_help_exits_0():
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "task_bookkeeping.py"
    proc = subprocess.run([sys.executable, str(wrapper), "--help"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# is_stub / not_a_task_shape / multi_stage_shape
# ---------------------------------------------------------------------------

def test_is_stub_ledger_only(tmp_path):
    task_dir = _mk(tmp_path, "t2", files={"cost-ledger.md": "x", "task-source.md": "y"})
    assert is_stub(task_dir) is True


def test_is_stub_false_with_other_file(tmp_path):
    task_dir = _mk(tmp_path, "t3", files={"cost-ledger.md": "x", "architecture.md": "y"})
    assert is_stub(task_dir) is False


def test_is_stub_empty_subdir_counts_as_stub(tmp_path):
    task_dir = _mk(tmp_path, "t4", files={"cost-ledger.md": "x"}, dirs=["stage-2"])
    assert is_stub(task_dir) is True


def test_not_a_task_program_md_only(tmp_path):
    task_dir = _mk(tmp_path, "prog", files={"program.md": "x", "spec.md": "y"})
    assert not_a_task_shape(task_dir) is True


def test_not_a_task_runs_dir_only(tmp_path):
    task_dir = _mk(tmp_path, "runs-only", dirs=["runs"])
    (task_dir / "runs" / "log.txt").write_text("x", encoding="utf-8")
    assert not_a_task_shape(task_dir) is True


def test_drive_conflict_name_not_a_task(tmp_path):
    wa = tmp_path / ".workflow_artifacts"
    conflict = wa / "my-task 2"
    conflict.mkdir(parents=True)
    (conflict / "current-plan.md").write_text("x", encoding="utf-8")
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = next(r for r in data["rows"] if r["task"] == "my-task 2")
    assert row["bucket"] == "not-a-task"


# ---------------------------------------------------------------------------
# T-02: single-stage classification, EOT evidence, not-a-task, empty
# ---------------------------------------------------------------------------

def test_empty_folder_old_is_abandoned(tmp_path):
    task_dir = _mk(tmp_path, "old-empty")
    os.utime(str(task_dir), (NOW - 20 * DAY, NOW - 20 * DAY))
    data = classify(tmp_path, now=NOW, stale_days=14, gh_enabled=False)
    row = data["rows"][0]
    assert row["task"] == "old-empty"
    assert row["bucket"] == "abandoned"


def test_empty_folder_fresh_is_in_progress(tmp_path):
    task_dir = _mk(tmp_path, "new-empty")
    os.utime(str(task_dir), (NOW - 1 * DAY, NOW - 1 * DAY))
    data = classify(tmp_path, now=NOW, stale_days=14, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "in-progress"


def test_review_gated_eot_complete_commit_hash_is_done(tmp_path):
    task_dir = _mk(tmp_path, "done1", files={
        "current-plan.md": "x",
        "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature", "commit_hash": "abc123"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "done"
    assert row["evidence"]


def test_review_gated_eot_complete_marker_is_done(tmp_path):
    task_dir = _mk(tmp_path, "done2", files={
        "current-plan.md": "x",
        "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature"}),
    })
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-done2.md").write_text(
        "## Status\ncompleted\nfinalized_by_end_of_task: true\n", encoding="utf-8"
    )
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = next(r for r in data["rows"] if r["task"] == "done2")
    assert row["bucket"] == "done"


def test_review_gated_no_eot_is_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "nearly1", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    assert row["next_commands"] == ["/end_of_task nearly1"]


def test_eot_abort_is_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "aborted1", files={
        "current-plan.md": "x",
        "eot-preflights.json": json.dumps({"commit_or_abort": "abort"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"


def test_eot_missing_commit_hash_no_marker_is_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "nomark1", files={
        "current-plan.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"


def test_eot_missing_archive_type_is_nearly_done_not_done(tmp_path):
    task_dir = _mk(tmp_path, "noat1", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"commit_hash": "abc"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"


def test_eot_archive_type_none_is_in_progress(tmp_path):
    task_dir = _mk(tmp_path, "morework1", files={
        "current-plan.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "none"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "in-progress"


def test_stage_scoped_root_preflight_ignored_for_single_stage(tmp_path):
    task_dir = _mk(tmp_path, "single1", files={
        "current-plan.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "none", "stage": "1"}),
    })
    _touch_all(task_dir, NOW - 40 * DAY)
    data = classify(tmp_path, now=NOW, idle_days=30, gh_enabled=False)
    row = data["rows"][0]
    # Ignored -> falls through to rule 8 (in-progress, prompt since idle > 30)
    assert row["bucket"] == "in-progress"
    assert row["prompt"] is True


def test_in_progress_idle_prompt_thresholds(tmp_path):
    old = _mk(tmp_path, "old-inprog", files={"current-plan.md": "x"})
    _touch_all(old, NOW - 31 * DAY)
    fresh = _mk(tmp_path, "fresh-inprog", files={"current-plan.md": "x"})
    _touch_all(fresh, NOW - 29 * DAY)
    data = classify(tmp_path, now=NOW, idle_days=30, gh_enabled=False)
    by_name = {r["task"]: r for r in data["rows"]}
    assert by_name["old-inprog"]["prompt"] is True
    assert by_name["fresh-inprog"]["prompt"] is False


def test_not_a_task_program_md_and_spec_only_via_classify(tmp_path):
    _mk(tmp_path, "opencode-adapter-program", files={"program.md": "x", "spec.md": "y"})
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "not-a-task"


def test_dotquoinnotatask_marker_silenced(tmp_path):
    _mk(tmp_path, "silenced1", files={".quoin-not-a-task": "x", "current-plan.md": "y"})
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    assert data["rows"] == []
    assert data["silenced"] == ["silenced1"]


def test_evidence_non_empty_on_every_row(tmp_path):
    _mk(tmp_path, "a", files={"current-plan.md": "x"})
    _mk(tmp_path, "b")
    _mk(tmp_path, "c", files={"program.md": "x"})
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    for row in data["rows"]:
        assert row["evidence"], row["task"]


# ---------------------------------------------------------------------------
# T-03: multi-stage classification
# ---------------------------------------------------------------------------

_ARCH_ALL_DONE = """## Stage decomposition

1. ✅ S-1: First stage
2. ✅ S-2: Second stage
"""

_ARCH_UNSTARTED = """## Stage decomposition

1. ✅ S-1: First stage
2. S-2: Second stage (not started)
3. S-3: Third stage
"""

_ARCH_S0 = """## Stage decomposition

1. ✅ S-0: Zeroth stage
2. ✅ S-1: First stage
"""

_ARCH_LIST_MISMATCH = """## Stage decomposition

3. ✓ S-5: out of order stage
"""

_ARCH_EM_DASH_BOLD_COLON = """## Stage decomposition

1. **S-1 — First stage**: some note
2. ✅ S-2: colon heading
"""

_ARCH_SUBLIST_ELSEWHERE = """## Stage decomposition

1. ✅ S-1: First stage

## Some other section

1. S-9: this should not count
"""

_ARCH_NO_STAGE_ROWS = """## Stage decomposition

Single stage — no stage subfolders.
"""


def test_multi_stage_all_finalized_is_done(tmp_path):
    task_dir = _mk(tmp_path, "ms-done", files={"architecture.md": _ARCH_ALL_DONE}, dirs=["finalized"])
    (task_dir / "finalized" / "stage-1").mkdir()
    (task_dir / "finalized" / "stage-2").mkdir()
    (task_dir / "finalized" / "stage-1" / "review-1.md").write_text("x", encoding="utf-8")
    (task_dir / "finalized" / "stage-2" / "review-1.md").write_text("x", encoding="utf-8")
    _touch_all(task_dir, NOW - 3 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "done"


def test_multi_stage_unstarted_is_in_progress(tmp_path):
    task_dir = _mk(tmp_path, "ms-unstarted", files={"architecture.md": _ARCH_UNSTARTED},
                   dirs=["finalized", "stage-2"])
    (task_dir / "finalized" / "stage-1").mkdir()
    (task_dir / "stage-2" / "current-plan.md").write_text("x", encoding="utf-8")
    _touch_all(task_dir, NOW - 3 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "in-progress"
    assert any("unstarted" in e for e in row["evidence"])


def test_multi_stage_live_review_gated_is_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "ms-nearly", files={"architecture.md": _ARCH_ALL_DONE},
                   dirs=["finalized", "stage-2"])
    (task_dir / "finalized" / "stage-1").mkdir()
    (task_dir / "stage-2" / "current-plan.md").write_text("x", encoding="utf-8")
    (task_dir / "stage-2" / "gate-post-review-2026-09-01.md").write_text("x", encoding="utf-8")
    _touch_all(task_dir, NOW - 3 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    assert row["next_commands"] == ["/end_of_task stage 2 of ms-nearly"]


def test_stage_0_row_with_stage0_dir(tmp_path):
    task_dir = _mk(tmp_path, "ms-s0", files={"architecture.md": _ARCH_S0}, dirs=["stage-0"])
    (task_dir / "stage-0" / "current-plan.md").write_text("x", encoding="utf-8")
    _touch_all(task_dir, NOW - 3 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    # S = {0, 1}, F = {}, C = {0, 1}, L = {0}. S <= (F|C|L) and L non-empty.
    assert row["bucket"] in ("nearly-done", "in-progress")  # phase-dependent; not a crash


def test_list_number_mismatch_keys_on_s_token(tmp_path):
    ids, completed = parse_stage_ids(_ARCH_LIST_MISMATCH)
    assert ids == {5}
    assert completed == {5}


def test_em_dash_bold_colon_headings_parsed(tmp_path):
    ids, completed = parse_stage_ids(_ARCH_EM_DASH_BOLD_COLON)
    assert ids == {1, 2}
    assert completed == {2}  # only S-2 row has a checkmark


def test_sublist_in_another_section_not_counted(tmp_path):
    ids, completed = parse_stage_ids(_ARCH_SUBLIST_ELSEWHERE)
    assert ids == {1}
    assert 9 not in ids


def test_no_stage_rows_heading_alone_not_multi_stage_shaped(tmp_path):
    task_dir = _mk(tmp_path, "ms-heading-only", files={
        "architecture.md": _ARCH_NO_STAGE_ROWS, "current-plan.md": "x",
    })
    assert multi_stage_shape(task_dir) is False


def test_stage_scoped_more_work_preflight_in_progress(tmp_path):
    task_dir = _mk(tmp_path, "ms-morework", files={
        "architecture.md": _ARCH_ALL_DONE,
        "eot-preflights.json": json.dumps({"archive_type": "none", "stage": "stage-2"}),
    }, dirs=["finalized", "stage-2"])
    (task_dir / "finalized" / "stage-1").mkdir()
    (task_dir / "stage-2" / "current-plan.md").write_text("x", encoding="utf-8")
    (task_dir / "stage-2" / "gate-post-review-2026-09-01.md").write_text("x", encoding="utf-8")
    _touch_all(task_dir, NOW - 3 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "in-progress"


# ---------------------------------------------------------------------------
# T-04: PR probe
# ---------------------------------------------------------------------------

def test_gh_missing_no_row_change(tmp_path, monkeypatch):
    task_dir = _mk(tmp_path, "prtest1", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature", "commit_hash": "abc"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    monkeypatch.setattr(_TB.shutil, "which", lambda name: None)
    data_gh = classify(tmp_path, now=NOW, gh_enabled=True)
    data_nogh = classify(tmp_path, now=NOW, gh_enabled=False)
    assert data_gh["gh"]["status"] == "unavailable"
    row_gh = data_gh["rows"][0]
    row_nogh = data_nogh["rows"][0]
    assert row_gh["bucket"] == row_nogh["bucket"] == "done"


def test_gh_auth_failure_no_row_change(tmp_path, monkeypatch):
    task_dir = _mk(tmp_path, "prtest2", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature", "commit_hash": "abc"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    monkeypatch.setattr(_TB.shutil, "which", lambda name: "/usr/bin/gh")

    def _fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
        return R()

    monkeypatch.setattr(_TB.subprocess, "run", _fake_run)
    data = classify(tmp_path, now=NOW, gh_enabled=True)
    assert data["gh"]["status"] == "unavailable"
    assert data["rows"][0]["bucket"] == "done"


def test_gh_timeout_no_row_change(tmp_path, monkeypatch):
    task_dir = _mk(tmp_path, "prtest3", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature", "commit_hash": "abc"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)
    monkeypatch.setattr(_TB.shutil, "which", lambda name: "/usr/bin/gh")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(_TB.subprocess, "run", _fake_run)
    data = classify(tmp_path, now=NOW, gh_enabled=True)
    assert data["gh"]["status"] == "unavailable"
    assert data["rows"][0]["bucket"] == "done"


def test_injected_pr_lookup_done_plus_open_becomes_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "prtest4", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
        "eot-preflights.json": json.dumps({"archive_type": "feature", "commit_hash": "abc", "branch": "feat/x"}),
    })
    _touch_all(task_dir, NOW - 2 * DAY)

    def lookup(task, branch_keys, commit_hash, root):
        return PrInfo(state="open", number=1, repo="quoin")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    assert "/pr" in row["next_commands"]


def test_injected_pr_lookup_merged_no_eot_becomes_nearly_done(tmp_path):
    task_dir = _mk(tmp_path, "prtest5", files={"current-plan.md": "x"})
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-prtest5.md").write_text("branch: feat/prtest5\n", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)

    def lookup(task, branch_keys, commit_hash, root):
        assert branch_keys == ["feat/prtest5"]
        return PrInfo(state="merged", number=2, repo="quoin")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    assert f"/end_of_task {row['task']}" in row["next_commands"]


def test_merged_pr_no_eot_never_yields_done(tmp_path):
    task_dir = _mk(tmp_path, "prtest5b", files={"current-plan.md": "x"})
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-prtest5b.md").write_text("branch: feat/prtest5b\n", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)

    def lookup(task, branch_keys, commit_hash, root):
        return PrInfo(state="merged")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    assert data["rows"][0]["bucket"] != "done"


def test_saturated_bulk_list_yields_unknown_not_none(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    probe = _TB.GhProbe(repo_dirs=[str(repo)])
    probe._status, probe._detail = "ok", ""
    # 300 rows, none of which match the requested branch key -> saturated no-match.
    probe._bulk_cache[str(repo)] = [
        {"number": i, "state": "OPEN", "headRefName": f"other-{i}"} for i in range(300)
    ]
    info = probe("some-task", ["feat/does-not-exist"], None, repo)
    assert info.state == "unknown"


def test_no_key_row_yields_unknown_pr_state(tmp_path):
    # marker-only EOT (no branch, no commit_hash) -> nothing to look up
    task_dir = _mk(tmp_path, "nokey1", files={"current-plan.md": "x"})
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-nokey1.md").write_text("finalized_by_end_of_task: true\n", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)

    called = {"n": 0}

    def lookup(task, branch_keys, commit_hash, root):
        called["n"] += 1
        return PrInfo(state="open")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    row = data["rows"][0]
    assert row["pr"]["state"] == "unknown"
    assert called["n"] == 0


def test_preflight_branch_preferred_over_session_branch(tmp_path):
    task_dir = _mk(tmp_path, "branchpref1", files={
        "current-plan.md": "x",
        "eot-preflights.json": json.dumps({"branch": "feat/from-preflight"}),
    })
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-branchpref1.md").write_text("branch: feat/from-session\n", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)

    seen = {}

    def lookup(task, branch_keys, commit_hash, root):
        seen["keys"] = list(branch_keys)
        return PrInfo(state="unknown")

    classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    assert seen["keys"][0] == "feat/from-preflight"


def test_multi_stage_row_ignores_probe(tmp_path):
    task_dir = _mk(tmp_path, "ms-noprobe", files={"architecture.md": _ARCH_ALL_DONE}, dirs=["finalized"])
    (task_dir / "finalized" / "stage-1").mkdir()
    (task_dir / "finalized" / "stage-2").mkdir()
    _touch_all(task_dir, NOW - 3 * DAY)

    called = {"n": 0}

    def lookup(task, branch_keys, commit_hash, root):
        called["n"] += 1
        return PrInfo(state="merged")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    assert called["n"] == 0
    assert data["rows"][0]["bucket"] == "done"


# ---------------------------------------------------------------------------
# T-05: options matrix, JSON/table output
# ---------------------------------------------------------------------------

def test_json_output_deterministic_across_two_runs(tmp_path):
    _mk(tmp_path, "det1", files={"current-plan.md": "x"})
    _mk(tmp_path, "det2", files={"cost-ledger.md": "x"})
    d1 = classify(tmp_path, now=NOW, gh_enabled=False)
    d2 = classify(tmp_path, now=NOW, gh_enabled=False)
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)


def test_table_contains_all_five_column_headers(tmp_path):
    _mk(tmp_path, "tblrow", files={"current-plan.md": "x"})
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    table = _TB._render_table(data)
    for col in ("task", "bucket", "evidence", "last activity", "recommended action"):
        assert col in table


def test_nearly_done_with_nonstub_twin_loses_archive_anyway(tmp_path):
    task_dir = _mk(tmp_path, "twin1", files={
        "current-plan.md": "x", "gate-post-review-2026-09-01.md": "x",
    })
    twin = tmp_path / ".workflow_artifacts" / "finalized" / "twin1"
    twin.mkdir(parents=True)
    (twin / "runs").mkdir()
    (twin / "runs" / "log.txt").write_text("real content", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    assert row["archive_blocked"] is True
    assert not any(o.lower().startswith("archive") for o in row["options"])
    assert row["recommended_action"] == "resolve-manually"


def test_not_a_task_keeps_skip_with_twin(tmp_path):
    task_dir = _mk(tmp_path, "quoin-benchmarks", dirs=["runs"])
    (task_dir / "runs" / "log.txt").write_text("x", encoding="utf-8")
    twin = tmp_path / ".workflow_artifacts" / "finalized" / "quoin-benchmarks"
    twin.mkdir(parents=True)
    (twin / "runs").mkdir()
    (twin / "runs" / "log.txt").write_text("real content", encoding="utf-8")
    data = classify(tmp_path, now=NOW, gh_enabled=False)
    row = data["rows"][0]
    assert row["bucket"] == "not-a-task"
    assert row["recommended_action"] == "skip"


def test_classify_is_read_only(tmp_path):
    task_dir = _mk(tmp_path, "readonly1", files={"current-plan.md": "x"})
    _touch_all(task_dir, NOW - 2 * DAY)

    def _hash_tree(root):
        import hashlib
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in sorted(os.walk(str(root))):
            for f in sorted(filenames):
                p = Path(dirpath) / f
                h.update(str(p.relative_to(root)).encode())
                h.update(p.read_bytes())
                h.update(str(p.stat().st_mtime).encode())
        return h.hexdigest()

    before = _hash_tree(tmp_path / ".workflow_artifacts")
    classify(tmp_path, now=NOW, gh_enabled=False)
    after = _hash_tree(tmp_path / ".workflow_artifacts")
    assert before == after


# ---------------------------------------------------------------------------
# T-06: apply
# ---------------------------------------------------------------------------

def test_apply_archive_success(tmp_path):
    task_dir = _mk(tmp_path, "arch1", files={"current-plan.md": "x"})
    result = apply_action(tmp_path, "archive", "arch1")
    assert result["ok"] is True
    assert not task_dir.exists()
    assert (tmp_path / ".workflow_artifacts" / "finalized" / "arch1").is_dir()


def test_apply_archive_onto_existing_target_refused(tmp_path):
    _mk(tmp_path, "arch2", files={"current-plan.md": "x"})
    existing = tmp_path / ".workflow_artifacts" / "finalized" / "arch2"
    existing.mkdir(parents=True)
    (existing / "old.md").write_text("keep me", encoding="utf-8")
    result = apply_action(tmp_path, "archive", "arch2")
    assert result["ok"] is False
    assert result["exit"] == 4
    assert (existing / "old.md").read_text(encoding="utf-8") == "keep me"
    assert (tmp_path / ".workflow_artifacts" / "arch2").is_dir()


def test_apply_trash_success_and_collision_suffix(tmp_path):
    today_str = _TB.datetime.date.today().isoformat()
    _mk(tmp_path, "trashme", files={"current-plan.md": "x"})
    r1 = apply_action(tmp_path, "trash", "trashme")
    assert r1["ok"] is True
    assert today_str in r1["to"]

    _mk(tmp_path, "trashme2", files={"current-plan.md": "x"})
    # Force a name collision by pre-creating the target dir.
    collide_dir = tmp_path / ".workflow_artifacts" / "trash" / today_str / "trashme2"
    collide_dir.mkdir(parents=True)
    r2 = apply_action(tmp_path, "trash", "trashme2")
    assert r2["ok"] is True
    assert r2["to"].endswith("trashme2-2")


def test_apply_name_validation_rejects_bad_names(tmp_path):
    for bad in ("../x", "a/b", "memory", "trash", "does-not-exist"):
        result = apply_action(tmp_path, "archive", bad)
        assert result["ok"] is False


def test_apply_name_validation_rejects_symlink(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / ".workflow_artifacts"
    link.mkdir()
    (link / "linked-task").symlink_to(real, target_is_directory=True)
    result = apply_action(tmp_path, "archive", "linked-task")
    assert result["ok"] is False


def test_apply_not_a_task_refused(tmp_path):
    _mk(tmp_path, "nota1", files={"program.md": "x"})
    result = apply_action(tmp_path, "archive", "nota1")
    assert result["ok"] is False
    assert result["exit"] == 3


def test_apply_not_a_task_marker_refused(tmp_path):
    _mk(tmp_path, "silencedapply", files={".quoin-not-a-task": "x", "current-plan.md": "y"})
    result = apply_action(tmp_path, "archive", "silencedapply")
    assert result["ok"] is False
    assert result["exit"] == 3


def test_apply_expect_mismatch_refused(tmp_path):
    task_dir = _mk(tmp_path, "fp1", files={"current-plan.md": "x"})
    result = apply_action(tmp_path, "archive", "fp1", expect="0:99999")
    assert result["ok"] is False
    assert task_dir.exists()


def test_apply_merged_pr_archive_anyway_succeeds(tmp_path):
    task_dir = _mk(tmp_path, "mergedok", files={"current-plan.md": "x"})
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-09-27-mergedok.md").write_text("branch: feat/mergedok\n", encoding="utf-8")
    _touch_all(task_dir, NOW - 2 * DAY)

    def lookup(task, branch_keys, commit_hash, root):
        return PrInfo(state="merged")

    data = classify(tmp_path, now=NOW, gh_enabled=True, pr_lookup=lookup)
    row = data["rows"][0]
    assert row["bucket"] == "nearly-done"
    result = apply_action(tmp_path, "archive", "mergedok", expect=row["fingerprint"])
    assert result["ok"] is True


def test_apply_dry_run_leaves_tree_unchanged(tmp_path):
    task_dir = _mk(tmp_path, "dryrun1", files={"current-plan.md": "x"})
    result = apply_action(tmp_path, "archive", "dryrun1", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert task_dir.exists()
    assert not (tmp_path / ".workflow_artifacts" / "finalized" / "dryrun1").exists()


# ---------------------------------------------------------------------------
# T-08 (script-side pin): trash excluded from scan_candidates already covered
# above in test_scan_candidates_excludes_reserved_names.
# ---------------------------------------------------------------------------
