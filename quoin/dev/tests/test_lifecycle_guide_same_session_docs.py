"""Prose-contract test: lifecycle-guide.md documents the /thorough_plan same-session guard."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIFECYCLE_GUIDE = REPO_ROOT / "quoin" / "memory" / "lifecycle-guide.md"


def _text() -> str:
    assert LIFECYCLE_GUIDE.exists(), f"Missing: {LIFECYCLE_GUIDE}"
    return LIFECYCLE_GUIDE.read_text(encoding="utf-8")


class TestLifecycleGuideSameSessionDocs:
    """IVG-105: same-session documentation in lifecycle-guide.md (T-03)."""

    def test_same_session_guard_documented(self):
        """lifecycle-guide.md must contain 'Same-session guard' for /thorough_plan §1b."""
        text = _text()
        assert "Same-session guard" in text, (
            "lifecycle-guide.md must document 'Same-session guard' under "
            "the /thorough_plan phase-boundary checkpoints section (IVG-105 T-03)."
        )
