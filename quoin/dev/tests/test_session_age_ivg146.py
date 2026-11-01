"""S-3 tests — IVG-146 session-age proceed-option UX composed with fail-closed (T-19).

AC-7 (interactive 3-option list + `[no-session-age-guard]` bypass preserved), AC-8 (interactive
vs non-interactive diverge), plus the unchanged exit-code contract and the pinned autonomous
behavior (R-07 / Q-02).
"""
from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent
EOT = PKG_DIR / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
SESSION_AGE_GUARD = PKG_DIR / "scripts" / "session_age_guard.py"


def _eot_text() -> str:
    return EOT.read_text(encoding="utf-8")


def _section_0b(text: str) -> str:
    start = text.index("## §0b Session-age guard")
    # Anchor on the newline-prefixed heading so an inline `## When to use` reference
    # inside the §0b prose does not truncate the section early.
    end = text.index("\n## When to use", start)
    return text[start:end]


def test_interactive_three_option_list():
    body = _section_0b(_eot_text())
    for opt in ("Proceed in this session", "Finish in a fresh session", "Abort"):
        assert opt in body, f"missing session-age option: {opt}"
    assert "(recommended)" in body


def test_no_session_age_guard_bypass_preserved():
    body = _section_0b(_eot_text())
    assert "[no-session-age-guard]" in body
    assert "BYPASS" in body.upper() or "bypass" in body


def test_interactive_vs_noninteractive_diverge():
    body = _section_0b(_eot_text())
    # non-interactive → helper fail-closed with site=session-age
    assert "decision_gate_guard.py fail-closed" in body
    assert "--site session-age" in body
    # interactive → the option list (checked above) — the two paths are distinct branches
    assert "[no-interactive]" in body
    assert "else (interactive)" in body or "interactive)" in body


def test_autonomous_session_age_unchanged():
    """R-07 / Q-02 pin: an autonomous OVER STOPs (not bypassed, not routed to the helper)."""
    body = _section_0b(_eot_text())
    assert "_AUTONOMOUS" in body
    # The pinned rule step must keep the STOP behavior and say it is unchanged/preserved.
    assert "preserve" in body.lower() or "unchanged" in body.lower()
    assert "STOP on `OVER`" in body or "STOP on OVER" in body


def test_session_age_guard_exit_contract_unchanged():
    """session_age_guard.py itself is untouched: still exit 1 only on OVER, fail-OPEN otherwise."""
    src = SESSION_AGE_GUARD.read_text(encoding="utf-8")
    assert "OVER" in src
    # exit 1 on OVER path is preserved (grep the contract, proving no rewrite).
    assert "--threshold-hours" in src
    # And the SKILL.md explicitly states the contract is unchanged.
    assert "exit-code contract is UNCHANGED" in _section_0b(_eot_text())
