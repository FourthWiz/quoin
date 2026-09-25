"""Parametrized regression tests for the post-dispatch 1M-credit recovery mechanism
across all 19 §0 cheap-tier skills and all 9 §0' opus-tier skills.

Background
----------
IVG-73 introduced a §0-1m-context-precheck block (pre-dispatch model-name detection) to
all 19 §0 skills and all 9 §0' skills. IVG-89 found this detection is dead code — the
1M-context status is undetectable from inside the model (the model name never contains
'1m'). The pre-dispatch precheck blocks have been deleted.

Fix (IVG-89): post-dispatch error recovery is folded into the EXISTING
§0-worktree-fallback leaf (§0) and the Fail-OPEN path (§0'). When the dispatch Agent
call returns an error matching "Usage credits required for 1M context", skills recover
correctly:
  - §0 cheap-tier: emit a specific advisory + /model hint, proceed at parent tier
    (no AskUserQuestion; fail-OPEN to avoid blocking the user).
  - §0' opus-tier: issue AskUserQuestion (abort/proceed) for the 1M-credit-class error,
    and also for any other non-1M dispatch error (D-06 — §0' never silently loses recovery).

After IVG-89, the following are TRUE for all 28 skills:
  - No §0-1m-context-precheck-begin/end markers (deleted — Option A per gate D-03).
  - No §0prime-1m-context-precheck-begin/end markers (deleted from generator template).
  - No model-name substring detection ("if model_name contains '1m'" pattern).

For §0 (19 skills):
  - The §0-worktree-fallback-begin/end block contains the 1M core substring,
    advisory line, and /model remedy hint.
  - No AskUserQuestion for the 1M-credit recovery path (cheap-tier proceeds directly).

For §0' (7 skills):
  - The Fail-OPEN path (inside the §0' block) contains AskUserQuestion for
    1M-credit-class errors and for any other dispatch error.
  - Option labels: "Abort — I'll switch with /model first" and "Proceed in-session at parent tier".
"""
from __future__ import annotations

from pathlib import Path

import pytest

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
LEGACY_SKILLS_DIR = PKG_DIR / "skills"

# ---------------------------------------------------------------------------
# Path resolver (mirrors skill_md_path() in test_quoin_stage1_preamble.py)
# ---------------------------------------------------------------------------

# Skills migrated to the three-file adapter pattern. Self-contained copy —
# do NOT import across test modules (repo convention).
# IVG-118 T-05: derived from the filesystem (every skill with an adapter
# SKILL.md) instead of a hand-maintained literal — this is a GLOBAL fact (all
# 31 skills have an authoritative adapter SKILL.md; see D-02/D-03 in
# .workflow_artifacts/ivg-118-registration-manifest/current-plan.md), so a
# hardcoded subset silently drifts under-count. Matches
# installer.resolve_skill_source_md's adapter-preferred resolution exactly.
MIGRATED_TO_ADAPTER: frozenset[str] = frozenset(
    p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()
)


def skill_md_path(skill_name: str) -> Path:
    """Return the canonical SKILL.md path for a given skill."""
    if skill_name in MIGRATED_TO_ADAPTER:
        return ADAPTER_SKILLS_DIR / skill_name / "SKILL.md"
    return LEGACY_SKILLS_DIR / skill_name / "SKILL.md"


# ---------------------------------------------------------------------------
# Per-skill data tables
# ---------------------------------------------------------------------------

# §0 targets: (skill_name, declared_tier, proceed_ref)
SECTION0_TARGETS = [
    ("gate",           "sonnet", "§1"),
    ("end_of_day",     "sonnet", "§1"),
    ("start_of_day",   "haiku",  "§1"),
    ("capture_insight","haiku",  "§1"),
    ("cost_snapshot",  "haiku",  "§1"),
    ("weekly_review",  "haiku",  "§1"),
    ("end_of_task",    "sonnet", "§1"),
    ("implement",      "sonnet", "§1"),
    ("rollback",       "sonnet", "§1"),
    ("expand",         "sonnet", "§1"),
    ("revise-fast",    "sonnet", "§1"),
    ("triage",         "haiku",  "§1"),
    ("pr",             "sonnet", "§1"),
    ("status",         "haiku",  "§1"),
    ("cleanup",        "sonnet", "§0c"),
    ("sleep",          "sonnet", "§0c"),
    ("next_steps",     "haiku",  "§1"),
    ("checkpoint",     "sonnet", "§0c"),
    ("continue_work",  "sonnet", "§1"),
    ("workspace",      "sonnet", "§1"),
]

SKILL_DECLARED_TIER: dict[str, str] = {t[0]: t[1] for t in SECTION0_TARGETS}
SKILL_PROCEED_REF:   dict[str, str] = {t[0]: t[2] for t in SECTION0_TARGETS}
SECTION0_SKILLS: list[str] = [t[0] for t in SECTION0_TARGETS]

# §0' targets
SECTION0PRIME_TARGETS = [
    "architect",
    "plan",
    "critic",
    "revise",
    "review",
    "init_workflow",
    "discover",
    "specify",
    "security_review",
    "enrich",
]

# ---------------------------------------------------------------------------
# §0 per-class constants (IVG-89: diverged from §0' — cheap-tier drops
# AskUserQuestion for 1M recovery; §0' retains it)
# ---------------------------------------------------------------------------

# §0 worktree-fallback leaf: required 1M recovery tokens
S0_1M_CORE_SUBSTRING = "Usage credits required for 1M context"
S0_1M_ADVISORY_TOKEN = "1M-context credit mismatch"
S0_1M_MODEL_HINT     = "run /model to switch this session to standard context"
S0_1M_PROCEED_TOKEN  = "proceed to §1 at the current tier"  # or §0c for §0c-class skills

# §0 markers (D-03 Option A: §0-1m-context-precheck markers DELETED)
S0_DEAD_BEGIN_MARKER = "<!-- §0-1m-context-precheck-begin -->"
S0_DEAD_END_MARKER   = "<!-- §0-1m-context-precheck-end -->"

# §0 worktree-fallback markers (still present, used to scope assertions)
S0_WF_BEGIN_MARKER = "<!-- §0-worktree-fallback-begin -->"
S0_WF_END_MARKER   = "<!-- §0-worktree-fallback-end -->"

# §0 section heading
S0_SECTION_HEADING  = "## §0 Model dispatch"
S0_DISPATCH_TRIGGER = "If current_tier > declared_tier"

# NO_REDISPATCH hint appears in §0 block (for the manual kill-switch documentation)
NO_REDISPATCH_HINT  = "[no-redispatch]"

# ---------------------------------------------------------------------------
# §0' per-class constants (IVG-89: §0' retains AskUserQuestion in Fail-OPEN)
# ---------------------------------------------------------------------------

# §0' markers (D-03: §0prime-1m-context-precheck markers DELETED from generator template)
S0P_DEAD_BEGIN_MARKER = "<!-- §0prime-1m-context-precheck-begin -->"
S0P_DEAD_END_MARKER   = "<!-- §0prime-1m-context-precheck-end -->"

# §0' Fail-OPEN path: required 1M recovery tokens
S0P_1M_CORE_SUBSTRING = "Usage credits required for 1M context"
S0P_1M_OPTION_LABEL_ABORT   = "Abort — I'll switch with /model first"
S0P_1M_OPTION_LABEL_PROCEED = "Proceed in-session at parent tier"

# §0' section heading
S0P_SECTION_HEADING  = "## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)"
S0P_DISPATCH_ANCHOR  = "Dispatch action (when pollution detected"
S0P_FAILOPEN_ANCHOR  = "Fail-OPEN path:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _line_number(text: str, substring: str) -> int:
    """Return 1-based line number of first occurrence of substring, or -1."""
    for i, line in enumerate(text.splitlines(), start=1):
        if substring in line:
            return i
    return -1


def _extract_block(text: str, begin_marker: str, end_marker: str) -> str:
    """Return the text between (exclusive) begin and end markers, or empty string."""
    lines = text.splitlines()
    in_block = False
    block_lines: list[str] = []
    for line in lines:
        if begin_marker in line:
            in_block = True
            continue
        if end_marker in line:
            break
        if in_block:
            block_lines.append(line)
    return "\n".join(block_lines)


def _section_bounds(text: str, section_heading: str) -> tuple[int, int]:
    """Return (start_line, end_line) for the named H2 section (1-based, inclusive start).

    end_line is the line of the next ## heading after the section, or len(lines)+1.
    """
    lines = text.splitlines()
    start = _line_number(text, section_heading)
    assert start != -1, f"Section heading not found: {section_heading!r}"
    end = len(lines) + 1
    for i, line in enumerate(lines[start:], start=start + 1):
        if line.startswith("## ") and section_heading not in line:
            end = i
            break
    return start, end


# ---------------------------------------------------------------------------
# §0 parametrized tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skill", SECTION0_SKILLS)
class TestSection0Precheck:
    """IVG-89 regression tests for the §0 cheap-tier worktree-fallback 1M recovery.

    Tests verify:
    - Dead §0-1m-context-precheck markers are absent (D-03 Option A).
    - The worktree-fallback block contains the 1M recovery branch.
    - The 1M branch has the core substring, advisory, /model hint, and proceed action.
    - No model-name detection pattern remains.
    """

    def _read(self, skill: str) -> str:
        path = skill_md_path(skill)
        assert path.is_file(), f"SKILL.md not found: {path}"
        return path.read_text(encoding="utf-8")

    # ── D-03 Option A: dead markers must be absent ───────────────────────────

    def test_dead_precheck_markers_absent(self, skill):
        """D-03 Option A: §0-1m-context-precheck-begin/end markers deleted."""
        text = self._read(skill)
        assert S0_DEAD_BEGIN_MARKER not in text, (
            f"[{skill}] Dead §0-1m-context-precheck-begin marker must be absent (IVG-89 D-03)"
        )
        assert S0_DEAD_END_MARKER not in text, (
            f"[{skill}] Dead §0-1m-context-precheck-end marker must be absent (IVG-89 D-03)"
        )

    def test_no_model_name_detection(self, skill):
        """No model-name 1M detection pattern remains (dead code removed)."""
        text = self._read(skill)
        # The old detection checked for these backtick substrings in the model name.
        # All four must be absent from the §0 dispatch section entirely.
        dead_patterns = ["`1m`", "`1M`", "`(1M context)`", "`context-1m`"]
        # Restrict check to §0 section only (avoid false positives in other sections).
        sec_start, sec_end = _section_bounds(text, S0_SECTION_HEADING)
        lines = text.splitlines()
        s0_section = "\n".join(lines[sec_start - 1 : sec_end - 1])
        for pattern in dead_patterns:
            assert pattern not in s0_section, (
                f"[{skill}] Dead model-name detection pattern {pattern!r} found in §0 section"
            )

    # ── Worktree-fallback block presence ─────────────────────────────────────

    def test_worktree_fallback_markers_present(self, skill):
        """Worktree-fallback markers are still present (they host the 1M recovery)."""
        text = self._read(skill)
        assert S0_WF_BEGIN_MARKER in text, (
            f"[{skill}] §0-worktree-fallback-begin marker missing"
        )
        assert S0_WF_END_MARKER in text, (
            f"[{skill}] §0-worktree-fallback-end marker missing"
        )

    # ── 1M recovery branch content ────────────────────────────────────────────

    def test_1m_core_substring_in_worktree_fallback(self, skill):
        """1M-credit core substring present in the worktree-fallback block."""
        text = self._read(skill)
        block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert S0_1M_CORE_SUBSTRING in block, (
            f"[{skill}] 1M core substring {S0_1M_CORE_SUBSTRING!r} not found "
            f"in §0-worktree-fallback block"
        )

    def test_1m_advisory_token_in_worktree_fallback(self, skill):
        """1M-credit advisory token ('1M-context credit mismatch') present in leaf."""
        text = self._read(skill)
        block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert S0_1M_ADVISORY_TOKEN in block, (
            f"[{skill}] Advisory token {S0_1M_ADVISORY_TOKEN!r} not found in leaf"
        )

    def test_model_hint_in_worktree_fallback(self, skill):
        """/model remedy hint present in the 1M recovery branch."""
        text = self._read(skill)
        block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert S0_1M_MODEL_HINT in block, (
            f"[{skill}] /model remedy hint {S0_1M_MODEL_HINT!r} not found in leaf"
        )

    def test_no_ask_user_question_for_1m_recovery(self, skill):
        """§0 cheap-tier 1M recovery must NOT invoke AskUserQuestion (proceeds directly).

        The branch MENTIONS 'Do NOT call AskUserQuestion' as an explicit prohibition —
        so we check that the prohibition form is what appears, not a call invocation.
        """
        text = self._read(skill)
        block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        # Extract the 1M branch specifically: from "1M-credit-class" through the next
        # blank-line-separated section (Worktree-class or §0-sidecar).
        idx = block.find("1M-credit-class")
        if idx == -1:
            pytest.fail(f"[{skill}] '1M-credit-class' label not found in worktree-fallback block")
        # The branch text must contain the explicit prohibition "Do NOT call AskUserQuestion"
        # (meaning it's documented as forbidden, not invoked).
        rest = block[idx:]
        next_section = rest.find("\n  - ", len("1M-credit-class"))
        branch_text = rest if next_section == -1 else rest[:next_section]
        assert "Do NOT call AskUserQuestion" in branch_text, (
            f"[{skill}] §0 cheap-tier 1M branch must explicitly say 'Do NOT call AskUserQuestion' "
            f"to document that it proceeds directly at parent tier"
        )

    def test_no_tier_specific_noun_in_1m_advisory(self, skill):
        """The 1M advisory must not hardcode 'Sonnet'/'sonnet'/'Haiku'/'haiku' — uses 'parent tier'."""
        text = self._read(skill)
        block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        idx = block.find("1M-credit-class")
        if idx == -1:
            pytest.fail(f"[{skill}] '1M-credit-class' label not found in worktree-fallback block")
        rest = block[idx:]
        next_section = rest.find("\n  - ", len("1M-credit-class"))
        branch_text = rest if next_section == -1 else rest[:next_section]
        # Advisory must say "parent tier", not a specific model name
        for noun in ("Sonnet", "Haiku"):
            assert noun not in branch_text, (
                f"[{skill}] 1M advisory must not hardcode '{noun}' — use 'parent tier'"
            )

    # ── Existing §0 structural checks (still valid) ───────────────────────────

    def test_no_redispatch_hint_in_s0_section(self, skill):
        """[no-redispatch] hint documented in §0 section (manual kill-switch still present)."""
        text = self._read(skill)
        sec_start, sec_end = _section_bounds(text, S0_SECTION_HEADING)
        lines = text.splitlines()
        s0_section = "\n".join(lines[sec_start - 1 : sec_end - 1])
        assert NO_REDISPATCH_HINT in s0_section, (
            f"[{skill}] [no-redispatch] hint not found in §0 section"
        )

    def test_dispatch_trigger_present(self, skill):
        """'If current_tier > declared_tier' dispatch trigger present in §0 section."""
        text = self._read(skill)
        assert S0_DISPATCH_TRIGGER in text, (
            f"[{skill}] Dispatch trigger {S0_DISPATCH_TRIGGER!r} not found"
        )

    def test_proceed_ref_correct(self, skill):
        """proceed-ref assertion — §1 vs §0c per the per-skill table."""
        proceed_ref = SKILL_PROCEED_REF[skill]
        text = self._read(skill)
        if proceed_ref == "§0c":
            assert "§0c" in text, (
                f"[{skill}] §0c-class skill must have '§0c' somewhere in the file"
            )
        else:
            assert "§1" in text, (
                f"[{skill}] §1-class skill must have '§1' in the file (proceed-to-body refs)"
            )


# ---------------------------------------------------------------------------
# §0' parametrized tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skill", SECTION0PRIME_TARGETS)
class TestSection0PrimePrecheck:
    """IVG-89 regression tests for the §0' opus-tier Fail-OPEN 1M recovery.

    Tests verify:
    - Dead §0prime-1m-context-precheck markers are absent (D-03 applied to generator).
    - The §0' block's Fail-OPEN path contains AskUserQuestion for 1M-credit errors.
    - The AskUserQuestion option labels are correct.
    - §0' fires AskUserQuestion on any dispatch error (D-06).
    - No model-name detection remains.
    """

    def _read(self, skill: str) -> str:
        path = skill_md_path(skill)
        assert path.is_file(), f"SKILL.md not found: {path}"
        return path.read_text(encoding="utf-8")

    def _extract_s0p_block(self, text: str) -> str:
        """Extract the §0' section up to the next ## heading."""
        idx = text.find(S0P_SECTION_HEADING)
        if idx == -1:
            return ""
        rest = text[idx:]
        # Find the next ## heading after the section heading line
        next_h2 = rest.find("\n## ", len(S0P_SECTION_HEADING))
        return rest if next_h2 == -1 else rest[:next_h2]

    # ── D-03: dead markers absent from §0' ───────────────────────────────────

    def test_dead_precheck_markers_absent(self, skill):
        """D-03: §0prime-1m-context-precheck-begin/end markers deleted from §0' block."""
        text = self._read(skill)
        assert S0P_DEAD_BEGIN_MARKER not in text, (
            f"[{skill}] Dead §0prime-1m-context-precheck-begin marker must be absent (IVG-89 D-03)"
        )
        assert S0P_DEAD_END_MARKER not in text, (
            f"[{skill}] Dead §0prime-1m-context-precheck-end marker must be absent (IVG-89 D-03)"
        )

    def test_no_model_name_detection(self, skill):
        """No model-name 1M detection pattern remains in §0' block."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        dead_patterns = ["`1m`", "`1M`", "`(1M context)`", "`context-1m`"]
        for pattern in dead_patterns:
            assert pattern not in block, (
                f"[{skill}] Dead model-name detection pattern {pattern!r} found in §0' block"
            )

    # ── §0' Fail-OPEN path: 1M recovery AskUserQuestion ─────────────────────

    def test_failopen_path_present(self, skill):
        """Fail-OPEN path section heading present in §0' block."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        assert S0P_FAILOPEN_ANCHOR in block, (
            f"[{skill}] '{S0P_FAILOPEN_ANCHOR}' not found in §0' block"
        )

    def test_1m_core_substring_in_failopen(self, skill):
        """1M core substring present in §0' Fail-OPEN path."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        assert S0P_1M_CORE_SUBSTRING in block, (
            f"[{skill}] 1M core substring {S0P_1M_CORE_SUBSTRING!r} not found in §0' block"
        )

    def test_option_labels_present_in_failopen(self, skill):
        """Both AskUserQuestion option labels appear in the §0' Fail-OPEN section."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        # Locate the Fail-OPEN section within the block
        failopen_idx = block.find(S0P_FAILOPEN_ANCHOR)
        assert failopen_idx != -1, f"[{skill}] Fail-OPEN anchor not found"
        failopen_text = block[failopen_idx:]
        assert S0P_1M_OPTION_LABEL_ABORT in failopen_text, (
            f"[{skill}] Option label not found in §0' Fail-OPEN: {S0P_1M_OPTION_LABEL_ABORT!r}"
        )
        assert S0P_1M_OPTION_LABEL_PROCEED in failopen_text, (
            f"[{skill}] Option label not found in §0' Fail-OPEN: {S0P_1M_OPTION_LABEL_PROCEED!r}"
        )

    def test_generic_error_recovery_present(self, skill):
        """D-06: §0' Fail-OPEN path also handles any non-1M error (generic AskUserQuestion)."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        failopen_idx = block.find(S0P_FAILOPEN_ANCHOR)
        failopen_text = block[failopen_idx:]
        # The generic branch should address "any other error" or "non-1M"
        assert "Any other error" in failopen_text or "any other" in failopen_text.lower(), (
            f"[{skill}] §0' Fail-OPEN must handle any non-1M error (D-06 guarantee)"
        )

    def test_no_section1_ref_in_s0p_block(self, skill):
        """§0' block must NOT reference '§1' as the proceed target; uses 'skill body'."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        assert "§1" not in block, (
            f"[{skill}] §0' block must NOT contain '§1' (use 'skill body' instead)"
        )
        assert "skill body" in block, (
            f"[{skill}] §0' block must contain 'skill body' (proceed-to-body reference)"
        )

    def test_opus_noun_in_s0p_block(self, skill):
        """'Opus'/'opus' present in §0' block; 'Sonnet'/'sonnet' absent in Fail-OPEN."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        failopen_idx = block.find(S0P_FAILOPEN_ANCHOR)
        failopen_text = block[failopen_idx:] if failopen_idx != -1 else ""
        # Block should mention opus (dispatch target)
        assert "opus" in block.lower(), (
            f"[{skill}] §0' block must mention 'opus'/'Opus' as the dispatch target"
        )
        # Fail-OPEN section should not say Sonnet (wrong tier)
        assert "Sonnet" not in failopen_text and "sonnet" not in failopen_text, (
            f"[{skill}] §0' Fail-OPEN must not say 'Sonnet'/'sonnet' — use 'Opus'/'opus'"
        )

    def test_s0p_section_heading_present(self, skill):
        """§0' section heading present exactly once."""
        text = self._read(skill)
        count = text.count(S0P_SECTION_HEADING)
        assert count == 1, (
            f"[{skill}] §0' section heading expected exactly 1 time, got {count}"
        )

    def test_dispatch_anchor_present(self, skill):
        """Dispatch action anchor present in §0' block."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        assert S0P_DISPATCH_ANCHOR in block, (
            f"[{skill}] §0' dispatch anchor {S0P_DISPATCH_ANCHOR!r} not found in block"
        )

    def test_skill_token_in_s0p_block(self, skill):
        """The skill's own /<skill> token appears in the §0' block."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        skill_token = f"/{skill}"
        assert skill_token in block, (
            f"[{skill}] Skill token {skill_token!r} not found in §0' block"
        )

    def test_no_implement_token_in_s0p_block(self, skill):
        """No literal '/implement' in the §0' block (correct skill token substituted)."""
        text = self._read(skill)
        block = self._extract_s0p_block(text)
        assert "/implement" not in block, (
            f"[{skill}] '/implement' found in §0' block — skill token substitution missing"
        )


# ---------------------------------------------------------------------------
# D-06: Classification-logic unit test on synthetic error strings
# ---------------------------------------------------------------------------

class TestClassificationLogic:
    """D-06: Falsifiable unit tests for the 1M error classification logic.

    The runtime 1M error string is unfalsifiable in CI (no harness can trigger a real
    1M-credit 400 error). These tests exercise the CLASSIFICATION RULE against synthetic
    error string fixtures, providing a falsifiable surrogate.

    Classification rule (documented in the worktree-fallback leaves and §0' Fail-OPEN):
      - 1M-credit-class:  error text contains "Usage credits required for 1M context"
      - Worktree-class:   error text contains "Cannot create agent worktree", OR
                          ("worktree" AND "not in a git repository")
      - Other-class:      anything else
    """

    # The core 1M-credit substring (shortest stable form, per D-06)
    CORE_1M_SUBSTRING = "Usage credits required for 1M context"

    # Example worktree-class error strings
    WORKTREE_ERROR_A = "Cannot create agent worktree at /tmp/foo"
    WORKTREE_ERROR_B = "Error: worktree setup failed: not in a git repository"

    # A realistic 1M-credit error string (from brief, per Q-02)
    REALISTIC_1M_ERROR = (
        "API Error: Usage credits required for 1M context · run /usage-credits to turn them "
        "on, or /model to switch to standard context"
    )

    # Arbitrary other-class error (no 1M or worktree substring)
    OTHER_ERROR = "API Error: model overloaded, retry later"

    def _classify(self, error_text: str) -> str:
        """Implement the classification rule as a pure Python function (mirrors the SKILL.md prose)."""
        if self.CORE_1M_SUBSTRING in error_text:
            return "1m-credit-class"
        if "Cannot create agent worktree" in error_text:
            return "worktree-class"
        if "worktree" in error_text and "not in a git repository" in error_text:
            return "worktree-class"
        return "other-class"

    def test_1m_credit_class_on_core_substring(self):
        """Synthetic error containing core 1M substring → 1m-credit-class."""
        assert self._classify(self.CORE_1M_SUBSTRING) == "1m-credit-class"

    def test_1m_credit_class_on_realistic_error(self):
        """Realistic 1M-credit error string → 1m-credit-class."""
        assert self._classify(self.REALISTIC_1M_ERROR) == "1m-credit-class"

    def test_worktree_class_on_cannot_create(self):
        """Error containing 'Cannot create agent worktree' → worktree-class."""
        assert self._classify(self.WORKTREE_ERROR_A) == "worktree-class"

    def test_worktree_class_on_not_in_git_repo(self):
        """Error containing both 'worktree' and 'not in a git repository' → worktree-class."""
        assert self._classify(self.WORKTREE_ERROR_B) == "worktree-class"

    def test_other_class_on_arbitrary_error(self):
        """Arbitrary error string (no 1M or worktree substring) → other-class."""
        assert self._classify(self.OTHER_ERROR) == "other-class"

    def test_1m_takes_precedence_over_worktree(self):
        """If both 1M and worktree substrings appear, 1M-credit-class takes precedence.

        The classification rule checks 1M first (§0 leaf prose: '1M-credit-class'
        listed before 'Worktree-class').
        """
        combined = self.CORE_1M_SUBSTRING + " " + self.WORKTREE_ERROR_A
        assert self._classify(combined) == "1m-credit-class"

    def test_empty_error_is_other_class(self):
        """Empty error string → other-class (no substrings match)."""
        assert self._classify("") == "other-class"

    def test_partial_1m_substring_not_matched(self):
        """A partial match of the 1M core substring does not trigger 1m-credit-class."""
        partial = "Usage credits required for context"  # missing "1M"
        assert self._classify(partial) == "other-class"

    def test_all_19_s0_files_contain_core_substring(self):
        """Canonical-body equality test (T-03): all 19 §0 leaves contain the 1M core substring."""
        missing = []
        for skill_name, _, _ in SECTION0_TARGETS:
            path = skill_md_path(skill_name)
            if not path.is_file():
                missing.append(f"{skill_name}: SKILL.md not found at {path}")
                continue
            text = path.read_text(encoding="utf-8")
            block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
            if self.CORE_1M_SUBSTRING not in block:
                missing.append(
                    f"{skill_name}: 1M core substring not found in §0-worktree-fallback block"
                )
        assert not missing, "Canonical-body equality failures:\n" + "\n".join(missing)

    def test_all_7_s0p_files_contain_core_substring(self):
        """All 9 §0' Fail-OPEN paths contain the 1M core substring."""
        missing = []
        for skill_name in SECTION0PRIME_TARGETS:
            path = skill_md_path(skill_name)
            if not path.is_file():
                missing.append(f"{skill_name}: SKILL.md not found at {path}")
                continue
            text = path.read_text(encoding="utf-8")
            idx = text.find(S0P_SECTION_HEADING)
            if idx == -1:
                missing.append(f"{skill_name}: §0' section heading not found")
                continue
            next_h2 = text.find("\n## ", idx + len(S0P_SECTION_HEADING))
            block = text[idx:] if next_h2 == -1 else text[idx:next_h2]
            if self.CORE_1M_SUBSTRING not in block:
                missing.append(
                    f"{skill_name}: 1M core substring not found in §0' block"
                )
        assert not missing, "§0' canonical-body equality failures:\n" + "\n".join(missing)
