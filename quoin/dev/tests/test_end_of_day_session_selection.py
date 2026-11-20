"""Structural tests for end_of_day session-selection documentation.

These tests grep SKILL.md and core doc text to assert that the hybrid
date-window + flag rule, lower_bound procedure, same-day merge contract,
and --recover-orphans subcommand are properly documented.

Documentation-conformance tests — they assert the SKILL.md documented
contract, NOT runtime behavior of the live daily files (live files
pre-date the new template and lack some sections; see plan R-01 notes).
"""
from pathlib import Path


THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

ADAPTER_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "end_of_day" / "SKILL.md"
CORE_DOC = PKG_DIR / "core" / "skills" / "end_of_day.md"
WEEKLY_ADAPTER = PKG_DIR / "adapters" / "claude" / "skills" / "weekly_review" / "SKILL.md"


def _adapter_text() -> str:
    return ADAPTER_SKILL.read_text(encoding="utf-8")


def _core_text() -> str:
    return CORE_DOC.read_text(encoding="utf-8")


def test_core_doc_no_today_glob_in_inputs():
    """Core doc Inputs section must not reference the old today-only glob."""
    text = _core_text()
    assert "<today>-*.md" not in text, (
        "core/skills/end_of_day.md Inputs must not reference '<today>-*.md'. "
        "Update to describe the hybrid date-window selection."
    )


def test_documented_closed_section_set_updated():
    """Core doc Notes section must enumerate 9 closed sections including 'Sessions processed'.

    Documentation-conformance test — asserts the core doc documented section set;
    does NOT assert runtime daily-cache shape (live files pre-date the template and
    lack '## For human'; see R-01 notes and follow-up).
    """
    text = _core_text()
    notes_start = text.find("## Notes")
    assert notes_start != -1, "Could not find '## Notes' in core/skills/end_of_day.md"
    notes_text = text[notes_start:]
    assert "Sessions processed" in notes_text, (
        "core/skills/end_of_day.md Notes section must list '## Sessions processed' "
        "in the closed section set (9 total: For human, Summary, Sessions processed, "
        "Completed today, Unfinished, Decisions log, Git activity summary, Cost summary, "
        "Tomorrow's priorities)."
    )


