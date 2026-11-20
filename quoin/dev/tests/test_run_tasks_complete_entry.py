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
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
RUN_CORE_DOC = REPO_ROOT / "quoin" / "core" / "skills" / "run_hard.md"
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


def _run_calls(budget: str, tmp: Path):
    cmds = []
    for block in re.findall(r"```\n(.*?)```", budget, flags=re.S):
        block = block.replace("\\\n", " ").replace(" || true", "")
        block = block.replace("${CLAUDE_PLUGIN_ROOT}/scripts", shlex.quote(str(CORE_SCRIPTS)))
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


def test_core_doc_completion_marker():
    doc = RUN_CORE_DOC.read_text(encoding="utf-8")
    assert "`{phase}.tasks.done`" in doc
    assert "never substitutes" in doc.replace("\n", " ")


