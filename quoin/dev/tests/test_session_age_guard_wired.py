"""T-04: Static test that session-age guard is wired into end_of_task/SKILL.md.

T-09 extends this file with run/SKILL.md ordering assertions.

Checks:
  (a) §0b heading present in end_of_task/SKILL.md
  (b) Helper call present in end_of_task/SKILL.md
  (c) [no-session-age-guard] override sentinel present in end_of_task/SKILL.md
  (d) run/SKILL.md contains 'Pre-flight: session-age guard' heading (T-09 section)
  (e) [T-09] 'Pre-flight: session-age guard' heading appears AFTER
      '### Initialize cost ledger' and BEFORE '### Check git state' in run/SKILL.md
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EOT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"


class TestSessionAgeGuardWired:
    """end_of_task wiring checks (T-04)."""

    def _eot_text(self) -> str:
        assert EOT_SKILL.exists(), f"end_of_task/SKILL.md not found at {EOT_SKILL}"
        return EOT_SKILL.read_text()

    def _run_text(self) -> str:
        assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
        return RUN_SKILL.read_text()

    def test_eot_has_0b_heading(self):
        """end_of_task/SKILL.md must contain the '## §0b Session-age guard' heading."""
        text = self._eot_text()
        assert "## §0b Session-age guard" in text, (
            "end_of_task/SKILL.md is missing '## §0b Session-age guard' heading"
        )

    def test_eot_has_helper_call(self):
        """end_of_task/SKILL.md must contain the session_age_guard.py helper call."""
        text = self._eot_text()
        assert "session_age_guard.py" in text, (
            "end_of_task/SKILL.md does not call session_age_guard.py"
        )
        assert "--threshold-hours 6.0" in text, (
            "end_of_task/SKILL.md does not pass --threshold-hours 6.0 to the guard"
        )

    def test_eot_has_no_session_age_guard_sentinel(self):
        """end_of_task/SKILL.md must document the [no-session-age-guard] override."""
        text = self._eot_text()
        assert "[no-session-age-guard]" in text, (
            "end_of_task/SKILL.md is missing the [no-session-age-guard] override sentinel"
        )

    # --- T-09 extensions: run/SKILL.md ordering assertions ---

    def test_run_has_preflight_heading(self):
        """run/SKILL.md must contain 'Pre-flight: session-age guard' heading (T-09)."""
        text = self._run_text()
        assert "Pre-flight: session-age guard" in text, (
            "run/SKILL.md is missing 'Pre-flight: session-age guard' section (T-09)"
        )

    def test_run_preflight_ordering(self):
        """In run/SKILL.md: '### Pre-flight: session-age guard' must appear AFTER
        '### Initialize cost ledger' and BEFORE '### Check git state'.

        Ordering is asserted by comparing line numbers — not just presence.
        """
        text = self._run_text()
        lines = text.splitlines()

        preflight_line = None
        init_ledger_line = None
        check_git_line = None

        for i, line in enumerate(lines, start=1):
            if "### Initialize cost ledger" in line:
                init_ledger_line = i
            elif "### Pre-flight: session-age guard" in line:
                preflight_line = i
            elif "### Check git state" in line:
                check_git_line = i

        assert init_ledger_line is not None, (
            "run/SKILL.md is missing '### Initialize cost ledger' heading"
        )
        assert preflight_line is not None, (
            "run/SKILL.md is missing '### Pre-flight: session-age guard' heading"
        )
        assert check_git_line is not None, (
            "run/SKILL.md is missing '### Check git state' heading"
        )

        assert init_ledger_line < preflight_line, (
            f"'### Initialize cost ledger' (line {init_ledger_line}) must appear BEFORE "
            f"'### Pre-flight: session-age guard' (line {preflight_line}) in run/SKILL.md"
        )
        assert preflight_line < check_git_line, (
            f"'### Pre-flight: session-age guard' (line {preflight_line}) must appear BEFORE "
            f"'### Check git state' (line {check_git_line}) in run/SKILL.md"
        )
