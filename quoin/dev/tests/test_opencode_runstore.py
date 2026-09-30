"""Run store: ids, sidecar, atomic records, hashes, revisions, orphans."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from quoin.opencode_adapter import events as ev
from quoin.opencode_adapter import runstore as rs
from quoin.opencode_adapter.events import EventType as ET
from quoin.opencode_adapter.proctree import Identity, ProcInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "oc-20260101T000000Z-0123abcd"
TS = "2026-01-01T00:00:00.000Z"


def _clock():
    return 1_767_225_600.0  # 2026-01-01T00:00:00Z


def _event(seq, attempt=1, *, kind="text", delegation=None, permission_outcome=None,
           etype=ET.PROGRESS, native_id=None, origin="native", finish=None):
    if etype is ET.USAGE:
        payload = ev.UsagePayload(input_tokens=1, output_tokens=1, finish_reason=finish)
    else:
        payload = ev.ProgressPayload(kind=kind, raw_type=kind, delegation=delegation,
                                     permission_outcome=permission_outcome)
    native = ev.NativeRef(type="part", id=native_id or "p%d" % seq, content_sha256=None) if origin == "native" else None
    return ev.RuntimeEvent(
        schema_version=1, run_id=RUN_ID, attempt=attempt, sequence=seq, session_id="s1",
        parent_id=None, timestamp=TS, observed_at=TS, type=etype, origin=origin,
        native=native, payload=payload,
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".workflow_artifacts").mkdir(parents=True)
    return root


# ------------------------------------------------------------------ names


def test_run_id_shape_and_uniqueness():
    ids = {rs.new_run_id(_clock) for _ in range(1000)}
    assert len(ids) == 1000
    assert all(ev.RUN_ID_RE.match(i) for i in ids)
    assert all(i.startswith("oc-20260101T000000Z-") for i in ids)


def test_reserve_run_id_claims_sidecar_and_retries_collisions(project, monkeypatch):
    d = rs.store_dir(project, create=True)
    seq = iter(["oc-20260101T000000Z-aaaaaaaa", "oc-20260101T000000Z-aaaaaaaa", "oc-20260101T000000Z-bbbbbbbb"])
    monkeypatch.setattr(rs, "new_run_id", lambda clock=None: next(seq))
    first, p1 = rs.reserve_run_id(d)
    second, p2 = rs.reserve_run_id(d)
    assert (first, second) == ("oc-20260101T000000Z-aaaaaaaa", "oc-20260101T000000Z-bbbbbbbb")
    assert p1.sidecar.exists() and oct(p1.sidecar.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("task", ["", "..", "a/b", "a..b", ".hidden", "-x", "a b", "x" * 129, "a\x00b", "../etc"])
def test_task_name_refusals(task, project):
    d = rs.store_dir(project, create=True)
    with pytest.raises(rs.RunStoreError) as info:
        rs.pointer_path(d, task)
    assert info.value.code == "invalid-task-name"


def test_task_name_ok_and_run_id_refusals(project):
    d = rs.store_dir(project, create=True)
    assert rs.pointer_path(d, "ivg-272.stage_2").name == "task-ivg-272.stage_2.json"
    for bad in ("../x", "oc-1", RUN_ID + "/x"):
        with pytest.raises(rs.RunStoreError):
            rs.run_paths(d, bad)


def test_store_dir_layout_and_symlink_refusal(project, tmp_path):
    d = rs.store_dir(project, create=True)
    assert d == project / ".workflow_artifacts/memory/runtime/opencode"
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert rs.store_dir(project) == d
    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.rmtree(project / ".workflow_artifacts/memory/runtime")
    os.symlink(str(other), str(project / ".workflow_artifacts/memory/runtime"))
    for create in (False, True):
        with pytest.raises(rs.RunStoreError) as info:
            rs.store_dir(project, create=create)
        assert info.value.code == "unsafe-path"
    assert list(other.iterdir()) == []


def test_symlinked_workflow_artifacts_refused(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(str(real), str(root / ".workflow_artifacts"))
    with pytest.raises(rs.RunStoreError):
        rs.store_dir(root, create=True)


# ------------------------------------------------------------------ sidecar


def _write_events(path, count):
    with rs.SidecarWriter(path) as w:
        for i in range(1, count + 1):
            w.write(_event(i))
        w.fsync()
        return w.offset


def test_sidecar_roundtrip_and_offset(tmp_path):
    p = tmp_path / "a.jsonl"
    offset = _write_events(p, 3)
    assert offset == p.stat().st_size
    read = rs.read_sidecar(p)
    assert [e.sequence for e in read.events] == [1, 2, 3]
    assert read.byte_offset == offset and not read.torn_tail
    with rs.SidecarWriter(p) as w:
        assert w.offset == offset
    assert rs.read_sidecar(tmp_path / "missing.jsonl") == rs.SidecarRead((), 0, False)


@pytest.mark.parametrize("tail", [b'{"schema_version":1,"run_id"', b"not json at all\n", b'{"a":1}\n'])
def test_torn_tail_ignored_then_truncated_on_repair(tmp_path, tail):
    p = tmp_path / "a.jsonl"
    good = _write_events(p, 2)
    with open(p, "ab") as f:
        f.write(tail)
    read = rs.read_sidecar(p)
    assert read.torn_tail and len(read.events) == 2 and read.byte_offset == good
    assert p.stat().st_size == good + len(tail)  # read alone never mutates
    repaired = rs.read_sidecar(p, repair=True)
    assert repaired.torn_tail and p.stat().st_size == good
    again = rs.read_sidecar(p)
    assert not again.torn_tail and len(again.events) == 2


def test_mid_file_corruption_is_an_error(tmp_path):
    p = tmp_path / "a.jsonl"
    good = _write_events(p, 1)
    with open(p, "ab") as f:
        f.write(b"garbage\n")
    with rs.SidecarWriter(p) as w:
        w.write(_event(9))
    with pytest.raises(rs.RunStoreError) as info:
        rs.read_sidecar(p, repair=True)
    assert info.value.code == "corrupt-sidecar"
    assert p.stat().st_size > good  # nothing was truncated


# ------------------------------------------------------------------ records


def test_record_helpers_roundtrip(project):
    d = rs.store_dir(project, create=True)
    rec = rs.new_run_record(RUN_ID, "task-a", {"role": "x"}, {"model": "m"}, _clock)
    rs.set_state(rec, "running", "started", _clock)
    rec["attempts"].append(rs.new_attempt(1, pid=5, pgid=5, child_start="s", driver_pid=4,
                                          driver_start="d", resume_mode="fresh", clock=_clock))
    rs.write_record(d, rec)
    path = d / (RUN_ID + ".run.json")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = rs.load_record(d, RUN_ID)
    assert loaded == rec and loaded["schema_version"] == 1
    assert [h["state"] for h in loaded["history"]] == ["prepared", "running"]
    assert loaded["resume_hint"] is None and loaded["attempts"][0]["driver_lost"] is False
    ck = rs.new_checkpoint(RUN_ID, 1, last_sequence=7, sidecar_offset=99, native_session_id="s",
                           repo_revisions=[], step_open=True, ran_anything=True,
                           state_changing_part_ids=["b", "a", "a"], clock=_clock)
    rs.write_checkpoint(d, ck)
    assert rs.load_checkpoint(d, RUN_ID)["state_changing_part_ids"] == ["a", "b"]
    rs.write_pointer(d, rs.new_pointer("task-a", RUN_ID, _clock))
    assert rs.load_pointer(d, "task-a")["run_id"] == RUN_ID
    assert rs.load_pointer(d, "nope") is None
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_unsupported_schema_and_corrupt_record(project):
    d = rs.store_dir(project, create=True)
    path = d / (RUN_ID + ".run.json")
    path.write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(rs.RunStoreError) as info:
        rs.load_record(d, RUN_ID)
    assert info.value.code == "unsupported-schema"
    path.write_text("{not json")
    with pytest.raises(rs.RunStoreError) as info:
        rs.load_record(d, RUN_ID)
    assert info.value.code == "corrupt-record"


def test_crash_between_temp_write_and_replace_keeps_previous_record(project, monkeypatch):
    d = rs.store_dir(project, create=True)
    rec = rs.new_run_record(RUN_ID, "task-a", {}, {}, _clock)
    rs.write_record(d, rec)
    path = d / (RUN_ID + ".run.json")
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("crash")

    monkeypatch.setattr(os, "replace", boom)
    rec2 = dict(rec, state="running")
    with pytest.raises(OSError):
        rs.write_record(d, rec2)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert rs.load_record(d, RUN_ID) == rec
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------------------------ facts


def test_run_facts_two_attempts():
    events = [
        _event(1, 1, kind="step_start"),
        _event(2, 1, kind="tool", delegation="background"),
        _event(3, 1, etype=ET.USAGE, finish="stop"),
        _event(4, 2, kind="step_start"),
    ]
    facts = rs.run_facts(events)
    assert facts.ran_anything and facts.delegation_downgrades == frozenset({"background"})
    assert facts.last_attempt_step_open and not facts.denied_halt_seen
    closed = events + [_event(5, 2, etype=ET.USAGE, finish="stop")]
    assert not rs.run_facts(closed).last_attempt_step_open
    halted = rs.run_facts([_event(1, kind="halted", permission_outcome="denied")])
    assert halted.denied_halt_seen


def test_run_facts_empty_and_driver_only():
    facts = rs.run_facts([])
    assert not facts.ran_anything and facts.delegation_downgrades == frozenset()
    driver = rs.run_facts([_event(1, origin="driver")])
    assert not driver.ran_anything


def test_add_counters_survives_resume_with_rebuilt_deduper():
    first = {"lines": 10, "dropped_duplicates": 2, "revisions": 1}
    pipe = ev.EventPipeline(RUN_ID, 2, observed_clock=_clock, deduper=ev.Deduper.from_events([]))
    pipe.feed_line(b"not json")
    total = rs.add_counters(first, pipe.counters)
    assert total["lines"] == 10 + pipe.counters["lines"] >= 11
    assert total["dropped_duplicates"] >= 2 and total["revisions"] >= 1
    assert first == {"lines": 10, "dropped_duplicates": 2, "revisions": 1}


# ------------------------------------------------------------------ hashes


def test_hash_inputs_and_diff(project, tmp_path):
    task = project / ".workflow_artifacts" / "t1"
    (task / "sub").mkdir(parents=True)
    (task / "a.md").write_text("one")
    (task / "sub" / "b.md").write_text("two")
    (project / "ctx.txt").write_text("ctx")
    (project / "big.bin").write_bytes(b"x" * 100)
    os.symlink(str(project / "ctx.txt"), str(task / "link.md"))
    before = rs.hash_inputs(project, "t1", ["ctx.txt", "big.bin"], max_file_bytes=50)
    assert set(before) == {".workflow_artifacts/t1/a.md", ".workflow_artifacts/t1/sub/b.md", "ctx.txt", "big.bin"}
    assert before["big.bin"] == {"skipped": "too-large"}
    import hashlib
    assert before["ctx.txt"] == hashlib.sha256(b"ctx").hexdigest()
    (task / "a.md").write_text("changed")
    (task / "sub" / "b.md").unlink()
    (task / "new.md").write_text("n")
    after = rs.hash_inputs(project, "t1", ["ctx.txt", "big.bin"], max_file_bytes=50)
    diff = rs.diff_hashes(before, after)
    assert [(d.path, d.change) for d in diff] == [
        (".workflow_artifacts/t1/a.md", "modified"),
        (".workflow_artifacts/t1/new.md", "created"),
        (".workflow_artifacts/t1/sub/b.md", "deleted"),
    ]
    assert diff[1].sha256_before is None and diff[2].sha256_after is None


def test_hash_inputs_refuses_escape_and_caps(project, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    for ref in ("../outside.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(rs.RunStoreError):
            rs.hash_inputs(project, "t1", [ref])
    task = project / ".workflow_artifacts" / "t1"
    task.mkdir()
    for i in range(5):
        (task / ("f%d" % i)).write_text(str(i))
    capped = rs.hash_inputs(project, "t1", max_files=3)
    assert capped["<truncated>"] == {"skipped": "file-cap"} and len(capped) == 4
    assert rs.hash_inputs(project, "missing-task") == {}


def test_hash_inputs_total_byte_cap_truncates(project):
    task = project / ".workflow_artifacts" / "t1"
    task.mkdir()
    for i in range(5):
        (task / ("f%d" % i)).write_bytes(b"x" * 10)
    capped = rs.hash_inputs(project, "t1", max_total_bytes=25)
    assert capped["<truncated>"] == {"skipped": "byte-cap"}
    assert len([k for k in capped if k != "<truncated>"]) == 2
    assert "<truncated>" not in rs.hash_inputs(project, "t1")


def test_hash_reads_no_more_than_the_measured_size_and_flags_growth(project, monkeypatch):
    task = project / ".workflow_artifacts" / "t1"
    task.mkdir()
    f = task / "grow"
    f.write_bytes(b"x" * 10)
    real_lstat = rs.os.lstat

    grown = []

    def stale_lstat(path, *a, **k):
        info = real_lstat(path, *a, **k)
        if str(path).endswith("grow") and len(grown) < 2:
            grown.append(True)
            if len(grown) < 2:
                return info
            # grows after the last measurement, before the read
            with open(str(path), "ab") as out:
                out.write(b"x" * 40)
        return info

    monkeypatch.setattr(rs.os, "lstat", stale_lstat)
    assert rs.hash_inputs(project, "t1")[".workflow_artifacts/t1/grow"] == {"skipped": "changed"}


def test_diff_hashes_suppresses_created_and_deleted_when_a_snapshot_is_truncated():
    trunc = {"<truncated>": {"skipped": "byte-cap"}}
    before = {"a": "1", "b": "2"}
    after = {"a": "9", "c": "3", **trunc}
    changes = {(p.path, p.change) for p in rs.diff_hashes(before, after)}
    assert changes == {("a", "modified")}
    changes = {(p.path, p.change) for p in rs.diff_hashes({**before, **trunc}, {"a": "1", "c": "3"})}
    assert changes == set()
    full = {(p.path, p.change) for p in rs.diff_hashes(before, {"a": "9", "c": "3"})}
    assert full == {("a", "modified"), ("b", "deleted"), ("c", "created")}


# ------------------------------------------------------------------ revisions


def test_repo_revisions_budget_marks_remaining_repos(project):
    (project / "a").mkdir()
    (project / "a" / ".git").mkdir()
    (project / "b").mkdir()
    (project / "b" / ".git").mkdir()
    calls = []

    def runner(argv, timeout):
        calls.append(timeout)
        return 0, "abc\n"

    out = rs.repo_revisions(project, runner=runner, budget_s=0)
    assert calls == [] and {r["error"] for r in out} == {"budget"}


def test_repo_revisions_with_fake_runner(project):
    (project / "sub").mkdir()
    (project / "sub" / ".git").mkdir()
    (project / "plain").mkdir()
    (project / ".git").mkdir()

    def runner(argv, timeout):
        assert timeout == 30.0
        repo = argv[2]
        if argv[3] == "rev-parse":
            return 0, "abc123\n"
        return 0, (" M f\n" if repo.endswith("sub") else "")

    out = rs.repo_revisions(project, runner=runner)
    assert [(r["path"], r["head"], r["dirty"], r["error"]) for r in out] == [
        (".", "abc123", False, None), ("sub", "abc123", True, None)]
    fail = rs.repo_revisions(project, runner=lambda a, t: (128, ""))
    assert fail[0]["error"] == "rev-parse-failed" and fail[0]["head"] is None

    def missing(a, t):
        raise OSError("no git")

    assert rs.repo_revisions(project, runner=missing)[0]["error"] == "git-unavailable"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_repo_revisions_real_subrepo(tmp_path):
    root = tmp_path / "ws"
    sub = root / "svc"
    sub.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

    def git(*args):
        subprocess.run(["git", "-C", str(sub), *args], check=True, env=env, stdout=subprocess.DEVNULL)

    git("init", "-q")
    (sub / "f.txt").write_text("1")
    git("add", "f.txt")
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    clean = rs.repo_revisions(root)
    assert len(clean) == 1 and clean[0]["path"] == "svc" and clean[0]["dirty"] is False
    assert re.match(r"^[0-9a-f]{40}$", clean[0]["head"])
    (sub / "f.txt").write_text("2")
    assert rs.repo_revisions(root)[0]["dirty"] is True


# ------------------------------------------------------------------ orphans


def _record(state="running", **attempt):
    a = rs.new_attempt(1, pid=100, pgid=100, child_start="c", driver_pid=50, driver_start="d",
                       resume_mode="fresh", clock=_clock)
    a.update(attempt)
    rec = rs.new_run_record(RUN_ID, "t", {}, {}, _clock)
    rec["state"] = state
    rec["attempts"] = [a]
    return rec


def test_orphan_state():
    table = {50: ProcInfo(50, 1, 50, "S", "d")}
    assert rs.orphan_state(_record(), table) is None
    assert rs.orphan_state(_record(), {50: ProcInfo(50, 1, 50, "S", "other")}) == "driver-lost"
    assert rs.orphan_state(_record(), {50: ProcInfo(50, 1, 50, "Z", "d"), 1: ProcInfo(1, 0, 1, "S", "i")}) == "driver-lost"
    assert rs.orphan_state(_record(state="succeeded"), {1: ProcInfo(1, 0, 1, "S", "i")}) is None
    assert rs.orphan_state(_record(), None) is None
    assert rs.orphan_state(_record(driver_start=None), table) is None


def test_live_identities():
    table = {
        100: ProcInfo(100, 1, 100, "S", "c"),
        101: ProcInfo(101, 100, 100, "S", "g1"),
        102: ProcInfo(102, 1, 200, "S", "g2"),
        103: ProcInfo(103, 1, 200, "S", "unrelated"),
        104: ProcInfo(104, 100, 100, "Z", "z"),
    }
    rec = _record(descendants=[{"pid": 102, "start": "g2"}, {"pid": 105, "start": "gone"}])
    got = rs.live_identities(rec, table)
    assert got == [Identity(100, "c"), Identity(101, "g1"), Identity(102, "g2")]
    # a recycled child pid does not anchor the group
    recycled = dict(table)
    recycled[100] = ProcInfo(100, 1, 100, "S", "new")
    assert Identity(101, "g1") not in rs.live_identities(rec, recycled)
    assert rs.live_identities(rec, None) == []


# ------------------------------------------------------------------ naming


def _find_commands(text):
    # join backslash continuations, then keep lines that start a find
    joined = re.sub(r"\\\n\s*", " ", text)
    return [ln.strip() for ln in joined.splitlines() if ln.strip().startswith("find ")]


def test_runtime_dir_is_outside_every_cleanup_sweep():
    files = [
        REPO_ROOT / "quoin/adapters/claude/skills/cleanup/SKILL.md",
        REPO_ROOT / "quoin/core/skills/cleanup.md",
    ]
    total = 0
    sample_names = [RUN_ID + ".jsonl", RUN_ID + ".run.json", RUN_ID + ".checkpoint.json", "task-t.json"]
    for path in files:
        text = path.read_text()
        for cmd in _find_commands(text):
            total += 1
            m = re.match(r'find\s+"(\$\{?MEMORY_DIR\}?)(/[A-Za-z0-9_.-]+)?"', cmd)
            assert m, cmd
            if m.group(2) is None:
                assert "-maxdepth 1" in cmd, cmd
            else:
                assert m.group(2) != "/runtime", cmd
                assert "-maxdepth 1" in cmd, cmd
            for glob in re.findall(r"-name\s+'([^']+)'", cmd):
                for name in sample_names:
                    assert not fnmatch.fnmatchcase(name, glob), (glob, name)
    assert total >= 1, "expected at least one find command in the cleanup skill"
    skill = files[0].read_text()
    families = re.findall(r"^\d+\. `([^`]+)`$", skill, re.M)
    assert families
    for name in sample_names:
        assert not any(fnmatch.fnmatchcase(name, f) for f in families)
        assert not name.startswith("run-state-")
