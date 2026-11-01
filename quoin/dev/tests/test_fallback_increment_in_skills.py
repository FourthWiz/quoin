"""T-13: fallback_fires increment spec test.

Verifies that the session-state fallback_fires increment instructions are present
at the correct sites in each SKILL.md file, per Stage 4 T-08.

Step 5 emit sites (6 skills + gate):
- architect, critic, revise, revise-fast, plan, review: canonical phrase
  "Before logging the `format-kit-skipped` warning, increment the session-state `fallback_fires`"
- gate: variant phrase
  "Before falling back to v2-style write, increment the session-state `fallback_fires`"

Step 2 retry sites (5 Class B writers: architect, plan, revise, revise-fast, review):
- canonical phrase "Before re-running Step 2, increment the session-state `fallback_fires`"
- Each insertion is followed (on the next non-blank line) by its verbatim Step 2 anchor.

Negative guard: capture_insight, end_of_day, start_of_day, implement must NOT carry
the Step 5 increment instruction.
"""
import re
from pathlib import Path


QUOIN_DIR = Path(__file__).parent.parent.parent  # quoin/ directory (3 levels up from test file)
ADAPTER_SKILLS_DIR = QUOIN_DIR / "adapters" / "claude" / "skills"
MIGRATED_SKILLS_DIR_OVERRIDES = {
    "architect": ADAPTER_SKILLS_DIR,
    "review": ADAPTER_SKILLS_DIR,
    "plan": ADAPTER_SKILLS_DIR,
    "critic": ADAPTER_SKILLS_DIR,
    "revise": ADAPTER_SKILLS_DIR,
    "revise-fast": ADAPTER_SKILLS_DIR,
    "gate": ADAPTER_SKILLS_DIR,
    "implement": ADAPTER_SKILLS_DIR,
    "security_review": ADAPTER_SKILLS_DIR,
}

STEP5_CANONICAL = (
    "Before logging the `format-kit-skipped` warning, increment the session-state `fallback_fires`"
)
GATE_VARIANT = (
    "Before falling back to v2-style write, increment the session-state `fallback_fires`"
)
STEP2_CANONICAL = (
    "Before re-running Step 2, increment the session-state `fallback_fires`"
)

STEP5_SKILLS = ["architect", "critic", "revise", "revise-fast", "plan", "review", "security_review"]
STEP2_SKILLS = ["architect", "plan", "revise", "revise-fast", "review", "security_review"]
NEGATIVE_SKILLS = ["capture_insight", "end_of_day", "start_of_day", "implement"]

# Verbatim Step 2 anchor lines per skill (from T-08 spec)
STEP2_ANCHORS = {
    "architect": "(a) Re-run ONLY Step 2 once (re-spawn the Haiku Agent subagent). Do NOT re-run Step 1 (body is fine; summary failed).",
    "plan": "(a) Re-run ONLY Step 2 once (re-spawn the Haiku Agent subagent against the unchanged",
    "revise": "**Step 2 failure:** re-run Step 2 once (re-spawn the Haiku Agent subagent); if still fails",
    "revise-fast": "**Step 2 failure:** re-run Step 2 once (re-spawn the Haiku Agent subagent); if still fails",
    "review": "Re-run ONLY Step 2 once (re-spawn the Haiku Agent subagent). If re-run also fails: fall back to v2-style write.",
    "security_review": "Re-run ONLY Step 2 once (re-spawn the Haiku Agent subagent). If re-run also fails: fall back to v2-style write.",
}


def skill_path(name: str) -> Path:
    base = MIGRATED_SKILLS_DIR_OVERRIDES.get(name, ADAPTER_SKILLS_DIR)
    return base / name / "SKILL.md"


def test_step5_emit_canonical_phrase_present_exactly_once():
    """Each of the 6 Step 5 skills must contain the canonical increment phrase exactly once."""
    for skill in STEP5_SKILLS:
        path = skill_path(skill)
        assert path.exists(), f"SKILL.md not found: {path}"
        content = path.read_text(encoding="utf-8")
        count = content.count(STEP5_CANONICAL)
        assert count == 1, (
            f"{skill}/SKILL.md: expected 1 occurrence of Step 5 canonical phrase, found {count}"
        )


def test_gate_variant_phrase_present_exactly_once():
    """gate/SKILL.md must contain the gate-specific variant increment phrase exactly once."""
    path = skill_path("gate")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    count = content.count(GATE_VARIANT)
    assert count == 1, (
        f"gate/SKILL.md: expected 1 occurrence of gate variant phrase, found {count}"
    )


def test_step2_retry_canonical_phrase_present_exactly_once():
    """Each of the 5 Step 2 retry skills must contain the Step 2 canonical phrase exactly once."""
    for skill in STEP2_SKILLS:
        path = skill_path(skill)
        content = path.read_text(encoding="utf-8")
        count = content.count(STEP2_CANONICAL)
        assert count == 1, (
            f"{skill}/SKILL.md: expected 1 occurrence of Step 2 canonical phrase, found {count}"
        )


def test_step2_insertion_followed_by_anchor():
    """Each Step 2 insertion must be on the line immediately before the verbatim anchor.

    This guards against insertion drift — ensures the increment sits immediately before the anchor.
    The increment instruction and the anchor are on consecutive lines (the increment instruction
    is a full line; the anchor follows on the next non-blank line).
    """
    for skill in STEP2_SKILLS:
        path = skill_path(skill)
        content = path.read_text(encoding="utf-8")
        anchor_fragment = STEP2_ANCHORS[skill]

        # Find the line containing the Step 2 canonical phrase
        lines = content.splitlines()
        increment_line_idx = None
        for i, line in enumerate(lines):
            if STEP2_CANONICAL in line:
                increment_line_idx = i
                break

        assert increment_line_idx is not None, (
            f"{skill}/SKILL.md: Step 2 canonical phrase not found on any line"
        )

        # Find the next non-blank line after the increment instruction line
        remaining = lines[increment_line_idx + 1:]
        non_blank_after = [ln for ln in remaining if ln.strip()]
        assert non_blank_after, (
            f"{skill}/SKILL.md: no non-blank lines after Step 2 increment instruction line"
        )
        next_line = non_blank_after[0]

        assert anchor_fragment in next_line, (
            f"{skill}/SKILL.md: Step 2 increment instruction not immediately followed by anchor.\n"
            f"  Expected anchor fragment: {anchor_fragment!r}\n"
            f"  Got: {next_line!r}"
        )


def test_negative_skills_no_step5_increment():
    """Non-instrumented skills must NOT carry the Step 5 canonical increment phrase."""
    for skill in NEGATIVE_SKILLS:
        path = skill_path(skill)
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        assert STEP5_CANONICAL not in content, (
            f"{skill}/SKILL.md unexpectedly contains the Step 5 increment instruction"
        )
