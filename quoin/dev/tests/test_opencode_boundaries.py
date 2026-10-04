"""Boundary listing, role rules and verification against a real git repository."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _opencode_gate_helpers as h  # noqa: E402

from quoin.opencode_adapter import boundaries, install  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TASK = "demo"
RUN = "20260929T000000Z-aaaaaaaa"
PRIOR = "20260928T000000Z-bbbbbbbb"
STORE = ".workflow_artifacts/memory/runtime/opencode/"
OWNED = (".opencode/commands/quoin-plan.md", ".opencode/agents/quoin-planner.md", ".opencode/opencode.jsonc")


def _install(root: Path) -> None:
    owned = {}
    for rel in OWNED:
        h.write(root / rel, "generated %s\n" % rel)
        owned[rel] = {"sha256": "0" * 64, "source_digest": "x", "kind": "command", "id": None}
    meta = install.Metadata(
        quoin_version="0", opencode_version="1.18.32", profile=None, owned=owned, created_dirs=[],
    )
    (root / ".quoin").mkdir(exist_ok=True)
    (root / install.METADATA_RELPATH).write_bytes(install.serialize_metadata(meta))
    h.write(root / ".quoin/runtime.json", "{}\n")


@pytest.fixture
def root(tmp_path, monkeypatch):
    h.isolate_git(monkeypatch, tmp_path / "home")
    project = h.make_repo(tmp_path / "proj")
    base = project / ".workflow_artifacts" / TASK
    h.write(base / "architecture.md", "arch\n")
    h.write(base / "stage-1" / "current-plan.md", "plan\n")
    h.write(project / ".workflow_artifacts" / "other" / "plan.md", "other\n")
    _install(project)
    return project


def listing(root, **kw):
    return boundaries.take_listing(root, clock=lambda: 1_700_000_000.0, **kw)


def run(role, root, change, **kw):
    before = listing(root)
    change(root)
    after = listing(root)
    return boundaries.verify(role, before, after, task=TASK, run_id=RUN, prior_run_id=PRIOR, **kw)


def task_file(rel, text="x\n"):
    return lambda root: h.write(root / ".workflow_artifacts" / TASK / rel, text)


def write_rel(rel, text="x\n"):
    return lambda root: h.write(root / rel, text)


def both(*fns):
    def go(root):
        for fn in fns:
            fn(root)
    return go


# --- listing scope ---


def test_listing_scope_and_kinds(root):
    h.write(root / ".workflow_artifacts/memory/recent-sessions.md", "x")
    h.write(root / ".workflow_artifacts/memory/continuation/demo.json", "{}")
    h.write(root / ".workflow_artifacts/memory/lessons-learned.md", "l")
    h.write(root / ".workflow_artifacts/cache/c.md", "c")
    h.write(root / ".workflow_artifacts/demo/cost-ledger.md", "ledger")
    h.write(root / ".workflow_artifacts/finalized/old/deep/file.md", "f")
    h.write(root / ".opencode/.gitignore", "x")
    h.write(root / ".opencode/package.json", "{}")
    h.write(root / ".opencode/node_modules/x/y.js", "x")
    h.write(root / ".opencode/agents/extra.md", "x")
    got = listing(root)
    keys = set(got.entries)
    assert ".workflow_artifacts/demo/stage-1/current-plan.md" in keys
    assert ".workflow_artifacts/memory/continuation/demo.json" in keys
    assert ".workflow_artifacts/memory/lessons-learned.md" in keys
    assert ".opencode/commands/quoin-plan.md" in keys and ".quoin/opencode-install.json" in keys
    assert ".workflow_artifacts/finalized/old" in keys
    assert got.entries[".workflow_artifacts/finalized/old"][0] == "d"
    for hidden in (
        ".workflow_artifacts/memory/recent-sessions.md", ".workflow_artifacts/cache/c.md",
        ".workflow_artifacts/demo/cost-ledger.md", ".workflow_artifacts/finalized/old/deep/file.md",
        ".opencode/.gitignore", ".opencode/package.json", ".opencode/node_modules/x/y.js", ".opencode/agents/extra.md",
    ):
        assert hidden not in keys
    assert got.error is None and not got.truncated and got.repos


def test_listing_records_symlinks_without_following(root):
    target = root / "elsewhere"
    target.mkdir()
    (target / "secret.md").write_text("s")
    link = root / ".workflow_artifacts" / "linked"
    link.symlink_to(target)
    got = listing(root)
    assert got.entries[".workflow_artifacts/linked"][0] == "l"
    assert not any("secret.md" in key for key in got.entries)


def test_missing_install_record_sets_an_error(root):
    (root / install.METADATA_RELPATH).unlink()
    assert listing(root).error == "install-record-missing"
    (root / install.METADATA_RELPATH).write_text("not json")
    assert listing(root).error == "install-record-unreadable"


def test_listing_cap_is_unverified(root):
    before = listing(root, max_entries=2)
    assert before.truncated
    after = listing(root)
    result = boundaries.verify("planner", before, after, task=TASK, run_id=RUN)
    assert result.status == "unverified" and result.reason == "listing-truncated"


def test_a_file_over_the_hash_cap_is_compared_by_size_not_unverified(root):
    capped = listing(root, max_hash_bytes=1)
    assert not capped.truncated
    assert any(entry[0] == "f" and entry[3] is None for entry in capped.entries.values())


def test_a_large_store_sidecar_does_not_make_the_listing_unverified(root):
    store = root / ".workflow_artifacts" / "memory" / "runtime" / "opencode"
    h.write(store / "r1.jsonl", "x" * 4096)
    h.write(store / "workflow-demo.json", "{}")
    before = listing(root, max_hash_bytes=1024)
    assert not before.truncated
    entries = before.entries
    assert entries[".workflow_artifacts/memory/runtime/opencode/r1.jsonl"][3] is None
    assert entries[".workflow_artifacts/memory/runtime/opencode/workflow-demo.json"][3] is not None
    after = listing(root, max_hash_bytes=1024)
    result = boundaries.verify("planner", before, after, task=TASK, run_id=RUN)
    assert result.reason != "listing-truncated"


def test_live_task_locks_name_live_processes_only(root):
    memory = root / ".workflow_artifacts" / "memory"
    h.write(memory / "run-supervisor-other.pid", json.dumps({"pid": os.getpid()}))
    h.write(memory / "run-supervisor-gone.pid", json.dumps({"pid": 2 ** 22 + 12345}))
    h.write(memory / "run-supervisor-bad.pid", "garbage")
    assert listing(root).live_task_locks == frozenset({"other"})


# --- role_key ---


def _record(phase, role=None, workspace=None):
    request = {"phase": phase}
    if workspace:
        request["workspace"] = workspace
    return {"request": request, "prepared": {"role": role} if role else {}}


@pytest.mark.parametrize("phase,role,workspace,expected", [
    ("discover", "investigator", None, "investigator"),
    ("architect", "architect", None, "architect"),
    ("plan", "planner", None, "planner"),
    ("thorough_plan", "coordinator", None, "planner"),
    ("thorough-plan", "coordinator", None, "planner"),
    ("implement", "implementer", None, "implementer"),
    ("critic", "critic", "/tmp/s", "critic-snapshot"),
    ("critic", "coordinator", None, "critic-real"),
    ("review", "reviewer", "/tmp/s", "reviewer-snapshot"),
    ("review", "coordinator", None, "reviewer-real"),
    ("gate", "gate", None, "gate"),
    ("checkpoint", "coordinator", None, "checkpoint"),
    ("continue_work", "coordinator", None, "continue_work"),
    ("end_of_task", "coordinator", None, "end_of_task"),
    ("mystery", "critic", "/tmp/s", "critic-snapshot"),
    ("mystery", "reviewer", None, "reviewer-real"),
    ("mystery", "weird", None, None),
])
def test_role_key(phase, role, workspace, expected):
    assert boundaries.role_key(_record(phase, role, workspace)) == expected


def test_role_key_of_an_empty_record_is_none():
    assert boundaries.role_key({}) is None


def test_window_exclusions_name_both_runs_and_the_pointer():
    got = boundaries.window_exclusions(TASK, RUN, PRIOR)
    assert STORE + RUN + ".jsonl" in got and STORE + PRIOR + ".checkpoint.json" in got
    assert STORE + "task-demo.json" in got
    assert STORE + RUN + ".run.json" in boundaries.window_exclusions(TASK, RUN)
    assert not any(PRIOR in p for p in boundaries.window_exclusions(TASK, RUN))


# --- planner / architect / investigator ---


def test_planner_writing_the_plan_and_session_state_is_ok(root):
    result = run("planner", root, both(
        task_file("stage-1/current-plan.md", "new\n"),
        write_rel(".workflow_artifacts/memory/sessions/s.md"),
    ))
    assert result.status == "ok", result


def test_planner_writing_runtime_state_is_a_violation(root):
    result = run("planner", root, write_rel(".workflow_artifacts/memory/runtime/opencode/workflow-demo.json"))
    assert result.status == "violation"
    assert result.violations[0]["path"] == ".workflow_artifacts/memory/runtime/opencode/workflow-demo.json"
    assert result.violations[0]["rule"] == "coordinator-owned"


def test_architect_editing_source_is_a_violation(root):
    result = run("architect", root, write_rel("src/app.py", "new = 1\n"))
    assert result.status == "violation"
    assert result.violations[0]["rule"] == "source-unchanged"


def test_architect_with_unverifiable_source_is_unverified(root):
    before = listing(root)
    after = listing(root)
    broken = [dict(after.repos[0], source_dirty=None)] + list(after.repos[1:])
    after = boundaries.Listing(
        entries=after.entries, repos=tuple(broken), live_task_locks=after.live_task_locks,
        taken_at=after.taken_at,
    )
    result = boundaries.verify("architect", before, after, task=TASK, run_id=RUN)
    assert result.status == "unverified" and result.reason == "source-unverifiable"


def test_gate_files_are_off_limits_to_task_writers(root):
    result = run("planner", root, task_file("gate-plan-2026.md"))
    assert result.status == "violation"


def test_investigator_may_write_the_discovery_map(root):
    assert run("investigator", root, write_rel(".workflow_artifacts/discovery-map.json", "{}")).status == "ok"
    assert run("architect", root, write_rel(".workflow_artifacts/discovery-map.json", "{\"a\": 1}")).status == "violation"


# --- implementer ---


def test_implementer_may_edit_source_and_the_task_folder(root):
    result = run("implementer", root, both(write_rel("src/app.py", "x = 2\n"), task_file("notes.md")))
    assert result.status == "ok", result


@pytest.mark.parametrize("change", [
    write_rel(".opencode/commands/quoin-plan.md", "tampered\n"),
    write_rel(".quoin/opencode-install.json", "{}"),
    write_rel(".workflow_artifacts/other/plan.md", "tampered\n"),
    write_rel(".workflow_artifacts/finalized/demo/x", "y"),
    write_rel(".workflow_artifacts/demo/finalized/x", "y"),
])
def test_implementer_forbidden_paths(root, change):
    assert run("implementer", root, change).status == "violation"


def test_opencode_managed_names_and_unowned_files_are_never_listed(root):
    def managed(r):
        h.write(r / ".opencode/.gitignore", "node_modules\n")
        h.write(r / ".opencode/package.json", "{}")
        h.write(r / ".opencode/bun.lock", "lock")
        h.write(r / ".opencode/node_modules/x/index.js", "x")
        h.write(r / ".opencode/agents/extra.md", "extra")
    for role in ("planner", "implementer", "reviewer-real", "critic-snapshot", "gate", "end_of_task"):
        result = run(role, root, managed)
        assert result.status == "ok", (role, result)


def test_files_other_writers_own_are_outside_the_listing(root):
    def other(r):
        h.write(r / ".workflow_artifacts/memory/recent-sessions.md", "x")
        h.write(r / ".workflow_artifacts/memory/run-state-demo.json", "{}")
        h.write(r / ".workflow_artifacts/memory/auto-resume-demo.txt", "x")
    assert run("planner", root, other).status == "ok"


def test_the_runs_own_store_files_the_prior_run_and_the_lock_are_excluded(root):
    def store(r):
        for name in (RUN + ".jsonl", RUN + ".run.json", PRIOR + ".run.json", "task-demo.json"):
            h.write(r / STORE / name, "{}")
        h.write(r / ".workflow_artifacts/memory/run-supervisor-demo.pid", "{}")
    assert run("planner", root, store).status == "ok"
    assert run("planner", root, write_rel(STORE + "cccccccc.run.json", "{}")).status == "violation"


# --- snapshot and real-tree critic / reviewer ---


@pytest.mark.parametrize("role", ["critic-snapshot", "reviewer-snapshot"])
@pytest.mark.parametrize("change", [
    task_file("stage-1/review-1.md"),
    write_rel("src/app.py", "x = 3\n"),
    write_rel(".quoin/runtime.json", "changed"),
])
def test_snapshot_roles_change_nothing_real(root, role, change):
    assert run(role, root, change).status == "violation"


def test_snapshot_role_with_no_change_is_ok(root):
    assert run("reviewer-snapshot", root, lambda r: None).status == "ok"


def test_real_tree_reviewer_may_add_only_a_numbered_review(root):
    assert run("reviewer-real", root, task_file("stage-1/review-2.md")).status == "ok"
    assert run("reviewer-real", root, task_file("stage-1/current-plan.md", "changed\n")).status == "violation"
    assert run("reviewer-real", root, task_file("stage-1/critic-response-1.md")).status == "violation"
    assert run("critic-real", root, task_file("critic-response-1.md")).status == "ok"
    assert run("critic-real", root, task_file("stage-1/review-1.md")).status == "violation"
    assert run("reviewer-real", root, task_file("stage-1/review-3.md"), stage_rel="stage-2").status == "violation"


# --- end of task ---


def _move(src, dst, *, extra=None):
    def go(root):
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / src), str(root / dst))
        if extra:
            extra(root)
    return go


def test_end_of_task_moves_the_folder_and_updates_lessons(root):
    lessons = write_rel(".workflow_artifacts/memory/lessons-learned.md", "lesson\n")
    result = run("end_of_task", root, _move(".workflow_artifacts/demo", ".workflow_artifacts/finalized/demo", extra=lessons))
    assert result.status == "ok", result


def test_end_of_task_moving_elsewhere_is_a_violation(root):
    result = run("end_of_task", root, _move(".workflow_artifacts/demo", ".workflow_artifacts/archive/demo"))
    assert result.status == "violation"


def test_end_of_task_deleting_the_folder_is_a_violation(root):
    result = run("end_of_task", root, lambda r: shutil.rmtree(r / ".workflow_artifacts" / TASK))
    assert result.status == "violation"


def test_end_of_task_sub_task_goes_to_the_parents_finalized(root):
    sub = root / ".workflow_artifacts" / "parent" / TASK
    h.write(sub / "current-plan.md", "p\n")
    result = run(
        "end_of_task", root, _move(".workflow_artifacts/parent/demo", ".workflow_artifacts/parent/finalized/demo"),
        parent="parent",
    )
    assert result.status == "ok", result


# --- checkpoint and gate ---


def test_checkpoint_writes_only_its_own_record(root):
    ok = write_rel(".workflow_artifacts/memory/continuation/demo.json", "{}")
    other = write_rel(".workflow_artifacts/memory/continuation/other.json", "{}")
    assert run("checkpoint", root, ok).status == "ok"
    assert run("checkpoint", root, other).status == "violation"


def test_gate_may_write_gate_files_and_its_workflow_record(root):
    both_ok = both(task_file("stage-1/gate-plan-2026.md"), write_rel(STORE + "workflow-demo.json", "{}"))
    assert run("gate", root, both_ok).status == "ok"
    assert run("gate", root, task_file("stage-1/current-plan.md", "changed\n")).status == "violation"


def test_continue_work_changes_nothing(root):
    assert run("continue_work", root, task_file("x.md")).status == "violation"


def test_unknown_role_is_unverified(root):
    result = boundaries.verify("nope", listing(root), listing(root), task=TASK, run_id=RUN)
    assert result.status == "unverified" and result.reason == "unknown-role"


# --- concurrency ---


def test_other_task_with_a_live_lock_is_unverified(root):
    h.write(root / ".workflow_artifacts/memory/run-supervisor-other.pid", json.dumps({"pid": os.getpid()}))
    result = run("planner", root, write_rel(".workflow_artifacts/other/plan.md", "changed\n"))
    assert result.status == "unverified" and result.reason == "concurrent-task-run"


def test_other_task_without_a_lock_depends_on_the_policy(root):
    change = write_rel(".workflow_artifacts/other/plan.md", "changed\n")
    assert run("planner", root, change).status == "violation"
    soft = run(
        "planner", root, write_rel(".workflow_artifacts/other/plan.md", "again\n"), other_task_policy="unverified",
    )
    assert soft.status == "unverified" and soft.reason == "concurrent-writer-unlocked"
    owned = run("planner", root, write_rel(".opencode/commands/quoin-plan.md", "t\n"), other_task_policy="unverified")
    assert owned.status == "violation"


def test_own_task_changes_are_never_downgraded(root):
    h.write(root / ".workflow_artifacts/memory/run-supervisor-other.pid", json.dumps({"pid": os.getpid()}))
    assert run("architect", root, write_rel("src/app.py", "x = 9\n"), other_task_policy="unverified").status == "violation"
    assert run("critic-snapshot", root, task_file("stage-1/current-plan.md", "c\n")).status == "violation"


# --- partial window and symlinks ---


def test_partial_window_is_never_ok(root):
    clean = run("planner", root, task_file("stage-1/current-plan.md", "new\n"), window_partial=True)
    assert clean.status == "unverified" and clean.reason == "boundary-window-partial"
    dirty = run("planner", root, write_rel(".opencode/commands/quoin-plan.md", "t\n"), window_partial=True)
    assert dirty.status == "violation"


@pytest.mark.parametrize("role", ["planner", "implementer", "architect", "end_of_task"])
def test_swapping_the_task_folder_for_a_symlink_is_a_violation(root, role):
    def swap(r):
        real = r / ".workflow_artifacts" / TASK
        moved = r.parent / "moved-task"
        shutil.move(str(real), str(moved))
        real.symlink_to(moved)
    result = run(role, root, swap)
    assert result.status == "violation"
    assert any(v["rule"] == "symlink" for v in result.violations)


def test_violations_are_capped_and_use_project_relative_paths(root):
    def many(r):
        for i in range(30):
            h.write(r / ".workflow_artifacts" / "other" / ("f%d.md" % i), "x")
    result = run("implementer", root, many)
    assert result.status == "violation" and len(result.violations) == 20
    assert all(not v["path"].startswith("/") for v in result.violations)


def test_missing_install_record_only_blocks_roles_that_need_it(root):
    (root / install.METADATA_RELPATH).unlink()
    assert run("planner", root, lambda r: None).status == "unverified"
    result = run(
        "end_of_task", root, _move(".workflow_artifacts/demo", ".workflow_artifacts/finalized/demo"),
    )
    assert result.status == "ok", result
