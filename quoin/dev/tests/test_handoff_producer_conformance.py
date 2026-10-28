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
# __QUOIN_HOME__ pointer sentence) is captured in full instead of truncating
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


def test_at_least_one_dispatch_and_one_return_block_found():
    blocks = all_producer_blocks()
    directions = {d for d, _ in blocks}
    assert "dispatch" in directions, "no dispatch block found across the producer file set"
    assert "return" in directions, "no return block found across the producer file set"


def test_return_blocks_cover_complete_and_partial_status():
    blocks = all_producer_blocks()
    statuses = set()
    for direction, block in blocks:
        if direction != "return":
            continue
        m = STATUS_RE.search(block)
        if m:
            statuses.add(m.group(1))
    assert "COMPLETE" in statuses
    assert "PARTIAL" in statuses


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


def test_each_producer_file_yields_exact_block_counts():
    """Per-file exact-count guard, mirroring the co-occurrence pattern in
    test_inline_step_summary_present.py's test_fail_closed_sites_emit_no_envelope
    (expected_counts keyed per file, asserted individually). The aggregate
    set-guards above are satisfied by run/SKILL.md alone and cannot reveal a
    producer file whose own inlined templates go unseen by the extractor, or
    whose non-COMPLETE templates are silently deletable — this is the gap
    that let three of five newly inlined templates validate nothing under an
    at-least-one guard."""
    for path, expected in EXPECTED_BLOCK_COUNTS.items():
        blocks = extract_blocks(path)
        assert len(blocks) == expected, (
            f"{path}: expected exactly {expected} extracted block(s), found {len(blocks)}"
        )


def test_summary_mandating_skill_return_block_shapes():
    """Per-file exact multiset of (status, verdict) return-block shapes for
    the three summary-mandating skills. Verified by direct deletion:
    removing implement's PARTIAL template, review's CHANGES_REQUESTED
    template, or review's APPROVED template must now fail this test, not
    merely pass a weaker at-least-one guard."""
    for path, expected in EXPECTED_RETURN_SHAPES.items():
        blocks = extract_blocks(path)
        shapes = sorted(
            status_and_verdict(block) for direction, block in blocks if direction == "return"
        )
        assert shapes == sorted(expected), (
            f"{path}: expected return-block shapes {sorted(expected)}, found {shapes}"
        )


def test_summary_mandating_complete_blocks_match_run_shape():
    """Cross-file guard: each summary-mandating skill's
    COMPLETE return block must be field-name- and field-order-identical to
    run/SKILL.md's injected COMPLETE template, so the two shape sources
    cannot silently diverge — `verdict` is explicitly allowlisted to differ
    in VALUE (each producer emits its own branch's outcome; that is expected
    and is exactly what test_summary_mandating_skill_return_block_shapes
    above already pins), so only field NAMES and their ORDER are compared
    here, never verdict's value."""
    run_complete = [
        block
        for direction, block in extract_blocks(RUN_SKILL)
        if direction == "return" and status_and_verdict(block)[0] == "COMPLETE"
    ]
    assert len(run_complete) == 1, f"expected exactly one COMPLETE block in {RUN_SKILL}"
    run_fields = field_names(run_complete[0])

    for path in SUMMARY_MANDATING_SKILLS:
        blocks = extract_blocks(path)
        complete_blocks = [
            block
            for direction, block in blocks
            if direction == "return" and status_and_verdict(block)[0] == "COMPLETE"
        ]
        assert complete_blocks, f"{path}: no COMPLETE return block found"
        for block in complete_blocks:
            fields = field_names(block)
            assert fields == run_fields, (
                f"{path}: COMPLETE block fields {fields} do not match "
                f"run/SKILL.md's COMPLETE block fields {run_fields}"
            )


def test_dispatch_block_carries_return_envelope_field():
    blocks = all_producer_blocks()
    dispatch_blocks = [block for direction, block in blocks if direction == "dispatch"]
    assert dispatch_blocks, "no dispatch block found"
    assert any("return: envelope" in block for block in dispatch_blocks)


def test_dispatch_block_no_longer_carries_spec_field():
    """Deliberate inversion of the former test_dispatch_block_carries_spec_field:
    stage 3 pinned spec: as a green invariant; this stage's 1.1 dispatch
    template drops the field in favor of the trailing note (see
    extract_trailing_note above), which becomes the sole path to the full
    contract for every dispatch. This flip is by design, not a regression."""
    blocks = all_producer_blocks()
    dispatch_blocks = [block for direction, block in blocks if direction == "dispatch"]
    assert dispatch_blocks, "no dispatch block found"
    assert not any(re.search(r"^spec:\s*\S+", block, re.MULTILINE) for block in dispatch_blocks), (
        "a dispatch block still carries a spec: field; the 1.1 template drops it "
        "in favor of the trailing note"
    )


# ── T-06: the trailing note names the contract and stays in budget ────────


def test_trailing_note_names_contract_file_and_resolves():
    """With `spec:` dropped, the trailing note is the only contract path
    discover/enrich/specify/architect/end_of_task ever receive. It must name
    the contract by the same `__QUOIN_HOME__`-substituted spelling D-10 uses
    for the core/reference pointer pair (a deployed child resolves from the
    deploy root, where a repo-root or bare-filename spelling would not), and
    that pointer must actually resolve on disk — mirrors
    test_handoff_format_core_and_reference_cross_reference in
    test_runtime_portability_docs.py, applied to this second pointer."""
    note = extract_trailing_note(RUN_SKILL)
    match = re.search(r"__QUOIN_HOME__(/core/workflow/handoff-format\.md)", note)
    assert match, (
        "trailing note must name the contract file by a backticked-free "
        "__QUOIN_HOME__-substituted path"
    )
    assert os.path.isfile(QUOIN_DIR + match.group(1))


def test_trailing_note_has_no_double_quote():
    """The note is injected verbatim inside a double-quoted block
    (`TRAILING_NOTE_RE`); an inner double quote would corrupt that framing."""
    note = extract_trailing_note(RUN_SKILL)
    assert '"' not in note


def test_trailing_note_byte_length_pinned():
    """Pins the note's exact byte length (measured per proc:bytes, without
    the enclosing quote characters) so a future edit that silently grows or
    truncates it is loud. Current: 377 B, cap 379 B (279 B pre-T-06 plus the
    100 B growth cap)."""
    note = extract_trailing_note(RUN_SKILL)
    length = len(note.encode("utf-8"))
    assert length == 377, f"trailing note is {length} B, expected exactly 377 B"
    assert length <= 379, f"trailing note is {length} B, exceeds the 379 B cap"


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


def test_producer_skills_name_the_contract_file():
    """Every producer file — each summary-mandating skill, and the run skill
    that spawns them — must name handoff-format.md somewhere in its own
    prose, so a producer reading its own SKILL.md can locate the normative
    contract without depending on the dispatch payload alone."""
    for path in PRODUCER_FILES:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert "handoff-format.md" in text, (
            f"{path} never names handoff-format.md"
        )


# ── Per-block validation — every extracted block validates clean ──────────


def test_every_extracted_block_validates_clean():
    blocks = all_producer_blocks()
    assert blocks, "producer file set yielded zero blocks"
    for direction, block in blocks:
        rc, lines = run_validator(block, direction)
        assert rc == 0, f"{direction} block failed validation: {lines}"
        assert lines == [], f"{direction} block warned: {lines}"


# ── Composed-payload case — sentinel stacking and bundle coexistence ────


def test_review_spawn_composed_payload_validates():
    """Build the review spawn's payload as the skill instructs post-774f83d:
    a stacked sentinel line, then the dispatch block extracted from the run
    skill verbatim, then the injected COMPLETE return template, then the
    trailing note appended immediately after the template (both extracted
    from the same handoff-envelope section — this is what the child spawn
    prompt actually carries, not only the template), then a prose line,
    then a wrapped bundle block with both bundle markers on their own
    lines. The whole payload must validate clean as a dispatch — this
    discharges the placement rule, sentinel stacking, the injected-template
    insertion, the trailing note, and bundle/envelope coexistence all at
    once."""
    run_blocks = extract_blocks(RUN_SKILL)
    dispatch_blocks = [block for direction, block in run_blocks if direction == "dispatch"]
    assert dispatch_blocks, "run skill carries no dispatch block to compose with"
    dispatch_block = dispatch_blocks[0]

    complete_blocks = [
        block
        for direction, block in run_blocks
        if direction == "return" and STATUS_RE.search(block)
        and STATUS_RE.search(block).group(1) == "COMPLETE"
    ]
    assert complete_blocks, "run skill carries no injected COMPLETE return template"
    complete_block = complete_blocks[0]

    trailing_note = extract_trailing_note(RUN_SKILL)

    payload = (
        "[no-redispatch] [autonomous]\n"
        + dispatch_block
        + "\n"
        + complete_block
        + "\n"
        + trailing_note
        + "\n"
        + "Review the implementation against the converged plan.\n"
        + "[quoin-bundle]\n"
        + "path/to/current-plan.md | plan summary\n"
        + "[/quoin-bundle]\n"
    )

    rc, lines = run_validator(payload, "dispatch")
    assert rc == 0, f"composed payload failed validation: {lines}"
    assert lines == [], f"composed payload warned: {lines}"


def test_composed_payload_reds_on_prose_between_sentinel_and_marker():
    """Mutation-proof: inserting a prose line between the sentinel zone and
    the open marker must trip the placement rule (H-13), proving the
    composed case above is load-bearing rather than decorative."""
    dispatch_blocks = [
        block for direction, block in extract_blocks(RUN_SKILL) if direction == "dispatch"
    ]
    assert dispatch_blocks
    dispatch_block = dispatch_blocks[0]

    payload = (
        "[no-redispatch] [autonomous]\n"
        + "This prose line does not belong here.\n"
        + dispatch_block
        + "\n"
    )

    rc, lines = run_validator(payload, "dispatch")
    assert rc == 1
    assert any("H-13" in ln for ln in lines)
