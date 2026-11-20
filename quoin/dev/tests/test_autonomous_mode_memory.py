"""Tests for IVG-153 T-18: autonomous-mode.md memory file and installer wiring.

Mirrors the AST-parse pattern in test_discovery_refresh_routine.py.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"

_DOC = _SOURCE_ROOT / "memory" / "autonomous-mode.md"
_INSTALLER = _REPO_ROOT / "src" / "quoin" / "installer.py"

# The full transitive spawn closure (currently 15 skills — see current-plan.md
# ## State transitive_skills). The auto-resolution table must name each one.
_TRANSITIVE_SKILLS = (
    "run",
    "discover",
    "enrich",
    "specify",
    "architect",
    "thorough_plan",
    "plan",
    "critic",
    "revise",
    "revise-fast",
    "implement",
    "gate",
    "review",
    "security_review",
    "end_of_task",
)


def _text() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    assert _DOC.exists(), f"autonomous-mode.md not found: {_DOC}"


def test_doc_starts_with_h1() -> None:
    assert _text().strip().startswith("# "), (
        "autonomous-mode.md must start with an H1 heading (Tier-1)"
    )


def test_auto_resolution_table_lists_all_transitive_skills() -> None:
    text = _text()
    missing = [s for s in _TRANSITIVE_SKILLS if s not in text]
    assert not missing, (
        f"autonomous-mode.md auto-resolution table is missing skills: {missing}"
    )


def test_auto_resolution_table_covers_end_of_task() -> None:
    text = _text()
    assert "end_of_task" in text, "autonomous-mode.md must cover end_of_task"
    assert "Abort" in text, (
        "autonomous-mode.md must document that end_of_task's commit prompt "
        "never auto-selects Abort"
    )


def test_doc_has_worktree_fail_open_row() -> None:
    text = _text()
    assert "§0-worktree" in text or "worktree-fallback" in text, (
        "autonomous-mode.md must document the §0-worktree hand-synced fail-OPEN row"
    )
    assert "fail-OPEN" in text, (
        "autonomous-mode.md must state the worktree-class fail-OPEN behavior"
    )


def test_doc_has_sentinel_and_propagation_rule() -> None:
    text = _text()
    assert "[autonomous]" in text, "autonomous-mode.md must document the [autonomous] sentinel"
    assert "transitive" in text.lower(), (
        "autonomous-mode.md must document the transitive propagation rule"
    )
    assert "end_of_task" in text and "run" in text, (
        "autonomous-mode.md must document the run→end_of_task direct spawn edge"
    )


def test_doc_has_formulation_bar_and_confidence_knob() -> None:
    text = _text()
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in text, (
        "autonomous-mode.md must document the QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD env knob"
    )
    assert "0.7" in text, "autonomous-mode.md must document the default threshold value (0.7)"


def test_doc_has_halt_sentinel_contract() -> None:
    text = _text()
    for field in ("task", "phase", "reason", "timestamp", "resume_hint"):
        assert field in text, (
            f"autonomous-mode.md halt-sentinel contract must document field: {field}"
        )
    assert "autonomous-halt-" in text, (
        "autonomous-mode.md must document the stable halt-sentinel file location"
    )


def test_doc_has_relocated_run_exception_paragraph() -> None:
    text = _text()
    assert "the gate confirmations provide the safety checkpoints" in text, (
        "autonomous-mode.md must contain the relocated verbose "
        "'Exception: /run orchestrator.' paragraph from CLAUDE.md"
    )
