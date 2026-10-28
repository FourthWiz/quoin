"""Guard tests for IVG-246 T-04: freeze the heading structure of the
Setup..Phase 1.6 region of `run/SKILL.md` against accidental heading-literal
leakage, and pin the `## Checkpoint interaction protocol` table's row count.

Two independent claims:

1. **Freeze-guard (CRIT-2).** The prefix of `run/SKILL.md` up to (but not
   including) `## Phase 2 — Architect` — i.e. Session bootstrap through the
   Phase 1.6 fast-path-triage placeholder — must not accidentally gain a
   second occurrence of any heading literal that belongs to a LATER section
   (Phase 2 onward), beyond the two known, already-inline baseline mentions
   (`` `## Resume` `` and `` `## Checkpoint interaction protocol` ``, each
   referenced exactly once in backticked prose). This is what makes the
   round-3 CRIT-2 fix (P-03b: new Phase 1.6 prose must not restate a later
   heading) mechanically checkable rather than only prose-documented.
2. **Row-count baseline (CRIT-1).** `## Checkpoint interaction protocol`'s
   table has exactly 6 filtered data rows (mirroring
   `test_plain_run_unchanged.py::test_checkpoint_protocol_row_count_unchanged`'s
   own predicate, copied verbatim) and exactly 8 raw pipe-leading lines. Any
   future prose inserted near the Phase 1.6 insertion point that accidentally
   drifts this table's row count will trip this guard.

The freeze-list is derived at RUNTIME from the live headings found in the
file (plus the `_PHASE_HEADINGS` prefix literals imported from
`test_run_resume_idempotent.py`) — no fixed 11- or 16-element literal list
is hardcoded here, so a future heading is covered automatically.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_RUN_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"

_TESTS_DIR = Path(__file__).resolve().parent

# Load test_run_resume_idempotent.py by file path (mirrors the
# importlib.util.spec_from_file_location pattern already used in
# test_plain_run_unchanged.py for cross-module symbol access) so we can
# reuse its `_PHASE_HEADINGS` mapping without hardcoding a duplicate copy.
_idempotent_spec = importlib.util.spec_from_file_location(
    "test_run_resume_idempotent_for_heading_freeze",
    _TESTS_DIR / "test_run_resume_idempotent.py",
)
assert _idempotent_spec is not None
_idempotent_mod = importlib.util.module_from_spec(_idempotent_spec)
assert _idempotent_spec.loader is not None
_idempotent_spec.loader.exec_module(_idempotent_mod)
_PHASE_HEADINGS = _idempotent_mod._PHASE_HEADINGS

# Both baseline mentions already appear exactly once, inline, in backticked
# prose ahead of the "## Phase 2 — Architect" cut point on the tree as it
# stands after T-02 (verified this session; a zero-baseline rule would be
# born RED against the actual file).
BASELINE = {"## Resume": 1, "## Checkpoint interaction protocol": 1}


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert _RUN_SKILL.exists(), f"run/SKILL.md not found at {_RUN_SKILL}"
    return _RUN_SKILL.read_text(encoding="utf-8")


def test_pre_phase2_heading_literals_stay_within_baseline(run_skill_text: str) -> None:
    text = run_skill_text
    cut = text.index("## Phase 2 — Architect")
    prefix, rest = text[:cut], text[cut:]

    later = {line for line in rest.splitlines() if line.startswith("## ")}
    # Add the phase-heading PREFIX literals (e.g. "## Phase 2 —") whose
    # target section actually falls in `rest`, so short-prefix collisions
    # are caught too, not just full-line collisions.
    for prefix_literal in _PHASE_HEADINGS.values():
        if prefix_literal in rest:
            later.add(prefix_literal)

    for heading in sorted(later):
        allowed = BASELINE.get(heading, 0)
        found = prefix.count(heading)
        assert found <= allowed, (
            f"heading literal {heading!r} (belongs to a later section) appears "
            f"{found} time(s) in the pre-'## Phase 2 — Architect' prefix, "
            f"exceeding the baseline of {allowed} — new prose near the Phase "
            "1.6 insertion point must not restate a later section's heading"
        )


def test_checkpoint_protocol_row_count_baseline(run_skill_text: str) -> None:
    """Mirrors test_plain_run_unchanged.py::test_checkpoint_protocol_row_count_unchanged's
    exact slice and row filter (copied verbatim, not paraphrased) so the two
    guards move in lockstep. Pins BOTH the filtered data-row count (6) and
    the raw pipe-leading line count (8) — the round-2 draft of this guard
    asserted only the raw count, mislabeled as the filtered baseline; the
    filtered baseline is 6, the raw count is 8, and they are deliberately
    different assertions."""
    text = run_skill_text
    start = text.index("## Checkpoint interaction protocol")
    end = text.index("## Resume", start)
    table_slice = text[start:end]

    raw_pipe_lines = [
        line for line in table_slice.splitlines() if line.strip().startswith("|")
    ]
    assert len(raw_pipe_lines) == 8, (
        f"expected exactly 8 raw pipe-leading lines (header + separator + 6 "
        f"data rows), found {len(raw_pipe_lines)}: {raw_pipe_lines}"
    )

    data_rows = [
        line
        for line in table_slice.splitlines()
        if line.strip().startswith("|")
        and not line.strip().startswith("| Response")
        and not line.strip().startswith("|--")
        and not line.strip().startswith("|-")
    ]
    assert len(data_rows) == 6, (
        f"expected exactly 6 filtered protocol data rows (5 non-autonomous + "
        f"1 autonomous), found {len(data_rows)}: {data_rows}"
    )
