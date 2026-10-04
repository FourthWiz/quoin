"""Building, writing and scoping continuation records for the OpenCode adapter."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

import _opencode_gate_helpers as g
import _opencode_handoff_helpers as hh
import _opencode_helpers as helpers
import _opencode_merge_helpers as mh
import _opencode_run_helpers as rh
from quoin.opencode_adapter import driver, handoff, merge, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
CLEANUP_SKILL = helpers.SOURCE_DIR / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    return hh.Project(tmp_path, monkeypatch)


@pytest.fixture()
def core(fx):
    return handoff.core(fx.source)


def pairs(record, key="pending"):
    return [(i["phase"], i["stage"]) for i in record[key]]


def test_build_validates_and_carries_every_field(fx, core):
    record = fx.build()
    assert core.validate(record) == []
    for field in ("phase", "completed", "pending", "decisions", "artifacts", "repo_revisions",
                  "validation", "provenance", "unavailable_telemetry", "scope"):
        assert field in record
    assert record["origin_runtime"] == "opencode"
    assert record["provenance"]["transcripts_imported"] is False
    assert record["unavailable_telemetry"] == list(handoff.UNAVAILABLE_TELEMETRY)


def test_build_with_scope_evaluated_over_a_real_world(tmp_path, monkeypatch):
    fx = hh.Project(tmp_path / "p", monkeypatch)
    world = mh.World(tmp_path / "w", profile=mh.PROFILE_PERSONAL, root=fx.root)
    scope = handoff.scope_for_profile(
        fx.root, "personal", env=world.env, home=world.home, clock=lambda: mh.NOW.timestamp(),
    )
    record = fx.build(scope=scope)
    assert handoff.core(fx.source).validate(record) == []


def test_nested_project_root_records_the_parent_repository(tmp_path, monkeypatch):
    g.isolate_git(monkeypatch, tmp_path / "home")
    outer = g.make_repo(tmp_path / "outer")
    proj = outer / "sub"
    g.build_task(proj, "t1")
    record = handoff.build_record(
        proj, "t1", scope=hh.fixed_scope(), scope_source="operator-flag", source_dir=hh.SOURCE_DIR,
    )
    assert record["repo_revisions"][0]["path"] == ".."
    assert handoff.core(hh.SOURCE_DIR).validate(record) == []
    handoff.write(proj, "t1", record, source_dir=hh.SOURCE_DIR)


def test_backslash_file_name_is_refused_by_name(fx):
    g.write(fx.base / "stage-1" / "bad\\name.md", "x")
    with pytest.raises(handoff.HandoffRefused) as err:
        fx.build()
    assert err.value.code == "artifacts-incomplete"
    assert any("bad" in r for r in err.value.reasons)


def test_completed_pending_and_validation(fx):
    record = fx.build()
    assert pairs(record, "completed") == [("plan", 1)]
    assert pairs(record) == [
        ("architect", None), ("implement", 1), ("review", 1), ("plan", 2), ("implement", 2), ("review", 2),
    ]
    assert record["phase"] == {"current": "architect", "stage": None, "status": "pending"}
    fx.seed_gate("implement", 1, "FAIL", reasons=("tests-failed",))
    record = fx.build()
    assert ("implement", 1) not in pairs(record, "completed")
    assert {"phase": "implement", "stage": 1, "verdict": "FAIL", "reasons": ["tests-failed"]} in record["validation"]
    assert ("implement", 1) in pairs(record)


def test_superseded_pass_entry_is_not_completed(fx):
    fx.record("plan", stage=1, origin="adopted")
    assert pairs(fx.build(), "completed") == []


def test_all_phases_passed_is_done(fx):
    for phase, stage in (("architect", None), ("implement", 1), ("review", 1), ("plan", 2),
                         ("implement", 2), ("review", 2)):
        fx.seed_gate(phase, stage, "PASS")
    record = fx.build()
    assert record["pending"] == []
    assert record["phase"] == {"current": None, "stage": None, "status": "done"}
    assert handoff.core(fx.source).validate(record) == []


def test_interrupted_run_sets_phase_and_native(fx, core):
    fx.seed_gate("architect", None, "PASS")
    run_id = fx.seed_run("implement", "1", "interrupted", checkpoint=True, session="ses_ok1")
    record = fx.build()
    assert record["phase"] == {"current": "implement", "stage": 1, "status": "interrupted"}
    assert record["native"] == {"run_id": run_id, "session_id": "ses_ok1"}
    assert core.validate(record) == []


def test_completed_run_has_no_native(fx):
    fx.seed_run("implement", "1", "completed", checkpoint=True)
    record = fx.build()
    assert "native" not in record
    assert record["phase"]["current"] == "architect"


@pytest.mark.parametrize("session", ["bad id!", "ses_ok bad", "-lead"])
def test_odd_native_session_id_is_dropped(fx, core, session):
    run_id = fx.seed_run("implement", "1", "interrupted", checkpoint=True, session=session)
    token = driver.Handoff.from_checkpoint(runstore.load_checkpoint(fx.directory(), run_id))
    assert token.native_session_id == session
    record = fx.build()
    assert record["native"]["session_id"] is None
    assert core.validate(record) == []


def test_session_pattern_matches_the_driver():
    assert handoff.NATIVE_SESSION_RE.pattern == driver.NATIVE_SESSION_RE.pattern


def test_entries_outside_the_sequence_sort_last_and_never_pend(fx, core):
    fx.seed_gate("plan", 3, "PASS")
    fx.seed_gate("implement", 2, "FAIL")
    record = fx.build()
    completed = pairs(record, "completed")
    assert completed == [("plan", 1), ("plan", 3)]
    assert ("plan", 3) not in pairs(record)
    assert core.validate(record) == []


def test_stage_entry_in_unstaged_task(tmp_path, monkeypatch):
    g.isolate_git(monkeypatch, tmp_path / "home")
    root = g.make_repo(tmp_path / "proj")
    g.write(root / ".workflow_artifacts" / "u1" / "current-plan.md", g.PLAN)
    from quoin.opencode_adapter import evidence

    evidence.record_evidence(root, "u1", 1, "plan", "adopted", evidence.take_snapshot(root, "u1", "plan"))
    directory = runstore.store_dir(root, create=True)
    state = runstore.load_workflow_state(directory, "u1")
    runstore.update_current_entry(state, 1, "plan", gate={"verdict": "PASS", "reasons": []})
    runstore.write_workflow_state(directory, state)
    record = handoff.build_record(
        root, "u1", scope=hh.fixed_scope(), scope_source="operator-flag", source_dir=hh.SOURCE_DIR,
    )
    assert pairs(record, "completed") == [("plan", 1)]
    assert pairs(record) == [("plan", None), ("implement", None), ("review", None)]
    assert handoff.core(hh.SOURCE_DIR).validate(record) == []


def test_artifacts_listing(fx):
    g.write(fx.base / "cost-ledger.md", "x")
    g.write(fx.base / "stage-1" / "gate-plan-2026.md", "x")
    g.write(fx.base / "stage-1" / "x.tmp", "x")
    g.write(fx.base / "spec.md", "spec")
    record = fx.build()
    listed = {a["path"].rsplit("/", 1)[-1]: a["type"] for a in record["artifacts"]}
    assert listed["architecture.md"] == "architecture" and listed["current-plan.md"] == "current-plan"
    assert listed["spec.md"] == "spec" and listed["review-1.md"] == "review"
    assert "cost-ledger.md" not in listed and "x.tmp" not in listed
    assert not any(n.startswith("gate-") for n in listed)
    assert not any(a["type"] == "discover" for a in record["artifacts"])
    fx.record("discover", stage=None, origin="adopted")
    record = fx.build()
    assert sorted(a["path"] for a in record["artifacts"] if a["type"] == "discover") == sorted(
        handoff.evidence.DISCOVER_FILES)


def test_file_cap_makes_artifacts_incomplete(fx, monkeypatch):
    g.patch_runstore(monkeypatch, max_files=1)
    with pytest.raises(handoff.HandoffRefused) as err:
        fx.build()
    assert err.value.code == "artifacts-incomplete"


def test_repo_revisions_track_source_not_artifacts(fx):
    first = fx.build()["repo_revisions"][0]
    assert first["source_dirty"] is False
    g.write(fx.base / "stage-1" / "extra.md", "new")
    assert fx.build()["repo_revisions"][0]["source_dirty"] is False
    g.write(fx.root / "src" / "x.py", "x = 2\n")
    second = fx.build()["repo_revisions"][0]
    assert second["source_dirty"] is True and second["source_digest"]


def test_decisions_are_kept_deduplicated_capped_and_redacted(fx):
    first = fx.build(decisions=["use A", "use B"])
    handoff.write(fx.root, fx.task, first, source_dir=fx.source)
    second = fx.build(decisions=["use B", "use C"], previous=first)
    assert [d["text"] for d in second["decisions"]] == ["use A", "use B", "use C"]
    many = fx.build(decisions=["d%d" % i for i in range(150)])
    assert len(many["decisions"]) == 100 and many["decisions"][-1]["text"] == "d149"
    secret = fx.build(decisions=["token " + rh.SEEDED_SECRET], notes=["n " + rh.SEEDED_SECRET])
    handoff.write(fx.root, fx.task, secret, source_dir=fx.source)
    handoff.write(fx.root, fx.task, fx.build(previous=secret), source_dir=fx.source)
    directory = handoff.continuation_dir(fx.root)
    for path in directory.iterdir():
        assert rh.SEEDED_SECRET not in path.read_text()
    assert "<redacted>" in secret["decisions"][0]["text"]


def test_previous_record_of_another_task_is_ignored(fx):
    other = fx.build(decisions=["keep me"])
    other["task"] = "other"
    assert fx.build(previous=other)["decisions"] == []


def evaluator(profile):
    name = profile or "personal"
    return hh.fixed_scope(profile=name, classification=name)


def test_resolve_profile_sources(fx):
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.resolve_profile(fx.root, fx.task, None, evaluate=evaluator)
    assert err.value.code == "profile-unknown"
    scope, source = handoff.resolve_profile(fx.root, fx.task, "work", evaluate=evaluator)
    assert (scope["profile"], source) == ("work", "operator-flag")
    fx.settings(profile="work")
    scope, source = handoff.resolve_profile(fx.root, fx.task, None, evaluate=evaluator)
    assert (scope["profile"], source) == ("work", "workflow-state")


def test_resolve_profile_from_run_record(fx):
    run_id = fx.seed_run("implement", "1", "completed", profile="personal")
    record = runstore.load_record(fx.directory(), run_id)
    record["request"]["profile"] = None
    runstore.write_record(fx.directory(), record)
    scope, source = handoff.resolve_profile(fx.root, fx.task, None, evaluate=evaluator)
    assert (scope["profile"], source) == ("personal", "run-record")
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.resolve_profile(fx.root, fx.task, "work", evaluate=evaluator)
    assert err.value.code == "profile-mismatch"
    scope, _ = handoff.resolve_profile(fx.root, fx.task, "personal", evaluate=evaluator)
    assert scope["profile"] == "personal"


def test_resolve_profile_run_refused_before_prepare_counts_as_default(fx):
    run_id = fx.seed_run("implement", "1", "refused", profile=None)
    record = runstore.load_record(fx.directory(), run_id)
    record["prepared"] = {}
    runstore.write_record(fx.directory(), record)
    scope, source = handoff.resolve_profile(fx.root, fx.task, None, evaluate=evaluator)
    assert (scope["profile"], source) == ("personal", "run-record")


def test_scope_from_compiled_over_a_hand_built_document():
    document = {
        "enabled_providers": ["b", "a"],
        "provider": {"a": {"whitelist": ["m2", "m1"]}, "b": {"whitelist": ["m3"]}},
        "agent": {"quoin-planner": {"model": "a/m1"}},
    }
    scope = handoff.scope_from_compiled("p", "c", document, {"max_run_seconds": 5})
    ceiling = scope["policy_ceiling"]
    assert ceiling["enabled_providers"] == ["a", "b"]
    assert ceiling["provider_allowlist"] == ["a/m1", "a/m2", "b/m3"]
    assert ceiling["role_models"] == {"quoin-planner": "a/m1"}
    assert set(ceiling["limits"]) == set(merge.LIMIT_NAMES) | {"subagent_depth"}
    assert ceiling["limits"]["max_run_seconds"] == 5 and ceiling["limits"]["subagent_depth"] == 1
    assert ceiling["network"] == "profile-default"


def test_scope_for_profile_over_a_real_world(tmp_path):
    world = mh.World(tmp_path, profile=mh.PROFILE_PERSONAL)
    clock = lambda: mh.NOW.timestamp()  # noqa: E731
    scope = handoff.scope_for_profile(world.root, "personal", env=world.env, home=world.home, clock=clock)
    assert scope["profile"] == "personal"
    ceiling = scope["policy_ceiling"]
    assert ceiling["enabled_providers"] and ceiling["provider_allowlist"]
    assert len(ceiling["role_models"]) >= 8
    assert set(ceiling["limits"]) == set(merge.LIMIT_NAMES) | {"subagent_depth"}
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.scope_for_profile(world.root, "no such profile", env=world.env, home=world.home, clock=clock)
    assert err.value.code == "scope-unavailable"
    assert "no such profile" not in err.value.message


def test_continuation_dir_refuses_symlinks(tmp_path):
    root = tmp_path / "p"
    (root / ".workflow_artifacts").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(root / ".workflow_artifacts" / "memory"))
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.continuation_dir(root, create=True)
    assert err.value.code == "unsafe-path"
    os.unlink(str(root / ".workflow_artifacts" / "memory"))
    (root / ".workflow_artifacts" / "memory").mkdir()
    os.symlink(str(elsewhere), str(root / ".workflow_artifacts" / "memory" / "continuation"))
    with pytest.raises(handoff.HandoffRefused):
        handoff.continuation_dir(root, create=True)


def test_modes_and_two_writes(fx):
    first = fx.build(decisions=["one"])
    path = handoff.write(fx.root, fx.task, first, source_dir=fx.source)
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(str(path.parent)).st_mode) == 0o700
    second = fx.build(decisions=["two"], previous=first)
    handoff.write(fx.root, fx.task, second, source_dir=fx.source)
    prev = json.loads(path.with_name(fx.task + ".prev.json").read_text())
    assert prev == first and json.loads(path.read_text()) == second


def test_write_refuses_an_invalid_record(fx):
    record = fx.build()
    record["task"] = "../x"
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.write(fx.root, fx.task, record, source_dir=fx.source)
    assert err.value.code == "record-invalid"


def test_finalized_and_missing_tasks_are_refused(fx):
    with pytest.raises(handoff.HandoffRefused) as err:
        handoff.build_record(fx.root, "nope", scope=hh.fixed_scope(), scope_source="x", source_dir=fx.source)
    assert err.value.code == "task-missing"
    g.write(fx.root / ".workflow_artifacts" / "finalized" / fx.task / "a.md", "x")
    with pytest.raises(handoff.HandoffRefused) as err:
        fx.build()
    assert err.value.code == "task-finalized"


def test_corrupt_state_is_refused(fx):
    runstore.workflow_state_path(fx.directory(), fx.task).write_text("{not json")
    with pytest.raises(handoff.HandoffRefused) as err:
        fx.build()
    assert err.value.code == "state-invalid"


def test_record_lives_below_every_cleanup_sweep(fx):
    text = CLEANUP_SKILL.read_text(encoding="utf-8")
    block = text.split("\n## Hardcoded sentinel allow-list", 1)[1].split("\n## ", 1)[0]
    families = re.findall(r"^\d+\.\s+`([^`]+)`", block, re.MULTILINE)
    families += ["run-state-*.json", "run-notes-*.md", "run-notes-*.md.1", "run-state-*.json.*.tmp"]
    assert len(families) >= 9
    path = handoff.write(fx.root, fx.task, fx.build(), source_dir=fx.source)
    handoff.write(fx.root, fx.task, fx.build(), source_dir=fx.source)
    memory = fx.root / ".workflow_artifacts" / "memory"
    assert path.relative_to(memory).parts[:-1] == ("continuation",)
    for name in (path.name, fx.task + ".prev.json"):
        for glob in families:
            assert not fnmatch.fnmatch(name, glob), (name, glob)
        assert not runstore._RECORD_FILE_RE.match(name)
    assert runstore.list_records(fx.directory()) == ([], 0)
