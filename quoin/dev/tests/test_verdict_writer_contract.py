"""The review and critic skills tell writers to produce only verdict shapes the gate accepts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import gate

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "quoin" / "adapters" / "claude" / "skills"
REVIEW_SRC = (SKILLS / "review" / "SKILL.md").read_text(encoding="utf-8")
CRITIC_SRC = (SKILLS / "critic" / "SKILL.md").read_text(encoding="utf-8")

REVIEW = gate.REVIEW_VERDICTS
CRITIC = gate.CRITIC_VERDICTS


def test_review_skill_states_the_value_only_contract():
    assert "the Verdict section holds one line, the value alone" in REVIEW_SRC
    assert "go in `## Test Coverage`, never in the Verdict section" in REVIEW_SRC
    assert "describe them in words rather than quoting them" in REVIEW_SRC
    assert "Describe earlier rounds in a sentence" in REVIEW_SRC
    assert "name an HTML element without angle brackets" in REVIEW_SRC
    assert "catches honest formatting mistakes" in REVIEW_SRC
    assert "`### Authored-content lint (advisory)`" in REVIEW_SRC
    assert REVIEW_SRC.count("in `## Test Coverage` (never in the Verdict section)") == 2


def test_critic_skill_states_the_contract():
    assert "Write `## Verdict: PASS` or `## Verdict: REVISE` on its own line, followed directly by `## Summary`" in CRITIC_SRC
    assert "write a per-finding judgement as `Assessment:`" in CRITIC_SRC
    assert "describe label and heading shapes in words rather than quoting them" in CRITIC_SRC
    assert "No other heading names the verdict or a value, in any case" in CRITIC_SRC
    assert "Describe earlier rounds in a sentence" in CRITIC_SRC
    assert "name it in words" in CRITIC_SRC
    assert "catches honest formatting mistakes" in CRITIC_SRC


def _template_headings(text):
    headings = []
    for match in re.finditer(r"^- `(## [^`]+)`", text, re.M):
        headings.append(match.group(1))
    block = text[text.index("Body content example"):] if "Body content example" in text else ""
    block = block.split("```markdown", 1)[-1].split("```", 1)[0] if block else ""
    headings += [ln for ln in block.splitlines() if ln.startswith("#")]
    return headings


@pytest.mark.parametrize("src", [REVIEW_SRC, CRITIC_SRC], ids=["review", "critic"])
def test_template_headings_do_not_name_a_verdict_value(src):
    headings = _template_headings(src)
    assert headings
    for heading in headings:
        if re.match(r"^## Verdict(?::|$| \|)", heading) or heading.startswith("## Verdict:") or heading == "## Dimension Verdicts" or heading.startswith("## Dimension Verdicts"):
            continue
        folded = heading.casefold()
        assert "verdict" not in folded, heading
        for word in ("blocked", "revise", "changes requested", "changes_requested"):
            assert word not in folded, heading


def review_doc(summary="Round 3 asked for changes, now fixed.", coverage="N/A - no active quoin task context.", value="APPROVED"):
    return (
        "## Summary\n\n%s\n\n## Verdict\n\n%s\n\n## Plan Compliance\n\nok\n\n## Test Coverage\n\n%s\n"
        % (summary, value, coverage)
    )


def test_approving_review_in_the_stated_shape_parses():
    assert gate.parse_verdict(review_doc(), REVIEW) == "APPROVED"
    assert gate.parse_verdict(review_doc(summary="Previous round: CHANGES_REQUESTED."), REVIEW) is None


def critic_doc(judgement):
    return (
        "## Verdict: PASS\n\n## Summary\n\nok\n\n## Issues\n\n### Major\n\n"
        "- **[MAJ-1] first**\n  - %s\n- **[MAJ-2] second**\n  - %s\n"
    ) % (judgement, judgement)


def test_critic_response_in_the_stated_shape_parses():
    assert gate.parse_verdict(critic_doc("Assessment: fix is correct."), CRITIC) == "PASS"
    assert gate.parse_verdict(critic_doc("**Verdict:** Fix is correct."), CRITIC) is None


@pytest.mark.parametrize("value", REVIEW)
def test_every_review_value_with_annotations_in_test_coverage(value):
    assert gate.parse_verdict(review_doc(value=value), REVIEW) == value
    assert gate.parse_verdict(review_doc(value="<verdict>%s</verdict>" % value), REVIEW) == value


@pytest.mark.parametrize("value", CRITIC)
def test_every_critic_value_parses(value):
    text = "## Verdict: %s\n\n## Summary\n\nok\n" % value
    assert gate.parse_verdict(text, CRITIC) == value


def test_annotation_in_the_verdict_section_refuses():
    text = review_doc(value="APPROVED\nN/A - no active quoin task context")
    assert gate.parse_verdict(text, REVIEW) is None
