"""Continuing a task from a continuation record with the real driver: foreign
records seed state, nothing outside the record is read, and every mismatch
refuses with its own code.

Commands proved here: quoin-implement, quoin-plan and the whole-task form
`quoin run --workflow --continue`.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
import shutil

import pytest

import _opencode_gate_helpers as gh
import _opencode_handoff_helpers as hh
import _opencode_workflow_e2e_helpers as eh
from _opencode_helpers import SOURCE_DIR
from quoin import cli
from quoin.opencode_adapter import handoff, runstore

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

TASK = eh.TASK
PLAN_PHASES = [("discover", None), ("architect", None), ("plan", 1)]


@pytest.fixture
def make(tmp_path, monkeypatch):
    holder = []

    def build(scenario=None, prebuilt=True):
        proj = eh.WorkflowProject(tmp_path, monkeypatch, scenario)
        holder.append(proj)
        if prebuilt:
            gh.build_task(proj.root, TASK, discover=True)
        return proj

    yield build
    for proj in holder:
        proj.cleanup()


def foreign(phases, **extra):
    return dict(
        origin_runtime="claude",
        completed=[{"phase": p, "stage": s, "run_id": None, "gate": "PASS"} for p, s in phases],
        validation=[{"phase": p, "stage": s, "verdict": "PASS", "reasons": []} for p, s in phases],
        **extra,
    )


def write_record(proj, **overrides):
    record = handoff.build_record(
        proj.root, TASK, scope=hh.fixed_scope(), scope_source="workflow-state", source_dir=SOURCE_DIR,
    )
    record.update(overrides)
    done = {(c["phase"], c["stage"]) for c in record["completed"]}
    record["pending"] = [p for p in record["pending"] if (p["phase"], p["stage"]) not in done]
    if record["pending"]:
        record["phase"] = {"current": record["pending"][0]["phase"], "stage": record["pending"][0]["stage"],
                           "status": "pending"}
    handoff.write(proj.root, TASK, record, source_dir=SOURCE_DIR)
    return record


def continue_run(proj, capsys, *extra):
    return proj.run_workflow(capsys, "--continue", "--no-pause", *extra)


def test_a_foreign_record_seeds_state_and_regates(make, capsys):
    proj = make()
    write_record(proj, **foreign(PLAN_PHASES))
    code, summary = continue_run(proj, capsys, "--through", "plan")
    assert code == 0, json.dumps(summary)
    entries = {(e["phase"], e["stage"]): e for e in proj.state()["entries"] if not e["superseded"]}
    assert entries[("plan", 1)]["origin"] == "continuation"
    assert entries[("plan", 1)]["gate"]["verdict"] == "PASS"
    assert proj.runs() == []


def test_seeded_implement_and_review_carry_the_seeded_test_result(make, capsys):
    proj = make()
    phases = PLAN_PHASES + [("implement", 1), ("review", 1)]
    write_record(proj, **foreign(phases))
    code, summary = continue_run(proj, capsys, "--through", "review", "--test-command", "sh -c true")
    assert code == 0, json.dumps(summary)
    state = proj.state()
    entries = {(e["phase"], e["stage"]): e for e in state["entries"] if not e["superseded"]}
    for phase in ("implement", "review"):
        entry = entries[(phase, 1)]
        assert entry["origin"] == "continuation", phase
        assert entry["tests"]["exit_code"] == 0, phase
    assert state["settings"]["test_command"] == ["sh", "-c", "true"]


class OpenSpy:
    def __init__(self, monkeypatch):
        self.paths = []
        for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
            self._wrap(monkeypatch, owner, name)
        original = pathlib.Path.open
        spy = self

        def path_open(path, *args, **kwargs):
            spy.paths.append(str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", path_open)

    def _wrap(self, monkeypatch, owner, name):
        original = getattr(owner, name)

        def wrapper(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                self.paths.append(os.fsdecode(file))
            return original(file, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)


def test_other_runtimes_transcripts_are_never_opened(make, capsys, monkeypatch):
    proj = make()
    home = proj.world.home
    poisoned = [
        home / ".claude" / "projects" / "x" / "session.jsonl",
        home / ".codex" / "sessions" / "x.jsonl",
        proj.root / ".workflow_artifacts" / "memory" / "sessions" / "2026-10-01-demo-codex.md",
        proj.root / ".workflow_artifacts" / "memory" / "recent-sessions.md",
    ]
    for path in poisoned:
        gh.write(path, "poisoned transcript for the continuation\n")
    write_record(proj, **foreign(PLAN_PHASES))
    spy = OpenSpy(monkeypatch)
    code, summary = continue_run(proj, capsys, "--through", "implement")
    assert code == 0, json.dumps(summary)
    opened = {os.path.realpath(p) for p in spy.paths}
    assert not opened & {os.path.realpath(str(p)) for p in poisoned}
    assert not any("/.claude/" in p or "/.codex/" in p for p in opened if str(proj.world.home) in p)
    runs = proj.runs()
    assert runs and "--session" not in runs[0]["argv"]
    assert runs[0]["session_id"]


def test_an_older_format_checkpoint_without_a_record_refuses(make, capsys):
    proj = make()
    gh.write(proj.root / ".workflow_artifacts" / "memory" / "checkpoints" / "2026-10-01T0930-demo.md", "old\n")
    names = handoff.legacy_markers(proj.root, TASK)
    assert names, "the older-format name pattern changed"
    code, summary = continue_run(proj, capsys)
    assert code == 3 and summary["reasons"] == ["continuation-legacy-format"], json.dumps(summary)
    assert proj.runs() == []


def test_a_finalized_task_refuses(make, capsys):
    proj = make()
    write_record(proj, **foreign(PLAN_PHASES))
    (proj.root / ".workflow_artifacts" / "finalized" / TASK).mkdir(parents=True)
    code, summary = continue_run(proj, capsys)
    assert code == 3 and summary["reasons"] == ["task-finalized"], json.dumps(summary)


def test_a_record_that_disagrees_with_state_refuses(make, capsys):
    proj = make(prebuilt=False)
    code, summary = proj.run_workflow(capsys, "--from-discover", "--no-pause", "--through", "plan")
    assert code == 0, json.dumps(summary)
    record = json.loads(handoff.record_path(proj.root, TASK).read_text(encoding="utf-8"))
    plan = ".workflow_artifacts/demo/stage-1/current-plan.md"
    for item in record["artifacts"]:
        if item["path"] == plan:
            item["sha256"] = "f" * 64
    handoff.write(proj.root, TASK, record, source_dir=SOURCE_DIR)
    code, summary = continue_run(proj, capsys)
    assert code == 3 and summary["reasons"] == ["continuation-state-mismatch"], json.dumps(summary)


def test_a_changed_artifact_refuses_seeding(make, capsys):
    proj = make()
    write_record(proj, **foreign(PLAN_PHASES))
    gh.write(proj.root / ".workflow_artifacts" / TASK / "stage-1" / "current-plan.md", gh.PLAN + "\nedited\n")
    code, summary = continue_run(proj, capsys)
    assert code == 3 and summary["reasons"] == ["continuation-artifact-changed"], json.dumps(summary)


@pytest.mark.parametrize("scope, code_name", [
    (dict(profile="other"), "profile-mismatch"),
    (dict(classification="regulated"), "classification-mismatch"),
])
def test_a_different_profile_or_classification_refuses(make, capsys, monkeypatch, scope, code_name):
    proj = make()
    write_record(proj, **foreign(PLAN_PHASES))
    other = hh.fixed_scope(**scope)
    monkeypatch.setattr(cli, "_handoff_evaluator", lambda root: (lambda profile: other))
    code, summary = continue_run(proj, capsys)
    assert code in (2, 3) and summary["reasons"] == [code_name], json.dumps(summary)
