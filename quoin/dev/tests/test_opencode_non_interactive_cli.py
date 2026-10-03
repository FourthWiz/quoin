"""`quoin run --non-interactive` and the adopt advice after an approval stop."""
from __future__ import annotations

import json
import os

import pytest

from quoin import cli
from quoin.opencode_adapter import phase_loop, runstore

import _opencode_driver_helpers as h
from _opencode_run_helpers import InstalledProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def world(tmp_path, monkeypatch):
    holder = {}

    def make(scenario):
        project = InstalledProject(tmp_path, monkeypatch, scenario)
        holder["project"] = project
        monkeypatch.setattr(cli, "_make_opencode_driver", project.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        return project

    yield make
    if "project" in holder:
        holder["project"].cleanup()


def run(project, capsys, *extra, phase="plan"):
    argv = ["run", "demo", "--runtime", "opencode", "--profile", "work", "--phase", phase,
            "--project-root", str(project.root)] + list(extra)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured


@pytest.mark.parametrize("phase", ["plan", "implement"])
def test_marker_on_argv_only_with_the_flag(world, capsys, phase):
    project = world("record_only")
    run(project, capsys, phase=phase)
    run(project, capsys, "--new-run", phase=phase)
    run(project, capsys, "--new-run", "--non-interactive", phase=phase)
    last = lambda inv: inv["argv"][-1]  # noqa: E731
    args = [last(i) for i in project.invocations()]
    assert args[0] == "demo" and args[-1] == "demo (non-interactive run)"
    assert "demo (non-interactive run)" not in args[:-1]


def test_claude_runtime_rejects_the_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "demo", "--non-interactive"])
    assert "--non-interactive is only valid with --runtime opencode" in capsys.readouterr().err


def test_approval_stop_prints_adopt_advice_on_stderr(world, capsys):
    project = world("approval_tool_error")
    code, summary, captured = run(project, capsys, "--stage", "1", phase="implement")
    assert code == 4 and summary["outcome"] == "AWAITING_APPROVAL"
    assert "quoin opencode adopt --task demo --stage 1 --phase implement" in captured.err
    assert captured.err.count("or finish the phase in the TUI, then run:") == 1
    assert len(captured.out.strip().splitlines()) == 1
    result = phase_loop.PhaseResult(outcome="AWAITING_APPROVAL", reason="approval-required")
    ident = {"task": "demo", "stage": "1", "phase": "implement", "profile": "work"}
    assert summary["resume_hint"] == phase_loop.resume_hint(result, ident, project.root)
    state = runstore.load_workflow_state(runstore.store_dir(project.root), "demo")
    assert not (state or {}).get("entries")


def test_no_advice_for_other_outcomes(world, capsys):
    project = world("record_only")
    _, summary, captured = run(project, capsys, "--stage", "1", phase="implement")
    assert summary["outcome"] != "AWAITING_APPROVAL"
    assert "quoin opencode adopt" not in captured.err
