"""Fixtures shared by the evidence and gate tests (not collected)."""
from __future__ import annotations

import functools
import os
import subprocess
import time
from pathlib import Path

from quoin.opencode_adapter import driver, runstore

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def isolate_git(monkeypatch, home) -> None:
    monkeypatch.setenv("HOME", str(home))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode()


def make_repo(path, files=None) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, text in (files or {"src/x.py": "x = 1\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def clock_at(value=None):
    base = time.time() if value is None else value
    return lambda: base


def patch_runstore(monkeypatch, **kwargs) -> None:
    """Make `repo_revisions` and `hash_inputs` default to the given limits."""
    for name in ("repo_revisions", "hash_inputs"):
        wanted = {k: v for k, v in kwargs.items() if k in _PARAMS[name]}
        if wanted:
            monkeypatch.setattr(runstore, name, functools.partial(getattr(runstore, name), **wanted))


_PARAMS = {
    "repo_revisions": {"max_source_bytes", "max_untracked_files", "budget_s"},
    "hash_inputs": {"max_files", "max_file_bytes", "max_total_bytes"},
}


# ---------------------------------------------------------------------------
# task artifacts that pass strict validation
# ---------------------------------------------------------------------------

FOR_HUMAN = "## For human\n\nA short summary for the reader.\n\n"

ARCHITECTURE = (
    "---\ntask: fixture\n---\n" + FOR_HUMAN
    + "## Context\n\ntext\n\n## Current state\n\ntext\n\n## Proposed architecture\n\ntext\n\n"
    "## Risk register\n\ntext\n\n## Stage decomposition\n\n"
    "1. S-1: First stage\n2. S-2: Second stage\n"
)

PLAN = (
    "---\ntask: fixture\n---\n" + FOR_HUMAN
    + "## State\n\ntext\n\n## Tasks\n\nwork\n\n## Risks\n\nnone\n"
)

CRITIC_PASS = (
    "## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Summary\n\ntext\n\n## Issues\n\nnone\n\n"
    "## What's good\n\ntext\n\n## Scorecard\n\ntext\n"
)
CRITIC_REVISE = CRITIC_PASS.replace("PASS", "REVISE")

REVIEW = (
    "---\ntask: fixture\n---\n" + FOR_HUMAN
    + "## Summary\n\ntext\n\n## Verdict\n\nAPPROVED\n\n## Plan Compliance\n\ntext\n\n"
    "## Issues Found\n\nnone\n\n## Integration Safety\n\ntext\n\n## Test Coverage\n\ntext\n\n"
    "## Risk Assessment\n\ntext\n\n## Dimension Verdicts\n\n| Dimension | Verdict |\n|---|---|\n| all | ok |\n"
)

DISCOVER_TEXT = {
    "repos-inventory.md": "# Repositories\n\ntext\n",
    "architecture-overview.md": "# Overview\n\n## System purpose\n\ntext\n",
    "dependencies-map.md": "# Dependencies\n\n## Dependency graph\n\ntext\n",
}


def write(path, text) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def task_dir(root, task) -> Path:
    return Path(root) / ".workflow_artifacts" / task


def build_task(root, task="t1", *, critic=CRITIC_PASS, review=REVIEW, discover=False) -> Path:
    """A multi-stage task with every gated phase's artifact in place."""
    base = task_dir(root, task)
    write(base / "architecture.md", ARCHITECTURE)
    write(base / "stage-1" / "current-plan.md", PLAN)
    if critic is not None:
        write(base / "stage-1" / "critic-response-1.md", critic)
    if review is not None:
        write(base / "stage-1" / "review-1.md", review)
    if discover:
        for name, text in DISCOVER_TEXT.items():
            write(Path(root) / ".workflow_artifacts" / "memory" / name, text)
    return base


def run_request(root, task, stage, phase):
    """The request mapping exactly as the driver stores it."""
    request = driver.RunRequest(
        project_root=Path(root), task=task, stage=stage, phase=phase, profile="p", budget=None,
    )
    return {
        "task": request.task, "stage": request.stage, "phase": request.phase, "profile": request.profile,
        "effort": request.effort, "timeout_s": request.timeout_s, "budget": request.budget,
        "context_refs": list(request.context_refs),
    }


def make_run(root, task="t1", *, stage="1", phase="plan", state="completed", evidence="full", task_override=None) -> str:
    """Write a finished run record and return its id."""
    directory = runstore.store_dir(root, create=True)
    run_id = runstore.new_run_id()
    record = runstore.new_run_record(
        run_id, task_override or task, run_request(root, task, stage, phase), {},
    )
    record["state"] = state
    record["outcome"] = {
        "state": state, "evidence": evidence, "reason": None, "exit_code": 0, "signal": None,
        "new_native_events": 1,
    }
    runstore.write_record(directory, record)
    return run_id


# ---------------------------------------------------------------------------
# recorded evidence
# ---------------------------------------------------------------------------


class Fixture:
    """A git project holding a complete multi-stage task."""

    def __init__(self, tmp_path, monkeypatch):
        isolate_git(monkeypatch, tmp_path / "home")
        self.root = make_repo(tmp_path / "proj")
        self.task = "t1"
        self.clock = clock_at()
        self.base = build_task(self.root, self.task, discover=True)

    def snapshot(self, phase):
        from quoin.opencode_adapter import evidence

        return evidence.take_snapshot(self.root, self.task, phase)

    def record(self, phase, *, stage=1, origin="coordinator", runs=None, boundary="ok", **fields):
        from quoin.opencode_adapter import evidence

        critic = self.base / "stage-1" / "critic-response-1.md"
        if phase == "plan" and origin == "coordinator" and "critic_responses" not in fields and critic.exists():
            fields["critic_responses"] = [str(critic)]
        if runs is None and origin in ("coordinator", "phase-run"):
            runs = [make_run(self.root, self.task, stage=None if stage is None else str(stage), phase=phase)]
        return evidence.record_evidence(
            self.root, self.task, stage, phase, origin, self.snapshot(phase),
            runs=runs or (), boundary=boundary, clock=self.clock, **fields,
        )

    def settings(self, **values):
        directory = runstore.store_dir(self.root, create=True)
        state = runstore.load_workflow_state(directory, self.task) or runstore.new_workflow_state(self.task, self.clock)
        state["settings"].update(values)
        runstore.write_workflow_state(directory, state)

    def evaluate(self, phase, *, stage=1, **kw):
        from quoin.opencode_adapter import gate

        from pathlib import Path as _P

        source = _P(__file__).resolve().parent.parent.parent
        return gate.evaluate(self.root, self.task, stage, phase, source_dir=source, clock=self.clock, **kw)
