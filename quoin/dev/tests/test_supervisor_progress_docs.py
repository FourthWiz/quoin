"""The autonomous-mode reference documents the supervisor's progress rule,
repair allowance and knobs, with defaults that match the code."""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"

sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from quoin import supervisor as sup
finally:
    sys.path.pop(0)


def _paragraph(lead: str) -> str:
    text = DOC.read_text(encoding="utf-8")
    assert text.count(lead) == 1, lead
    start = text.index(lead)
    match = re.search(r"\n\n(?=\*\*|#)", text[start:])
    end = start + match.start() if match else len(text)
    return " ".join(text[start:end].split())


def _knobs_paragraph() -> str:
    return _paragraph("**Knobs and defaults:**")


def test_supervisor_loop_paragraph():
    p = _paragraph("**Supervisor loop.**")
    assert p
    for needle in (
        "`phase completion not repaired: <phase>`",
        "no forward progress",
        sup.REPAIR_RELAUNCHES_ENV,
        sup.LAUNCH_TIMEOUT_ENV,
        sup.HEAD_PROBE_ENV,
        "`QUOIN_HEADLESS_CHILD=1`",
        "never writes",
        f"default {sup.DEFAULT_REPAIR_RELAUNCHES}, clamp 0..5",
        f"default {int(sup.DEFAULT_LAUNCH_TIMEOUT_SECONDS)}, clamp 900..14400",
    ):
        assert needle in p, needle


def test_knob_line_names_all_three():
    p = _knobs_paragraph()
    for name in (sup.REPAIR_RELAUNCHES_ENV, sup.LAUNCH_TIMEOUT_ENV, sup.HEAD_PROBE_ENV):
        assert name in p, name


def test_halt_reasons_paragraph():
    p = _paragraph("**Halt reasons**")
    assert p
    assert "`phase completion not repaired: <phase>`" in p
    assert "`no forward progress`" in p
