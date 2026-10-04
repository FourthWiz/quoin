"""The workflow gate: path checks, verdict parsing, critic status (part one)."""
from __future__ import annotations

import os
import shutil

import pytest

import _opencode_gate_helpers as h
import _opencode_helpers as helpers
from quoin.opencode_adapter import gate

SOURCE = helpers.SOURCE_DIR
GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- verdict parsing -------------------------------------------------------

CRITIC = ("PASS", "REVISE")
REVIEW = ("APPROVED", "CHANGES_REQUESTED", "BLOCKED")
TAG = "<verdict>APPROVED</verdict>"
PTAG = "<verdict>PASS</verdict>"
FENCE = "`" * 3
FENCE4 = "`" * 4
CAN = "## Verdict\n\nAPPROVED\n"
PCAN = "## Verdict: PASS\n"


def doc(body, pre="## Summary\n\nx\n\n", post="\n\n## Plan Compliance\n\ny\n"):
    """A review whose Verdict section holds `body`."""
    return pre + "## Verdict\n\n" + body + post


# A finalized review: frontmatter, For human, Summary, a bare value, then the
# dimension table.
FINAL_REVIEW = """---
task: demo
stage: 1
verdict: APPROVED
---
## For human

Everything lines up.

## Summary

The work matches the plan.

## Verdict

APPROVED

## Dimension Verdicts

| dimension | verdict | top issue |
|---|---|---|
| correctness | APPROVED | none |
"""

# A finalized critic response in heading form followed by the summary.
FINAL_CRITIC = """## Verdict: REVISE

## Summary

One major issue remains.

## Issues

### Major

- **[MAJ-1] T-09 cannot pass as written**
"""

ACCEPT = [
    ("bare value", REVIEW, doc("APPROVED"), "APPROVED"),
    ("bare tag", REVIEW, doc(TAG), "APPROVED"),
    ("tag in a code span", REVIEW, doc("`" + TAG + "`"), "APPROVED"),
    ("bold value", REVIEW, doc("**APPROVED**"), "APPROVED"),
    ("bold value with period", REVIEW, doc("**APPROVED.**"), "APPROVED"),
    ("heading form then summary", CRITIC, "## Verdict: PASS\n## Summary\n\nok\n", "PASS"),
    ("heading form with blank section", CRITIC, "## Verdict: PASS\n\n## Summary\n\nok\n", "PASS"),
    ("bare value then rule", REVIEW, doc("APPROVED\n\n---"), "APPROVED"),
    ("changes requested parses", REVIEW, doc("CHANGES_REQUESTED"), "CHANGES_REQUESTED"),
    ("blocked parses", REVIEW, doc("BLOCKED"), "BLOCKED"),
    ("agreeing frontmatter", REVIEW, "---\nverdict: APPROVED\n---\n" + CAN, "APPROVED"),
    ("quoted agreeing frontmatter", REVIEW, '---\nverdict: "APPROVED"\n---\n' + CAN, "APPROVED"),
    ("agreeing summary line", REVIEW, "## Summary\n\nVerdict: APPROVED.\n\n" + CAN, "APPROVED"),
    ("label starting with the value", REVIEW,
     "## Summary\n\n**Verdict:** APPROVED - all affected tests green\n\n" + CAN, "APPROVED"),
    ("crlf document", REVIEW, ("## Summary\n\nx\n\n" + CAN).replace("\n", "\r\n"), "APPROVED"),
    ("symbols outside the section", REVIEW, "## Summary\n\n✅ done → next — fine\n\n" + CAN, "APPROVED"),
    ("verb pass in a bold list item", CRITIC,
     "## Verdict: PASS\n\n## Summary\n\nx\n\n## Issues\n\n- **[MAJ-1] T-09 cannot pass as written**\n", "PASS"),
    ("dimension table header", REVIEW,
     CAN + "\n## Dimension Verdicts\n\n| dimension | verdict | top issue |\n|---|---|---|\n| security | APPROVED | - |\n",
     "APPROVED"),
    ("dimension table with another value", REVIEW,
     CAN + "\n## Dimension Verdicts\n\n| dimension | verdict |\n|---|---|\n| security | CHANGES_REQUESTED |\n",
     "APPROVED"),
    ("real finalized review", REVIEW, FINAL_REVIEW, "APPROVED"),
    ("real critic response", CRITIC, FINAL_CRITIC, "REVISE"),
    ("non-approving value with a later approving line", REVIEW,
     doc("CHANGES_REQUESTED", post="\n\n## Summary\n\n- APPROVED once fixed\n"), "CHANGES_REQUESTED"),
]


def build_refusals():
    c, r, p = CRITIC, REVIEW, PTAG
    rows = []

    def add(name, allowed, text):
        rows.append((name, allowed, text))

    # a second statement of a different value next to the section
    add("standalone value then a tag line", r, doc("CHANGES_REQUESTED\n\n" + TAG))
    add("standalone value then a tag in prose", r, doc("CHANGES_REQUESTED\n\nOnce fixed this becomes " + TAG))
    add("critic REVISE and a bare PASS tag", c, doc("REVISE\n\n" + p))
    add("critic REVISE with a later bare PASS tag", c, "## Verdict\n\nREVISE\n\n## Summary\n\n" + p + "\n")
    add("bold value then a tag", r, doc("**CHANGES_REQUESTED**\n\n" + TAG))
    add("negated tag", r, doc("Not " + TAG + " yet"))
    add("tag in an indented block", r, doc("CHANGES_REQUESTED\n\n    " + TAG))
    add("fence with an info string closing an earlier fence", r,
        FENCE + "\n## Verdict\n\n" + TAG + "\n" + FENCE + "python\n\n## Verdict\n\nREVISE\n")
    add("second exact Verdict heading", r, CAN + "\n## Summary\n\nx\n\n## Verdict\n\nAPPROVED\n")
    add("bullet value then a tag", r, doc("- CHANGES_REQUESTED\n\n" + TAG))
    add("numbered value then a tag", r, doc("1. CHANGES_REQUESTED\n\n" + TAG))
    add("level three heading value then a tag", r, doc("### CHANGES_REQUESTED\n\n" + TAG))
    add("tag then a level one heading value", r, doc(TAG + "\n\n# CHANGES_REQUESTED"))
    add("table cell value then a tag", r, doc("| CHANGES_REQUESTED |\n\n" + TAG))
    add("label with a value and a tag on one line", r, doc("**Verdict:** CHANGES_REQUESTED, once fixed " + TAG))
    add("html bold value then a tag", r, doc("<b>CHANGES_REQUESTED</b>\n\n" + TAG))
    add("tag then a value on the same line", r, doc(TAG + " actually CHANGES_REQUESTED"))
    add("critic Result label with REVISE", c, doc("Result: REVISE\n\n" + p))
    add("four backtick fence holding a three backtick line", r,
        doc(FENCE4 + "\n" + FENCE + "\n" + TAG + "\n" + FENCE4 + "\nCHANGES_REQUESTED: two blockers\n" + FENCE))
    for name, first in (
        ("heading with leading spaces", "  ## Verdict"),
        ("heading with closing hashes", "## Verdict ##"),
        ("setext heading", "Verdict\n==="),
        ("heading with a zero width space", "## Verdict​"),
        ("level one heading", "# Verdict"),
    ):
        add("first Verdict heading: " + name, r, first + "\n\nCHANGES_REQUESTED\n\n## Verdict\n\n" + TAG + "\n")
    add("bullet value, rule, after a tag", r, doc(TAG + "\n- CHANGES_REQUESTED: two blockers\n---"))
    add("quoted value, rule, after a tag", r, doc(TAG + "\n> CHANGES_REQUESTED\n---"))
    add("value, fence, rule, after a tag", r,
        doc(TAG + "\nCHANGES_REQUESTED\n" + FENCE + "\nx\n" + FENCE + "\n---"))
    add("adjacent value and rule", r, doc(TAG + "\nCHANGES_REQUESTED\n---"))
    add("critic bullet value and rule", c, doc(p + "\n- REVISE: one major\n---"))
    add("bold spaced value after a tag", r, doc(TAG + "\n\n**CHANGES REQUESTED**: two blockers"))
    add("prose spelling after a tag", r, doc(TAG + "\n\nChanges requested, see issues."))
    add("hyphen spelling after a tag", r, doc(TAG + "\n\nChanges-requested"))
    add("escaped underscore after a tag", r, doc(TAG + "\n\nCHANGES\\_REQUESTED"))
    add("zero width space inside a value after a tag", r, doc(TAG + "\n\nCHANGES_​REQUESTED"))
    add("bold Verdict heading", r, "## **Verdict**\n\nCHANGES_REQUESTED\n\n## Verdict\n\n" + TAG + "\n")
    add("code span Verdict heading", r, "## `Verdict`\n\nCHANGES_REQUESTED\n\n## Verdict\n\n" + TAG + "\n")
    add("html Verdict heading", r, "<h2>Verdict</h2>\n\nCHANGES_REQUESTED\n\n## Verdict\n\n" + TAG + "\n")
    add("heading form section with a prose value", r,
        "## Verdict: APPROVED\n\nCHANGES_REQUESTED: two blockers remain\n")
    add("fence opener indented four spaces", r,
        doc("CHANGES_REQUESTED\n\n    " + FENCE + "\n" + TAG + "\n    " + FENCE))
    add("comment opener in a code span", r,
        "## Summary\n\n`<!--`\n\n## Verdict\n\nCHANGES_REQUESTED\n\n`-->`\n" + TAG + "\n")
    add("comment opener in a code span before an approving section", r,
        "## Summary\n\nsee `<!--` here\n\n" + CAN + "\n## Plan Compliance\n\nend `-->` x\n")
    # later headings that name the verdict, next to a canonical section
    for name, heading in (
        ("after re-run", "## Verdict (after re-run)"),
        ("round two", "## Verdict — round 2"),
        ("updated", "## Updated verdict"),
        ("final", "## Final verdict"),
    ):
        add("later heading: " + name, r, CAN + "\n" + heading + "\n\nCHANGES_REQUESTED\n")
    for name, heading in (("plural", "## Verdicts"), ("period", "## Verdict."), ("article", "## The verdict")):
        add("earlier heading: " + name, r, heading + "\n\nCHANGES_REQUESTED\n\n" + CAN)
    add("Cyrillic letter in a heading", r, "## Vеrdict\n\nCHANGES_REQUESTED\n\n" + CAN)
    add("reversed heading by a bidi override", r, "## ‮TCIDREV\n\nCHANGES_REQUESTED\n\n" + CAN)
    add("carriage return hides a section", r, "Context\r## Verdict\rCHANGES_REQUESTED\n\n" + CAN)
    for name, line in (
        ("not approved yet", "**Verdict:** Not approved yet."),
        ("needs changes", "**Verdict:** Needs changes."),
        ("small capitals", "**Verdict:** ᴄʜᴀɴɢᴇs ʀᴇǫᴜᴇsᴛᴇᴅ"),
        ("bidi reversed", "‮:tcidreV DETSEUQER_SEGNAHC"),
        ("dash then words", "**Verdict** — changes requested"),
    ):
        add("label line: " + name, r, "## Summary\n\n" + line + "\n\n" + CAN)
    add("critic heading form ended by a revision heading", c, "## Verdict: PASS\n# Needs revision\n\nx\n")
    add("review section ended by a rejection heading", r, CAN + "## REJECTED\n\nx\n")
    add("review section ended by a not approved heading", r, CAN + "## Not approved\n\nx\n")
    add("canonical section inside a script block", r, "<script>\n" + CAN + "</script>\n")
    add("canonical section inside a style block", r, "<style>\n" + CAN + "</style>\n")
    add("canonical section inside a processing instruction", r, "<?\n" + CAN + "?>\n")
    add("canonical section inside a cdata block", r, "<![CDATA[\n" + CAN + "]]>\n")
    add("frontmatter value then a different one", r, "---\nverdict: APPROVED\nverdict: CHANGES_REQUESTED\n---\n" + CAN)
    # outside the section, next to a canonical section
    add("summary value with trailing text", r, CAN + "\n## Summary\n\nCHANGES_REQUESTED: two blockers remain.\n")
    add("summary bullet value", r, CAN + "\n## Summary\n\n- CHANGES_REQUESTED\n")
    add("summary quoted bold value", r, CAN + "\n## Summary\n\n> **CHANGES_REQUESTED**\n")
    add("summary table row", r, CAN + "\n## Summary\n\n| overall | CHANGES_REQUESTED |\n|---|---|\n")
    add("summary Result label", r, CAN + "\n## Summary\n\n**Result:** CHANGES_REQUESTED\n")
    add("later heading in title case", r, CAN + "\n## Summary\n\nx\n\n# Changes Requested\n")
    add("later heading with a negation", r, CAN + "\n## Summary\n\nx\n\n## Not approved: changes requested\n")
    add("critic later revise heading", c, "## Verdict: PASS\n\n## Summary\n\nx\n\n# Revise\n\nthe plan.\n")
    add("script opener in a blockquote before the heading", r, "> <script>\n\n" + CAN + "\n## Summary\n\n</script>\n")
    add("style opener in a list item before the heading", r, "- <style>\n\n" + CAN + "\n## Summary\n\n</style>\n")
    add("pre opener in a numbered item before the heading", r, "1. <pre>\n\n" + CAN + "\n## Summary\n\n</pre>\n")
    add("inline script opener ending a paragraph", r, "See notes <script>\n\n" + CAN + "\n## Summary\n\n</script>\n")
    add("inline textarea opener ending a paragraph", r, "x <textarea>\n\n" + CAN + "\n## Summary\n\n</textarea>\n")
    # line boundaries and hidden structure
    add("U+2028 hides a section", r, "Context ## Verdict CHANGES_REQUESTED\n\n" + CAN)
    add("U+0085 hides a section", r, "Context\x85## Verdict\x85CHANGES_REQUESTED\n\n" + CAN)
    add("details block hides the section", r, "<details>\n\n" + CAN + "\n## Summary\n\nx\n")
    add("frontmatter key in another case", r, "---\nVerdict: CHANGES_REQUESTED\n---\n" + CAN)
    add("nested frontmatter key", r, "---\nreview:\n  verdict: CHANGES_REQUESTED\n---\n" + CAN)
    add("bold pseudo heading after the section", r, CAN + "\n**Verdict (after re-run)**\n\nCHANGES_REQUESTED\n")
    add("bold label with a qualifier", r, "## Summary\n\n**Verdict (after re-run)**: changes requested\n\n" + CAN)
    add("inline mention with a value", r, "## Summary\n\nDone, with verdict CHANGES_REQUESTED.\n\n" + CAN)
    add("combining strikethrough on every letter", r, doc("".join(ch + "̶" for ch in "APPROVED")))
    add("digit for a letter in a heading", r, "## VERD1CT\n\nCHANGES_REQUESTED\n\n" + CAN)
    add("lower l for a capital I in a heading", r, "## VERDlCT\n\nCHANGES_REQUESTED\n\n" + CAN)
    add("setext Verdict after the section", r, CAN + "\nVerdict (updated)\n---\n\nCHANGES_REQUESTED\n")
    add("html Verdict heading after the section", r, CAN + "\n<h2>Final verdict</h2>\n\nCHANGES_REQUESTED\n")
    add("value heading in a later section", r, CAN + "\n## Summary\n\nx\n\n### CHANGES_REQUESTED\n")
    add("fenced Verdict example holding a value", r, CAN + "\n" + FENCE + "\n## Verdict\nCHANGES_REQUESTED\n" + FENCE + "\n")
    add("replacement character", r, CAN + "�\n")
    add("standalone bold value in a later section", r, CAN + "\n## Summary\n\n**CHANGES_REQUESTED**\n")
    add("Cyrillic letter inside a value next to a tag", r, doc(TAG + "\n\nCHANGЕS_REQUESTED"))
    add("fullwidth value next to a tag", r, doc(TAG + "\n\nＣＨＡＮＧＥＳ_REQUESTED"))
    add("comment wrapping the only tag", r, doc("<!--\n" + TAG + "\n-->"))
    add("frontmatter disagrees", r, "---\nverdict: CHANGES_REQUESTED\n---\n" + CAN)
    add("summary label with changes requested", r, "## Summary\n\n**Verdict:** Changes requested.\n\n" + CAN)
    add("lowercase value alone", r, doc("approved"))
    add("value then blocked", r, doc("APPROVED\n\nblocked"))
    add("value then not approved yet", r, doc("APPROVED\n\nNot approved yet."))
    add("value with an annotation", r, doc("APPROVED — N/A, no active quoin task context"))
    add("tag then an affected tests line", r, doc(TAG + "\nAffected tests: exit 0"))
    add("heading form with a parenthetical", c, "## Verdict: PASS (with notes)\n\n## Summary\n\nok\n")
    add("heading with no value line", r, "## Verdict\n\n## Summary\n\nok\n")
    add("two different value lines", r, doc("APPROVED\nBLOCKED"))
    add("unclosed frontmatter", r, "---\nverdict: APPROVED\n" + CAN)
    add("advisory heading naming the verdict", r,
        CAN + "\n## Plan Compliance\n\nok\n\n### Advisory (does not affect the verdict)\n\nlint\n")
    add("heading form value outside the allowed set", r, "## Verdict: PASS\n\n## Summary\n\nok\n")
    add("section value outside the allowed set", r, doc("PASS"))
    add("section line indented four spaces", r, doc("    APPROVED"))
    add("section line starting with a tab", r, doc("\tAPPROVED"))
    add("equals underline in the section", r, doc("APPROVED\n==="))
    add("heading with an unclosed fence before it", r, FENCE + "\n\n" + CAN)
    add("heading directly after prose", r, "Context line\n" + CAN)
    add("no Verdict heading", r, "## Summary\n\nAPPROVED\n")
    add("empty text", r, "")
    # one row per verification mutation, refused by its own rule only
    add("bold value then a rule", r, "## Summary\n\nx\n\n## Verdict\n\n**APPROVED**\n---\n\n## Plan Compliance\n\ny\n")
    add("bidi heading holding lower case prose", r, "## ‮TCIDREV\n\nchanges requested\n\n" + CAN)
    add("Cyrillic heading holding lower case prose", r, "## Vеrdict\n\nchanges requested\n\n" + CAN)
    add("fenced Verdict example holding lower case prose", r, CAN + "\n## Summary\n\n" + FENCE + "\n## Verdict\n\nnot approved\n" + FENCE + "\n")
    add("table row without a trailing pipe", r, CAN + "\n## Summary\n\n| overall | CHANGES_REQUESTED\n|---|---\n")
    return rows


REFUSALS = build_refusals()


def test_the_table_is_large_enough():
    assert len(REFUSALS) >= 105
    assert len({name for name, _a, _t in REFUSALS}) == len(REFUSALS)
    assert len(ACCEPT) >= 20


@pytest.mark.parametrize("name,allowed,text,expected", ACCEPT, ids=[row[0] for row in ACCEPT])
def test_parse_verdict_accepts(name, allowed, text, expected):
    assert gate.parse_verdict(text, allowed) == expected


@pytest.mark.parametrize("name,allowed,text", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_parse_verdict_refuses(name, allowed, text):
    assert gate.parse_verdict(text, allowed) is None
    value, reason, _line = gate._parse_verdict(text, allowed)
    assert value is None and reason


def test_a_critic_response_never_approves_a_review_value():
    assert gate.parse_verdict(doc("PASS"), REVIEW) is None
    assert gate.parse_verdict(doc("APPROVED"), CRITIC) is None


# -- refusal reasons -------------------------------------------------------


def line_of(text, line):
    return text.split("\n").index(line) + 1


def reason_rows():
    r = REVIEW
    after_blank = "## Summary\n\nx\n\n## Verdict\n\n"
    rows = []
    t = "a\rb\n" + CAN
    rows.append(("line-break", r, t, 1))
    t = "x\n‮\n" + CAN
    rows.append(("bidi", r, t, 2))
    t = CAN + "�\n"
    rows.append(("undecodable", r, t, 4))
    t = "---\nverdict: APPROVED\n" + CAN
    rows.append(("frontmatter-unclosed", r, t, 1))
    t = "---\nverdict: >\n  APPROVED\n---\n" + CAN
    rows.append(("frontmatter-key", r, t, 2))
    t = "---\nverdict: APPROVED\nverdict: BLOCKED\n---\n" + CAN
    rows.append(("frontmatter-duplicate", r, t, 3))
    t = "## VERD1CT\n\nBLOCKED\n\n" + CAN
    rows.append(("lookalike", r, t, 1))
    t = "## Summary\n\nAPPROVED\n"
    rows.append(("heading-count", r, t, None))
    t = CAN + "\n## Summary\n\nx\n\n## Verdict\n\nAPPROVED\n"
    rows.append(("heading-count", r, t, 9))
    t = "## Verdict ##\n\nAPPROVED\n"
    rows.append(("heading-spelling", r, t, 1))
    t = "## Verdict: PASS\n"
    rows.append(("heading-value", r, t, 1))
    t = "See the <title> element\n\n" + CAN
    rows.append(("raw-text-before", r, t, 1))
    t = "<details>\n\n" + CAN
    rows.append(("html-block-before", r, t, 1))
    t = FENCE + "\n\n" + CAN
    rows.append(("heading-hidden", r, t, 3))
    t = "Context line\n" + CAN
    rows.append(("heading-not-after-blank", r, t, 2))
    t = after_blank + "    APPROVED\n"
    rows.append(("indented", r, t, 7))
    t = after_blank + "**APPROVED**\n---\n"
    rows.append(("setext", r, t, 8))
    t = after_blank + "APPROVED\nNot approved yet.\n"
    rows.append(("section-line", r, t, 8))
    t = after_blank + "PASS\n"
    rows.append(("section-value", r, t, 7))
    t = "## Verdict\n\n## Summary\n"
    rows.append(("value-count", r, t, 1))
    t = "---\nverdict: BLOCKED\n---\n" + CAN
    rows.append(("frontmatter-disagree", r, t, 1))
    t = "## Summary\n\n**Verdict:** Not approved yet.\n\n" + CAN
    rows.append(("label-disagree", r, t, 3))
    t = CAN + "\n## Summary\n\nBLOCKED\n"
    rows.append(("value-line-elsewhere", r, t, 7))
    t = CAN + "\n## Summary\n\n- BLOCKED: two\n"
    rows.append(("approving-line-start", r, t, 7))
    t = CAN + "\n## Summary\n\n| overall | BLOCKED |\n"
    rows.append(("approving-cell", r, t, 7))
    t = CAN + "\n## Summary\n\n### Blocked items\n"
    rows.append(("approving-heading", r, t, 7))
    t = CAN + "## Notes\n\nx\n"
    rows.append(("terminator", r, t, 4))
    return rows


REASON_ROWS = reason_rows()


@pytest.mark.parametrize("code,allowed,text,line", REASON_ROWS, ids=[row[0] for row in REASON_ROWS])
def test_refusal_reason_and_line(code, allowed, text, line):
    assert gate._parse_verdict(text, allowed) == (None, gate._REFUSALS[code], line)


def test_every_refusal_reason_has_a_row():
    assert {row[0] for row in REASON_ROWS} == set(gate._REFUSALS)
    sentences = list(gate._REFUSALS.values())
    assert len(set(sentences)) == len(sentences)
    assert all(s.endswith(".") and "\n" not in s for s in sentences)


def test_accepted_documents_carry_no_reason():
    assert gate._parse_verdict(doc("APPROVED"), REVIEW) == ("APPROVED", "", None)


def test_unparseable_detail_order():
    detail = gate.unparseable_detail("review-3.md", gate._REFUSALS["label-disagree"], 42)
    assert detail.startswith("review-3.md: line 42: a label line names the verdict but does not start with the value. ")
    assert detail.endswith(gate.RECOVERY_SENTENCE)
    assert "quoin opencode adopt" in detail
    unreadable = gate.unparseable_detail("review-3.md", gate.UNREADABLE_REASON, None)
    assert unreadable.endswith(gate.UNREADABLE_RECOVERY_SENTENCE)
    assert "Verdict section" not in unreadable
    no_line = gate.unparseable_detail("review-3.md", gate._REFUSALS["heading-count"], None)
    assert no_line.startswith("review-3.md: the document does not have exactly one Verdict heading. ")


def test_text_after_dimension_table_is_scanned():
    text = CAN + "\n## Dimension Verdicts\n\n| d | v |\n|---|---|\n| a | APPROVED |\n\nBLOCKED\n"
    assert gate.parse_verdict(text, REVIEW) is None


# -- linear time -----------------------------------------------------------

MB = 1 << 20


def timed_rows():
    return [
        ("long atx", doc("# x" + " " * MB + "y")),
        ("hyphen run", doc("changes" + "-" * MB)),
        ("repeated word", doc("changes " * (MB // 8))),
        ("unclosed tag", doc("<verdict>" + " " * MB)),
        ("star run", doc("**" * (MB // 2))),
        ("comment openers", "<!--" * (MB // 4) + "\n" + CAN),
        ("quote containers", doc("> " * (MB // 2) + "x")),
        ("list containers", doc("- " * (MB // 2) + "x")),
        ("angle a run", doc("<a" * (MB // 2))),
        ("long tag candidates", doc("<" + "a" * 199 + ("<" + "a" * 199) * (MB // 200))),
        ("label lines", doc("Verdict: x\n" * 100000)),
        ("verdict word run", doc("verdict " * (MB // 8))),
        ("word then spaces mid-line", doc("x verdict" + " " * MB + "y")),
        ("word then tabs mid-line", doc("x verdict" + "\t" * MB + "y")),
        ("word then spaces at start", doc("verdict" + " " * MB + "y")),
        ("word then tabs at start", doc("verdict" + "\t" * MB + "y")),
        ("equals lines", doc("=\n" * (MB // 2))),
        ("dash lines", doc("-\n" * (MB // 2))),
        ("blank lines", doc("\n" * MB)),
        ("label qualifier", doc("Verdict (" + "a" * MB)),
        ("wildcards", doc("→" * (MB // 3))),
        ("pipes after the section", CAN + "\n## Summary\n\n" + "|" * MB + "\n"),
        ("table rows", CAN + "\n## Summary\n\n" + "| a | b |\n" * 100000),
        ("raw text opener prefix", "<scrip" * (MB // 6) + "\n\n" + CAN),
    ]


@pytest.mark.parametrize("name,text", timed_rows(), ids=[row[0] for row in timed_rows()])
def test_parse_verdict_is_linear(name, text):
    import time

    # Linear work finishes in well under a second; a quadratic pattern takes
    # tens of seconds. Best of three keeps a loaded host from failing the row.
    best = None
    for _ in range(3):
        started = time.monotonic()
        gate.parse_verdict(text, REVIEW)
        elapsed = time.monotonic() - started
        best = elapsed if best is None else min(best, elapsed)
        if best < 2.0:
            break
    assert best < 2.0


# -- writer shapes ---------------------------------------------------------

REAL_REVIEW = FINAL_REVIEW
REAL_CRITIC = "## Summary\n\nThe plan holds up.\n\n## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Issues Found\n\nNone.\n"


def test_parse_verdict_real_skill_shapes():
    assert gate.parse_verdict(REAL_REVIEW, REVIEW) == "APPROVED"
    assert gate.parse_verdict(REAL_CRITIC, CRITIC) == "PASS"


# -- critic status ---------------------------------------------------------


def sdir_with(tmp_path, *responses):
    sdir = tmp_path / "stage-1"
    sdir.mkdir()
    for number, text in responses:
        (sdir / ("critic-response-%d.md" % number)).write_text(text)
    return sdir


def status(entry, origin, sdir, settings=None, root=None):
    return gate.critic_status(entry, origin, sdir, settings or {}, project_root=root)


def test_coordinator_never_falls_back_to_disk(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    assert status({"critic_responses": []}, "coordinator", sdir) == [
        ("FAIL", "critic-missing", "no critic response was recorded for this plan")]


def test_critic_not_required_warns(tmp_path):
    sdir = sdir_with(tmp_path)
    result = status({}, "coordinator", sdir, {"critic_required": False})
    assert [(s, c) for s, c, _ in result] == [("WARN", "critic-not-run")]


def test_adopted_with_nothing_warns_and_phase_run_fails(tmp_path):
    sdir = sdir_with(tmp_path)
    assert status({}, "adopted", sdir)[0][:2] == ("WARN", "critic-not-run")
    assert status({}, "phase-run", sdir)[0][:2] == ("FAIL", "critic-missing")


def test_disk_fallback_for_phase_run_passes(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_REVISE), (2, h.CRITIC_PASS))
    assert status({}, "phase-run", sdir) == [("PASS", "", "")]


def test_recorded_responses_win_over_disk(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    other = sdir / "critic-response-9.md"
    other.write_text(h.CRITIC_REVISE)
    result = status({"critic_responses": [str(other)]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "critic-not-converged")


@pytest.mark.parametrize("where", ["outside", "dotdot", "relative-outside"])
def test_recorded_response_outside_the_task_folder_is_refused(tmp_path, where):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text(h.CRITIC_PASS)
    value = {
        "outside": str(outside),
        "dotdot": str(sdir / ".." / ".." / outside.name),
        "relative-outside": "../" + outside.name,
    }[where]
    result = status({"critic_responses": [value]}, "coordinator", sdir, root=tmp_path)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "path-unresolved")]


def test_recorded_response_through_a_symlink_is_refused(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    real = tmp_path / "real"
    real.mkdir()
    (real / "r.md").write_text(h.CRITIC_PASS)
    (sdir / "link").symlink_to(real)
    result = status({"critic_responses": [str(sdir / "link" / "r.md")]}, "coordinator", sdir, root=tmp_path)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "path-unresolved")]


def test_read_text_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(str(fifo))
    assert gate._read_text(fifo, 1024) is None


def test_adopt_command_quotes_its_arguments():
    text = gate.adopt_command("t1", 1, "plan", "/my path/with space")
    assert "'/my path/with space'" in text


def test_revise_last_does_not_converge(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS), (2, h.CRITIC_REVISE))
    assert status({}, "adopted", sdir)[0][:2] == ("FAIL", "critic-not-converged")


def test_six_responses_exceed_the_cap(tmp_path):
    sdir = sdir_with(tmp_path, *[(n, h.CRITIC_REVISE) for n in range(1, 6)], (6, h.CRITIC_PASS))
    result = status({}, "adopted", sdir)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "critic-not-converged")]
    assert status({}, "adopted", sdir, {"max_critic_rounds": 6}) == [("PASS", "", "")]


def test_unparseable_last_response(tmp_path):
    sdir = sdir_with(tmp_path, (1, "## Verdict\n\nPASS - looks fine\n"))
    assert status({}, "adopted", sdir)[0][:2] == ("FAIL", "verdict-unparseable")


def test_numeric_ordering_of_responses(tmp_path):
    sdir = sdir_with(tmp_path, (9, h.CRITIC_REVISE), (10, h.CRITIC_PASS))
    assert status({}, "adopted", sdir) == [("PASS", "", "")]


# -- stage and artifact paths ---------------------------------------------


def test_stage_dir_requires_a_listed_stage(tmp_path):
    h.write(h.task_dir(tmp_path, "t1") / "architecture.md", h.ARCHITECTURE)
    assert gate.stage_dir(tmp_path, "t1", 2, SOURCE) == h.task_dir(tmp_path, "t1") / "stage-2"
    assert gate.stage_dir(tmp_path, "t1", None, SOURCE) == h.task_dir(tmp_path, "t1")
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 3, SOURCE)


def test_stage_dir_without_architecture(tmp_path):
    h.task_dir(tmp_path, "t1").mkdir(parents=True)
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_stage_row_with_em_dash_heading_is_not_a_row(tmp_path):
    text = h.ARCHITECTURE.replace("1. S-1: First stage", "1. S-1 — First stage")
    h.write(h.task_dir(tmp_path, "t1") / "architecture.md", text)
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_symlinked_stage_dir_is_unresolved(tmp_path):
    base = h.task_dir(tmp_path, "t1")
    h.write(base / "architecture.md", h.ARCHITECTURE)
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, base / "stage-1")
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_review_number_chosen_numerically(tmp_path):
    base = h.build_task(tmp_path, review=None)
    for n in (2, 9, 10):
        h.write(base / "stage-1" / ("review-%d.md" % n), h.REVIEW)
    paths, missing = gate.expected_artifacts(tmp_path, "t1", 1, "review", {}, base / "stage-1")
    assert [p.name for p in paths] == ["review-10.md"] and missing == []


def test_missing_artifacts_are_named(tmp_path):
    base = h.build_task(tmp_path, review=None)
    assert gate.expected_artifacts(tmp_path, "t1", 1, "review", {}, base / "stage-1") == ([], ["review-N.md"])
    paths, missing = gate.expected_artifacts(tmp_path, "t1", None, "discover", {}, base)
    assert paths == [] and len(missing) == 3 and all(m.startswith(".workflow_artifacts/memory/") for m in missing)
    assert gate.expected_artifacts(tmp_path, "t1", 1, "implement", {}, base / "stage-1") == ([], [])


# -- validator subprocess --------------------------------------------------


def test_validator_pass_and_fail(tmp_path):
    base = h.build_task(tmp_path)
    assert gate.run_validator(tmp_path, SOURCE, base / "stage-1" / "current-plan.md") is None
    bad = h.write(base / "stage-1" / "current-plan.md", h.PLAN.replace("## Risks", "## Surprise"))
    detail = gate.run_validator(tmp_path, SOURCE, bad)
    assert detail and "V-02" in detail and "Surprise" in detail


def test_validator_unavailable_fails_closed(tmp_path):
    base = h.build_task(tmp_path)
    plan = base / "stage-1" / "current-plan.md"
    assert gate.run_validator(tmp_path, tmp_path / "nowhere", plan) == "validator-unavailable"
    broken = tmp_path / "src"
    shutil.copytree(SOURCE / "core" / "scripts", broken / "core" / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    assert gate.run_validator(tmp_path, broken, plan) == "validator-unavailable"
    (broken / "memory").mkdir()
    (broken / "memory" / "format-kit.sections.json").write_text("{not json")
    assert gate.run_validator(tmp_path, broken, plan) == "validator-unavailable"


def test_local_redaction_matches_the_launcher():
    from quoin.opencode_adapter import launch_env

    samples = ["token sk-abcdefghijklmnopqrstuvwx123456", "plain text", "ghp_" + "a" * 36, "Bearer abcdefghijklmnopqrstuvwxyz0123"]
    for text in samples:
        assert gate._redact(text) == launch_env.Redactor()(text)


def test_core_loading_is_cached():
    first = gate.load_core(SOURCE, "path_resolve")
    assert gate.load_core(SOURCE, "path_resolve") is first
    assert hasattr(gate.load_core(SOURCE, "handoff_validate"), "validate")


def test_recorded_critic_outside_stage_or_misnamed_is_refused(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    sibling = sdir.parent / "stage-9"
    sibling.mkdir(exist_ok=True)
    (sibling / "critic-response-1.md").write_text(h.CRITIC_PASS)
    result = status({"critic_responses": [str(sibling / "critic-response-1.md")]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "path-unresolved")
    (sdir / "notes.md").write_text(h.CRITIC_PASS)
    result = status({"critic_responses": [str(sdir / "notes.md")]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "path-unresolved")
