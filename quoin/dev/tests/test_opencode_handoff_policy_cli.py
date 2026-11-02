"""Continuing a task whose recorded runs predate the effective-config record,
through the real CLI and driver."""
from __future__ import annotations

import json
import os
import shutil

import pytest

import _opencode_workflow_e2e_helpers as eh
from quoin.opencode_adapter import runstore

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only"),
]

TASK = eh.TASK
FIRST = ("--from-discover", "--no-pause", "--through", "plan")


@pytest.fixture
def started(tmp_path, monkeypatch, capsys):
    proj = eh.WorkflowProject(tmp_path, monkeypatch)
    code, summary = proj.run_workflow(capsys, *FIRST)
    assert code == 0, json.dumps(summary)
    yield proj
    proj.cleanup()


def edit_run_records(proj, edit):
    directory = runstore.store_dir(proj.root)
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("prepared"), dict) and "request" in record:
            edit(record["prepared"])
            runstore.write_record(directory, record)


def strip_records(proj):
    edit_run_records(proj, lambda prepared: prepared.pop("effective_config", None))


def cont(proj, capsys, *extra):
    return proj.run_workflow(capsys, "--continue", "--no-pause", "--through", "plan", *extra)


def test_a_pre_upgrade_run_needs_the_flag_once_and_the_flag_is_remembered(started, capsys):
    strip_records(started)
    code, summary = cont(started, capsys)
    assert summary["reasons"] == ["scope-unverifiable"] and code == 3, json.dumps(summary)
    assert "--accept-unverified-policy" in summary["refusal"]["message"]
    assert "--workflow --continue" in summary["refusal"]["message"]
    code, summary = cont(started, capsys, "--accept-unverified-policy")
    assert summary["outcome"] != "REFUSED", json.dumps(summary)
    assert "policy-unverified" in started.last_err
    accepted = started.state()["settings"]["policy_unverified_accepted"]
    assert set(accepted) == {"run_id"}
    code, summary = cont(started, capsys)
    assert summary["outcome"] != "REFUSED", json.dumps(summary)


def test_the_flag_never_bypasses_a_widened_policy(started, capsys):
    edit_run_records(started, lambda p: p["effective_config"]["policy"].__setitem__("retention.runs_days", 1))
    code, summary = cont(started, capsys, "--accept-unverified-policy")
    assert summary["reasons"] == ["policy-widened"], json.dumps(summary)


def test_the_flag_without_continue_exits_2(started, capsys):
    code, summary = started.run_workflow(capsys, "--accept-unverified-policy")
    assert code == 2 and summary["reasons"] == ["continue-required"], json.dumps(summary)


def test_the_flag_is_only_valid_with_workflow(started, capsys):
    with pytest.raises(SystemExit) as info:
        eh.cli.main(["run", TASK, "--runtime", "opencode", "--project-root", str(started.root),
                     "--profile", "work", "--phase", "plan", "--accept-unverified-policy"])
    assert info.value.code != 0
