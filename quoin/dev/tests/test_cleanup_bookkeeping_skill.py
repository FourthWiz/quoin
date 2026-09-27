"""IVG-262 T-12: task-bookkeeping prose contracts across cleanup/SKILL.md,
checkpoint/SKILL.md, the reference doc, and the runtime-neutral core docs.

Two-slicer pattern (lessons: scoped greps, not whole-file counts) plus a
handful of cross-file ordering and region-scoped-count assertions that pin
the D-16/D-17 decisions: the report-only resume hint, and the unconditional
Step 7/Step 8 defer-to-the-pass rewording.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "quoin"
CLEANUP_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"
CHECKPOINT_SKILL = SOURCE_ROOT / "adapters" / "claude" / "skills" / "checkpoint" / "SKILL.md"
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


def test_slicer_a_heading_occurs_exactly_once():
    text = _text(CLEANUP_SKILL)
    assert text.count(_BOOKKEEPING_HEADING) == 1


def test_slicer_a_contains_required_tokens():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    required = [
        "--no-tasks", "--dry-run", "[no-interactive]", "[autonomous]",
        "report-only", "task_bookkeeping.py classify", "cleanup-task-bookkeeping.md",
        "pidfile_release cleanup", "[no-redispatch] /cleanup", "skip steps 1",
        "not under",
    ]
    for token in required:
        assert token in region, f"slicer A region missing required token: {token!r}"


def test_slicer_a_no_unguarded_pr_or_eot_invocation():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    _assert_no_unguarded_invocation_line(region, "slicer A")


def test_slicer_a_region_non_vacuous():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    assert len(region) > 300


def test_slicer_a_mentions_askuserquestion_without_call_syntax():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    assert "AskUserQuestion" in region
    assert "AskUserQuestion(" not in region


def test_slicer_a_does_not_mention_step_5c_or_step_6():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    assert "Step 5c" not in region
    assert "Step 6" not in region


# ---------------------------------------------------------------------------
# Slicer B: checkpoint SKILL.md "### Step 1.47"
# ---------------------------------------------------------------------------


def test_slicer_b_contains_exact_exclusion_sentence():
    region = _slice_to_next_h3(_text(CHECKPOINT_SKILL), "### Step 1.47")
    assert (
        "The `/cleanup` task bookkeeping pass is never run here; it is standalone-only."
        in region
    )


def test_slicer_b_does_not_mention_task_bookkeeping_script():
    region = _slice_to_next_h3(_text(CHECKPOINT_SKILL), "### Step 1.47")
    assert "task_bookkeeping.py" not in region


# ---------------------------------------------------------------------------
# Cross-cutting order + region-scoped counts in cleanup SKILL.md
# ---------------------------------------------------------------------------


def test_order_step6_before_heading_before_last_pidfile_release():
    text = _text(CLEANUP_SKILL)
    step6_idx = text.index("**Step 6.")
    heading_idx = text.index(_BOOKKEEPING_HEADING)
    last_pidfile_idx = text.rindex("pidfile_release cleanup")
    assert step6_idx < heading_idx < last_pidfile_idx


def _core_procedure_region(text: str) -> str:
    start = text.index("## Core procedure")
    end = text.index(_BOOKKEEPING_HEADING)
    return text[start:end]


def test_core_procedure_region_has_zero_literal_pidfile_release_calls():
    region = _core_procedure_region(_text(CLEANUP_SKILL))
    assert region.count("pidfile_release cleanup") == 0


def test_bookkeeping_region_has_exactly_one_pidfile_release_after_step7():
    region = _slice_to_next_h2(_text(CLEANUP_SKILL), _BOOKKEEPING_HEADING)
    assert region.count("pidfile_release cleanup") == 1
    step7_idx = region.index("Step 7")
    release_idx = region.index("pidfile_release cleanup")
    assert step7_idx < release_idx


def test_core_procedure_step1_and_step2_are_unconditional_and_mention_pass():
    region = _core_procedure_region(_text(CLEANUP_SKILL))
    step1 = region[region.index("**Step 1."): region.index("**Step 2.")]
    step2 = region[region.index("**Step 2."): region.index("**Step 3.")]
    assert "Task bookkeeping pass" in step1
    assert "exit 0" not in step1
    assert "Task bookkeeping pass" in step2
    assert "exit 0" not in step2


def test_core_procedure_step7_body_kept_verbatim():
    region = _core_procedure_region(_text(CLEANUP_SKILL))
    assert "phase: `cleanup`" in region
    assert "cost-ledger-format.md" in region


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
