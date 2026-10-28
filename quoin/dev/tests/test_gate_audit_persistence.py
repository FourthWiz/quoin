"""Static-assertion tests for gate audit-log persistence contracts (T-05 / T-06).

These tests assert SKILL.md text contracts established in Stage 5 T-05.
They fire if a future edit removes the MANDATORY guard or the subagent-dispatch
requirements without a corresponding test update.
"""

import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
GATE_SKILL = PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
RUN_SKILL = PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
THOROUGH_PLAN_SKILL = PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "thorough_plan" / "SKILL.md"


def _lines_after_heading(text: str, heading: str, window: int = 35) -> str:
    """Return up to `window` lines of text starting from the line containing `heading`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if heading in line:
            return "\n".join(lines[i : i + window])
    return ""


def test_gate_step5_mandatory_phrase():
    """MANDATORY guard must appear within 35 lines of 'Step 4' heading."""
    text = GATE_SKILL.read_text()
    window = _lines_after_heading(text, "### Step 4", window=35)
    assert "MANDATORY" in window, (
        "gate/SKILL.md: 'MANDATORY' phrase not found within 35 lines of '### Step 4'"
    )
    assert "Step 5" in window, (
        "gate/SKILL.md: 'Step 5' reference not found within 35 lines of '### Step 4'"
    )


def test_gate_step5_inline_invocation_clause():
    """Step 5 must contain the inline-invocation guard clause."""
    text = GATE_SKILL.read_text()
    assert "inline" in text and "audit log" in text, (
        "gate/SKILL.md: inline-invocation audit-log clause missing from Step 5"
    )
    # More specific: the clause should be near 'Step 5'
    window = _lines_after_heading(text, "### Step 5", window=10)
    assert "inline" in window, (
        "gate/SKILL.md: inline-invocation clause not found within 10 lines of '### Step 5'"
    )


def test_run_gate_boundaries():
    """run/SKILL.md must correctly distinguish inline vs subagent gate boundaries (Stage 3).

    Post-architect gate: subagent dispatch (context shape diverges after architect phase).
    Post-implement gates (primary + recursive recovery) and post-review gate: inline.
    """
    text = RUN_SKILL.read_text()

    # Post-architect gate still spawns as subagent (NOT modified by Stage 3).
    assert re.search(r"spawn.*`/gate`.*subagent.*architect|architect.*gate.*subagent", text), (
        "run/SKILL.md: post-architect gate must still spawn as subagent"
    )

    # Post-implement primary gate must be inline.
    assert "After implement completes, run `/gate` inline" in text, (
        "run/SKILL.md: post-implement primary gate must run inline"
    )

    # Recursive recovery paths must also be inline.
    assert "then re-run `/gate` inline" in text, (
        "run/SKILL.md: post-implement recursive recovery path must re-run /gate inline"
    )
    assert "re-run the post-implementation gate inline" in text, (
        "run/SKILL.md: post-implement fix path must re-run gate inline"
    )

    # Post-review gate must be inline.
    assert "run `/gate` inline (Full level, post-review" in text, (
        "run/SKILL.md: post-review gate must run inline"
    )

    # The old 'never inline — the subagent must read gate/SKILL.md' wording must be gone.
    assert "never inline — the subagent must read gate/SKILL.md" not in text, (
        "run/SKILL.md: old 'never inline — the subagent' wording should have been replaced"
    )


def test_thorough_plan_dispatches_gate_as_subagent():
    """thorough_plan/SKILL.md must instruct subagent dispatch for gate."""
    text = THOROUGH_PLAN_SKILL.read_text()
    assert re.search(r"spawn.*`?/gate`?.*subagent|subagent.*`?/gate`?", text), (
        "thorough_plan/SKILL.md: no 'spawn /gate as subagent' instruction found"
    )


def test_negative_case_caught():
    """Assertion machinery must FAIL on a synthetic SKILL.md missing the MANDATORY phrase.

    Guards against the test regressing to a no-op (lesson 2026-04-24).
    """
    synthetic = "### Step 4\n\nDo something.\n\n### Step 5\n\nWrite audit log.\n"
    window = _lines_after_heading(synthetic, "### Step 4", window=35)
    # Confirm the assertion would raise for this synthetic file
    assert "MANDATORY" not in window, (
        "Test machinery flaw: 'MANDATORY' found in synthetic file that shouldn't have it"
    )
    # The real test_gate_step5_mandatory_phrase would assert "MANDATORY" in window —
    # confirm that assertion would indeed fail on this synthetic text.
    try:
        assert "MANDATORY" in window
        raise AssertionError("Expected assertion to fail on synthetic file but it passed")
    except AssertionError as exc:
        if "Expected assertion to fail" in str(exc):
            raise
        # Correct: the assertion failed as expected — test machinery works
