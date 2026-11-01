"""Guard test: /architect and /plan skills must contain feature-existence pre-flight phrases.

Regression guard pinning canonical substrings introduced in IVG-94 so the
instruction cannot silently regress.
"""
from pathlib import Path

QUOIN_DIR = Path(__file__).parent.parent.parent  # quoin/quoin/ package dir
ADAPTER_SKILLS = QUOIN_DIR / "adapters" / "claude" / "skills"
CORE_SKILLS = QUOIN_DIR / "core" / "skills"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestArchitectPreflightPhrases:
    ARCHITECT_SKILL = ADAPTER_SKILLS / "architect" / "SKILL.md"

    def test_primary_phrase(self):
        assert "Feature-existence pre-flight" in _read(self.ARCHITECT_SKILL)

    def test_git_log_phrase(self):
        assert "git log --oneline -30" in _read(self.ARCHITECT_SKILL)

    def test_fail_open_clause(self):
        assert "If `git log` is unavailable" in _read(self.ARCHITECT_SKILL)

    def test_already_implemented_phrase(self):
        assert "already implemented in" in _read(self.ARCHITECT_SKILL)

    def test_conventions_real_files(self):
        assert "conventions against real files" in _read(self.ARCHITECT_SKILL)

    def test_confirm_existence(self):
        assert "confirm the feature doesn't already exist" in _read(self.ARCHITECT_SKILL)

class TestPlanPreflightPhrases:
    PLAN_SKILL = ADAPTER_SKILLS / "plan" / "SKILL.md"

    def test_primary_phrase(self):
        assert "Feature-existence pre-flight" in _read(self.PLAN_SKILL)

    def test_git_log_phrase(self):
        assert "git log --oneline -30" in _read(self.PLAN_SKILL)

    def test_fail_open_clause(self):
        assert "If `git log` is unavailable" in _read(self.PLAN_SKILL)

    def test_conventions_real_files(self):
        assert "conventions against real files" in _read(self.PLAN_SKILL)

class TestCoreContractPhrases:
    ARCHITECT_CORE = CORE_SKILLS / "architect.md"
    PLAN_CORE = CORE_SKILLS / "plan.md"

    def test_architect_core_feature_phrase(self):
        assert "feature does not already exist" in _read(self.ARCHITECT_CORE)

    def test_plan_core_feature_phrase(self):
        assert "feature does not already exist" in _read(self.PLAN_CORE)
