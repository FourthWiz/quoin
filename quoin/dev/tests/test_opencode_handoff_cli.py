"""`quoin opencode handoff write|show|validate` through `cli.main`."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil

import pytest

import _opencode_handoff_helpers as hh
import _opencode_merge_helpers as mh
import _opencode_run_helpers as rh
from quoin import cli
from quoin.opencode_adapter import handoff, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture()
def box(monkeypatch):
    """The scope every evaluation returns; tests change it between calls."""
    state = {"classification": "personal", "ceiling": {}}

    def fake(root, profile, **kw):
        return hh.fixed_scope(profile or "personal", state["classification"], **state["ceiling"])

    monkeypatch.setattr(handoff, "scope_for_profile", fake)
    return state


@pytest.fixture()
def fx(tmp_path, monkeypatch, box):
    project = hh.Project(tmp_path, monkeypatch)
    project.settings(profile="personal")
    project.seed_gate("architect", None, "PASS")
    return project


def call(capsys, verb, fx, *extra, task="t1"):
    code = cli.main(["opencode", "handoff", verb, "--task", task, "--project-root", str(fx.root), *extra])
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def record_file(fx, suffix=".json"):
    return fx.root / ".workflow_artifacts" / "memory" / "continuation" / ("t1" + suffix)


def refusal(data):
    assert data["outcome"] == "HANDOFF_REFUSED"
    return data["refusal"]


def test_write_validate_show(fx, capsys):
    code, data = call(capsys, "write", fx)
    assert code == 0 and data["outcome"] == "HANDOFF_WRITTEN"
    assert data["record"] == ".workflow_artifacts/memory/continuation/t1.json"
    assert data["phase"] == "implement" and data["scope_source"] == "workflow-state"
    assert data["completed"] == 2 and data["pending"] == 5 and data["unrecorded"] == 1
    assert handoff.core(fx.source).validate(json.loads(record_file(fx).read_text())) == []
    code, data = call(capsys, "validate", fx)
    assert code == 0 and data["outcome"] == "RECORD_VALID"
    code, data = call(capsys, "show", fx)
    assert code == 0 and data["outcome"] == "CONTINUATION_READY"
    assert data["origin_runtime"] == "opencode"
    assert data["next"]["steps"] == [
        "quoin run --runtime opencode --profile personal --phase implement --stage 1 t1 --project-root %s"
        % shlex.quote(str(fx.root))
    ]
    assert data["native_resume"] is False


def test_second_write_keeps_the_first_and_its_decisions(fx, capsys):
    call(capsys, "write", fx, "--decision", "use the cache", "--note", "first note")
    first = record_file(fx).read_bytes()
    call(capsys, "write", fx, "--decision", "drop the flag")
    assert record_file(fx, ".prev.json").read_bytes() == first
    texts = [d["text"] for d in json.loads(record_file(fx).read_text())["decisions"]]
    assert texts == ["use the cache", "drop the flag"]
    assert json.loads(record_file(fx).read_text())["notes"] == ["first note"]


def test_decision_with_a_secret_is_stored_redacted(fx, capsys):
    call(capsys, "write", fx)
    code, data = call(capsys, "write", fx, "--decision", "token " + rh.SEEDED_SECRET)
    out = json.dumps(data)
    assert code == 0 and rh.SEEDED_SECRET not in out
    for suffix in (".json", ".prev.json"):
        assert rh.SEEDED_SECRET not in record_file(fx, suffix).read_text()
    assert "<redacted>" in record_file(fx).read_text()


def test_text_caps(fx, capsys):
    code, data = call(capsys, "write", fx, "--decision", "x" * 2001)
    assert code == 2 and refusal(data)["code"] == "argument-invalid"
    args = [a for i in range(21) for a in ("--note", "n%d" % i)]
    code, data = call(capsys, "write", fx, *args)
    assert code == 2 and refusal(data)["code"] == "argument-invalid"
    assert not record_file(fx).exists()


def test_profile_mismatch_and_unknown(tmp_path, monkeypatch, box, capsys):
    project = hh.Project(tmp_path, monkeypatch)
    code, data = call(capsys, "write", project)
    assert code == 2 and refusal(data)["code"] == "profile-unknown"
    project.seed_run("implement", "1", "completed", profile="personal")
    code, data = call(capsys, "write", project, "--profile", "work")
    assert code == 2 and refusal(data)["code"] == "profile-mismatch"
    code, data = call(capsys, "write", project, "--profile", "personal")
    assert code == 0 and data["scope_source"] == "run-record"


def test_lock_held_refuses_without_writing(fx, capsys):
    paths = cli._supervisor_paths(fx.root, "t1")
    paths["memory_dir"].mkdir(parents=True, exist_ok=True)
    paths["lock"].write_text(json.dumps({
        "pid": os.getppid(), "started_at": "2026-01-01T00:00:00Z", "granted": 0, "writer": "cli",
        "task": "t1", "runtime": "opencode",
    }) + "\n")
    code, data = call(capsys, "write", fx)
    assert code == 3 and refusal(data)["code"] == "lock-held"
    assert not record_file(fx).exists()


def test_source_dir_without_the_core_script(fx, capsys, tmp_path):
    bare = tmp_path / "bare"
    (bare / "skills").mkdir(parents=True)
    for extra in (str(bare), str(tmp_path / "missing")):
        code, data = call(capsys, "write", fx, "--source-dir", extra)
        assert code == 2 and refusal(data)["code"] == "source-unavailable"
    assert not record_file(fx).exists()


# ---------------------------------------------------------------------------
# show: advice
# ---------------------------------------------------------------------------


def gate_args(fx, phase, *extra, verb="gate"):
    argv = ["opencode", verb, "--task", "t1", "--phase", phase, "--project-root", str(fx.root)]
    return argv + list(extra)


def test_show_for_a_task_without_workflow_state(tmp_path, monkeypatch, box, capsys):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    assert project.state() is None
    code, written = call(capsys, "write", project, "--profile", "personal")
    assert code == 0 and written["unrecorded"] == 3 and written["scope_source"] == "operator-flag"
    code = cli.main(["opencode", "handoff", "show", "--task", "t1", "--project-root", str(project.root)])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert code == 0 and data["next"]["status"] == "unrecorded"
    assert "quoin opencode adopt --task t1 --phase architect" in out
    assert "quoin run" not in out


def test_show_after_adopt_and_a_failing_gate(tmp_path, monkeypatch, box, capsys):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    assert cli.main(gate_args(project, "architect", verb="adopt")) == 0
    capsys.readouterr()
    assert call(capsys, "write", project, "--profile", "personal")[0] == 0
    code = cli.main(["opencode", "handoff", "show", "--task", "t1", "--project-root", str(project.root)])
    out = capsys.readouterr().out
    assert json.loads(out)["next"]["status"] == "awaiting-gate"
    assert "opencode adopt" not in out
    arch = project.base / "architecture.md"
    arch.write_text(arch.read_text() + "\nlater edit\n")
    assert cli.main(gate_args(project, "architect", "--write")) == 7
    capsys.readouterr()
    code, data = call(capsys, "show", project)
    nxt = data["next"]
    assert code == 0 and nxt["status"] == "gate-failed" and nxt["reasons"]
    assert nxt["steps"] == [handoff.gate.adopt_command("t1", None, "architect", project.root),
                            handoff.gate_write_command("t1", None, "architect", project.root)]


def test_show_with_an_open_run(fx, capsys):
    fx.seed_run("implement", "1", "interrupted", checkpoint=True)
    call(capsys, "write", fx)
    code, data = call(capsys, "show", fx)
    nxt = data["next"]
    assert code == 0 and nxt["status"] == "run-open"
    assert nxt["steps"] == [
        "quoin run --runtime opencode --profile personal --phase implement --stage 1 t1 --project-root %s"
        % shlex.quote(str(fx.root))
    ]
    assert data["native_resume"] is True
    assert "opencode adopt" not in json.dumps(data)


def test_show_after_completed_runs_offers_adopt_not_run(fx, capsys):
    fx.seed_run("implement", "1", "completed")
    call(capsys, "write", fx)
    for _ in range(2):
        code = cli.main(["opencode", "handoff", "show", "--task", "t1", "--project-root", str(fx.root)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert code == 0 and data["next"]["status"] == "run-completed"
        assert data["next"]["steps"] == [
            handoff.gate.adopt_command("t1", 1, "implement", fx.root),
            handoff.gate_write_command("t1", 1, "implement", fx.root),
        ]
        assert "quoin run" not in out
        fx.seed_run("review", "1", "completed")


@pytest.mark.parametrize("variant", ["interrupted", "blocked", "completed", "failed", "running"])
def test_native_resume_flag(fx, capsys, variant):
    kw = {"checkpoint": True}
    state = "interrupted"
    if variant == "blocked":
        kw["resume_blocked"] = "session-lost"
    elif variant != "interrupted":
        state = variant
    fx.seed_run("implement", "1", state, **kw)
    call(capsys, "write", fx)
    code, data = call(capsys, "show", fx)
    assert code == 0 and data["native_resume"] is (variant == "interrupted")


def test_store_problems_are_refused_not_guessed(fx, capsys):
    call(capsys, "write", fx)
    directory = fx.directory()
    state_file = runstore.workflow_state_path(directory, "t1")
    good = state_file.read_bytes()
    state_file.write_text("{broken")
    code, data = call(capsys, "show", fx)
    assert code == 2 and refusal(data)["code"] == "state-invalid"
    state_file.write_bytes(good)
    moved = directory.parent / "moved-store"
    directory.rename(moved)
    directory.symlink_to(moved)
    code, data = call(capsys, "show", fx)
    assert code == 2 and refusal(data)["code"] == "store-unreadable"
    directory.unlink()
    shutil.rmtree(moved)
    code, data = call(capsys, "show", fx)
    assert code == 0 and data["native_resume"] is False


def test_show_refusals_are_actionable(fx, capsys):
    code, data = call(capsys, "show", fx)
    assert code == 2 and refusal(data)["code"] == "continuation-missing"
    assert "handoff write" in refusal(data)["message"]
    checkpoint = fx.root / ".workflow_artifacts" / "memory" / "checkpoints" / "2026-01-01T0900-t1.md"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("x")
    code, data = call(capsys, "show", fx)
    assert code == 2 and refusal(data)["code"] == "continuation-legacy-format"
    assert "2026-01-01T0900-t1.md" in refusal(data)["message"]
    checkpoint.unlink()
    call(capsys, "write", fx)
    (fx.root / ".workflow_artifacts" / "finalized" / "t1").mkdir(parents=True)
    code, data = call(capsys, "show", fx)
    assert code == 2 and refusal(data)["code"] == "task-finalized"
    assert "finalized" in refusal(data)["message"]


def test_show_scope_checks(fx, box, capsys):
    call(capsys, "write", fx)
    code, data = call(capsys, "show", fx, "--profile", "work")
    assert code == 2 and refusal(data)["code"] == "profile-mismatch"
    box["classification"] = "work"
    code, data = call(capsys, "show", fx, "--profile", "personal")
    assert code == 2 and refusal(data)["code"] == "classification-mismatch"
    box["classification"] = "personal"
    box["ceiling"] = {"enabled_providers": ["alpha", "beta"]}
    code, data = call(capsys, "show", fx, "--profile", "personal")
    assert code == 2 and refusal(data)["code"] == "policy-widened"
    box["ceiling"] = {"enabled_providers": [], "provider_allowlist": []}
    code, data = call(capsys, "show", fx, "--profile", "personal")
    assert code == 0


def test_validate_names_the_reasons(fx, capsys):
    call(capsys, "write", fx)
    data = json.loads(record_file(fx).read_text())
    data.pop("decisions")
    record_file(fx).write_text(json.dumps(data))
    code, out = call(capsys, "validate", fx)
    assert code == 2 and refusal(out)["code"] == "continuation-invalid"
    assert "field-missing:decisions" in refusal(out)["reasons"]


def test_validate_missing_record(fx, capsys):
    code, data = call(capsys, "validate", fx)
    assert code == 2 and refusal(data)["code"] == "continuation-missing"


def test_invalid_task_name(fx, capsys):
    code, data = call(capsys, "write", fx, task="../x")
    assert code == 2 and refusal(data)["code"] == "invalid-task-name"


# ---------------------------------------------------------------------------
# a real configuration, and the parser surface
# ---------------------------------------------------------------------------


def test_write_with_a_real_configuration(tmp_path, monkeypatch, capsys):
    project = hh.Project(tmp_path / "p", monkeypatch)
    world = mh.World(tmp_path / "w", profile=mh.PROFILE_PERSONAL, root=project.root)
    monkeypatch.setattr(cli, "_opencode_config_env", lambda: dict(world.env))
    monkeypatch.setattr(cli.pathlib.Path, "home", classmethod(lambda cls: world.home))
    real = handoff.scope_for_profile
    monkeypatch.setattr(
        handoff, "scope_for_profile",
        lambda root, profile, **kw: real(root, profile, **{**kw, "clock": lambda: mh.NOW.timestamp()}),
    )
    code, data = call(capsys, "write", project, "--profile", "personal")
    assert code == 0 and data["scope_source"] == "operator-flag"
    saved = json.loads(record_file(project).read_text())
    assert saved["scope"]["profile"] == "personal" and saved["scope"]["policy_ceiling"]["role_models"]
    assert handoff.core(project.source).validate(saved) == []
    code, data = call(capsys, "show", project, "--profile", "personal")
    assert code == 0


def test_parser_exposes_exactly_the_documented_options(tmp_path, monkeypatch):
    parsers = []
    original = argparse.ArgumentParser.parse_args

    def spy(self, *a, **k):
        parsers.append(self)
        return original(self, *a, **k)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    cli.main(["opencode", "handoff", "validate", "--task", "t1", "--project-root", str(tmp_path)])
    top = parsers[0]

    def sub(parser, name):
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return action.choices[name], action

    opencode, _ = sub(top, "opencode")
    handoff_p, action = sub(opencode, "handoff")
    _, verbs = sub(handoff_p, "write")
    assert sorted(verbs.choices) == ["show", "validate", "write"]
    assert verbs.required is True

    def options(name):
        return {s for a in verbs.choices[name]._actions for s in a.option_strings if s != "-h" and s != "--help"}

    assert options("write") == {"--task", "--project-root", "--source-dir", "--profile", "--decision", "--note"}
    assert options("show") == {"--task", "--project-root", "--source-dir", "--profile"}
    assert options("validate") == {"--task", "--project-root", "--source-dir"}
