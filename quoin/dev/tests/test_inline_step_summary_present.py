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
