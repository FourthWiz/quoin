"""T-09 (IVG-141): additive-only guard — hooks/_lib.sh thresholds + formula unchanged.

Import-free / bare-checkout runnable — pure text/regex parse of hooks/_lib.sh.
NEVER `import quoin`. Pins the retired context-threshold constants as absent from _lib.sh and keeps
the compute_utilization formula and the bytes-per-token and window defaults
from drifting.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SH = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"


def _text() -> str:
    return LIB_SH.read_text(encoding="utf-8")


def test_removed_threshold_names_are_absent():
    text = _text()
    for name in (
        "STOP_BPS",
        "BLOCK_BPS",
        "COMPACT_FIRST_BPS",
        "PANIC_BPS",
        "PRECOMPACT_NORUN_CHECKPOINT",
    ):
        assert name not in text, f"_lib.sh still mentions the removed constant {name}"


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
