"""SKILL.md-lint tests for autonomous perfectionist depth-within-profile (IVG-153, T-04).

Text-level guard over `run/SKILL.md` asserting the depth-tuning clauses are
present and profile-preserving, mirroring the repo's SKILL.md-lint style.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


def test_depth_section_present(run_skill_text: str) -> None:
    assert "Perfectionist depth-within-profile" in run_skill_text


def test_small_stays_single_pass_with_full_gate(run_skill_text: str) -> None:
    text = run_skill_text
    assert "`--autonomous small:` stays a Small, single-pass `/plan`" in text
    assert "Small stays single-pass, gate goes Full" in text
    assert "Full at every gate" in text


def test_medium_large_full_gate_profile_default_rounds_all_opus_revise(run_skill_text: str) -> None:
    text = run_skill_text
    assert "Full at every gate" in text
    assert "the profile default" in text.lower() or "profile default" in text.lower()
    assert "Medium 4 rounds, Large 5 rounds" in text
    assert "all-Opus revise" in text
    assert "profile itself unchanged" in text


def test_autonomous_never_upgrades_small_to_medium_or_large(run_skill_text: str) -> None:
    text = run_skill_text
    assert "Autonomous never maps a `small:` input to Medium or Large" in text
