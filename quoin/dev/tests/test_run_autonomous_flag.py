"""SKILL.md-lint tests for the `--autonomous` flag on /run (IVG-153, T-01/T-02/T-03).

These are text-level guards over `run/SKILL.md` — they assert the required
clauses/keywords are present, mirroring the repo's existing SKILL.md-lint
test style (grep the source, not a runtime harness).
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


def test_run_skill_parses_autonomous_token(run_skill_text: str) -> None:
    text = run_skill_text

    # Parse+strip block exists.
    assert "--autonomous" in text
    assert "Parse `--autonomous` flag" in text

    # Stripped BEFORE profile classification.
    assert "before profile classification" in text.lower()

    # Documented as opt-in / default off.
    assert "opt-in" in text.lower()
    assert "default off" in text.lower() or "default: off" in text.lower()

    # Internal state flag is set.
    assert "AUTONOMOUS=true" in text
    assert "AUTONOMOUS=false" in text


def test_run_injects_autonomous_marker_into_all_spawns(run_skill_text: str) -> None:
    text = run_skill_text

    assert "Autonomous propagation" in text
    assert "[autonomous]" in text

    # 9 direct sub-phases named alongside the sentinel.
    direct_phases = [
        "discover",
        "enrich",
        "specify",
        "architect",
        "thorough_plan",
        "implement",
        "review",
        "end_of_task",
        "gate",
    ]
    for phase in direct_phases:
        assert phase in text, f"direct sub-phase {phase!r} not documented"

    # Inline-gate direct-apply note.
    assert "Inline gates" in text
    assert "no spawn prompt to prefix" in text or "orchestrator applies autonomous gate behavior directly" in text

    # Transitive re-prefix rule.
    assert "Transitive propagation rule" in text
    assert "re-prefix" in text
    assert "/plan" in text and "/critic" in text and "/revise" in text and "/revise-fast" in text
    assert "/security_review" in text

    # Stacked-sentinel rule text.
    assert "Stacking" in text
    assert "stack" in text.lower()

    # Dedicated assertion: Phase-6 end_of_task spawn (L278) carries [autonomous].
    assert "end_of_task** (Phase 6, the terminal `/end_of_task` spawn) — prefix `[autonomous]`" in text


def test_checkpoints_autoresolve(run_skill_text: str) -> None:
    text = run_skill_text

    assert "## Checkpoint interaction protocol" in text
    assert "**Autonomous**" in text
    assert "PASS auto-resolves to \"continue\"" in text

    # Non-PASS never silently proceeds.
    assert "never" in text.lower()
    assert "silent proceed" in text.lower()
