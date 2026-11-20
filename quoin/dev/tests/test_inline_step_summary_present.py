"""Regression test: inline end-of-step summary requirement (IVG-52).

Checks that:
1. Each of the three adapter SKILL.md files (thorough_plan, implement, review)
   contains the required inline-summary instruction in its output/completion section.
2. The shared CLAUDE.md rule exists under ### Communication.

Sentinel phrases are intentionally stable (not exact paragraphs) so minor wording
edits do not break the test. Both phrases must co-occur in the relevant section.

Pin on:
  - "inline summary"  (the instruction name)
  - "plain" OR "human-readable"  (the English-language requirement)
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]  # quoin/quoin/dev/tests -> repo root (quoin/)
ADAPTER_SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
CORE_CLAUDE_MD = REPO_ROOT / "quoin" / "CLAUDE.md"

SKILLS = ["thorough_plan", "implement", "review"]

# Section headings that must contain the summary instruction (case-insensitive search
# from the heading to end-of-file, stopping at the next same-level heading).
SECTION_HEADINGS = {
    "thorough_plan": "## Final output",
    "implement": "## After implementation",
    "review": "## After the review",
}


def _strip_code_fences(text: str) -> str:
    """Remove content inside fenced code blocks (``` ... ```) to avoid false heading matches."""
    return re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)


def _extract_section(text: str, heading: str) -> str:
    """Return the text from `heading` to the next same-level heading (or EOF).

    Code fences are stripped before searching for the section boundary so that
    heading-like lines inside a ``` block don't prematurely terminate the section.
    The original text (with code fences) is returned so sentinel phrases inside
    prose blocks are still found.
    """
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    start = m.end()
    section_text = text[start:]

    # Strip fences from a copy to find the next real heading boundary
    stripped = _strip_code_fences(section_text)
    level = len(heading.split()[0])  # number of # chars in the heading marker
    next_heading = re.compile(
        r"^#{" + str(level) + r"}(?!#)\s",
        re.MULTILINE,
    )
    m2 = next_heading.search(stripped)
    if not m2:
        return section_text

    # Map the match position in `stripped` back to `section_text`.
    # Because stripping can change lengths, we search for the same heading text
    # in section_text starting from a heuristic offset.
    heading_text = stripped[m2.start(): m2.end() + 60]  # take a prefix to anchor on
    anchor = heading_text[:30].rstrip()
    m3 = re.search(re.escape(anchor), section_text)
    if m3:
        return section_text[: m3.start()]
    # Fallback: use stripped-text offset directly (approximate)
    return section_text[: m2.start()]


class TestAdapterSkillInlineSummary:
    """Each adapter SKILL.md must instruct an inline English summary."""

    def _skill_text(self, skill: str) -> str:
        path = ADAPTER_SKILLS / skill / "SKILL.md"
        assert path.exists(), f"Adapter SKILL.md not found: {path}"
        return path.read_text(encoding="utf-8")

    def test_thorough_plan_has_inline_summary(self):
        text = self._skill_text("thorough_plan")
        section = _extract_section(text, SECTION_HEADINGS["thorough_plan"])
        assert section, "## Final output section not found in thorough_plan/SKILL.md"
        low = section.lower()
        assert "inline summary" in low, (
            "thorough_plan/SKILL.md ## Final output must contain 'inline summary'"
        )
        assert "plain" in low or "human-readable" in low, (
            "thorough_plan/SKILL.md ## Final output must contain 'plain' or 'human-readable'"
        )

    def test_implement_has_inline_summary(self):
        text = self._skill_text("implement")
        section = _extract_section(text, SECTION_HEADINGS["implement"])
        assert section, "## After implementation section not found in implement/SKILL.md"
        low = section.lower()
        assert "inline summary" in low, (
            "implement/SKILL.md ## After implementation must contain 'inline summary'"
        )
        assert "plain" in low or "human-readable" in low, (
            "implement/SKILL.md ## After implementation must contain 'plain' or 'human-readable'"
        )
        # Must appear before STOP line
        stop_pos = low.find("stop and wait")
        summary_pos = low.find("inline summary")
        assert summary_pos < stop_pos, (
            "Inline summary instruction must appear BEFORE the STOP line in ## After implementation"
        )

    def test_review_has_inline_summary(self):
        text = self._skill_text("review")
        section = _extract_section(text, SECTION_HEADINGS["review"])
        assert section, "## After the review section not found in review/SKILL.md"
        low = section.lower()
        assert "inline summary" in low, (
            "review/SKILL.md ## After the review must contain 'inline summary'"
        )
        assert "plain" in low or "human-readable" in low, (
            "review/SKILL.md ## After the review must contain 'plain' or 'human-readable'"
        )

    def test_inline_summary_count_review(self):
        """Review must instruct a summary on BOTH verdict branches (APPROVED + CHANGES_REQUESTED)."""
        text = self._skill_text("review")
        section = _extract_section(text, SECTION_HEADINGS["review"])
        count = section.lower().count("inline summary")
        assert count >= 2, (
            f"review/SKILL.md ## After the review should contain 'inline summary' at least twice "
            f"(once per verdict branch); found {count}"
        )

    def test_thorough_plan_has_envelope_branch(self):
        """A dispatched thorough_plan phase emits the return envelope instead of prose."""
        text = self._skill_text("thorough_plan")
        section = _extract_section(text, SECTION_HEADINGS["thorough_plan"])
        assert section, "## Final output section not found in thorough_plan/SKILL.md"
        low = section.lower()
        assert "return: envelope" in low, (
            "thorough_plan/SKILL.md ## Final output must name the 'return: envelope' sentinel"
        )
        assert "inline summary" in low, (
            "thorough_plan/SKILL.md ## Final output must still carry the inline-summary branch"
        )
        assert "otherwise" in low, (
            "thorough_plan/SKILL.md ## Final output must present the two branches as alternatives"
        )

    def test_implement_has_envelope_branch(self):
        """A dispatched implement phase emits the return envelope instead of prose."""
        text = self._skill_text("implement")
        section = _extract_section(text, SECTION_HEADINGS["implement"])
        assert section, "## After implementation section not found in implement/SKILL.md"
        low = section.lower()
        assert "return: envelope" in low, (
            "implement/SKILL.md ## After implementation must name the 'return: envelope' sentinel"
        )
        assert "inline summary" in low, (
            "implement/SKILL.md ## After implementation must still carry the inline-summary branch"
        )
        assert "otherwise" in low, (
            "implement/SKILL.md ## After implementation must present the two branches as alternatives"
        )

    def test_review_has_envelope_branch(self):
        """A dispatched review phase emits the return envelope instead of prose, on both outcome branches."""
        text = self._skill_text("review")
        section = _extract_section(text, SECTION_HEADINGS["review"])
        assert section, "## After the review section not found in review/SKILL.md"
        low = section.lower()
        assert "return: envelope" in low, (
            "review/SKILL.md ## After the review must name the 'return: envelope' sentinel"
        )
        assert "inline summary" in low, (
            "review/SKILL.md ## After the review must still carry the inline-summary branch"
        )
        assert "otherwise" in low, (
            "review/SKILL.md ## After the review must present the two branches as alternatives"
        )

    def test_envelope_branch_count_review(self):
        """Review must name the envelope branch on BOTH outcome branches (APPROVED + CHANGES_REQUESTED)."""
        text = self._skill_text("review")
        section = _extract_section(text, SECTION_HEADINGS["review"])
        count = section.lower().count("return: envelope")
        assert count >= 2, (
            f"review/SKILL.md ## After the review should name 'return: envelope' at least twice "
            f"(once per verdict branch); found {count}"
        )

    def test_fail_closed_sites_emit_no_envelope(self):
        """Producer-side guard: every fail-closed final-message line also carries the
        no-envelope clause, same line, in implement, end_of_task, and gate. File-level, not
        section-level — the fail-closed sites sit outside the sectioned After-implementation /
        After-the-review blocks this class otherwise reads. gate is covered because it runs
        INLINE inside an envelope-dispatched implement/review session, sharing the final-message
        slot with that phase's envelope branch (unlike rollback, which is not run-dispatched)."""
        fail_closed_literal = "gate-result: needs-decision` block as the final message"
        no_envelope_sentinel = "emits no envelope here"
        expected_counts = {"implement": 1, "end_of_task": 4, "gate": 1}
        for skill, expected in expected_counts.items():
            text = self._skill_text(skill).lower()
            lines = text.splitlines()
            literal_lines = [ln for ln in lines if fail_closed_literal in ln]
            sentinel_lines = [ln for ln in lines if no_envelope_sentinel in ln]
            co_occurring = [ln for ln in literal_lines if no_envelope_sentinel in ln]
            assert len(literal_lines) == expected, (
                f"{skill}/SKILL.md: expected {expected} fail-closed final-message line(s), "
                f"found {len(literal_lines)}"
            )
            assert len(sentinel_lines) == expected, (
                f"{skill}/SKILL.md: expected {expected} no-envelope sentinel line(s), "
                f"found {len(sentinel_lines)}"
            )
            assert len(co_occurring) == expected and len(co_occurring) > 0, (
                f"{skill}/SKILL.md: every fail-closed final-message line must also carry "
                f"'{no_envelope_sentinel}' on the SAME line; {len(co_occurring)}/{expected} co-occur"
            )


class TestClaudeMdCommunicationRule:
    """quoin/quoin/CLAUDE.md must define the shared End-of-step inline summary rule."""

    def test_communication_rule_exists(self):
        assert CORE_CLAUDE_MD.exists(), f"CLAUDE.md not found at {CORE_CLAUDE_MD}"
        text = CORE_CLAUDE_MD.read_text(encoding="utf-8")
        # Must be under ### Communication heading
        section = _extract_section(text, "### Communication")
        assert section, "### Communication section not found in quoin/quoin/CLAUDE.md"
        low = section.lower()
        assert "end-of-step inline summary" in low, (
            "quoin/quoin/CLAUDE.md ### Communication must contain 'End-of-step inline summary' rule"
        )
