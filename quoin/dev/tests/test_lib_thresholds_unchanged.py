"""T-09 (IVG-141): additive-only guard — hooks/_lib.sh thresholds + formula unchanged.

Import-free / bare-checkout runnable — pure text/regex parse of hooks/_lib.sh.
NEVER `import quoin`. Backs spec AC-2 ("No existing QUOIN_*_BPS hook constant is
changed") and R-07 (formula parity anchor): IVG-141 is additive and must not
modify any hook threshold or the compute_utilization formula.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SH = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"


def _text() -> str:
    return LIB_SH.read_text(encoding="utf-8")


def test_four_bps_literals_present():
    text = _text()
    for literal in (
        "STOP_BPS=${QUOIN_STOP_BPS:-7000}",
        "COMPACT_FIRST_BPS=${QUOIN_COMPACT_FIRST_BPS:-9000}",
        "BLOCK_BPS=${QUOIN_BLOCK_BPS:-9500}",
        "PANIC_BPS=${QUOIN_PANIC_BPS:-10000}",
    ):
        assert literal in text, f"_lib.sh threshold changed/missing: {literal}"


def test_compute_utilization_formula_unchanged():
    text = _text()
    # The load-bearing awk formula, now the byte-estimate fallback used when a
    # transcript has no usage record: printf "%d\n", (b / bpt / lim) * 10000
    assert re.search(
        r'printf\s+"%d\\n",\s*\(b\s*/\s*bpt\s*/\s*lim\)\s*\*\s*10000',
        text,
    ), "compute_utilization awk formula changed/missing in _lib.sh"


def test_bpt_and_limit_defaults_unchanged():
    text = _text()
    assert "BPT=${QUOIN_BYTES_PER_TOKEN:-8.0}" in text
    assert "LIMIT=${QUOIN_EFFECTIVE_CONTEXT_LIMIT:-150000}" in text
