"""T-09: Static test that stream-idle retry policy is present in both orchestrator skills.

Checks:
  - thorough_plan/SKILL.md contains 'Stream idle timeout recovery' heading
  - run/SKILL.md contains 'Stream idle timeout recovery' heading
  - Both contain the verified string 'Stream idle timeout - partial response received'
    (empirically confirmed as the exact verbatim string from the Claude harness)
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
THOROUGH_PLAN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "thorough_plan" / "SKILL.md"
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"

# Verified string from live jsonl inspection (2026-05-01 — CRIT-2 resolution):
# 'Stream idle timeout' appears in tool_result.content[].text of user-type messages.
# The exact suffix is ' - partial response received' per the harness source.
VERIFIED_TIMEOUT_STRING = "Stream idle timeout - partial response received"


def _read(path: Path) -> str:
    assert path.exists(), f"SKILL.md not found at {path}"
    return path.read_text()


class TestStreamIdleRetryPolicyPresent:
    """Stream-idle retry policy presence assertions (T-08/T-09)."""

    def test_thorough_plan_has_retry_marker(self):
        """thorough_plan/SKILL.md must contain 'Stream-idle timeout recovery' text."""
        text = _read(THOROUGH_PLAN_SKILL)
        assert "Stream-idle timeout recovery" in text, (
            "thorough_plan/SKILL.md is missing 'Stream-idle timeout recovery' text"
        )

    def test_run_has_retry_marker(self):
        """run/SKILL.md must contain 'Stream-idle timeout recovery' text."""
        text = _read(RUN_SKILL)
        assert "Stream-idle timeout recovery" in text, (
            "run/SKILL.md is missing 'Stream-idle timeout recovery' text"
        )

    def test_thorough_plan_has_verified_timeout_string(self):
        """thorough_plan/SKILL.md must contain the verified verbatim timeout string."""
        text = _read(THOROUGH_PLAN_SKILL)
        assert VERIFIED_TIMEOUT_STRING in text, (
            f"thorough_plan/SKILL.md is missing the verified timeout string: "
            f"'{VERIFIED_TIMEOUT_STRING}'"
        )

    def test_run_has_verified_timeout_string(self):
        """run/SKILL.md must contain the verified verbatim timeout string."""
        text = _read(RUN_SKILL)
        assert VERIFIED_TIMEOUT_STRING in text, (
            f"run/SKILL.md is missing the verified timeout string: "
            f"'{VERIFIED_TIMEOUT_STRING}'"
        )
