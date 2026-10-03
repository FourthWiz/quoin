"""Snapshot creation, base trees, review context, harvest and removal against a
real git repository with a real install."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import _opencode_boundary_helpers as bh
import _opencode_gate_helpers as gh
from quoin.opencode_adapter import boundaries, install, runstore, snapshot

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX-only"),
]

TASK = bh.TASK
SOURCE = bh.SOURCE_DIR


@pytest.fixture
def proj(tmp_path, monkeypatch):
    project = bh.SnapshotProject(tmp_path, monkeypatch)
    project.state_root = tmp_path / "state-root"
    yield project
    # make sure nothing read-only is left for the temp cleanup
    for root in project.snapshot_roots() + list((project.state_root / "snapshots").glob("*/*")):
        snapshot.remove(snapshot.Snapshot(root, ""))


def make(proj, role="reviewer", **kw):
    kw.setdefault("clock", lambda: 1_700_000_000.0)
    return snapshot.create(
        proj.root, TASK, 1, role=role, source_dir=SOURCE, state_root=proj.state_root, **kw,
    )


def files_of(snap):
    out = set()
    for base, dirs, names in os.walk(str(snap.root)):
        if ".git" in dirs and Path(base) == snap.root:
            dirs.remove(".git")
        for name in names:
            out.add(os.path.relpath(os.path.join(base, name), str(snap.root)).replace(os.sep, "/"))
    return out


def mode(path):
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def test_contents_exclude_git_and_env_files_and_secrets(proj):
    (proj.root / ".env").write_text("TOKEN=hunter2-secret\n")
    (proj.root / ".env.local").write_text("TOKEN=hunter2-secret\n")
    (proj.root / ".env.example").write_text("TOKEN=hunter2-secret\n")
    (proj.root / "src" / ".env").write_text("TOKEN=hunter2-secret\n")
    (proj.root / "notes.txt").write_text("plain untracked\n")
    (proj.root / "env").mkdir()
    (proj.root / "env" / "ignored.txt").write_text("ignored by git\n")
    snap = make(proj)
    got = files_of(snap)
    assert not any(os.path.basename(p).startswith(".env") for p in got)
    assert "src/app.py" in got and "notes.txt" in got and ".gitignore" in got
    assert "env/ignored.txt" not in got
    assert not any(p.startswith(".git/") for p in got)
    for rel in got:
        assert b"hunter2-secret" not in (snap.root / rel).read_bytes()
    reasons = {(s["path"], s["reason"]) for s in snap.skipped}
    assert (".env", "secret-name") in reasons and ("src/.env", "secret-name") in reasons


def test_symlinks_and_deleted_tracked_files_are_listed_not_errors(proj):
    (proj.root / "link.txt").symlink_to(proj.root / "src" / "app.py")
    (proj.root / "src" / "gone.py").write_text("gone\n")
    subprocess.run(["git", "-C", str(proj.root), "add", "src/gone.py"], check=True)
    subprocess.run(["git", "-C", str(proj.root), "commit", "-q", "-m", "more"], check=True)
    (proj.root / "src" / "gone.py").unlink()
    snap = make(proj)
    reasons = {(s["path"], s["reason"]) for s in snap.skipped}
    assert ("link.txt", "symlink") in reasons
    assert ("src/gone.py", "missing") in reasons
    assert "link.txt" not in files_of(snap) and "src/gone.py" not in files_of(snap)


def test_owned_files_are_byte_equal_and_the_task_folder_is_copied(proj):
    meta = install.load_metadata(proj.root)
    snap = make(proj)
    for rel in meta.owned:
        assert (snap.root / rel).read_bytes() == (proj.root / rel).read_bytes()
    assert (snap.root / ".quoin/opencode-install.json").is_file()
    assert (snap.root / ".workflow_artifacts" / TASK / "stage-1" / "current-plan.md").is_file()
    gh.write(proj.root / ".workflow_artifacts" / TASK / "stage-1" / "scratch.md.tmp", "tmp")
    other = make(proj)
    assert not any(p.endswith(".tmp") for p in files_of(other))
    for rel, digest in snap.manifest.items():
        assert hashlib.sha256((snap.root / rel).read_bytes()).hexdigest() == digest


def test_permissions_read_only_except_outbox_agent_dirs_and_opencode_dir(proj):
    snap = make(proj)
    assert snap.outbox_rel == ".workflow_artifacts/%s/stage-1" % TASK
    assert mode(snap.root / "src" / "app.py") == 0o444
    assert mode(snap.root / "src") == 0o555
    assert mode(snap.root / snap.outbox_rel) == 0o755
    for rel in ("memory/sessions", "memory/daily"):
        assert mode(snap.root / ".workflow_artifacts" / rel) == 0o755
    assert mode(snap.root / ".workflow_artifacts" / "cache") == 0o755
    assert mode(snap.root / ".opencode") == 0o755
    owned = snap.root / ".opencode" / "commands" / "quoin-plan.md"
    assert mode(owned) == 0o444
    (snap.root / ".opencode" / "package.json").write_text("{}\n")
    (snap.root / ".opencode" / "node_modules" / "x").mkdir(parents=True)
    with pytest.raises(PermissionError):
        owned.write_text("changed")
    with pytest.raises(PermissionError):
        (snap.root / "src" / "new.py").write_text("x")
    (snap.root / snap.outbox_rel / "review-1.md").write_text("ok")
    (snap.root / ".workflow_artifacts" / "memory" / "sessions" / "s.md").write_text("ok")


def test_snapshot_is_its_own_git_root_with_one_commit(proj):
    snap = make(proj)
    top = subprocess.run(
        ["git", "-C", str(snap.root), "rev-parse", "--show-toplevel"], check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    assert os.path.realpath(top) == os.path.realpath(str(snap.root))
    count = subprocess.run(
        ["git", "-C", str(snap.root), "rev-list", "--count", "HEAD"], check=True, stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    assert count == "1"
    assert snap.root.parent.parent.name == "snapshots"
    assert mode(snap.root.parent) == 0o700


def test_a_cap_refuses_and_leaves_nothing(proj):
    for limits in ({"max_bytes": 10}, {"max_files": 2}):
        with pytest.raises(snapshot.SnapshotRefused) as info:
            make(proj, **limits)
        assert info.value.code == "snapshot-too-large"
    left = list((proj.state_root / "snapshots").glob("*/*"))
    assert left == []


def test_an_unusable_request_refuses(proj):
    with pytest.raises(snapshot.SnapshotRefused):
        snapshot.create(proj.root, TASK, 1, role="gate", source_dir=SOURCE, state_root=proj.state_root)
    with pytest.raises(snapshot.SnapshotRefused) as info:
        snapshot.create(proj.root, TASK, 7, role="critic", source_dir=SOURCE, state_root=proj.state_root)
    assert info.value.code == "snapshot-outbox-unresolved"
    assert list((proj.state_root / "snapshots").glob("*/*")) == []


def test_base_tree_first_call_wins(proj):
    first = snapshot.record_base_tree(proj.root, TASK, 1)
    assert first["."] and len(first["."]) >= 40
    (proj.root / "src" / "app.py").write_text("x = 2\n")
    again = snapshot.record_base_tree(proj.root, TASK, 1)
    assert again == first
    state = runstore.load_workflow_state(runstore.store_dir(proj.root), TASK)
    assert state["settings"]["base_trees"]["1"] == first
    other = snapshot.record_base_tree(proj.root, TASK, 2)
    assert other["."] != first["."]
    # the real index was never touched
    status = subprocess.run(
        ["git", "-C", str(proj.root), "diff", "--cached", "--name-only"], check=True, stdout=subprocess.PIPE,
    ).stdout.decode()
    assert status.strip() == ""


def test_review_context_holds_changes_made_after_the_base_tree(proj):
    (proj.root / "src" / "app.py").write_text("x = 'before the stage'\n")
    snapshot.record_base_tree(proj.root, TASK, 2)
    (proj.root / "src" / "later.py").write_text("later = True\n")
    snap = snapshot.create(proj.root, TASK, 2, role="reviewer", source_dir=SOURCE, state_root=proj.state_root)
    diff = (snap.root / "review-context" / "root.diff").read_text()
    assert "later.py" in diff and "later = True" in diff
    assert "before the stage" not in diff
    readme = (snap.root / "review-context" / "README.txt").read_text()
    assert "recorded before the stage began" in readme
    assert mode(snap.root / "review-context" / "root.diff") == 0o444
    critic = snapshot.create(proj.root, TASK, 2, role="critic", source_dir=SOURCE, state_root=proj.state_root)
    assert not (critic.root / "review-context").exists()


def test_review_context_falls_back_to_head_without_a_base_tree(proj):
    (proj.root / "src" / "app.py").write_text("x = 'uncommitted'\n")
    snap = make(proj)
    diff = (snap.root / "review-context" / "root.diff").read_text()
    assert "uncommitted" in diff
    assert "HEAD" in (snap.root / "review-context" / "README.txt").read_text()


def test_the_real_tree_is_unchanged_by_create(proj):
    before = proj.tree_state()
    listing_before = boundaries.take_listing(proj.root)
    make(proj)
    assert proj.tree_state() == before
    verdict = boundaries.verify(
        "reviewer-snapshot", listing_before, boundaries.take_listing(proj.root), task=TASK, run_id="r",
    )
    assert verdict.status == "ok", verdict


# --- harvest ----------------------------------------------------------------

def stage_dir(proj):
    return proj.root / ".workflow_artifacts" / TASK / "stage-1"


def harvest(proj, snap, before, kind="review"):
    return snapshot.harvest(snap, proj.root, TASK, 1, kind=kind, source_dir=SOURCE, before_names=before)


def test_harvest_renames_to_the_next_free_number(proj):
    gh.write(stage_dir(proj) / "review-1.md", gh.REVIEW)
    gh.write(stage_dir(proj) / "review-2.md", gh.REVIEW)
    gh.write(stage_dir(proj) / "security-review-9.md", "other")
    snap = make(proj)
    before = snapshot.outbox_names(snap)
    assert {"review-1.md", "review-2.md"} <= before
    (snap.root / snap.outbox_rel / "review-1x.md").write_text("stray")  # not matching the pattern
    (snap.root / snap.outbox_rel / "review-9.md").write_text(gh.REVIEW)
    (snap.root / snap.outbox_rel / "review-9.md.tmp").write_text("leftover")
    got = harvest(proj, snap, before)
    assert got.error is None and got.number == 3
    assert got.path_rel == ".workflow_artifacts/%s/stage-1/review-3.md" % TASK
    assert got.sha256 == hashlib.sha256(gh.REVIEW.encode()).hexdigest()
    assert got.ignored_tmp == ("review-9.md.tmp",) and got.unexpected == ("review-1x.md",)
    assert (stage_dir(proj) / "review-3.md").read_text() == gh.REVIEW
    assert not (stage_dir(proj) / "review-9.md").exists()


def test_harvest_critic_numbering_is_per_kind(proj):
    gh.write(stage_dir(proj) / "review-4.md", gh.REVIEW)
    snap = make(proj, role="critic")
    before = snapshot.outbox_names(snap)
    (snap.root / snap.outbox_rel / "critic-response-1.md").write_text(gh.CRITIC_PASS)
    got = harvest(proj, snap, before, kind="critic")
    assert got.error is None and got.number == 1
    assert (stage_dir(proj) / "critic-response-1.md").read_text() == gh.CRITIC_PASS


def test_harvest_refuses_none_two_and_invalid_without_touching_the_real_folder(proj):
    snap = make(proj)
    before = snapshot.outbox_names(snap)
    names_before = sorted(os.listdir(stage_dir(proj)))
    assert harvest(proj, snap, before).error == "harvest-none"
    (snap.root / snap.outbox_rel / "review-5.md").write_text(gh.REVIEW)
    (snap.root / snap.outbox_rel / "review-6.md").write_text(gh.REVIEW)
    assert harvest(proj, snap, before).error == "harvest-ambiguous"
    (snap.root / snap.outbox_rel / "review-6.md").unlink()
    (snap.root / snap.outbox_rel / "review-5.md").write_text("not a review\n")
    got = harvest(proj, snap, before)
    assert got.error == "harvest-invalid" and got.detail
    assert sorted(os.listdir(stage_dir(proj))) == names_before
    assert not Path(str(snap.root) + ".candidate").exists()


def test_harvesting_the_same_finding_twice_stores_two_numbered_files(proj):
    snap = make(proj)
    before = snapshot.outbox_names(snap)
    (snap.root / snap.outbox_rel / "review-1.md").write_text(gh.REVIEW)
    first = harvest(proj, snap, before)
    assert first.number == 1
    second = harvest(proj, snap, before)
    assert second.error is None and second.number == 2
    assert second.sha256 == first.sha256


def test_stageless_task_uses_the_task_root_for_both_sides(proj):
    plain = proj.root / ".workflow_artifacts" / "solo"
    gh.write(plain / "review-1.md", gh.REVIEW)
    snap = snapshot.create(
        proj.root, "solo", None, role="reviewer", source_dir=SOURCE, state_root=proj.state_root,
    )
    assert snap.outbox_rel == ".workflow_artifacts/solo"
    before = snapshot.outbox_names(snap)
    (snap.root / snap.outbox_rel / "review-1.md.tmp").write_text("x")
    (snap.root / snap.outbox_rel / "review-8.md").write_text(gh.REVIEW)
    got = snapshot.harvest(snap, proj.root, "solo", None, kind="review", source_dir=SOURCE, before_names=before)
    assert got.error is None and got.number == 2
    assert (plain / "review-2.md").read_text() == gh.REVIEW


# --- remove -----------------------------------------------------------------

def test_remove_deletes_a_read_only_tree_and_never_raises(proj):
    snap = make(proj)
    assert snapshot.remove(snap) is True
    assert not snap.root.exists()
    assert snapshot.remove(snap) is True  # already gone


def test_remove_after_an_error_and_with_a_model_made_symlink(proj, tmp_path):
    snap = make(proj)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (snap.root / snap.outbox_rel / "evil").symlink_to(target)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        assert snapshot.remove(snap) is True
    assert (target / "keep.txt").read_text() == "keep"
    assert mode(target) != 0 and not snap.root.exists()


def test_the_real_tree_is_unchanged_by_a_refused_harvest_and_removal(proj):
    snap = make(proj)
    before = proj.tree_state()
    names = snapshot.outbox_names(snap)
    assert harvest(proj, snap, names).error == "harvest-none"
    snapshot.remove(snap)
    assert proj.tree_state() == before
