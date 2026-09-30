"""
test_planner_trace.py — asserts /plan writes and /critic reads .planner-trace.md correctly.

Two-layer structural test (SKILL.md string assertions only — no live LLM calls):

Layer 1 — plan/SKILL.md assertions:
  (a) Contains heading "## Write planner trace breadcrumb".
  (b) Contains "## Files read" and "## Patterns observed" in the breadcrumb section.
  (c) Contains the word "optional" near "## Gotchas".
  (d) Contains "skip" or "silently" near the fail-path sentence.

Layer 2 — critic/SKILL.md assertions:
  (e) Contains "## Critical rule: Breadcrumb independence".
  (f) Contains ".planner-trace.md" in the "## Session bootstrap" section.
  (g) Contains "search-prior" within the bootstrap breadcrumb step.
  (h) Contains "if it exists" or "if present" or "if absent" near the breadcrumb step.

Layer 3 — end_of_task/SKILL.md assertion:
  (i) Contains "planner-trace.md" and "fsops.py rm" in Sub-phase C steps.

Layer 4 — quoin/CLAUDE.md assertion:
  (j) Contains ".planner-trace.md" with "ephemeral" nearby in the Tier-1 section.
"""

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # Claude_workflow/
TESTS_DIR = Path(__file__).parent
SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"
CLAUDE_MD = _PROJECT_ROOT / "quoin" / "CLAUDE.md"
# Tier-1 catalog relocated here (claude-md-trim 2026-06-25)
TIER1_FILES_MD = _PROJECT_ROOT / "quoin" / "memory" / "tier1-files.md"

PLAN_SKILL = SKILLS_DIR / "plan" / "SKILL.md"
CRITIC_SKILL = SKILLS_DIR / "critic" / "SKILL.md"
EOT_SKILL = SKILLS_DIR / "end_of_task" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_section(text: str, heading: str) -> str:
    """Extract text from heading until the next top-level ## heading (or end of file).

    Works on the original text, ignoring ## headings that appear inside fenced
    code blocks by iterating line-by-line and tracking fence state.
    """
    lines = text.split("\n")
    in_section = False
    in_fence = False
    collected: list[str] = []
    fence_start = re.compile(r"^```")

    for line in lines:
        if in_section:
            if fence_start.match(line):
                in_fence = not in_fence
            if not in_fence and re.match(r"^## ", line):
                # Next real ## heading ends the section
                break
            collected.append(line)
        else:
            if line.startswith(heading):
                in_section = True
                collected.append(line)

    return "\n".join(collected)


# --- Layer 1: plan/SKILL.md ---

def test_plan_skill_has_breadcrumb_heading():
    """plan/SKILL.md must contain the breadcrumb section heading exactly once."""
    text = _read(PLAN_SKILL)
    count = text.count("## Write planner trace breadcrumb")
    assert count == 1, (
        f"Expected 1 occurrence of '## Write planner trace breadcrumb' in plan/SKILL.md, "
        f"found {count}"
    )


def test_plan_breadcrumb_schema_required_fields():
    """plan/SKILL.md breadcrumb section must document ## Files read and ## Patterns observed."""
    text = _read(PLAN_SKILL)
    section = _extract_section(text, "## Write planner trace breadcrumb")
    assert section, "## Write planner trace breadcrumb section not found in plan/SKILL.md"
    assert "## Files read" in section, (
        "'## Files read' not found in breadcrumb section of plan/SKILL.md"
    )
    assert "## Patterns observed" in section, (
        "'## Patterns observed' not found in breadcrumb section of plan/SKILL.md"
    )


def test_plan_breadcrumb_gotchas_optional():
    """plan/SKILL.md breadcrumb section must document ## Gotchas as optional."""
    text = _read(PLAN_SKILL)
    section = _extract_section(text, "## Write planner trace breadcrumb")
    assert section, "## Write planner trace breadcrumb section not found in plan/SKILL.md"
    assert "optional" in section.lower(), (
        "The word 'optional' not found near ## Gotchas in breadcrumb section of plan/SKILL.md"
    )
    assert "## Gotchas" in section, (
        "'## Gotchas' not found in breadcrumb section of plan/SKILL.md"
    )


def test_plan_breadcrumb_fail_silent():
    """plan/SKILL.md breadcrumb section must instruct skipping silently on path-resolution failure."""
    text = _read(PLAN_SKILL)
    section = _extract_section(text, "## Write planner trace breadcrumb")
    assert section, "## Write planner trace breadcrumb section not found in plan/SKILL.md"
    has_skip = "skip" in section.lower()
    has_silent = "silently" in section.lower()
    assert has_skip or has_silent, (
        "Neither 'skip' nor 'silently' found in breadcrumb section of plan/SKILL.md — "
        "fail-silent behavior must be documented"
    )


# --- Layer 2: critic/SKILL.md ---

def test_critic_has_breadcrumb_independence_rule():
    """critic/SKILL.md must contain the breadcrumb independence rule section exactly once."""
    text = _read(CRITIC_SKILL)
    count = text.count("## Critical rule: Breadcrumb independence")
    assert count == 1, (
        f"Expected 1 occurrence of '## Critical rule: Breadcrumb independence' in critic/SKILL.md, "
        f"found {count}"
    )


def test_critic_bootstrap_reads_breadcrumb():
    """critic/SKILL.md Session bootstrap must reference .planner-trace.md."""
    text = _read(CRITIC_SKILL)
    bootstrap_section = _extract_section(text, "## Session bootstrap")
    assert bootstrap_section, "## Session bootstrap section not found in critic/SKILL.md"
    assert ".planner-trace.md" in bootstrap_section, (
        "'.planner-trace.md' not found in ## Session bootstrap of critic/SKILL.md"
    )


def test_critic_bootstrap_search_prior():
    """critic/SKILL.md breadcrumb step in bootstrap must say 'search-prior'."""
    text = _read(CRITIC_SKILL)
    bootstrap_section = _extract_section(text, "## Session bootstrap")
    assert bootstrap_section, "## Session bootstrap section not found in critic/SKILL.md"
    assert "search-prior" in bootstrap_section, (
        "'search-prior' not found in ## Session bootstrap of critic/SKILL.md"
    )


def test_critic_bootstrap_graceful_absence():
    """critic/SKILL.md breadcrumb step must handle file absence gracefully."""
    text = _read(CRITIC_SKILL)
    bootstrap_section = _extract_section(text, "## Session bootstrap")
    assert bootstrap_section, "## Session bootstrap section not found in critic/SKILL.md"
    has_if_exists = "if it exists" in bootstrap_section.lower()
    has_if_present = "if present" in bootstrap_section.lower()
    has_if_absent = "if absent" in bootstrap_section.lower()
    assert has_if_exists or has_if_present or has_if_absent, (
        "Graceful-absence language ('if it exists', 'if present', or 'if absent') not found "
        "in ## Session bootstrap of critic/SKILL.md"
    )


# --- Layer 3: end_of_task/SKILL.md ---

def test_end_of_task_deletes_breadcrumb():
    """end_of_task/SKILL.md Sub-phase C must delete .planner-trace.md through the fsops helper, fail-silently."""
    text = _read(EOT_SKILL)
    assert "planner-trace.md" in text, (
        "'planner-trace.md' not found in end_of_task/SKILL.md"
    )
    section = text[text.index("**Step 8: Dispatch Sub-phase C"):text.index("## Important behaviors")]
    assert 'fsops.py rm "<task_dir>/.planner-trace.md"' in section, (
        "Sub-phase C does not delete the planner trace with fsops.py rm"
    )
    # The marker name and the helper call appear near each other (within 500 chars).
    idx_trace = text.find("planner-trace.md")
    idx_rm = text.find("fsops.py rm")
    assert abs(idx_trace - idx_rm) < 500, (
        "'planner-trace.md' and 'fsops.py rm' are not near each other in end_of_task/SKILL.md "
        f"(positions: {idx_trace}, {idx_rm})"
    )


# --- Layer 4: quoin/CLAUDE.md / tier1-files.md ---

def test_claude_md_tier3_note():
    """The Tier-3 note about .planner-trace.md with 'ephemeral' nearby must exist.

    (claude-md-trim 2026-06-25): the Tier-1 catalog was extracted from CLAUDE.md
    into quoin/memory/tier1-files.md, so the .planner-trace.md Tier-3 paragraph
    now lives in tier1-files.md. Check both files for the note.
    """
    # The Tier-3 ephemeral note may appear in the catalog file (tier1-files.md)
    # or remain in CLAUDE.md; accept either.
    claude_text = _read(CLAUDE_MD)
    tier1_text = _read(TIER1_FILES_MD)
    combined = claude_text + tier1_text

    assert ".planner-trace.md" in combined, (
        "'.planner-trace.md' not found in quoin/CLAUDE.md or quoin/memory/tier1-files.md"
    )
    # Find the position and check 'ephemeral' is nearby (within 200 chars)
    idx = combined.find(".planner-trace.md")
    window = combined[max(0, idx - 50): idx + 200]
    assert "ephemeral" in window.lower(), (
        "'ephemeral' not found within 200 chars of '.planner-trace.md' "
        "in quoin/CLAUDE.md or quoin/memory/tier1-files.md"
    )
    # Must NOT be in the Source files sub-list (Tier 1 inventory) of tier1-files.md
    source_files_idx = tier1_text.find("**Source files:**")
    if source_files_idx != -1:
        after_source = tier1_text[source_files_idx:]
        lines = after_source.split("\n")
        source_lines = []
        for i, line in enumerate(lines):
            if i == 0:
                source_lines.append(line)
                continue
            stripped = line.strip()
            if stripped and not stripped.startswith("- ") and not stripped.startswith("  "):
                break
            source_lines.append(line)
        source_block = "\n".join(source_lines)
        assert ".planner-trace.md" not in source_block, (
            "'.planner-trace.md' appears in the **Source files:** sub-list of tier1-files.md "
            "— it must appear in the Tier-3 guidance paragraph instead"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
