"""Workflow evidence snapshots, comparison and recording (real git)."""
from __future__ import annotations

import shutil

import pytest

import _opencode_gate_helpers as h
from quoin.opencode_adapter import evidence, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TASK = "t1"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    h.isolate_git(monkeypatch, tmp_path / "home")
    root = h.make_repo(tmp_path / "proj")
    task = root / ".workflow_artifacts" / TASK
    (task / "stage-1").mkdir(parents=True)
    (task / "stage-1" / "current-plan.md").write_text("plan\n")
    (task / "cost-ledger.md").write_text("# ledger\n")
    return root


def snap(root, phase="plan", **kw):
    return evidence.take_snapshot(root, TASK, phase, **kw)


def codes(findings):
    return sorted(f.code for f in findings)


def cmp_(root, before, **kw):
    return evidence.compare(before, snap(root, **kw))


@pytest.mark.parametrize("rel,expected", [
    (".workflow_artifacts/t1/stage-1/current-plan.md", True),
    (".workflow_artifacts/t1/architecture.md", True),
    (".workflow_artifacts/t1/cost-ledger.md", False),
    (".workflow_artifacts/t1/stage-1/cost-ledger.md", False),
    (".workflow_artifacts/t1/stage-1/gate-plan-2026-01-01.md", False),
    (".workflow_artifacts/t1/x.md.tmp", False),
    (".workflow_artifacts/t2/current-plan.md", False),
    (".workflow_artifacts/t1", False),
    ("src/x.py", False),
])
def test_in_scope(rel, expected):
    assert evidence.in_scope(rel, TASK) is expected


def test_unchanged_tree_has_no_findings(project):
    before = snap(project)
    assert before["coverage"] == "full" and before["scope_version"] == 1
    assert cmp_(project, before) == ([], [])
    assert all("dirty" not in r for r in before["repos"])


def test_ledger_growth_and_gate_files_are_not_evidence(project):
    before = snap(project)
    with open(project / ".workflow_artifacts" / TASK / "cost-ledger.md", "a") as handle:
        handle.write("row\n")
    (project / ".workflow_artifacts" / TASK / "stage-1" / "gate-plan-2026-01-01.md").write_text("gate\n")
    (project / ".workflow_artifacts" / TASK / "stage-1" / "x.tmp").write_text("t\n")
    assert cmp_(project, before) == ([], [])


def test_new_artifact_is_added_and_source_state_is_unchanged(project):
    before = snap(project)
    (project / ".workflow_artifacts" / TASK / "stage-1" / "review-1.md").write_text("r\n")
    tasks, repos = cmp_(project, before)
    assert [(f.code, f.detail) for f in tasks] == [
        ("artifact-added", ".workflow_artifacts/t1/stage-1/review-1.md")]
    assert repos == []


def test_removed_and_changed(project):
    before = snap(project)
    plan = project / ".workflow_artifacts" / TASK / "stage-1" / "current-plan.md"
    plan.write_text("changed\n")
    assert codes(cmp_(project, before)[0]) == ["input-hash-changed"]
    plan.unlink()
    assert codes(cmp_(project, before)[0]) == ["artifact-removed"]


def test_head_changed(project):
    before = snap(project)
    (project / "src" / "x.py").write_text("x = 2\n")
    h.git(project, "commit", "-q", "-am", "next")
    assert codes(cmp_(project, before)[1]) == ["repo-head-changed"]


def test_dirty_changed(project):
    before = snap(project)
    (project / "src" / "x.py").write_text("x = 2\n")
    assert codes(cmp_(project, before)[1]) == ["repo-dirty-changed"]


def test_content_changed_when_dirty_stays_dirty(project):
    (project / "src" / "x.py").write_text("x = 2\n")
    before = snap(project)
    (project / "src" / "x.py").write_text("x = 3\n")
    assert codes(cmp_(project, before)[1]) == ["repo-content-changed"]
    assert cmp_(project, snap(project)) == ([], [])


def test_unverifiable_digest(project, monkeypatch):
    (project / "src" / "x.py").write_text("x = 2\n")
    h.patch_runstore(monkeypatch, max_source_bytes=1)
    before = snap(project)
    assert before["repos"][0]["source_error"] == "too-large"
    assert codes(cmp_(project, before)[1]) == ["repo-content-unverifiable"]


def test_current_side_error_is_unverifiable_not_missing(project):
    before = snap(project)
    current = snap(project)
    current["repos"][0].update(error="budget", head=None)
    assert codes(evidence.compare(before, current)[1]) == ["repo-content-unverifiable"]


def test_recorded_error_is_incomplete(project):
    before = snap(project)
    before["repos"][0].update(error="budget", head=None)
    assert codes(cmp_(project, before)[1]) == ["evidence-incomplete"]


def test_repo_missing_and_added(project):
    h.make_repo(project / "svc", {"s.txt": "s\n"})
    before = snap(project)
    assert {r["path"] for r in before["repos"]} == {".", "svc"}
    shutil.rmtree(project / "svc")
    assert codes(cmp_(project, before)[1]) == ["repo-missing"]
    base = snap(project)
    h.make_repo(project / "svc", {"s.txt": "s\n"})
    assert codes(cmp_(project, base)[1]) == ["repo-added"]


def test_truncated_coverage(project, monkeypatch):
    (project / ".workflow_artifacts" / TASK / "extra.md").write_text("e\n")
    h.patch_runstore(monkeypatch, max_files=1)
    before = snap(project)
    assert before["coverage"] == "truncated"
    assert codes(cmp_(project, before)[0]) == ["evidence-incomplete"]


def test_malformed_snapshot_is_incomplete(project):
    current = snap(project)
    assert codes(evidence.compare({}, current)[0]) == ["evidence-incomplete"]
    assert codes(evidence.compare(current, None)[1]) == ["evidence-incomplete"]


def test_discover_snapshot_covers_the_discover_files(project):
    memory = project / ".workflow_artifacts" / "memory"
    memory.mkdir()
    for name in ("repos-inventory.md", "architecture-overview.md", "dependencies-map.md", "other.md"):
        (memory / name).write_text(name + "\n")
    before = snap(project, "discover")
    keys = set(before["task_hashes"])
    assert set(evidence.DISCOVER_FILES) <= keys
    assert ".workflow_artifacts/memory/other.md" not in keys
    assert not (set(evidence.DISCOVER_FILES) & set(snap(project, "plan")["task_hashes"]))
    (memory / "repos-inventory.md").write_text("changed\n")
    assert codes(cmp_(project, before, phase="discover")[0]) == ["input-hash-changed"]


def test_symlink_in_task_folder_is_skipped(project, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("o\n")
    (project / ".workflow_artifacts" / TASK / "link.md").symlink_to(outside)
    assert ".workflow_artifacts/t1/link.md" not in snap(project)["task_hashes"]


def test_record_evidence(project):
    state_clock = h.clock_at(0)
    entry = evidence.record_evidence(
        project, TASK, 1, "plan", "adopted", snap(project), runs=["oc-1"], clock=state_clock,
        critic_responses=["a.md"],
    )
    assert entry["origin"] == "adopted" and entry["stage"] == 1
    assert entry["recorded_at"] == "1970-01-01T00:00:00Z" and entry["runs"] == ["oc-1"]
    assert entry["critic_responses"] == ["a.md"] and entry["evidence"]["coverage"] == "full"
    loaded = runstore.load_workflow_state(runstore.store_dir(project), TASK)
    assert runstore.current_entry(loaded, 1, "plan")["origin"] == "adopted"
    again = evidence.record_evidence(project, TASK, 1, "plan", "coordinator", snap(project), clock=state_clock)
    loaded = runstore.load_workflow_state(runstore.store_dir(project), TASK)
    assert len(loaded["entries"]) == 2 and loaded["entries"][0]["superseded"] is True
    assert runstore.current_entry(loaded, 1, "plan")["origin"] == again["origin"]
