"""Task-bookkeeping prose contracts across cleanup/SKILL.md,
the reference doc, and the runtime-neutral core docs.

Two-slicer pattern (lessons: scoped greps, not whole-file counts) plus a
handful of cross-file ordering and region-scoped-count assertions that pin
the report-only resume hint, and the unconditional Step 7
defer-to-the-pass rewording.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "quoin"
CLEANUP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"
REFERENCE_DOC = SOURCE_ROOT / "memory" / "cleanup-task-bookkeeping.md"
CORE_CLEANUP_DOC = SOURCE_ROOT / "core" / "skills" / "cleanup.md"
CLAUDE_MD = SOURCE_ROOT / "CLAUDE.md"
RULES_MD = SOURCE_ROOT / "core" / "workflow" / "rules.md"
TASK_LAYOUT_MD = SOURCE_ROOT / "core" / "workflow" / "task-layout.md"
README_MD = REPO_ROOT / "README.md"

_NEVER_ESCAPE_RE = re.compile(
    r"(?i)\b(run|invoke|call|execute)\b[^.\n]*/(pr|end_of_task)\b"
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice_to_next_h2(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    m = re.search(r"\n## ", rest)
    end = start + len(heading) + (m.start() if m else len(rest))
    return text[start:end]


def _slice_to_next_h3(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    m = re.search(r"\n### ", rest)
    end = start + len(heading) + (m.start() if m else len(rest))
    return text[start:end]


def _assert_no_unguarded_invocation_line(region: str, label: str) -> None:
    for i, line in enumerate(region.splitlines(), 1):
        if _NEVER_ESCAPE_RE.search(line) and "never" not in line.lower():
            raise AssertionError(
                f"{label} line {i} instructs invoking /pr or /end_of_task without "
                f"'never': {line!r}"
            )


# ---------------------------------------------------------------------------
# Slicer A: cleanup SKILL.md "## Task bookkeeping pass (standalone only)"
# ---------------------------------------------------------------------------

_BOOKKEEPING_HEADING = "## Task bookkeeping pass (standalone only)"


# ---------------------------------------------------------------------------
# Cross-cutting order + region-scoped counts in cleanup SKILL.md
# ---------------------------------------------------------------------------


def _core_procedure_region(text: str) -> str:
    start = text.index("## Core procedure")
    end = text.index(_BOOKKEEPING_HEADING)
    return text[start:end]


# ---------------------------------------------------------------------------
# Reference doc
# ---------------------------------------------------------------------------


def test_reference_doc_askuserquestion_sites_have_markers():
    text = _text(REFERENCE_DOC)
    for m in re.finditer(r"AskUserQuestion\(", text):
        preceding = text[: m.start()].splitlines()[-3:]
        assert any("decision-gate: best-effort" in ln for ln in preceding), (
            f"AskUserQuestion( at offset {m.start()} has no decision-gate marker "
            "within the preceding 3 lines"
        )


def test_reference_doc_apply_only_after_report_only_heading():
    text = _text(REFERENCE_DOC)
    report_only_idx = text.index("## Report-only conditions")
    before = text[:report_only_idx]
    assert not re.search(r"(?i)apply", before), (
        "reference doc mentions 'apply' before the report-only stop heading"
    )


def test_reference_doc_required_phrases():
    text = _text(REFERENCE_DOC)
    assert "Next commands (type these yourself):" in text
    assert "never invokes" in text.lower()
    assert "[no-redispatch] /cleanup" in text


def test_reference_doc_no_unguarded_pr_or_eot_invocation():
    text = _text(REFERENCE_DOC)
    _assert_no_unguarded_invocation_line(text, "reference doc")


# ---------------------------------------------------------------------------
# Runtime-neutral docs + rules
# ---------------------------------------------------------------------------


def test_core_cleanup_doc_names_buckets_and_flags():
    text = _text(CORE_CLEANUP_DOC)
    for token in ("five buckets", "--no-tasks", "finalized/"):
        assert token in text, f"core/skills/cleanup.md missing {token!r}"
    assert "dispatched" in text.lower() and "AskUserQuestion" in text


def test_claude_md_rules_and_task_layout_mention_cleanup_exception():
    # CLAUDE.md is the Claude adapter file, so the slash form is expected there.
    claude_text = _text(CLAUDE_MD)
    assert "/cleanup" in claude_text, "CLAUDE.md does not mention /cleanup in its finalization rule text"
    # rules.md and task-layout.md are runtime-neutral core/workflow docs and must
    # never use Claude Code slash-command syntax (test_core_workflow_portability_tokens.py);
    # they name the skill bare instead.
    for path in (RULES_MD, TASK_LAYOUT_MD):
        text = _text(path)
        assert "cleanup" in text.lower(), f"{path} does not mention the cleanup skill in its finalization rule text"
        assert "/cleanup" not in text, f"{path} must not use the /cleanup slash-command form (core/workflow is runtime-neutral)"


def test_readme_utilities_row_mentions_task_folder_bookkeeping():
    text = _text(README_MD)
    idx = text.index("/cleanup")
    window = text[idx: idx + 400]
    assert "task" in window.lower() and "bookkeeping" in window.lower()
