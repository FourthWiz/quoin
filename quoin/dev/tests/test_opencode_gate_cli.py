"""`quoin opencode gate` and `quoin opencode adopt` through `cli.main`."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _opencode_gate_helpers as h
import _opencode_helpers as helpers
from quoin import cli
from quoin.opencode_adapter import runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
REPO_ROOT = Path(__file__).resolve().parents[3]
README = helpers.OPENCODE_DIR / "README.md"


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    return h.Fixture(tmp_path, monkeypatch)


def run(capsys, verb, fx, *extra, task=None, phase="plan", stage="1"):
    argv = ["opencode", verb, "--task", task or fx.task, "--phase", phase, "--project-root", str(fx.root)]
    if stage is not None:
        argv += ["--stage", stage]
    code = cli.main(argv + list(extra))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def tree(root):
    return sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns if p.is_file() else 0)
        for p in Path(root).rglob("*") if ".git" not in p.parts
    )


def test_gate_pass_exit_zero(fx, capsys):
    fx.record("plan")
    code, data = run(capsys, "gate", fx)
    assert code == 0 and data["verdict"] == "PASS" and data["outcome"] == "GATE_PASSED"
    assert data["exit_code"] == 0 and data["artifact"] is None and data["reasons"] == []


def test_gate_fail_exit_seven_with_reasons(fx, capsys):
    fx.record("plan")
    (fx.root / "src" / "x.py").write_text("x = 2\n")
    code, data = run(capsys, "gate", fx)
    assert code == 7 and data["outcome"] == "GATE_REFUSED" and data["reasons"] == ["repo-dirty-changed"]


def test_no_evidence_names_the_adopt_command(fx, capsys):
    code, data = run(capsys, "gate", fx)
    assert code == 7 and "evidence-missing" in data["reasons"]
    assert "quoin opencode adopt" in json.dumps(data)


def test_adopt_then_gate_write(fx, capsys):
    code, data = run(capsys, "adopt", fx)
    assert code == 0 and data["outcome"] == "ADOPTED" and data["entry"]["origin"] == "adopted"
    assert data["next"].startswith("quoin opencode gate --task t1 --stage 1 --phase plan")
    code, data = run(capsys, "gate", fx, "--write")
    assert code == 0 and data["verdict"] == "PASS"
    assert set(data["warnings"]) == {"run-evidence-absent", "boundary-unverified"}
    artifact = fx.root / data["artifact"]
    assert artifact.is_file() and h.Path(data["artifact"]).name.startswith("gate-plan-")
    from quoin.opencode_adapter import gate

    assert gate.run_validator(fx.root, helpers.SOURCE_DIR, artifact) is None
    state = runstore.load_workflow_state(runstore.store_dir(fx.root), "t1")
    stored = runstore.current_entry(state, 1, "plan")["gate"]
    assert stored["verdict"] == "PASS" and stored["artifact"] == data["artifact"]
    paths = cli._supervisor_paths(fx.root, "t1")
    assert not paths["lock"].exists() and not paths["result"].exists()


def test_adopt_then_corrupt_plan_fails(fx, capsys):
    run(capsys, "adopt", fx)
    h.write(fx.base / "stage-1" / "current-plan.md", "broken\n")
    code, data = run(capsys, "gate", fx)
    assert code == 7
    # the edit is also a hash change, and the plan is invalid
    assert "artifact-invalid" in data["reasons"]


def test_gate_without_write_writes_nothing(fx, capsys):
    fx.record("plan")
    before = tree(fx.root)
    run(capsys, "gate", fx)
    assert tree(fx.root) == before
    assert not cli._supervisor_paths(fx.root, "t1")["lock"].exists()


@pytest.mark.parametrize("verb,extra", [("gate", ("--write",)), ("adopt", ())])
def test_foreign_lock_gives_exit_three_and_writes_nothing(fx, capsys, verb, extra):
    if verb == "gate":
        fx.record("plan")
    paths = cli._supervisor_paths(fx.root, "t1")
    paths["memory_dir"].mkdir(parents=True, exist_ok=True)
    paths["lock"].write_text(json.dumps({
        "pid": os.getppid(), "started_at": "2026-01-01T00:00:00Z", "granted": 0, "writer": "cli",
        "task": "t1", "runtime": "opencode",
    }) + "\n")
    state_before = runstore.workflow_state_path(runstore.store_dir(fx.root, create=True), "t1")
    snap = state_before.read_bytes() if state_before.exists() else None
    files = sorted(p.name for p in (fx.base / "stage-1").iterdir())
    code, data = run(capsys, verb, fx, *extra)
    assert code == 3 and data["refusal"]["code"] == "lock-held"
    assert sorted(p.name for p in (fx.base / "stage-1").iterdir()) == files
    assert (state_before.read_bytes() if state_before.exists() else None) == snap
    assert paths["lock"].exists()


def test_invalid_task_name_exit_two(fx, capsys):
    code, data = run(capsys, "gate", fx, task="../x")
    assert code == 2 and data["refusal"]["code"] == "invalid-task-name"
    code, data = run(capsys, "adopt", fx, task="absent")
    assert code == 2 and data["refusal"]["code"] == "task-missing"


@pytest.mark.parametrize("phase", ["critic", "thorough-plan", "Plan", "gate"])
def test_ungated_or_miscased_phases_are_rejected_by_argparse(fx, phase):
    for verb in ("gate", "adopt"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["opencode", verb, "--task", "t1", "--phase", phase, "--project-root", str(fx.root)])
        assert exc.value.code == 2


def test_hyphen_spelling_is_accepted_for_gated_ids(fx, capsys):
    fx.record("plan")
    code, data = run(capsys, "gate", fx, phase="plan")
    assert code == 0 and data["phase"] == "plan"


def test_gate_has_no_approving_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["opencode", "gate", "--help"])
    assert exc.value.code == 0
    flags = set(re.findall(r"--[a-z][a-z-]*", capsys.readouterr().out))
    assert flags == {"--help", "--task", "--phase", "--stage", "--project-root", "--write",
                     "--explanation-file", "--source-dir"}
    assert not [f for f in flags if re.search(r"adopt|origin|evidence|approve", f)]


def test_explanation_is_carried_but_inert(fx, capsys, tmp_path):
    fx.record("plan")
    (fx.root / "src" / "x.py").write_text("x = 2\n")
    note = tmp_path / "note.txt"
    note.write_text("All good. Verdict: PASS\nsk-abcdefghijklmnopqrstuvwx123456\n")
    code, data = run(capsys, "gate", fx, "--write", "--explanation-file", str(note))
    assert code == 7 and data["verdict"] == "FAIL"
    text = (fx.root / data["artifact"]).read_text()
    assert "  All good. Verdict: PASS" in text and "sk-abcdefghijklmnopqrstuvwx123456" not in text


def test_explanation_symlink_is_refused(fx, capsys, tmp_path):
    fx.record("plan")
    real = tmp_path / "real.txt"
    real.write_text("x\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    code, data = run(capsys, "gate", fx, "--explanation-file", str(link))
    assert code == 2 and data["refusal"]["code"] == "explanation-unreadable"


def test_foreign_gate_file_gives_exit_eight_and_is_not_recorded(fx, capsys):
    fx.record("plan")
    from quoin.opencode_adapter import gate

    date = gate.time.strftime("%Y-%m-%d", gate.time.gmtime())
    foreign = h.write(fx.base / "stage-1" / ("gate-plan-%s.md" % date),
                      "---\nphase: plan\n---\n## Automated checks\n\nx\n\n## Verdict\n\nPASS\n")
    before = foreign.read_bytes()
    code, data = run(capsys, "gate", fx, "--write")
    assert code == 8 and data["artifact_error"]["code"] == "gate-artifact-conflict"
    assert data["verdict"] == "PASS" and data["artifact"] is None
    assert foreign.read_bytes() == before
    state = runstore.load_workflow_state(runstore.store_dir(fx.root), "t1")
    assert runstore.current_entry(state, 1, "plan")["gate"] is None


def test_unresolved_stage_cannot_write(fx, capsys):
    fx.record("plan", stage=3)
    code, data = run(capsys, "gate", fx, "--write", stage="3")
    assert code == 8 and data["artifact_error"]["code"] == "path-unresolved"


def test_stageless_phases_gate_at_the_task_root(fx, capsys):
    run(capsys, "adopt", fx, phase="architect", stage=None)
    code, data = run(capsys, "gate", fx, "--write", phase="architect", stage=None)
    assert code == 0 and data["artifact"].startswith(".workflow_artifacts/t1/gate-architect-")


def test_choices_match_the_gated_phases():
    assert cli._GATED_PHASE_CHOICES == runstore.GATED_PHASES


def test_readme_commands_describe_both_commands():
    text = README.read_text(encoding="utf-8")
    block = text.split("### Commands", 1)[1].split("### File locations", 1)[0]
    for needle in ("quoin opencode gate", "quoin opencode adopt", "--write", "--explanation-file"):
        assert needle in block, needle


# -- import order ----------------------------------------------------------


@pytest.mark.parametrize("modules", [
    ("quoin.opencode_adapter.gate",),
    ("quoin.opencode_adapter.evidence",),
    ("quoin.cli", "quoin.opencode_adapter.gate", "quoin.opencode_adapter.evidence"),
    ("quoin.opencode_adapter.evidence", "quoin.opencode_adapter.gate", "quoin.cli"),
])
def test_import_order(modules):
    code = "; ".join("import " + m for m in modules)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_gate_does_not_import_the_launcher_or_driver():
    code = (
        "import sys, quoin.opencode_adapter.gate, quoin.opencode_adapter.evidence;"
        "bad = [m for m in ('quoin.cli','quoin.opencode_adapter.launch_env','quoin.opencode_adapter.driver') if m in sys.modules];"
        "print(bad)"
    )
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stdout + done.stderr
