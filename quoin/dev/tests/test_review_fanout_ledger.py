"""The review fan-out writes on-behalf 8-column cost rows, one per dimension subagent."""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
REVIEW = (REPO / "quoin" / "adapters" / "claude" / "skills" / "review" / "SKILL.md").read_text(
    encoding="utf-8"
)


def _ledger_slice() -> str:
    i = REVIEW.index("**Ledger:**")
    return REVIEW[i : REVIEW.index("**Merge:**", i)]


def test_ledger_block_is_the_eight_column_onbehalf_write():
    s = _ledger_slice()
    assert "agent_transcript_cost.py" in s
    assert '--agent-id "$AID"' in s
    assert "printf '%s | %s | %s | %s | task | %s | %s | %s\\n'" in s
    assert "on-behalf: review fan-out" in s
    assert 'grep -qF -e "$AID | "' in s


def test_opt_out_keeps_the_synthetic_uuid_form():
    s = _ledger_slice()
    assert "QUOIN_INLINE_COST_CAPTURE=0" in s
    assert "get_session_uuid.py" in s


def test_synthetic_uuid_is_no_longer_the_default():
    s = _ledger_slice()
    assert s.index("agent_transcript_cost.py") < s.index("get_session_uuid.py")
    assert "Subagent session UUIDs are not resolvable" not in s


def test_large_security_spawn_is_onbehalf_prefixed():
    i = REVIEW.index("- **Large ONLY:**")
    sentence = REVIEW[i : REVIEW.index("\n", i)]
    assert "[quoin-onbehalf]" in sentence
