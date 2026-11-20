"""IVG-71 T-09: adapter-pilot token greps over review/SKILL.md.

Cheap literal-token asserts that verify the review wiring for the
affected-area test suite precondition is present and correctly formed.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEW_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "review" / "SKILL.md"


def _load() -> str:
    return REVIEW_SKILL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Basic reference checks
# ---------------------------------------------------------------------------

def test_affected_tests_script_referenced():
    """affected_tests.py must appear in review SKILL.md."""
    text = _load()
    assert "affected_tests.py" in text, (
        "review/SKILL.md must reference 'affected_tests.py'"
    )


def test_quoin_home_token_form():
    """`__QUOIN_HOME__/scripts/affected_tests.py` must appear (not ~/.claude/ form)."""
    text = _load()
    assert "__QUOIN_HOME__/scripts/affected_tests.py" in text, (
        "review/SKILL.md must use __QUOIN_HOME__/scripts/affected_tests.py "
        "(not ~/.claude/scripts/affected_tests.py)"
    )


def test_no_tilde_claude_literal():
    """No ~/.claude/ literal in review SKILL.md (lessons-learned 2026-05-15)."""
    import re
    text = _load()
    matches = re.findall(r"~/\.claude/", text)
    assert not matches, (
        f"review/SKILL.md must not contain ~/.claude/ — found {len(matches)} occurrence(s). "
        "Use __QUOIN_HOME__ instead."
    )


def test_affected_area_test_literal_present():
    """Literal 'Affected-area test' appears in review SKILL.md."""
    text = _load()
    assert "Affected-area test" in text, (
        "review/SKILL.md must contain the literal 'Affected-area test' token"
    )


def test_step_6b_header_present():
    """### Step 6b header must be present (placement check per D-02)."""
    text = _load()
    assert "### Step 6b" in text, (
        "review/SKILL.md must contain a '### Step 6b' header for the "
        "affected-area test gate section (D-02: placed parallel to Step 6a)"
    )


def test_precondition_for_approved_phrase():
    """Stable literal 'precondition for APPROVED' must be present (T-09 spec)."""
    text = _load()
    assert "precondition for APPROVED" in text, (
        "review/SKILL.md must contain 'precondition for APPROVED' in the "
        "Step 6b affected-area gate section"
    )


# ---------------------------------------------------------------------------
# Invocation correctness
# ---------------------------------------------------------------------------

def test_invocation_uses_project_root():
    """The invocation must use --project-root (CRIT-1 fix)."""
    text = _load()
    assert "--project-root" in text, (
        "review/SKILL.md must use --project-root in the affected_tests.py invocation "
        "(CRIT-1 fix: helper resolves git repo from --project-root itself)"
    )


def test_no_caller_side_git_in_step_6b():
    """Step 6b pseudocode block must NOT contain `git -C` or caller-side git invocations.

    The regression guard checks the pseudocode block (``` ... ```) inside Step 6b —
    not the surrounding prose, which legitimately mentions `main...HEAD` as an
    explanation of what the CRIT-2 fix avoids.
    """
    text = _load()
    import re
    m_header = re.search(r"^### Step 6b:", text, re.MULTILINE)
    assert m_header is not None, "Step 6b heading not found"
    start = m_header.start()
    after = text[start + len("### Step 6b:"):]
    m_next = re.search(r"^### ", after, re.MULTILINE)
    block = after[: m_next.start()] if m_next else after[:2000]

    # Extract code blocks only (between ``` fences) — the regression guard applies
    # to the pseudocode/invocation blocks, not the surrounding explanatory prose
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", block, re.DOTALL)
    code_text = "\n".join(code_blocks)

    assert "git -C" not in code_text, (
        "review/SKILL.md Step 6b pseudocode must NOT use `git -C` — "
        "the helper resolves the git repo itself (CRIT-1)"
    )
    assert "/tmp/quoin-changed.txt" not in code_text, (
        "review/SKILL.md Step 6b pseudocode must NOT use a fixed /tmp/quoin-changed.txt path (MIN-3)"
    )
    # The pseudocode invocation must use affected_tests.py (not a bare git diff)
    assert "affected_tests.py" in code_text, (
        "review/SKILL.md Step 6b pseudocode must invoke affected_tests.py"
    )


# ---------------------------------------------------------------------------
# Verdict rule wiring
# ---------------------------------------------------------------------------

def test_ran_pytest_in_step_6b():
    """Step 6b verdict rule must reference `ran_pytest` (MAJ-1 docs-only N/A sub-state)."""
    text = _load()
    # Find the SECOND Step 6b occurrence (the actual section header, not the cross-link in Step 5)
    # More reliable: find the line that starts with `### Step 6b:`
    import re
    # Find a line that IS the Step 6b heading (starts the line)
    m_header = re.search(r"^### Step 6b:", text, re.MULTILINE)
    assert m_header is not None, "Step 6b heading not found"
    start = m_header.start()
    after = text[start + len("### Step 6b:"):]
    m_next = re.search(r"^### ", after, re.MULTILINE)
    block = after[: m_next.start()] if m_next else after[:3000]

    assert "ran_pytest" in block, (
        "review/SKILL.md Step 6b must reference `ran_pytest` in the verdict rule "
        "so exit 0 distinguishes 'affected green' from 'no affected tests — N/A' (MAJ-1). "
        f"Block preview: {block[:200]!r}"
    )


def _step_6b_block(text: str) -> str:
    import re
    m_header = re.search(r"^### Step 6b:", text, re.MULTILINE)
    assert m_header is not None, "Step 6b heading not found"
    after = text[m_header.start() + len("### Step 6b:"):]
    m_next = re.search(r"^### ", after, re.MULTILINE)
    return after[: m_next.start()] if m_next else after[:3000]


def test_require_task_context_flag_in_step_6b():
    """IVG-151: Step 6b invocation must carry --require-task-context."""
    block = _step_6b_block(_load())
    assert "--require-task-context" in block, (
        "review/SKILL.md Step 6b must pass --require-task-context to affected_tests.py")


def test_exit5_verdict_rule_in_step_6b():
    """IVG-151: Step 6b verdict rule must carry an exit 5 row + the hyphenated token."""
    block = _step_6b_block(_load())
    assert "exit 5" in block, "review/SKILL.md Step 6b must carry an exit 5 verdict-rule row"
    assert "no-quoin-task-context" in block, (
        "review/SKILL.md Step 6b must pin the hyphenated exit_reason token no-quoin-task-context")
