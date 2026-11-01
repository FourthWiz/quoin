"""
Structural-canary regression test for the inline-gate audit-log contract (Stage 3).

Positive cases assert that each SKILL.md file contains the phrases required to
ensure audit-log persistence fires at every gate boundary.

Negative cases prove the assertions are meaningful: for each positive check, a
corrupted in-memory copy (canary phrase deleted) must cause the assertion to fail.
"""
import pathlib
import pytest

QUOIN_ROOT = pathlib.Path(__file__).parent.parent.parent

GATE_SKILL = QUOIN_ROOT / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"
RUN_SKILL = QUOIN_ROOT / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
REVIEW_SKILL = QUOIN_ROOT / "adapters" / "claude" / "skills" / "review" / "SKILL.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _lines(text: str):
    return text.splitlines()


def _line_region(lines: list, anchor_phrase: str, radius: int = 20) -> str:
    """Return a text snippet within ±radius lines of the first line matching anchor_phrase."""
    for i, line in enumerate(lines):
        if anchor_phrase in line:
            start = max(0, i - radius)
            end = min(len(lines), i + radius + 1)
            return "\n".join(lines[start:end])
    return ""


# ---------------------------------------------------------------------------
# Parametrized positive + negative cases
# ---------------------------------------------------------------------------

POSITIVE_CASES = [
    pytest.param(
        "post-implement-primary",
        lambda: _read(RUN_SKILL),
        # After T-04, the primary post-implement gate line must say "inline"
        # and must NOT retain the old "never inline — the subagent must read gate/SKILL.md" wording
        lambda text: (
            "After implement completes, run `/gate` inline" in text
            and "never inline — the subagent must read gate/SKILL.md" not in text
        ),
        "run/SKILL.md post-implement primary site (line ~151) must say 'run /gate inline' and must not retain old 'never inline — the subagent' wording",
        id="post-implement-primary",
    ),
    pytest.param(
        "post-implement-recursive",
        lambda: _read(RUN_SKILL),
        # Lines ~169 and ~185 must contain "inline" in the gate re-run context
        lambda text: (
            "then re-run `/gate` inline" in text
            and "re-run the post-implementation gate inline" in text
        ),
        "run/SKILL.md recursive recovery paths (lines ~169, ~185) must say 'inline'",
        id="post-implement-recursive",
    ),
    pytest.param(
        "post-review-orchestrated",
        lambda: _read(RUN_SKILL),
        lambda text: "run `/gate` inline (Full level, post-review" in text,
        "run/SKILL.md post-review site (line ~182) must say 'run /gate inline'",
        id="post-review-orchestrated",
    ),
    pytest.param(
        "post-review-manual",
        lambda: _read(REVIEW_SKILL),
        lambda text: "Run `/gate` inline" in text and "/gate/SKILL.md" in text,
        "review/SKILL.md 'After the review' APPROVED branch must say 'Run /gate inline' and reference /gate/SKILL.md",
        id="post-review-manual",
    ),
    pytest.param(
        "gate-audit-log-must",
        lambda: _read(GATE_SKILL),
        lambda text: (
            "MUST write" in text
            and "audit log persistence" in text
        ),
        "gate/SKILL.md Step 5 region must contain both 'MUST write' and 'audit log persistence'",
        id="gate-audit-log-must",
    ),
]


@pytest.mark.parametrize("_name,reader,assertion,description", POSITIVE_CASES)
def test_positive(_name, reader, assertion, description):
    """Each positive case asserts the canary phrase is present in the SKILL.md."""
    text = reader()
    assert assertion(text), f"POSITIVE CASE FAILED: {description}"


@pytest.mark.parametrize("_name,reader,assertion,description", POSITIVE_CASES)
def test_negative_canary_deletion(_name, reader, assertion, description):
    """
    Each negative case proves the assertion is meaningful:
    deleting the key phrase from an in-memory copy must cause the assertion to FAIL.
    """
    text = reader()

    # Determine which phrase to delete to break this particular assertion
    canary_phrases = {
        "post-implement-primary": "After implement completes, run `/gate` inline",
        # ^ deleting this makes "After implement completes, run `/gate` inline" absent → assertion fails
        "post-implement-recursive": "then re-run `/gate` inline",
        "post-review-orchestrated": "run `/gate` inline (Full level, post-review",
        "post-review-manual": "Run `/gate` inline",
        "gate-audit-log-must": "MUST write",
    }
    phrase_to_delete = canary_phrases[_name]
    corrupted = text.replace(phrase_to_delete, "CANARY_DELETED")

    assert not assertion(corrupted), (
        f"NEGATIVE CANARY FAILED — deleting '{phrase_to_delete}' should break the assertion "
        f"but didn't. The assertion is too weak: {description}"
    )
