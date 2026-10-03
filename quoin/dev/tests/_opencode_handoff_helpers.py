"""Fixtures shared by the continuation-record tests (not collected)."""
from __future__ import annotations

import itertools
from pathlib import Path

import _opencode_gate_helpers as g
import _opencode_helpers as helpers
from quoin.opencode_adapter import handoff, merge, runstore

SOURCE_DIR = helpers.SOURCE_DIR
_COUNTER = itertools.count(1)


def fixed_scope(profile="personal", classification="personal", **ceiling):
    """A policy scope using the real limit names, so injected scopes exercise
    the same vocabulary as evaluated ones."""
    limits = {name: None for name in merge.LIMIT_NAMES}
    limits["max_run_seconds"] = 600
    limits["subagent_depth"] = handoff.SUBAGENT_DEPTH
    policy = {
        "enabled_providers": ["alpha"],
        "provider_allowlist": ["alpha/model-one"],
        "role_models": {"coordinator": "alpha/model-one"},
        "limits": limits,
        "network": "profile-default",
    }
    policy.update(ceiling)
    return {"profile": profile, "classification": classification, "policy_ceiling": policy}


class Project(g.Fixture):
    """A git project with a two-stage architecture, a stage-1 plan and a
    workflow state whose stage-1 plan entry has a PASS gate."""

    def __init__(self, tmp_path, monkeypatch, *, passed=True):
        super().__init__(tmp_path, monkeypatch)
        self.source = SOURCE_DIR
        if passed:
            self.seed_gate("plan", 1, "PASS")

    def seed_gate(self, phase, stage, verdict, reasons=(), origin="adopted"):
        state = self.state()
        entry = runstore.current_entry(state, stage, phase) if state is not None else None
        if entry is None:
            self.record(phase, stage=stage, origin=origin)
        directory = runstore.store_dir(self.root, create=True)
        state = runstore.load_workflow_state(directory, self.task)
        runstore.update_current_entry(state, stage, phase, gate={
            "verdict": verdict, "reasons": list(reasons), "warnings": [], "artifact": "x", "artifact_sha256": "0" * 64,
            "evaluated_at": "2026-01-01T00:00:00Z",
        })
        runstore.write_workflow_state(directory, state)

    def state(self):
        directory = runstore.inspect_store(self.root)
        return None if directory is None else runstore.load_workflow_state(directory, self.task)

    def directory(self):
        return runstore.store_dir(self.root, create=True)

    def build(self, **kw):
        kw.setdefault("scope", fixed_scope())
        kw.setdefault("scope_source", "workflow-state")
        kw.setdefault("source_dir", self.source)
        kw.setdefault("clock", self.clock)
        return handoff.build_record(self.root, self.task, **kw)

    def seed_run(self, phase, stage, state, *, profile="personal", pointer=True, resume_blocked=None,
                 driver_lost=False, session="ses_abc", checkpoint=False):
        directory = self.directory()
        number = next(_COUNTER)
        base = 1_900_000_000 + number

        def clock():
            return float(base)

        run_id = runstore.new_run_id(clock)
        request = g.run_request(self.root, self.task, stage, phase)
        request["profile"] = profile
        record = runstore.new_run_record(run_id, self.task, request, {"profile": profile} if profile else {}, clock)
        record["state"] = state
        record["resume_blocked"] = resume_blocked
        if driver_lost:
            record["attempts"] = [{"attempt": 1, "driver_lost": True}]
        runstore.write_record(directory, record)
        if checkpoint:
            runstore.write_checkpoint(directory, runstore.new_checkpoint(
                run_id, 1, last_sequence=3, sidecar_offset=0, native_session_id=session, repo_revisions=[],
                step_open=False, ran_anything=True, state_changing_part_ids=[], clock=clock,
            ))
        if pointer:
            runstore.write_pointer(directory, runstore.new_pointer(self.task, run_id, clock))
        return run_id


def record_for(project, **overrides):
    record = project.build()
    record.update(overrides)
    return record
