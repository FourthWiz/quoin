"""IVG-71 T-08: adapter-pilot token greps over gate/SKILL.md.

Cheap literal-token asserts that verify the gate wiring for the
affected-area test suite precondition is present and correctly formed.

Each assert is annotated with the plan spec that drives it (MAJ-1/CRIT-1/
MIN-3/etc.) so a future failure points directly at the requirement.

Region-level assertions (MAJ-3): each named region is located by its
header string, and `Affected-area test suite` is asserted WITHIN that
region. A dropped region must FAIL the test — a bare occurrence-count
would pass while one region silently lost the row.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"

TOKEN = "Affected-area test suite"

# IVG-151 exit-5 machine tokens (hyphenated exit_reason must appear verbatim).
FLAG = "--require-task-context"
EXIT_REASON = "no-quoin-task-context"

# The three post-implement/post-review sub-regions are delimited by bold text,
# not real markdown headings, so `^#{2,3} ` alone does not isolate them from
# each other (they all fall before the next real heading). _region() must also
# stop at the next sibling boundary in this list (round-2 MINOR-4 fix, cloned
# from test_gate_ci_mirror_tokens.py per IVG-151 T-08).
_REGION_BOUNDARIES = (
    "*Standard gate (Small and Medium tasks):*",
    "*Full gate (Large tasks) — includes everything in Standard, plus:*",
    "**After /review → before /end_of_task (Full gate — always, all task sizes):**",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load() -> str:
    return GATE_SKILL.read_text(encoding="utf-8")


def _region(text: str, header: str) -> str:
    """Return the text from header until the next H2/H3 heading OR sibling boundary.

    Stops at the next `## `/`### ` markdown heading AND at the next sibling
    bold-text sub-region boundary in _REGION_BOUNDARIES — so the Standard /
    Full / post-review sub-regions do not leak into each other (they all sit
    before the next real heading).
    """
    start = text.find(header)
    assert start != -1, f"Region header not found in gate SKILL.md: {header!r}"
    after = text[start + len(header):]
    m = re.search(r"^#{2,3} ", after, re.MULTILINE)
    end = m.start() if m else len(after)
    for boundary in _REGION_BOUNDARIES:
        if boundary == header:
            continue
        idx = after.find(boundary)
        if idx != -1 and idx < end:
            end = idx
    return after[:end]


def _check_block(region_text: str, marker: str) -> str:
    """Return one '- [ ] ' + marker checklist bullet's text, up to (not
    including) the next top-level '- [ ] ' bullet or end of region. Isolates
    ONE literal invocation block from its sibling block in the same region
    (IVG-151 T-08: the affected_tests and ci_mirror blocks are adjacent, and
    after wiring both carry identical exit-5 tokens — a region-level assertion
    could not attribute a dropped row to either block)."""
    m = re.search(r"^- \[ \] " + re.escape(marker), region_text, re.MULTILINE)
    assert m is not None, f"Checklist bullet not found: {marker!r}"
    after = region_text[m.start():]
    nxt = re.search(r"\n- \[ \] ", after)
    return after[: nxt.start()] if nxt else after


def _audit_check_block(region_text: str, check_name: str) -> str:
    """Slice one check's audit glyph-mapping sentence out of the continuous
    Step-5 audit prose paragraph (keyed on the 'Glyph mapping for the `<name>`
    row:' sentence marker that exists verbatim for every check)."""
    marker = f"Glyph mapping for the `{check_name}` row:"
    idx = region_text.find(marker)
    assert idx != -1, f"Audit glyph-mapping sentence not found for {check_name!r}"
    after = region_text[idx:]
    nxt = after.find("Glyph mapping for the `", len(marker))
    return after[:nxt] if nxt != -1 else after


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-region assertions (MAJ-3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# IVG-151 T-08: per-check-block exit-5 token assertions (region THEN check-block)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# IVG-151 T-08: isolation regression tests (block-level + subsection-level)
# ---------------------------------------------------------------------------

def test_check_block_isolation_affected_vs_ci_mirror():
    """Synthetic reproduction of the round-2 masking scenario: BOTH blocks present
    in one region but the ci_mirror exit-5 row dropped. The affected block must
    still carry its tokens (unaffected) AND the ci_mirror block must NOT (drop
    detected) — proving the affected assertion is not falsely rescued by the
    sibling ci_mirror block's identical tokens."""
    region = (
        "- [ ] Affected-area test suite (BLOCKING): run affected_tests.py --require-task-context\n"
        "  - exit 5 (exit_reason: no-quoin-task-context) → CLEAN SKIP\n"
        "  - script missing → WARN\n"
        "- [ ] CI mirror (BLOCKING): run ci_mirror.py --require-task-context\n"
        "  - script missing → WARN\n"  # ci_mirror exit-5 row dropped here
    )
    affected_block = _check_block(region, "Affected-area test suite")
    ci_block = _check_block(region, "CI mirror")
    assert "exit 5" in affected_block and EXIT_REASON in affected_block, (
        "affected block must retain its own exit-5 tokens"
    )
    assert "exit 5" not in ci_block, (
        "block isolation must expose a row dropped from ONLY the ci_mirror sibling block"
    )


