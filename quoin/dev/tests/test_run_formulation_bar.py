"""SKILL.md-lint tests for the autonomous Formulation quality bar
(IVG-153, T-12/T-13).

Text-level guard over `run/SKILL.md` (and companion files) asserting the
Formulation->Execution bar is documented between Phase 3 and Phase 4, keys
on critic-PASS convergence for Medium/Large, keys on smoke-gate PASS +
confidence threshold for Small, only fires under AUTONOMOUS, and routes a
below-bar formulation to the halt-sentinel hard-stop path. Mirrors the
repo's existing SKILL.md-lint style (see test_run_autonomous_depth.py).
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
PLAN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "plan" / "SKILL.md"
AUTONOMOUS_MODE_DOC = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plan_skill_text() -> str:
    assert PLAN_SKILL.exists(), f"plan/SKILL.md not found at {PLAN_SKILL}"
    return PLAN_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def autonomous_mode_text() -> str:
    assert AUTONOMOUS_MODE_DOC.exists(), f"autonomous-mode.md not found at {AUTONOMOUS_MODE_DOC}"
    return AUTONOMOUS_MODE_DOC.read_text(encoding="utf-8")


def test_formulation_bar_section_present(run_skill_text: str) -> None:
    assert "Formulation quality bar" in run_skill_text


def test_bar_sits_between_phase_3_and_phase_4(run_skill_text: str) -> None:
    text = run_skill_text
    phase3_idx = text.index("## Phase 3 — Thorough Plan")
    bar_idx = text.index("## Formulation quality bar (autonomous)")
    phase4_idx = text.index("## Phase 4 — Implement")
    assert phase3_idx < bar_idx < phase4_idx, (
        "Formulation quality bar must sit between Phase 3 and Phase 4"
    )


def test_medium_large_revise_at_cap_halts(run_skill_text: str) -> None:
    text = run_skill_text
    assert "require the `thorough_plan` critic loop to have converged with a **PASS** verdict" in text
    assert "does **NOT** pass the bar" in text
    assert "REVISE" in text


def test_small_underspecified_halts_with_sentinel(run_skill_text: str) -> None:
    text = run_skill_text
    assert "the post-plan smoke gate to PASS" in text
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in text
    assert "Hard-stop #6" in text
    assert "write the halt-sentinel" in text
    assert "Do **NOT** enter Phase 4 / Execution" in text


def test_bar_only_under_autonomous(run_skill_text: str) -> None:
    text = run_skill_text
    assert "Only evaluated under `AUTONOMOUS`" in text
    assert "plain `/run` never evaluates this bar" in text


def test_confidence_threshold(run_skill_text: str, plan_skill_text: str, autonomous_mode_text: str) -> None:
    # (a) threshold env knob (default 0.7) read+compared, below halts
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in run_skill_text
    assert "default `0.7`" in run_skill_text
    assert "Hard-stop #6" in run_skill_text  # below threshold routes to the hard stop
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in autonomous_mode_text
    assert "0.7" in autonomous_mode_text

    # (b) Small-path confidence signal is attributed to plan, not specify
    assert "single-pass `/plan` skill's own `confidence: <float 0..1>` line" in run_skill_text
    assert "confidence: <float 0..1>" in plan_skill_text
    assert "`[autonomous]`, Small path" in plan_skill_text or "Small path" in plan_skill_text

    # (c) knob name present in autonomous-mode.md (already covered above,
    # asserted again explicitly per task wording)
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in autonomous_mode_text
