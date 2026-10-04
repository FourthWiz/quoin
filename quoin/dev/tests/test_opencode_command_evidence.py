"""Evidence for the commands whose behaviour lives in a helper the earlier
suites do not name: quoin-gate, quoin-checkpoint and quoin-continue-work.

Each command is rendered into an installed project bound to its role, and the
helper it drives (`quoin opencode gate`, `handoff write|validate`,
`workflow next`) runs against a project the coordinator has just worked on.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

import _opencode_workflow_e2e_helpers as eh
from quoin import cli

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

BINDINGS = {"quoin-gate": "quoin-gate", "quoin-checkpoint": "quoin-coordinator",
            "quoin-continue-work": "quoin-coordinator"}


@pytest.fixture
def proj(tmp_path, monkeypatch):
    project = eh.WorkflowProject(tmp_path, monkeypatch)
    yield project
    project.cleanup()


@pytest.mark.parametrize("command", sorted(BINDINGS))
def test_the_command_and_skill_are_rendered_and_bound_to_their_role(proj, command):
    text = (proj.root / ".opencode" / "commands" / (command + ".md")).read_text(encoding="utf-8")
    assert 'agent: "%s"' % BINDINGS[command] in text
    assert "Load the %s skill" % command in text
    assert (proj.root / ".opencode" / "skills" / command / "SKILL.md").is_file()


def run_cli(proj, capsys, *argv):
    code = cli.main([*argv])
    return code, capsys.readouterr().out


def test_quoin_gate_helper_reports_a_verdict_for_a_worked_phase(proj, capsys):
    code, summary = proj.run_workflow(capsys, "--from-discover", "--no-pause", "--through", "plan")
    assert code == 0, json.dumps(summary)
    code, out = run_cli(proj, capsys, "opencode", "gate", "--task", eh.TASK, "--stage", "1",
                        "--phase", "plan", "--project-root", str(proj.root))
    assert code == 0 and "PASS" in out, out


def test_quoin_checkpoint_helper_writes_and_validates_the_record(proj, capsys):
    code, summary = proj.run_workflow(capsys, "--from-discover", "--no-pause", "--through", "plan")
    assert code == 0, json.dumps(summary)
    for verb in ("write", "validate"):
        code, out = run_cli(proj, capsys, "opencode", "handoff", verb, "--task", eh.TASK,
                            "--project-root", str(proj.root))
        assert code == 0, (verb, out)
    record = proj.root / ".workflow_artifacts" / "memory" / "continuation" / (eh.TASK + ".json")
    assert record.is_file()


def test_quoin_continue_work_helper_names_the_next_step(proj, capsys):
    code, summary = proj.run_workflow(capsys, "--from-discover", "--no-pause", "--through", "plan")
    assert code == 0, json.dumps(summary)
    code, out = run_cli(proj, capsys, "opencode", "workflow", "next", "--task", eh.TASK,
                        "--project-root", str(proj.root))
    data = json.loads(out.strip().splitlines()[-1])
    assert code == 0 and data["outcome"] == "NEXT_STEP", data
    assert data["next"]
