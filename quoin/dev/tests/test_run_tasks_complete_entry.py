"""Text and behavior guards for the run skill's tasks-complete entry, retry budget,
fix-scope lines, escalation cleanup and resume handling of completion markers."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
RUN_CORE_DOC = REPO_ROOT / "quoin" / "core" / "skills" / "run.md"
CORE_SCRIPTS = REPO_ROOT / "quoin" / "core" / "scripts"


@pytest.fixture(scope="module")
def text() -> str:
    return RUN_SKILL.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


@pytest.fixture(scope="module")
def phase4(text):
    return _between(text, "## Phase 4 — Implement", "**Checkpoint C:**")


@pytest.fixture(scope="module")
def checkpoint_c(text):
    return _between(text, "**Checkpoint C:**", "## Phase 5 — Review")


@pytest.fixture(scope="module")
def entry(phase4):
    return _between(phase4, "**Tasks-complete entry.**", "Under `AUTONOMOUS`, once Checkpoint C confirms")


@pytest.fixture(scope="module")
def budget(checkpoint_c):
    return _between(checkpoint_c, "**Automatic-retry budget (autonomous).**", "If the user says \"show changes\"")


@pytest.fixture(scope="module")
def phase5_fix(text):
    return _between(text, '1. **"fix"** → before spawning', "2. **\"accept\"**")


@pytest.fixture(scope="module")
def escalation_step4(checkpoint_c):
    return _between(checkpoint_c, "4. DELETE", "5. (optional cleanup)")


@pytest.fixture(scope="module")
def resume(text):
    return _between(text, "## Resume", "## Session state tracking")


def test_entry_names_both_points_and_keys_on_plan(entry):
    assert "(a)" in entry and "(b)" in entry
    assert "plan_tasks.py" in entry and "`ALLDONE|`" in entry
    assert "implement.done` is absent" in entry
    assert "alone never qualifies" in entry
    condition = entry[entry.index("Condition:"):entry.index("The marker")]
    assert "implement.tasks.done" not in condition


def test_second_point_return_shapes(entry):
    second = entry[entry.index("(b)"):]
    for word in ("COMPLETE", "unparseable", "NEEDS-DECISION", "BLOCKED", "PARTIAL"):
        assert word in second
    assert "any return shape" not in entry
    assert "continue to the gate as today" not in entry


def test_entry_paragraph_order(entry):
    tail = entry[entry.index("When the condition is true"):]
    assert tail.index("gate") < tail.index("`implement.done`") < tail.index("boundary write for the review phase")
    assert '--next-action "start review"' not in entry


def test_fix_scope_lines(checkpoint_c, phase5_fix):
    assert "Fix scope: gate" in checkpoint_c
    assert "Fix scope: review" in phase5_fix


def test_fix_path_never_reenters_entry(entry):
    assert "never re-enters" in entry


def test_budget_paragraph_shape(budget, text):
    assert budget.startswith("**Automatic-retry budget (autonomous).**")
    assert not budget.startswith("Under `AUTONOMOUS`")
    assert "--require-existing" not in budget
    blocks = re.findall(r"```\n(.*?)```", budget, flags=re.S)
    assert len(blocks) == 2
    for block, step in zip(blocks, ("--step gate-retry-1", '--step ""')):
        for flag in ('--phase "thorough_plan"', "--phase-index 3", step, "--at-stage-boundary true",
                     '--route "{route}"', '--profile "{profile}"', '--next-action "start implement"',
                     '--artifact "{task_dir}/current-plan.md"'):
            assert flag in block, flag
        assert "--require-existing" not in block
    assert '--next-action "start review"' not in budget
    assert budget.index("--step gate-retry-1") < budget.index("fix re-dispatch")
    assert "Hard-stop #2" in budget and "already spent" in budget
    assert budget.index("halt-sentinel") < budget.index('--step ""')
    assert text.count("--require-existing") == 1


def test_uniqueness_of_headings(text):
    assert text.count("**Tasks-complete entry.**") == 1
    assert text.count("**Automatic-retry budget (autonomous).**") == 1


def test_escalation_sites_enumerate_python_side(escalation_step4, text):
    assert "implement.*.done" in escalation_step4 and "glob" in escalation_step4
    assert "implement.tasks.done" in escalation_step4
    assert "and any other `implement.*.done`" in " ".join(escalation_step4.split())
    for marker in ("AND `autonomous-progress-{task}/implement.done`", "AND `autonomous-progress-{task}/implement.done`"):
        assert marker in text
    phase5 = text[text.index("## Phase 5 — Review"):text.index("## Phase 6 — End of Task")]
    sites = [m.start() for m in re.finditer(r"implement\.\*\.done", phase5)]
    assert len(sites) >= 2
    for pos in sites:
        window = phase5[pos - 200: pos + 300]
        assert "pathlib.Path(...).glob" in window


def _run_calls(budget: str, tmp: Path):
    cmds = []
    for block in re.findall(r"```\n(.*?)```", budget, flags=re.S):
        block = block.replace("\\\n", " ").replace(" || true", "")
        block = block.replace("__QUOIN_HOME__/scripts", shlex.quote(str(CORE_SCRIPTS)))
        block = block.replace("$PROJECT_ROOT", shlex.quote(str(tmp))).replace("$CLAUDE_CODE_SESSION_ID", "sess-1")
        block = block.replace("{task}", "demo").replace("{route}", "full").replace("{profile}", "Medium")
        block = block.replace("{task_dir}", str(tmp / "task"))
        argv = shlex.split(block)
        argv[0] = sys.executable
        cmds.append(argv)
    return cmds


def _read(tmp: Path, fields: str):
    r = subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "run_state.py"), "--read", "--project-root", str(tmp),
         "--task", "demo", "--fields", fields],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


def test_budget_and_reset_writes_keep_the_boundary_record(budget, tmp_path):
    (tmp_path / ".workflow_artifacts" / "memory").mkdir(parents=True)
    seed = [sys.executable, str(CORE_SCRIPTS / "run_state.py"), "--write", "--project-root", str(tmp_path),
            "--task", "demo", "--session-id", "sess-1", "--phase", "thorough_plan", "--phase-index", "3",
            "--subphase", "", "--step", "", "--at-stage-boundary", "true", "--route", "full",
            "--profile", "Medium", "--next-action", "start implement", "--artifact", str(tmp_path / "task" / "current-plan.md")]
    assert subprocess.run(seed, capture_output=True, text=True, timeout=60).returncode == 0
    budget_call, reset_call = _run_calls(budget, tmp_path)
    for step, call in (("gate-retry-1", budget_call), ("", reset_call)):
        r = subprocess.run(call, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        got = _read(tmp_path, "phase,phase_index,at_stage_boundary,step,route,profile,next_action")
        assert got["phase"] == "thorough_plan" and got["phase_index"] == "3"
        assert got["at_stage_boundary"] == "true" and got["step"] == step
        assert got["route"] == "full" and got["profile"] == "Medium" and got["next_action"] == "start implement"
    notes = list((tmp_path / ".workflow_artifacts" / "memory").glob("run-notes-*"))
    assert all("gate-retry" not in n.read_text() for n in notes)


def test_resume_bullets(resume):
    assert "`implement.tasks.done`" in resume and "`implement.batch-N.done`" in resume
    assert "not resumable\n  sub-phases" in resume or "not resumable sub-phases" in resume.replace("\n  ", " ")
    assert "plan_tasks.py" in resume
    assert "never from the marker alone" in resume.replace("\n  ", " ")
    flat = resume.replace("\n  ", " ")
    assert "`gate-retry-1`" in flat and "never a re-entry position" in flat
    assert "`step` is never a re-entry position" in flat


def test_entry_and_resume_name_the_same_helper(entry, resume):
    assert "plan_tasks.py" in entry and "plan_tasks.py" in resume


def test_core_doc_completion_marker():
    doc = RUN_CORE_DOC.read_text(encoding="utf-8")
    assert "`{phase}.tasks.done`" in doc
    assert "never substitutes" in doc.replace("\n", " ")


def test_budget_read_command_from_text_sees_the_spent_retry(budget, tmp_path, monkeypatch):
    # Each Bash call is a fresh shell: the read must not lean on variables set elsewhere.
    monkeypatch.delenv("_RUN_STATE_STALE_DAYS", raising=False)
    monkeypatch.delenv("QUOIN_RUN_STATE_STALE_DAYS", raising=False)
    (tmp_path / ".workflow_artifacts" / "memory").mkdir(parents=True)
    budget_call, _reset = _run_calls(budget, tmp_path)
    assert subprocess.run(budget_call, capture_output=True, text=True, timeout=60).returncode == 0
    found = re.findall(r"`(python3 \S*run_state\.py --read [^`]*)`", budget)
    assert len(found) == 1
    cmd = found[0].replace(" || true", "")
    assert "$_" not in cmd
    cmd = cmd.replace("__QUOIN_HOME__/scripts", shlex.quote(str(CORE_SCRIPTS)))
    cmd = cmd.replace("$PROJECT_ROOT", shlex.quote(str(tmp_path))).replace("{task}", "demo")
    argv = shlex.split(cmd)
    argv[0] = sys.executable
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "step=gate-retry-1" in r.stdout
