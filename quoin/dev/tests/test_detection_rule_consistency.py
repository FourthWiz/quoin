"""
Cross-skill byte-equality test for the §5.7.1 verbatim detection rule.

The fixture v3-detection-comment.txt is the single source of truth.
Every SKILL.md that reads current-plan.md must contain the 9-line block
verbatim (byte-for-byte), exactly once.
"""
import pathlib
import pytest

TESTS_DIR = pathlib.Path(__file__).parent
FIXTURE = TESTS_DIR / "fixtures" / "v3-detection-comment.txt"
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"

SKILL_FILES = [
    # Phase 11 migration: gate's authoritative SKILL.md lives in the Claude adapter path.
    ADAPTER_SKILLS_DIR / "gate" / "SKILL.md",
    # Phase 19 migration: expand's authoritative SKILL.md lives in the Claude adapter path.
    ADAPTER_SKILLS_DIR / "expand" / "SKILL.md",
    # Phase 9 migration: critic's authoritative SKILL.md lives in the Claude adapter path.
    ADAPTER_SKILLS_DIR / "critic" / "SKILL.md",
    # Phase 12 migration: implement's authoritative SKILL.md lives in the Claude adapter path.
    ADAPTER_SKILLS_DIR / "implement" / "SKILL.md",
    # Phase 8 migration: review's authoritative SKILL.md lives in the
    # Claude adapter path (the v3-detection-comment fixture moves with the body).
    ADAPTER_SKILLS_DIR / "review" / "SKILL.md",
    # Phase 9 migration: revise and revise-fast authoritative SKILL.md files live
    # in the Claude adapter path.
    ADAPTER_SKILLS_DIR / "revise" / "SKILL.md",
    ADAPTER_SKILLS_DIR / "revise-fast" / "SKILL.md",
    # Phase 7 migration: start_of_day's authoritative SKILL.md lives in the
    # Claude adapter path (the v3-detection-comment fixture moves with the body).
    ADAPTER_SKILLS_DIR / "start_of_day" / "SKILL.md",
]


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _first_line_bytes() -> bytes:
    return _fixture_bytes().split(b"\n")[0]


@pytest.mark.parametrize("skill_file", SKILL_FILES, ids=[f.parent.name for f in SKILL_FILES])
def test_each_skill_has_verbatim_comment_block(skill_file):
    """Fixture content must appear as a contiguous substring in each skill file (byte-level)."""
    fixture = _fixture_bytes()
    skill_bytes = skill_file.read_bytes()
    assert fixture in skill_bytes, (
        f"{skill_file.parent.name}/SKILL.md does not contain the verbatim §5.7.1 "
        f"detection rule block from {FIXTURE.name}. "
        "Copy the 9-line block byte-for-byte from the fixture into the skill file."
    )


@pytest.mark.parametrize("skill_file", SKILL_FILES, ids=[f.parent.name for f in SKILL_FILES])
def test_comment_block_count_per_file(skill_file):
    """The first line of the fixture must appear exactly once in each skill file."""
    first_line = _first_line_bytes()
    skill_bytes = skill_file.read_bytes()
    count = skill_bytes.count(first_line)
    assert count == 1, (
        f"{skill_file.parent.name}/SKILL.md contains the detection rule first line "
        f"{count} times (expected exactly 1). "
        "Check for accidental duplication or omission."
    )
