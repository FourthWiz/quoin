"""pytest CI wrapper for quoin/CLAUDE.md hook-documentation section correctness.

Verifies several structural pieces of quoin/CLAUDE.md that earlier stages added:
  (a) `checkpoint` appears in the Phase values enumeration line
  (b) The `### Hooks deployed by quoin` section exists (hook event/matcher table)
  (c) The `### Lifecycle skills (checkpoint / end_of_day / sleep)` section exists
  (d) The hooks paragraph's ordered (event, matcher) roster matches the shipped
      stanza set exactly (not merely a per-tuple substring presence check)

These are regression guards: they catch future edits to CLAUDE.md that accidentally
remove an addition (e.g., a merge conflict resolution that drops a section, or a
rebase that loses an edit), or that silently drift the hooks paragraph's roster
away from what installer.py actually registers.

Run:
  python3 -m pytest quoin/dev/tests/test_claude_md_hook_sections.py -v
"""

import re

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_MD = REPO_ROOT / "quoin" / "CLAUDE.md"

# The ordered (event, matcher) roster the hooks paragraph must name exactly —
# kept in sync with installer.py's _append_stanza call list by
# test_hook_stanza_count_parity.py, which reads that list via AST.
EXPECTED_HOOK_TUPLES = [
    ("UserPromptSubmit", "*"),
    ("PreCompact", "auto"),
    ("PostCompact", "auto"),
    ("SessionStart", "startup"),
    ("SessionStart", "resume"),
    ("SessionStart", "compact"),
    ("SessionEnd", "*"),
    ("WorktreeCreate", "*"),
    ("Stop", "*"),
]


def _read_claude_md() -> str:
    assert CLAUDE_MD.exists(), f"quoin/CLAUDE.md not found at {CLAUDE_MD}"
    return CLAUDE_MD.read_text(encoding="utf-8")


class TestClaudeMdHookSections:
    """Regression guards for hook-documentation sections in quoin/CLAUDE.md."""

    def test_claude_md_exists(self):
        """quoin/CLAUDE.md must be present in the repo."""
        assert CLAUDE_MD.exists(), f"quoin/CLAUDE.md not found at {CLAUDE_MD}"

    def test_checkpoint_in_phase_values(self):
        """`checkpoint` must appear in the Phase values enumeration.

        The line looks like:
          **Phase values:** `discover`, `architect`, ..., `checkpoint`, `ad-hoc`
        """
        content = _read_claude_md()
        # Find the Phase values line
        phase_line = None
        for line in content.splitlines():
            if "**Phase values:**" in line:
                phase_line = line
                break
        assert phase_line is not None, (
            "Could not find a line containing '**Phase values:**' in quoin/CLAUDE.md. "
            "The Phase values enumeration was removed or reformatted."
        )
        assert "checkpoint" in phase_line, (
            f"'checkpoint' not found in Phase values line:\n  {phase_line}\n\n"
            "This assertion catches accidental removal of that entry."
        )

    def test_hooks_deployed_section_exists(self):
        """`### Hooks deployed by quoin` section must exist.

        This section documents the (event, matcher) tuples deployed by
        `bash install.sh` and the event/timeout table. Its presence confirms
        the hooks-deploy documentation is in place.
        """
        content = _read_claude_md()
        assert "### Hooks deployed by quoin" in content, (
            "'### Hooks deployed by quoin' section not found in quoin/CLAUDE.md. "
            "It may have been accidentally removed."
        )

    def test_lifecycle_skills_section_exists(self):
        """`### Lifecycle skills (checkpoint / end_of_day / sleep)` section must exist.

        This section defines the boundary between /checkpoint, /end_of_day, and /sleep.
        """
        content = _read_claude_md()
        assert "### Lifecycle skills" in content, (
            "'### Lifecycle skills' section not found in quoin/CLAUDE.md. "
            "It may have been removed."
        )

    def test_hooks_paragraph_roster_matches_shipped_stanzas_exactly(self):
        """The hooks paragraph's ordered (event, matcher) roster must equal the
        shipped stanza set exactly — a list-equality check, not a whole-file
        substring presence check.

        The prior form of this assertion (`event in content and matcher in
        content`) was vacuous: `"*"` and `"auto"` appear all over CLAUDE.md, so
        the matcher half never discriminated, and the tuple pairing was never
        checked at all — it had been passing for three stanzas it did not know
        about. This form anchors on the single hooks paragraph line and parses
        the `Event/`matcher`` tokens from it directly, so a ninth token, a
        missing one, or a reordering all fail it.
        """
        content = _read_claude_md()
        anchor = f"registers {len(EXPECTED_HOOK_TUPLES)} (event, matcher) stanzas"
        para_line = None
        for line in content.splitlines():
            if anchor in line:
                para_line = line
                break
        assert para_line is not None, (
            f"Could not find the hooks paragraph line containing '{anchor}' in "
            "quoin/CLAUDE.md. The hooks-deploy paragraph may have been removed "
            "or its count word changed without updating this test."
        )
        tokens = re.findall(r"\b([A-Z][A-Za-z]+)/`([^`\s]+)`", para_line)
        assert tokens == EXPECTED_HOOK_TUPLES, (
            f"Hooks paragraph roster is {tokens}; expected {EXPECTED_HOOK_TUPLES} "
            "(ordered list equality — content and order both matter)."
        )

    def test_checkpoint_phase_value_is_backtick_quoted(self):
        """The `checkpoint` phase value must be quoted with backticks (style consistency)."""
        content = _read_claude_md()
        # Find the Phase values line and check backtick form
        for line in content.splitlines():
            if "**Phase values:**" in line:
                # Should contain `checkpoint` (with backticks)
                assert "`checkpoint`" in line, (
                    f"Phase values line found but `checkpoint` not backtick-quoted:\n  {line}"
                )
                return
        pytest.fail("No **Phase values:** line found in quoin/CLAUDE.md")

    def test_userpromptsubmit_section_exists(self):
        """userpromptsubmit.sh contract documentation must exist in CLAUDE.md."""
        content = _read_claude_md()
        assert "userpromptsubmit.sh" in content, (
            "'userpromptsubmit.sh' not found in quoin/CLAUDE.md — "
            "the hooks section may be missing or the filename was changed."
        )

    def test_basis_points_convention_documented(self):
        """The basis-points convention must be documented (prevents floating-point comparison regressions)."""
        content = _read_claude_md()
        assert "basis-points" in content.lower() or "basis_points" in content.lower(), (
            "Basis-points convention not found in quoin/CLAUDE.md. "
            "This documents the integer arithmetic used in hook threshold comparisons."
        )

    def test_tunable_constants_table_exists(self):
        """The tunable constants table (QUOIN_* env vars) must be documented."""
        content = _read_claude_md()
        assert "QUOIN_BYTES_PER_TOKEN" in content, (
            "'QUOIN_BYTES_PER_TOKEN' not found in quoin/CLAUDE.md — "
            "the tunable constants table may be missing."
        )
        assert "QUOIN_EFFECTIVE_CONTEXT_LIMIT" in content, (
            "'QUOIN_EFFECTIVE_CONTEXT_LIMIT' not found in quoin/CLAUDE.md — "
            "the tunable constants table may be missing."
        )
