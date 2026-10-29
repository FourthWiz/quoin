"""T-12: pitfall-injection presence test.

Verifies that format-kit-pitfalls.md is injected exactly once into each of the
8 target SKILL.md files (architect, plan, revise, revise-fast, review, critic, gate,
security_review) and NOT present in the 4 non-target skills (capture_insight,
implement, end_of_day, start_of_day).

Per Stage 4 D-04-rev2: target iff (a) output IS validated by validate_artifact.py
AND (b) writer follows the §5.3 Class B 5-step pattern (or Class A variant with
retry-with-recomposition). /capture_insight is OUT despite being validated because
its V-failure path is log-and-continue with no body recomposition.
"""
import re
from pathlib import Path


QUOIN_DIR = Path(__file__).parent.parent.parent  # quoin/ directory (3 levels up from test file)
ADAPTER_SKILLS_DIR = QUOIN_DIR / "adapters" / "claude" / "skills"
MIGRATED_SKILLS_DIR_OVERRIDES = {
    "architect": ADAPTER_SKILLS_DIR,
    "review": ADAPTER_SKILLS_DIR,
    "plan": ADAPTER_SKILLS_DIR,
    "critic": ADAPTER_SKILLS_DIR,
    "revise": ADAPTER_SKILLS_DIR,
    "revise-fast": ADAPTER_SKILLS_DIR,
    "gate": ADAPTER_SKILLS_DIR,
    "implement": ADAPTER_SKILLS_DIR,
    "security_review": ADAPTER_SKILLS_DIR,
}

TARGET_SKILLS = [
    "architect",
    "plan",
    "revise",
    "revise-fast",
    "review",
    "critic",
    "gate",
    "security_review",
]

NON_TARGET_SKILLS = [
    "capture_insight",
    "implement",
    "end_of_day",
    "start_of_day",
]

CANONICAL_REGEX = re.compile(
    r"Read .+format-kit-pitfalls\.md.+three pre-write reminders",
    re.DOTALL,
)

MARKER = "format-kit-pitfalls.md"


def skill_path(name: str) -> Path:
    base = MIGRATED_SKILLS_DIR_OVERRIDES.get(name, ADAPTER_SKILLS_DIR)
    return base / name / "SKILL.md"


def test_target_skills_contain_injection_exactly_once():
    """Each target skill must contain format-kit-pitfalls.md exactly once."""
    for skill in TARGET_SKILLS:
        path = skill_path(skill)
        assert path.exists(), f"SKILL.md not found for {skill}: {path}"
        content = path.read_text(encoding="utf-8")
        count = content.count(MARKER)
        assert count == 1, (
            f"{skill}/SKILL.md: expected exactly 1 occurrence of '{MARKER}', found {count}"
        )


def test_target_skills_canonical_wording():
    """Each target skill must match the canonical regex for the injected line."""
    for skill in TARGET_SKILLS:
        path = skill_path(skill)
        content = path.read_text(encoding="utf-8")
        assert CANONICAL_REGEX.search(content), (
            f"{skill}/SKILL.md: injected line does not match canonical regex "
            f"'Read .+format-kit-pitfalls.md.+three pre-write reminders'"
        )


def test_non_target_skills_have_no_injection():
    """Non-target skills must NOT contain format-kit-pitfalls.md."""
    for skill in NON_TARGET_SKILLS:
        path = skill_path(skill)
        if not path.exists():
            continue  # skip if skill not present (optional skills)
        content = path.read_text(encoding="utf-8")
        assert MARKER not in content, (
            f"{skill}/SKILL.md unexpectedly contains '{MARKER}' — accidental over-injection"
        )
