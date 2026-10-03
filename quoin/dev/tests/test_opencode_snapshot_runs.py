"""Isolated critic and review runs end to end: the real driver launches the fake
executable inside a snapshot, and the real tree must stay untouched."""
from __future__ import annotations

import os
import shutil

import pytest

import _opencode_boundary_helpers as bh
import _opencode_gate_helpers as gh
from quoin.opencode_adapter import snapshot

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

CRITIC_NAME = "critic-response-1.md"


def build(tmp_path, monkeypatch, scenario):
    proj = bh.SnapshotProject(tmp_path, monkeypatch, scenario)
    proj.spy_on_remove(monkeypatch)
    return proj


def escape(kind, **kw):
    import _opencode_driver_helpers as dh

    if kind == "critic":
        kw.setdefault("name", CRITIC_NAME)
        kw.setdefault("body", dh.fake.CRITIC_PASS_TEXT)
    return ("boundary_escape", kw)


def written(effects, path):
    return path in effects


@pytest.mark.parametrize("kind", ["review", "critic"])
def test_a_contained_run_changes_nothing_outside_and_its_finding_is_harvested(tmp_path, monkeypatch, kind):
    proj = build(tmp_path, monkeypatch, escape(kind))
    before = proj.tree_state()
    run = proj.run_isolated(kind)
    try:
        assert run.result.outcome == "COMPLETED", run.result
        assert run.boundary.status == "ok", run.boundary
        # the run started in the snapshot, never in the project
        cwds = {os.path.realpath(i["cwd"]) for i in proj.invocations() if i.get("attempt") is not None}
        assert cwds == {os.path.realpath(str(run.snapshot_root))}
        assert os.path.realpath(str(proj.root)) not in cwds
        # the real tree is unchanged apart from the harvested finding
        after = proj.tree_state()
        harvested = run.harvest.path_rel
        gained = set(after["task"]) - set(before["task"])
        assert gained == {harvested}
        assert {k: v for k, v in after["task"].items() if k != harvested} == before["task"]
        assert after["repos"] == before["repos"] and after["src"] == before["src"]
        assert before["src"]["src/app.py"] == b"x = 1\n"
        # writes outside the outbox either failed or landed only in the snapshot
        state = proj.removed_state
        effects = "\n".join(state["effects"])
        assert "denied src/app.py" in effects
        for stray in ("src/shell.txt", "src/spawned.txt"):
            assert not (proj.root / stray).exists()
            assert stray not in state["files"]
        # the finding is the next numbered file in the real stage folder and validates
        prefix = "review-" if kind == "review" else "critic-response-"
        assert run.harvest.error is None and run.harvest.number == 1
        assert harvested.endswith("stage-1/%s1.md" % prefix)
        assert proj.finding_names(prefix) == ["%s1.md" % prefix]
        assert run.harvest.ignored_tmp == (("review-1.md.tmp" if kind == "review" else CRITIC_NAME + ".tmp"),)
        assert not any(name.endswith(".tmp") for name in os.listdir(proj.root / ".workflow_artifacts/demo/stage-1"))
        assert run.removed is True and not run.snapshot_root.exists()
    finally:
        proj.cleanup()


def test_managed_launch_writes_are_not_a_violation(tmp_path, monkeypatch):
    import _opencode_driver_helpers as dh

    scenario = ("opencode_managed_writes", {
        "extra": [(".workflow_artifacts/demo/stage-1/review-1.md", dh.fake.REVIEW_APPROVED_TEXT)],
    })
    proj = build(tmp_path, monkeypatch, scenario)
    try:
        run = proj.run_isolated("review")
        assert run.boundary.status == "ok", run.boundary
        effects = proj.removed_state["effects"]
        assert not [line for line in effects if line.startswith("denied")], effects
        for name in (".opencode/.gitignore", ".opencode/package.json", ".opencode/bun.lock",
                     ".opencode/node_modules/@opencode-ai/plugin/package.json"):
            assert name in effects
        assert run.harvest is not None and run.harvest.error is None
        assert proj.finding_names("review-") == ["review-1.md"]
    finally:
        proj.cleanup()


def test_a_write_to_the_real_root_is_a_violation_and_nothing_is_harvested(tmp_path, monkeypatch):
    real = str(tmp_path / "project")
    proj = build(tmp_path, monkeypatch, ("boundary_escape", {"real_root": real, "absolute": True}))
    try:
        run = proj.run_isolated("review")
        assert run.result.outcome == "COMPLETED"
        assert run.boundary.status == "violation"
        assert run.harvest is None and run.harvest_skipped == "boundary-violation"
        assert (proj.root / "src" / "escaped.txt").exists()
        assert any(v["path"] == "src" or "escaped" in v["path"] or v["rule"] == "source-unchanged"
                   for v in run.boundary.violations), run.boundary.violations
        assert proj.finding_names("review-") == []
        assert run.removed is True
    finally:
        proj.cleanup()


def test_the_snapshot_is_removed_after_a_run_that_does_not_complete(tmp_path, monkeypatch):
    proj = build(tmp_path, monkeypatch, "native_error")
    try:
        run = proj.run_isolated("review")
        assert run.result.outcome != "COMPLETED"
        assert run.harvest is None and run.harvest_skipped.startswith("outcome-")
        assert run.removed is True and not proj.removed_state["root"].exists()
    finally:
        proj.cleanup()


def test_runs_have_separate_native_sessions_and_never_resume(tmp_path, monkeypatch):
    proj = build(tmp_path, monkeypatch, "record_only")
    try:
        first = proj.run_isolated("review")
        second = proj.run_isolated("critic")
        plain = proj.run_plain("plan")
        runs = [first.result.run_id, second.result.run_id, plain.run_id]
        assert len(set(runs)) == 3
        launches = [i for i in proj.invocations() if i.get("attempt") is not None]
        sessions = [i["session_id"] for i in launches]
        assert len(launches) == 3 and all(sessions) and len(set(sessions)) == 3, sessions
        for call in proj.invocations():
            if call.get("attempt") is not None:
                assert "session" not in (call.get("parsed") or {})
        assert proj.record(first.result.run_id)["request"]["workspace"]
        assert proj.record(plain.run_id)["request"]["workspace"] is None
    finally:
        proj.cleanup()


def test_a_prepare_refusal_spawns_nothing_and_removes_the_snapshot(tmp_path, monkeypatch):
    proj = build(tmp_path, monkeypatch, "record_only")
    real_create = snapshot.create

    def drifted(*args, **kwargs):
        snap = real_create(*args, **kwargs)
        target = snap.root / ".opencode" / "commands" / "quoin-review.md"
        os.chmod(str(target), 0o644)
        target.write_text(target.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
        return snap

    monkeypatch.setattr(snapshot, "create", drifted)
    try:
        run = proj.run_isolated("review")
        assert run.result.outcome == "REFUSED"
        assert run.result.refusal["code"] == "owned-file-drift"
        assert [i for i in proj.invocations() if i.get("attempt") is not None] == []
        assert run.harvest is None and run.removed is True
        assert not proj.removed_state["root"].exists()
    finally:
        proj.cleanup()


def test_a_snapshot_refusal_returns_without_a_run(tmp_path, monkeypatch):
    proj = build(tmp_path, monkeypatch, "record_only")
    try:
        run = proj.run_isolated("review", stage="7")
        assert run.result is None and run.refusal == "snapshot-outbox-unresolved"
        assert run.removed is True
        assert proj.snapshot_roots() == []
    finally:
        proj.cleanup()
