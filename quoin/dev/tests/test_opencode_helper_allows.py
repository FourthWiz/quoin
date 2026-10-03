"""Helper-command allows in the generated shell maps, and the parser and
explanation-file hardening that keeps them from reaching other trees,
projects or secret files."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import _opencode_gate_helpers as h
from quoin import cli
from quoin.opencode_adapter import boundaries, generate

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLES = generate.ROLES

GATE_CMD = "quoin opencode gate --task t --phase plan --write"
HANDOFF_CMD = "quoin opencode handoff write --task t"
NEXT_CMD = "quoin opencode workflow next --task t"
TEST_RUN_CMD = "quoin opencode test-run --task t"

ALLOWED = {
    "gate": {GATE_CMD},
    "coordinator": {GATE_CMD, HANDOFF_CMD, NEXT_CMD},
    "implementer": {TEST_RUN_CMD},
}
ALL_HELPER_CMDS = {GATE_CMD, HANDOFF_CMD, NEXT_CMD, TEST_RUN_CMD}


def action(role, command):
    return boundaries.evaluate_rule(generate.shell_rule(role), command)


@pytest.mark.parametrize("role", ROLES)
def test_each_role_allows_exactly_its_helper_forms(role):
    for command in sorted(ALL_HELPER_CMDS):
        expected = "allow" if command in ALLOWED.get(role, set()) else "ask"
        assert action(role, command) == expected, (role, command)


@pytest.mark.parametrize("role", sorted(ALLOWED))
def test_asked_arguments_ask_for_every_helper_role(role):
    for command in sorted(ALLOWED[role]):
        for tail in (
            "--source-dir /x",
            "--source-dir=/x",
            "--project-root /x",
            "--project-root=/x",
            "--explanation-file .env",
        ):
            assert action(role, "%s %s" % (command, tail)) == "ask", (role, command, tail)


@pytest.mark.parametrize("role", ROLES)
def test_human_only_operations_ask_for_every_role(role):
    for command in (
        "quoin opencode adopt --task t --phase plan",
        "quoin run --runtime opencode --workflow t --test-command x",
        "quoin run t --runtime opencode --phase plan --test-include env",
        "quoin run t --workflow --continue --rerun-from implement",
        "quoin run t --workflow --continue --adopt implement",
    ):
        assert action(role, command) == "ask", (role, command)


@pytest.mark.parametrize("role", ROLES)
def test_redirect_rules_stay_last(role):
    keys = list(generate.shell_rule(role))
    assert keys[-2:] == ["*>*", "*<*"]


def test_no_rendered_text_tells_an_agent_to_run_adopt_itself():
    files = generate.render_source_dir(REPO_ROOT / "quoin")
    for path, rendered in files.items():
        text = rendered.content.decode("utf-8")
        for line in text.splitlines():
            if re.search(r"quoin opencode adopt", line):
                assert re.search(r"\b(user|human|operator)\b", line, re.I), (path, line)


# -- parser and explanation-file behaviour through cli.main ---------------

pytestmark_cli = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOIN_OPENCODE_STATE_DIR", raising=False)
    return h.Fixture(tmp_path, monkeypatch)


def gate(capsys, fx, *extra):
    argv = ["opencode", "gate", "--task", fx.task, "--phase", "plan", "--stage", "1",
            "--project-root", str(fx.root), "--write"]
    code = cli.main(argv + list(extra))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out.startswith("{") else out


@pytestmark_cli
@pytest.mark.parametrize("flag", ["--proj", "--source"])
def test_abbreviated_options_are_argument_errors(fx, capsys, flag):
    fx.record("plan")
    with pytest.raises(SystemExit) as exc:
        cli.main(["opencode", "gate", "--task", fx.task, "--phase", "plan", "--write", flag, "/other"])
    assert exc.value.code == 2
    capsys.readouterr()


@pytestmark_cli
def test_handoff_and_test_run_reject_abbreviated_options(fx, capsys):
    for argv in (
        ["opencode", "handoff", "show", "--task", "t", "--proj", "/x"],
        ["opencode", "handoff", "write", "--task", "t", "--source", "/x"],
        ["opencode", "handoff", "validate", "--task", "t", "--proj", "/x"],
        ["opencode", "test-run", "--task", "t", "--sta", "1"],
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2, argv
    capsys.readouterr()


@pytestmark_cli
def test_launcher_refuses_an_explanation_outside_the_artifact_root(fx, capsys, tmp_path, monkeypatch):
    fx.record("plan")
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", str(tmp_path / "state"))
    note = tmp_path / "home" / "secret.txt"
    note.parent.mkdir()
    note.write_text("TOP-SECRET-CONTENT")
    code, data = gate(capsys, fx, "--explanation-file", str(note))
    assert code == 2 and data["refusal"]["code"] == "explanation-outside-artifacts"
    for path in fx.root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert "TOP-SECRET-CONTENT" not in path.read_text(errors="ignore"), path


@pytestmark_cli
def test_launcher_carries_an_explanation_inside_the_artifact_root(fx, capsys, tmp_path, monkeypatch):
    fx.record("plan")
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", str(tmp_path / "state"))
    note = fx.root / ".workflow_artifacts" / "note.txt"
    note.write_text("INSIDE-NOTE")
    code, data = gate(capsys, fx, "--explanation-file", str(note))
    assert code == 0 and data["artifact"]
    assert "INSIDE-NOTE" in (fx.root / data["artifact"]).read_text()


@pytestmark_cli
def test_launcher_still_reports_an_outside_symlink_as_unreadable(fx, capsys, tmp_path, monkeypatch):
    fx.record("plan")
    monkeypatch.setenv("QUOIN_OPENCODE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "elsewhere.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    code, data = gate(capsys, fx, "--explanation-file", str(link))
    assert code == 2 and data["refusal"]["code"] == "explanation-unreadable"


@pytestmark_cli
def test_human_path_carries_an_outside_explanation(fx, capsys, tmp_path):
    fx.record("plan")
    note = tmp_path / "home" / "note.txt"
    note.parent.mkdir()
    note.write_text("HUMAN-NOTE")
    code, data = gate(capsys, fx, "--explanation-file", str(note))
    assert code == 0
    assert "HUMAN-NOTE" in (fx.root / data["artifact"]).read_text()
