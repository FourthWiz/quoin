"""
Black-box pytest tests for the run skill's handoff-envelope producer
prose.

The validator must be driven from REAL emitted payload values
rather than synthetic hand-constructed pairs. The producer here is
instruction text in adapter skill files, not executable code, so "real
emitted values" means the literal fenced blocks those files instruct the
orchestrator to emit. This file extracts those blocks and runs them
through the same core validator subprocess idiom test_handoff_validate.py
uses — no import of validator internals.

Mirrors test_handoff_validate.py's two-part pass condition throughout:
rc == 0 AND the filtered (blank-line-dropped) stderr line list is empty.
Exit 0 alone admits every advisory-severity rule, which is exactly where
version and key drift surface.
"""

import os
import re
import subprocess
import sys
import tempfile
import textwrap

import pytest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_DIR = os.path.dirname(TEST_DIR)                # quoin/dev/
QUOIN_DIR = os.path.dirname(DEV_DIR)                # quoin/
PROJECT_ROOT = os.path.dirname(QUOIN_DIR)           # project root

VALIDATOR = os.path.join(QUOIN_DIR, "core", "scripts", "handoff_validate.py")

RUN_SKILL = os.path.join(
    QUOIN_DIR, "adapters", "claude", "skills", "run_hard", "SKILL.md"
)
# The three summary-mandating skills each inline the COMPLETE (and, for
# implement, PARTIAL) return template directly in their final-message
# prose, so a compliant emission never requires reading the workflow-
# directory contract at runtime — the contract stays the normative pointer.
# These inlined blocks sit indented under a numbered-list or bullet item in
# their source files, so extract_blocks() below dedents each candidate
# block before the marker match: without that, an indented block's first
# line fails the column-zero marker anchor and is silently dropped, and
# this comment's coverage claim would be false.
SUMMARY_MANDATING_SKILLS = [
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "implement", "SKILL.md"),
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "review", "SKILL.md"),
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "thorough_plan", "SKILL.md"),
]
PRODUCER_FILES = [RUN_SKILL] + SUMMARY_MANDATING_SKILLS

# The closing fence tolerates leading whitespace ([ \t]*) because a block
# embedded under a numbered-list or bullet item (implement/SKILL.md,
# review/SKILL.md) indents every line of the fence, including the close —
# without this, the non-greedy match never finds a same-indentation close
# fence and either absorbs unrelated content until the next column-zero
# close fence, or fails to match at all when no such fence follows.
FENCE_RE = re.compile(r"```text\n(.*?)\n[ \t]*```", re.DOTALL)
MARKER_RE = re.compile(r"^\[quoin-handoff/\d+\.\d+ (dispatch|return)\]$")
STATUS_RE = re.compile(r"^status:\s*(\S+)\s*$", re.MULTILINE)


def run_validator(payload_text, direction):
    """Write payload_text to a temp file and run the core validator against it."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False
    ) as fh:
        fh.write(payload_text)
        path = fh.name
    try:
        cmd = [sys.executable, VALIDATOR, "--direction", direction, path]
        try:
            result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"validator subprocess exceeded 30s timeout for direction={direction!r}: {exc}"
            ) from exc
    finally:
        os.unlink(path)
    stderr = result.stderr.decode("utf-8", errors="replace")
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    return result.returncode, lines


def extract_blocks(path):
    """Return a list of (direction, block_text) for every marker-first fenced
    block in the file at path. Each candidate block is dedented before the
    marker match, so a block indented under a numbered-list or bullet item
    (implement/SKILL.md, review/SKILL.md) is found on equal footing with a
    column-zero block (run/SKILL.md, thorough_plan/SKILL.md) — MARKER_RE is
    anchored to the start of the line, so a leading-whitespace first line
    would otherwise fail the anchor and the whole block would be silently
    dropped. block_text is the dedented span from open marker through close
    marker inclusive — the first-line rule is what the envelope templates'
    marker-first shape exists to satisfy."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    found = []
    for m in FENCE_RE.finditer(content):
        block = textwrap.dedent(m.group(1))
        first_line = block.splitlines()[0] if block else ""
        marker_match = MARKER_RE.match(first_line)
        if marker_match:
            found.append((marker_match.group(1), block))
    return found


def all_producer_blocks():
    blocks = []
    for path in PRODUCER_FILES:
        blocks.extend(extract_blocks(path))
    return blocks


# Matches the quoted trailing note appended after the injected COMPLETE
# template in run/SKILL.md's handoff-envelope section — the note carries the
# verdict-vocabulary and own-template-preference instruction into the actual
# child spawn prompt, not only into this file's surrounding prose.
# Paragraph-bounded, not quote-bounded: the group closes on the LAST quote
# in the paragraph (the one immediately followed by the blank line that ends
# it), not the FIRST quote found. A note carrying an embedded quote (e.g. the
# ${CLAUDE_PLUGIN_ROOT} pointer sentence) is captured in full instead of truncating
# at that inner quote, which the prior non-greedy quote-bounded pattern did
# silently. Parity is still checked below — this widening trades silent
# truncation for a loud rejection, not for silent over-capture.
TRAILING_NOTE_RE = re.compile(r'reads, verbatim:\s*\n\n(".*?")\n\n', re.DOTALL)


def _extract_trailing_note_from_text(content, source_label="<text>"):
    m = TRAILING_NOTE_RE.search(content)
    assert m, f"{source_label}: no trailing note found after 'reads, verbatim:'"
    block = m.group(1)
    assert block.count('"') % 2 == 0, (
        f"{source_label}: trailing note has an unbalanced (odd-count) quote"
    )
    inner = block[1:-1]
    return " ".join(line.strip() for line in inner.splitlines())


def extract_trailing_note(path):
    """Return the trailing note text as it is appended to a spawn prompt —
    the markdown source wraps it across lines for readability, so this joins
    those lines back into the single unbroken line the child actually
    receives."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    return _extract_trailing_note_from_text(content, source_label=path)


# ── Set-guards (a), (b), (c) — a zero-block sweep must FAIL loudly ─────────


VERDICT_RE = re.compile(r"^verdict:\s*(\S+)\s*$", re.MULTILINE)

# Exact total block count (dispatch + return combined) per producer file.
# An at-least-one guard fires only when a file's LAST block goes, so a
# file's non-final templates were silently deletable — this covers the
# deletion of ANY block below, including implement's PARTIAL template and
# either of review's two verdict-carrying COMPLETE templates (deleting
# implement's PARTIAL template, review's CHANGES_REQUESTED template, or
# review's APPROVED template each left the suite green under a weaker
# at-least-one guard).
EXPECTED_BLOCK_COUNTS = {
    RUN_SKILL: 3,  # 1 dispatch + COMPLETE + PARTIAL
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "implement", "SKILL.md"): 2,
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "review", "SKILL.md"): 2,
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "thorough_plan", "SKILL.md"): 1,
}

# Exact per-file multiset of (status, verdict) shapes for the summary-mandating
# skills' return blocks. Pins the SHAPES, not merely the count, so swapping one
# template for a same-status duplicate would also be caught.
EXPECTED_RETURN_SHAPES = {
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "implement", "SKILL.md"): [
        ("COMPLETE", "PASS"),
        ("PARTIAL", None),
    ],
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "review", "SKILL.md"): [
        ("COMPLETE", "CHANGES_REQUESTED"),
        ("COMPLETE", "APPROVED"),
    ],
    os.path.join(QUOIN_DIR, "adapters", "claude", "skills", "thorough_plan", "SKILL.md"): [
        ("COMPLETE", "PASS"),
    ],
}


def status_and_verdict(block):
    status_m = STATUS_RE.search(block)
    verdict_m = VERDICT_RE.search(block)
    return (
        status_m.group(1) if status_m else None,
        verdict_m.group(1) if verdict_m else None,
    )


def field_names(block):
    """Ordered field names (the `name` in each `name: value` line), skipping
    the open/close marker lines. Used to compare block SHAPE (which fields,
    in which order) independently of field VALUES."""
    names = []
    for line in block.splitlines():
        if MARKER_RE.match(line.strip()):
            continue
        m = re.match(r"^([A-Za-z_]+):\s*.*$", line)
        if m:
            names.append(m.group(1))
    return names


# ── T-06: the trailing note names the contract and stays in budget ────────


def test_trailing_note_extractor_rejects_unbalanced_quote():
    """Mutation-proof for the widened, paragraph-bounded TRAILING_NOTE_RE: a
    note carrying an odd (unbalanced/unclosed) quote count must fail loudly.
    The prior non-greedy quote-bounded pattern would have silently truncated
    at the first inner quote instead; a naive greedy widening would instead
    silently over-capture past the note's real end. Neither is acceptable —
    this proves the parity check catches what the bounding alone cannot."""
    text = (
        "The trailing note reads, verbatim:\n\n"
        '"This note has an unbalanced " quote inside it."\n\n'
        "Next paragraph.\n"
    )
    with pytest.raises(AssertionError, match="unbalanced"):
        _extract_trailing_note_from_text(text)


# ── Per-block validation — every extracted block validates clean ──────────


# ── Composed-payload case — sentinel stacking and bundle coexistence ────


