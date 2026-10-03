"""Overlay rewrites of the generated skills: cost-row text, run command, notes."""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin import cli
from quoin.opencode_adapter import generate, install, scripts

SOURCE_DIR = helpers.SOURCE_DIR

PHASE_SKILLS = (
    "architect", "checkpoint", "continue_work", "critic", "discover", "end_of_task",
    "gate", "implement", "plan", "review", "thorough_plan",
)
NON_INTERACTIVE_ENTRIES = (
    "architect", "checkpoint", "critic", "discover", "end_of_task", "gate",
    "implement", "plan", "review", "thorough_plan",
)
MARKER_SENTENCE = "ask no questions, and if you cannot continue without an answer, stop and say what is missing"
LEDGER_RESIDUE = (
    "Cost-ledger writes are",
    "Cost-ledger write is",
    "MUST append a row",
    "MUST append the session's cost-ledger row",
    "MUST be logged to the cost ledger",
)
RUN_RESIDUE = (
    "invoke the implementation",
    "offer/run the spec phase",
    "MUST run finalization",
    "pause at every phase boundary",
    "chain phases",
    "separate session",
    "session-state file after each phase",
    "full pipeline",
    "routing sub-skills",
)
RECORDS_SENTENCE = re.compile(r"(runtime records|runtime reads|cost row)", re.IGNORECASE)


@pytest.fixture(scope="module")
def rendered():
    files = generate.render_source_dir(SOURCE_DIR)
    return {k: v.content.decode("utf-8") for k, v in files.items()}


def skill(rendered, cid):
    return rendered[".opencode/skills/quoin-%s/SKILL.md" % cid.replace("_", "-")]


def test_no_phase_skill_tells_the_model_to_write_the_cost_ledger(rendered):
    for cid in PHASE_SKILLS:
        text = skill(rendered, cid)
        for residue in LEDGER_RESIDUE:
            assert residue not in text, (cid, residue)
    for cid in ("architect", "plan", "critic", "thorough_plan", "discover", "implement"):
        assert "The runtime records this run's cost row" in skill(rendered, cid), cid
    for cid in ("review", "gate", "checkpoint"):
        assert "The runtime records this run's cost row; do not edit the cost ledger." in skill(rendered, cid), cid
    for cid in ("end_of_task",):
        assert "The runtime records this run's cost row before launch" in skill(rendered, cid)
    assert "do not edit that file" in skill(rendered, "architect")


def test_non_interactive_note_is_on_exactly_the_listed_entries(rendered):
    for cid in PHASE_SKILLS + ("run",):
        has = MARKER_SENTENCE in skill(rendered, cid)
        assert has == (cid in NON_INTERACTIVE_ENTRIES), cid


def test_implement_names_test_run_and_no_commit(rendered):
    text = skill(rendered, "implement")
    assert "quoin opencode test-run --task TASK --stage N" in text
    assert "never commit" in text
    assert "skip the branch checks" in text


def test_helper_commands_are_named(rendered):
    gate = skill(rendered, "gate")
    assert "quoin opencode gate --task TASK [--stage N] --phase PHASE --write" in gate
    assert "never run that command yourself" in gate
    assert "task lock is held" in gate
    checkpoint = skill(rendered, "checkpoint")
    assert "quoin opencode handoff write --task TASK" in checkpoint
    assert "--decision" in checkpoint and "--note" in checkpoint
    assert "task lock is held" in checkpoint
    cont = skill(rendered, "continue_work")
    assert "quoin opencode handoff show --task TASK" in cont
    assert "never read transcripts" in cont
    run = skill(rendered, "run")
    assert "quoin opencode workflow next --task TASK" in run


def test_no_skill_tells_an_agent_to_run_adopt_itself(rendered):
    for relpath, text in rendered.items():
        for line in text.splitlines():
            if "quoin opencode adopt" in line:
                assert "never run" in line or "tell the user" in line, (relpath, line)


def test_rendered_run_skill_has_no_orchestration_text(rendered):
    text = skill(rendered, "run")
    for residue in RUN_RESIDUE:
        assert residue not in text, residue
    assert "never runs a phase itself" in text
    assert "Never invoke implementation or finalization." in text
    assert "Never edit the cost ledger or any task artifact." in text
    assert "Never edit the session-state file." in text


def test_run_command_binds_to_the_coordinator(rendered):
    command = rendered[".opencode/commands/quoin-run.md"]
    assert re.search(r'(?m)^agent: "quoin-coordinator"$', command)
    assert "only reports the next step" in command


def test_new_texts_pass_the_residue_words():
    overlays = json.loads((helpers.OPENCODE_DIR / "overlays.json").read_text(encoding="utf-8"))
    tier = re.compile(r"\b(haiku|sonnet|opus)\b", re.IGNORECASE)
    script_py = tuple("%s.py" % n for n in scripts.ALLOWED_SCRIPTS)
    new_texts = []
    for cid, entry in overlays["entries"].items():
        new_texts += entry["notes"] + [entry["command_note"], entry["description"]]
        for rw in entry["rewrites"]:
            if "cost" in rw["to"].lower() or cid == "run" or "run" in rw["to"].lower():
                new_texts.append(rw["to"])
    for text in new_texts:
        assert "JSONL" not in text
        assert not re.search(r"\bhooks?\b", text, re.IGNORECASE), text
        assert not tier.search(text), text
        assert not any(name in text for name in script_py), text
        assert "model diversity" not in text.replace("not model diversity", "")


def test_install_twice_is_byte_identical_with_run_installed(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    out, err = io.StringIO(), io.StringIO()
    assert install.run_install(str(root), SOURCE_DIR, None, False, out, err) == 0, err.getvalue()
    assert (root / ".opencode" / "commands" / "quoin-run.md").is_file()
    assert (root / ".opencode" / "skills" / "quoin-run" / "SKILL.md").is_file()

    def snap():
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
        }

    first = snap()
    assert install.run_install(str(root), SOURCE_DIR, None, False, io.StringIO(), io.StringIO()) == 0
    assert snap() == first
    assert install.run_install(str(root), SOURCE_DIR, None, True, io.StringIO(), io.StringIO()) == 0


def test_whole_task_run_phase_still_refuses(tmp_path, capsys):
    root = tmp_path / "project"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    code = cli.main([
        "run", "demo", "--runtime", "opencode", "--project-root", str(root),
        "--profile", "work", "--phase", "run",
    ])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3
    assert summary["refusal"]["code"] == "whole-task-unavailable"


@pytest.mark.parametrize("verb,extra", [("gate", ("--phase", "plan", "--write")), ("handoff", ())])
def test_headless_helper_is_refused_while_the_task_lock_is_held(tmp_path, monkeypatch, capsys, verb, extra):
    import _opencode_gate_helpers as h

    fx = h.Fixture(tmp_path, monkeypatch)
    paths = cli._supervisor_paths(fx.root, "t1")
    paths["memory_dir"].mkdir(parents=True, exist_ok=True)
    paths["lock"].write_text(json.dumps({
        "pid": os.getppid(), "started_at": "2026-01-01T00:00:00Z", "granted": 0, "writer": "cli",
        "task": "t1", "runtime": "opencode",
    }) + "\n")
    before = sorted(str(p.relative_to(fx.root)) for p in fx.root.rglob("*") if ".git" not in p.parts)
    if verb == "gate":
        argv = ["opencode", "gate", "--task", "t1", "--project-root", str(fx.root), "--stage", "1", *extra]
    else:
        argv = ["opencode", "handoff", "write", "--task", "t1", "--project-root", str(fx.root)]
    code = cli.main(argv)
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3
    assert json.dumps(data).count("lock-held") >= 1
    after = sorted(str(p.relative_to(fx.root)) for p in fx.root.rglob("*") if ".git" not in p.parts)
    assert after == before
