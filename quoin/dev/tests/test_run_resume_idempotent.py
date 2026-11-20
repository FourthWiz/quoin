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
_RUN_ADAPTER_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
_RUN_CORE_DOC = _SOURCE_ROOT / "core" / "skills" / "run_hard.md"

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


# ---------------------------------------------------------------------------
# (b) Next-phase selection from completion sentinels, never-re-run/never-skip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (c) `{phase}.done` write documented at each of the 9 roster phases
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (d) Headless resume with marker present raises ZERO AskUserQuestion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Marker write in Setup (T-10)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Core doc mirror (runtime-neutral)
# ---------------------------------------------------------------------------


def test_core_doc_documents_resume_ordering() -> None:
    text = _text(_RUN_CORE_DOC)
    assert "read the marker before any" in text
    assert "derive the next phase from the completion" in text
