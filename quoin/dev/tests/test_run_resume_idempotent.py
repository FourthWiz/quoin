"""Tests for IVG-153 Stage 2 T-09/T-10: sub-phase-granular, idempotent
`/run --resume`.

T-09 is the reader half (`## Resume`): on resume, read the T-05/T-10 marker
FIRST — before any other decision point — to re-establish `AUTONOMOUS=true`
without reverting to interactive; then derive the next phase from the
per-phase completion sentinels (never re-run finished, never skip
unfinished), falling back to session-state prose only when no sentinel
directory exists.

T-10 is the writer half: `/run` writes the marker at autonomous entry
(Setup, right after `AUTONOMOUS=true`), and writes each `{phase}.done`
completion sentinel at all 9 resumable phase-completion sites.

Covers plan acceptance:
- (a) Resume reads the marker and sets AUTONOMOUS before the first decision
  point.
- (b) Resume documents next-phase selection from `{phase}.done` sentinels
  with the never-re-run/never-skip invariant.
- (c) A `{phase}.done` write is documented at each phase in the T-05
  9-phase roster (derived from `quoin.supervisor.RESUMABLE_PHASES`, never a
  frozen count or a "1..6" range) — ties to the T-05 coverage guard.
- (d) A headless `/run --resume --autonomous {task}` path with the marker
  present is asserted to raise ZERO `AskUserQuestion`.
- Marker write documented in Setup, gated on `AUTONOMOUS`, atomic, and
  inert for plain (non-autonomous) `/run`.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_RUN_ADAPTER_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
_RUN_CORE_DOC = _SOURCE_ROOT / "core" / "skills" / "run.md"

# Phase name -> the "## Phase N —" heading prefix that opens its section in
# run/SKILL.md. Used only to slice each phase's own section for the
# per-phase completion-sentinel-write assertion (c); the phase SET itself
# is derived live from quoin.supervisor.RESUMABLE_PHASES, never hardcoded.
_PHASE_HEADINGS = {
    "discover": "## Phase 1 —",
    "enrich": "## Phase 1.4 —",
    "specify": "## Phase 1.5 —",
    "fast_path_triage": "## Phase 1.6 —",
    "architect": "## Phase 2 —",
    "thorough_plan": "## Phase 3 —",
    "implement": "## Phase 4 —",
    "review": "## Phase 5 —",
    "end_of_task": "## Phase 6 —",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert _RUN_ADAPTER_SKILL.is_file(), f"missing: {_RUN_ADAPTER_SKILL}"
    return _text(_RUN_ADAPTER_SKILL)


@pytest.fixture(scope="module")
def resumable_phases() -> tuple:
    supervisor = importlib.import_module("quoin.supervisor")
    return supervisor.RESUMABLE_PHASES


# ---------------------------------------------------------------------------
# (a) Resume reads the marker + sets AUTONOMOUS before the first decision point
# ---------------------------------------------------------------------------


def test_resume_reads_marker_before_first_decision_point(run_skill_text: str) -> None:
    text = run_skill_text
    assert "## Resume" in text
    start = text.index("## Resume")
    end = text.index("## Session state tracking", start)
    section = text[start:end]

    assert "autonomous-run-{task}.marker" in section
    # The marker read must be documented as happening BEFORE anything else —
    # "Step 0" ordering, and explicit "FIRST" language.
    step0_idx = section.index("Step 0")
    step1_idx = section.index("Step 1")
    assert step0_idx < step1_idx, "marker read (Step 0) must precede next-phase selection (Step 1)"
    assert "FIRST" in section[step0_idx:step1_idx]
    assert "AUTONOMOUS=true" in section[step0_idx:step1_idx]


# ---------------------------------------------------------------------------
# (b) Next-phase selection from completion sentinels, never-re-run/never-skip
# ---------------------------------------------------------------------------


def test_resume_selects_next_phase_from_completion_sentinels(run_skill_text: str) -> None:
    text = run_skill_text
    start = text.index("## Resume")
    end = text.index("## Session state tracking", start)
    section = text[start:end]

    assert "autonomous-progress-{task}/" in section
    assert "never re-run it" in section
    assert "never skip it" in section
    # Sub-phase partial-resume clause.
    assert "sub-phase" in section.lower()
    assert "{phase}.{subphase}.done" in section


def test_resume_falls_back_to_session_state_when_no_sentinel_dir(run_skill_text: str) -> None:
    """Plain resume (no sentinel contract present) must still work exactly
    as it did pre-T-09, via session-state prose."""
    text = run_skill_text
    start = text.index("## Resume")
    end = text.index("## Session state tracking", start)
    section = text[start:end]

    assert "fall back to the pre-T-09 behavior" in section
    assert ".workflow_artifacts/memory/sessions/<latest>-<task-name>.md" in section


# ---------------------------------------------------------------------------
# (c) `{phase}.done` write documented at each of the 9 roster phases
# ---------------------------------------------------------------------------


def test_completion_sentinel_write_at_every_resumable_phase(
    run_skill_text: str, resumable_phases: tuple
) -> None:
    """Derived from the live T-05 roster (quoin.supervisor.RESUMABLE_PHASES) —
    never a frozen count or a "1..6" abbreviation. A future added/renamed
    phase without a documented `{phase}.done` write fails this test."""
    text = run_skill_text
    assert set(resumable_phases) == set(_PHASE_HEADINGS), (
        "the local phase->heading map is out of sync with "
        "quoin.supervisor.RESUMABLE_PHASES — update _PHASE_HEADINGS"
    )
    assert "enrich" in resumable_phases
    assert "specify" in resumable_phases

    headings_in_order = sorted(
        ((text.index(h), phase) for phase, h in _PHASE_HEADINGS.items() if h in text)
    )
    assert len(headings_in_order) == len(_PHASE_HEADINGS), "not every phase heading was found in run/SKILL.md"

    missing = []
    for i, (idx, phase) in enumerate(headings_in_order):
        end = headings_in_order[i + 1][0] if i + 1 < len(headings_in_order) else len(text)
        section = text[idx:end]
        expected = f"autonomous-progress-{{task}}/{phase}.done"
        if expected not in section:
            missing.append(phase)

    assert not missing, f"phases missing a documented completion-sentinel write: {missing}"


# ---------------------------------------------------------------------------
# (d) Headless resume with marker present raises ZERO AskUserQuestion
# ---------------------------------------------------------------------------


def test_headless_autonomous_resume_raises_zero_askuserquestion(run_skill_text: str) -> None:
    text = run_skill_text
    start = text.index("## Resume")
    end = text.index("## Session state tracking", start)
    section = text[start:end]

    assert "Headless autonomous path" in section
    headless_idx = section.index("Headless autonomous path")
    headless_clause = section[headless_idx : headless_idx + 600]
    assert "ZERO" in headless_clause
    assert "AskUserQuestion" in headless_clause
    assert "zero `AskUserQuestion` prompts" in headless_clause


# ---------------------------------------------------------------------------
# Marker write in Setup (T-10)
# ---------------------------------------------------------------------------


def test_marker_written_on_autonomous_entry(run_skill_text: str) -> None:
    text = run_skill_text
    assert "Write the autonomous-span marker" in text
    start = text.index("Write the autonomous-span marker")
    end = text.index("### Parse input and determine task profile", start)
    section = text[start:end]

    assert "autonomous-run-{task}.marker" in section or "autonomous-run-<task-name>.marker" in section
    assert "AUTONOMOUS=true" in section
    # Atomic-write idiom.
    assert "fsops.py write-atomic" in section
    # Inert for plain /run.
    assert "AUTONOMOUS=false" in section

    # This site must fall AFTER the flag-parsing block that sets AUTONOMOUS.
    flag_parse_idx = text.index("Set an internal state flag `AUTONOMOUS=true`")
    assert flag_parse_idx < start, "marker write must come after AUTONOMOUS is set, not before"


def test_plain_run_never_writes_marker(run_skill_text: str) -> None:
    text = run_skill_text
    start = text.index("Write the autonomous-span marker")
    end = text.index("### Parse input and determine task profile", start)
    section = text[start:end]
    assert "never writes this marker" in section


# ---------------------------------------------------------------------------
# Core doc mirror (runtime-neutral)
# ---------------------------------------------------------------------------


def test_core_doc_documents_resume_ordering() -> None:
    text = _text(_RUN_CORE_DOC)
    assert "read the marker before any" in text
    assert "derive the next phase from the completion" in text
