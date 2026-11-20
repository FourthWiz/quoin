"""Tests for IVG-153 Stage 2 T-12: within-phase subagent isolation +
PARTIAL continuation.

Under `AUTONOMOUS`, `/run` maximizes true subagent isolation for each
phase it spawns: the phase subagent returns a PATH plus a short summary
never raw content — so the orchestrator's own transcript stays small
across a multi-relaunch autonomous span. A phase subagent that nears its
own limit writes a checkpoint to disk and returns a structured `PARTIAL`
signal instead of exhausting itself mid-phase; the orchestrator responds
by dispatching a FRESH subagent to continue that same phase from the
checkpoint (mirroring the existing stream-idle "re-dispatch a fresh
narrower child" recovery pattern already documented in
`## Error handling`).

Per the plan's T-03 POC note, if a subagent cannot resolve its own
transcript utilization, the fallback is a fixed tool-use/scope cap
(mirroring `end_of_task`'s existing "Scope cap: ~N tool uses; if blocked,
write to disk and return" pattern) that returns `PARTIAL` deterministically.

These are SKILL.md-lint assertions (grep the documented text), mirroring
the style of `test_run_resume_idempotent.py` and
`test_autonomous_hooks_untouched.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
RUN_CORE_DOC = PKG_DIR / "core" / "skills" / "run_hard.md"


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.is_file(), f"missing: {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def subagent_mgmt_section(run_skill_text: str) -> str:
    text = run_skill_text
    start = text.index("## Subagent session management")
    end = text.index("## Parallel tasks", start)
    return text[start:end]


@pytest.fixture(scope="module")
def error_handling_section(run_skill_text: str) -> str:
    text = run_skill_text
    start = text.index("## Error handling")
    end = text.index("## Hook cooperation (autonomous)", start)
    return text[start:end]


# ---------------------------------------------------------------------------
# (a) Paths-not-content is a HARD autonomous requirement
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (b) PARTIAL-signal contract: subagent writes checkpoint + returns PARTIAL
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (c) Orchestrator branch: dispatch a fresh subagent to continue on PARTIAL
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (d) References the T-03-chosen util/scope-cap mechanism
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Core doc mirror (runtime-neutral, no forbidden tokens — separately
# enforced by test_run_adapter_pilot.py::test_core_skill_doc_no_forbidden_tokens)
# ---------------------------------------------------------------------------


def test_core_doc_documents_within_phase_mechanism() -> None:
    text = RUN_CORE_DOC.read_text(encoding="utf-8")
    assert "only paths and short summaries" in text
    assert "writes a checkpoint to disk" in text
    assert "partial-completion signal" in text
    assert "fresh subagent to continue the same phase" in text
