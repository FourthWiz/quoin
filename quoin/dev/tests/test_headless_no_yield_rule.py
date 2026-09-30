"""Guards for the headless no-yield rule: the shared subsection, the gate recipe and
the references from the run skill's inline gate sites."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTONOMOUS_MODE = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
GATE_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
GATE_CORE = REPO_ROOT / "quoin" / "core" / "skills" / "gate.md"

HEADING = "### Headless children never yield with pending work"


@pytest.fixture(scope="module")
def subsection() -> str:
    text = AUTONOMOUS_MODE.read_text(encoding="utf-8")
    assert text.count(HEADING) == 1
    i = text.index(HEADING)
    return text[i : text.index("\n### ", i + 10)]


def test_subsection_contents(subsection):
    flat = " ".join(subsection.split())
    for needle in (
        "wait_for.py start", "wait_for.py wait", "600000", "--max-secs 540", "--token",
        "`QUOIN_HEADLESS_CHILD=1` is present in the environment", "`DEAD", "`EXPIRED",
        "QUOIN_WAIT_BUDGET_SECS", "must not end its turn", "The only exemption is an interactive session",
        "running in, or was moved to, the background",
    ):
        assert needle in flat, needle
    assert flat.index("wait_for.py wait") < flat.index("`known_red.py`")
    assert "is set by the supervisor once its launch-time change lands" in flat


def test_start_command_has_no_shell_background_or_nohup(subsection):
    for line in subsection.splitlines():
        if "wait_for.py start" in line:
            command = line[line.index("python3"):]
            assert "nohup" not in command.split("(")[0]
            assert not re.search(r"\s&\s*$", command)


def _paragraphs(text: str):
    return re.split(r"\n\s*\n", text)


def test_run_inline_gate_sites_reference_the_rule():
    text = RUN_SKILL.read_text(encoding="utf-8")
    region = text[text.index("## Phase 4 — Implement"):text.index("## Phase 6 — End of Task")]
    pattern = re.compile(r"(?:re-)?run `/gate` inline|re-run the post-implementation gate inline")
    hits = 0
    for para in _paragraphs(region):
        for item in re.split(r"\n(?=\d+\. |- )", para):
            if pattern.search(item):
                hits += 1
                assert HEADING in item or "no-yield rule" in item, item[:120]
    assert hits >= 3
    entry = region[region.index("**Tasks-complete entry.**"):]
    assert HEADING in entry.split("\n\n")[2] or HEADING in entry[:3000]


def test_hook_cooperation_names_the_rule():
    text = RUN_SKILL.read_text(encoding="utf-8")
    section = text[text.index("## Hook cooperation (autonomous)"):text.index("## Gate boundaries reference")]
    assert HEADING in section


def test_gate_skill_headless_variant():
    text = GATE_SKILL.read_text(encoding="utf-8")
    para = text[text.index("**Headless variant (autonomous):**"):]
    para = para[: para.index("\n  `RC=$?`")]
    for needle in ("wait_for.py start", "wait_for.py wait", "--token", "600000", "`DEAD`", "`EXPIRED`", HEADING):
        assert needle in para, needle
    assert para.index("wait_for.py wait") < para.index("`known_red.py`")
    assert "same no-yield rule" in text


def test_gate_core_doc_sentence():
    text = " ".join(GATE_CORE.read_text(encoding="utf-8").split())
    assert "non-interactive session MUST wait on long test runs in the foreground" in text
