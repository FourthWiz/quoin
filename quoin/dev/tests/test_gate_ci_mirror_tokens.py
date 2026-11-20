"""IVG-138 T-07: token-parity greps over gate/SKILL.md for the CI mirror wiring.

Per-region two-slicer (lesson 2026-06-04, cloned from
test_gate_deploy_drift_tokens.py): each named region is located by its header
string and the `CI mirror` / `ci_mirror.py` tokens are asserted WITHIN that
region, so a dropped region FAILs (a bare occurrence-count would pass while
one region silently lost the row).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "gate" / "SKILL.md"

CHECK_NAME = "CI mirror"
SCRIPT = "ci_mirror.py"

# IVG-151 exit-5 machine tokens (hyphenated exit_reason must appear verbatim).
FLAG = "--require-task-context"
EXIT_REASON = "no-quoin-task-context"

# The three post-implement/post-review sub-regions are delimited by bold text, not
# real markdown headings, so `^#{2,3} ` alone does not isolate them from each other
# (they all fall before the next real heading, "### Step 3a: ..."). _region() must
# also stop at the next sibling boundary in this list (mirrors the round-2 MINOR-4
# fix in test_gate_deploy_drift_tokens.py).
_REGION_BOUNDARIES = (
    "*Standard gate (Small and Medium tasks):*",
    "*Full gate (Large tasks) — includes everything in Standard, plus:*",
    "**After /review → before /end_of_task (Full gate — always, all task sizes):**",
)


def _load() -> str:
    return GATE_SKILL.read_text(encoding="utf-8")


def _region(text: str, header: str) -> str:
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
    """Isolate ONE '- [ ] ' + marker checklist bullet from its sibling block in
    the same region (IVG-151 T-08 — mirrors test_gate_affected_area_tokens)."""
    m = re.search(r"^- \[ \] " + re.escape(marker), region_text, re.MULTILINE)
    assert m is not None, f"Checklist bullet not found: {marker!r}"
    after = region_text[m.start():]
    nxt = re.search(r"\n- \[ \] ", after)
    return after[: nxt.start()] if nxt else after


def _audit_check_block(region_text: str, check_name: str) -> str:
    """Slice one check's audit glyph-mapping sentence out of the Step-5 audit prose."""
    marker = f"Glyph mapping for the `{check_name}` row:"
    idx = region_text.find(marker)
    assert idx != -1, f"Audit glyph-mapping sentence not found for {check_name!r}"
    after = region_text[idx:]
    nxt = after.find("Glyph mapping for the `", len(marker))
    return after[:nxt] if nxt != -1 else after


# ---------------------------------------------------------------------------
# IVG-151 T-08: per-check-block exit-5 token assertions (all three ci_mirror
# blocks are LITERAL — assert flag + hyphenated token + exit 5 in each).
# ---------------------------------------------------------------------------


def test_check_block_isolation_ci_mirror_vs_affected():
    """Roles-reversed synthetic reproduction: the affected_tests exit-5 row is
    dropped while the ci_mirror block is intact. The ci_mirror block must retain
    its tokens and the affected block must NOT — proving the ci_mirror assertion
    is not falsely rescued by the sibling affected block."""
    region = (
        "- [ ] Affected-area test suite (BLOCKING): run affected_tests.py --require-task-context\n"
        "  - script missing → WARN\n"  # affected exit-5 row dropped here
        "- [ ] CI mirror (BLOCKING): run ci_mirror.py --require-task-context\n"
        "  - exit 5 (exit_reason: no-quoin-task-context) → CLEAN SKIP\n"
        "  - script missing → WARN\n"
    )
    affected_block = _check_block(region, "Affected-area test suite")
    ci_block = _check_block(region, "CI mirror")
    assert "exit 5" in ci_block and EXIT_REASON in ci_block, (
        "ci_mirror block must retain its own exit-5 tokens")
    assert "exit 5" not in affected_block, (
        "block isolation must expose a row dropped from ONLY the affected sibling block")
