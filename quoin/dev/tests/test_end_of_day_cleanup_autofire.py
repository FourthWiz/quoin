"""`/end_of_day` Step 5b spawns `/cleanup --no-tasks`, with an opt-out.

Two-slicer pattern: the Step 5b slice is pinned on its own, and the rest of the
file is scanned to prove nothing else spawns `/cleanup`.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EOD = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_day" / "SKILL.md"

STEP_5B = "### Step 5b: Invoke /cleanup (sentinel hygiene)"


def _text() -> str:
    return EOD.read_text(encoding="utf-8")


def _step_5b_slice() -> str:
    text = _text()
    start = text.index(STEP_5B)
    end = text.index("### Step 6:", start)
    return text[start:end]


def test_step_5b_heading_occurs_once():
    assert _text().count(STEP_5B) == 1


def test_step_5b_spawns_cleanup_without_the_task_pass():
    region = _step_5b_slice()
    assert 'model: "sonnet"' in region
    assert "[no-redispatch]" in region
    assert "/cleanup --no-tasks" in region
    assert "[quoin-S-3: /cleanup invocation failed; run /cleanup standalone to retry]" in region
    assert "continue to Step 6" in region


def test_step_5b_sits_between_step_5_and_step_6():
    text = _text()
    assert text.index("### Step 5: Report to user") < text.index(STEP_5B) < text.index("### Step 6:")


def test_no_cleanup_flag_is_documented_under_important_behaviors():
    text = _text()
    behaviors = text[text.index("## Important behaviors"):]
    assert "--no-cleanup" in behaviors
    assert "--no-cleanup" in _step_5b_slice()


def test_no_other_section_spawns_cleanup():
    text = _text()
    region = _step_5b_slice()
    rest = text.replace(region, "")
    assert not re.search(r"prompt:\s*\"\[no-redispatch\]\\n/cleanup", rest)


def test_step_5b_never_runs_the_task_bookkeeping_script():
    assert "task_bookkeeping.py" not in _step_5b_slice()
