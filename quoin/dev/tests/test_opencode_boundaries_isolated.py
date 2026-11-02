"""The role-boundary scenarios of test_opencode_boundary_runs.py, re-run in a
world where isolation is verified, so a regression in the boundary check cannot
hide behind a launch that silently ran without the managed boundary.

The first test pins that isolation is really active in this world; the rest
reuse the same fixtures and assertions."""
from __future__ import annotations

import pytest

import _opencode_boundary_helpers as bh
from quoin.opencode_adapter import profile_dirs, runstore
from test_opencode_boundary_runs import (  # noqa: F401  (World is the shared fixture class)
    PLAN_REL, World, last_entry, writes,
)
import _opencode_gate_helpers as gh

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"), reason="process groups are POSIX-only")


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario="record_only"):
        proj = World(tmp_path, monkeypatch, scenario)
        holder.append(proj)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def test_isolation_is_active_in_this_world(make):
    proj = make("record_only")
    drv = proj.driver_factory()(proj.root)
    prepared = drv.prepare(proj.request("plan"))
    record = runstore.load_record(runstore.store_dir(proj.root), prepared.run_id)["prepared"]
    effective = record["effective_config"]
    assert effective["isolation"]["status"] == "verified"
    assert effective["isolation"]["environment"] == "container"
    assert effective["pending_sections"] == []
    enforcement = {row["control"]: row for row in effective["enforcement"]}
    assert enforcement["provider-destinations"]["status"] == "enforced"
    assert enforcement["shell-egress"]["status"] == "delegated-verified"
    assert enforcement["shell-egress"]["layer"] == "managed-boundary"
    names = set(record["env_names"])
    assert {"XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR", "XDG_DATA_HOME", "OPENCODE_CONFIG"} <= names
    assert {"OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_PURE"} <= names
    dirs = profile_dirs.ProfileDirs.at(profile_dirs.profile_root(drv.state_root, "work"))
    values = prepared.launch_env.materialize()
    expected = profile_dirs.child_storage_env(dirs)
    for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        assert values[name] == expected[name]


def _assert_isolated(proj):
    record = runstore.load_record(runstore.store_dir(proj.root), _latest_run(proj))
    assert record["prepared"]["effective_config"]["isolation"]["status"] == "verified"


def _latest_run(proj):
    pointer = runstore.load_pointer(runstore.store_dir(proj.root), bh.TASK)
    return pointer["run_id"]


def test_an_architect_that_edits_source_fails_and_the_gate_refuses(make, capsys):
    proj = make(writes(("src/app.py", "x = 2\n")))
    code, summary = proj.run_cli(capsys, "architect")
    assert code == 2 and summary["outcome"] == "FAILED" and summary["reason"] == "boundary-violation"
    assert last_entry(proj)["boundary"] == "violation"
    _assert_isolated(proj)
    code, data = proj.gate(capsys, "architect", None)
    assert "boundary-violation" in data["reasons"]


def test_an_implementer_that_edits_an_installed_command_is_a_violation(make, capsys):
    proj = make(("edits_path", {"path": ".opencode/commands/quoin-plan.md"}))
    code, summary = proj.run_cli(capsys, "implement")
    assert code == 2 and summary["reason"] == "boundary-violation"
    assert last_entry(proj)["boundary"] == "violation"
    _assert_isolated(proj)


def test_a_planner_that_writes_into_the_coordinator_store_is_a_violation(make, capsys):
    proj = make(writes((".workflow_artifacts/memory/runtime/opencode/stray-note.json", "{}")))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 2 and summary["reason"] == "boundary-violation"
    assert last_entry(proj)["boundary"] == "violation"
    _assert_isolated(proj)


def test_launch_time_managed_files_are_not_violations(make, capsys):
    proj = make(("opencode_managed_writes", {"extra": [[PLAN_REL, gh.PLAN + "\nmore\n"]]}))
    code, summary = proj.run_cli(capsys, "plan")
    assert code == 0 and summary["outcome"] == "COMPLETED"
    assert last_entry(proj)["boundary"] == "ok"
    _assert_isolated(proj)


def test_an_absolute_path_write_is_detected_after_the_run(make, capsys):
    """The write leaves the project through an absolute path to a file the
    boundary lists; it is found afterwards, never prevented (a residual)."""
    proj = make("record_only")
    target = str(proj.root / "src" / "app.py")
    proj.set_scenario(("writes_paths", {"paths": [[target, "x = 3\n"]]}))
    code, summary = proj.run_cli(capsys, "architect")
    assert code == 2 and summary["reason"] == "boundary-violation"
    assert (proj.root / "src" / "app.py").read_text() == "x = 3\n"
    assert last_entry(proj)["boundary"] == "violation"
    _assert_isolated(proj)
