"""Parametrized regression tests for the proactive 1M-dispatch precheck (IVG-90 Stage 2).

Background
----------
IVG-90 Stage 2 adds two byte-identical marked regions into the §0 block of all
19 cheap-tier SKILL.md files:

  1. <!-- §0-1m-decide-begin --> ... <!-- §0-1m-decide-end -->
     Pre-dispatch 1M check: runs dispatch_config.py --decide; if "safe-path" is returned,
     bypasses dispatch and proceeds at the current tier. Must appear BEFORE the
     "Spawn an Agent subagent" line. Must be byte-identical across all 19 files.

  2. <!-- §0-1m-cachewrite-begin --> ... <!-- §0-1m-cachewrite-end --> (appears TWICE per file)
     Occurrence 0 (success path, --result safe): after "Wait for the subagent." and
       BEFORE "Return its output as your final response. STOP." (m-06 ordering constraint).
     Occurrence 1 (IVG-89 leaf, --result unsafe): inside the §0-worktree-fallback block,
       inside the 1M-credit-class branch, BEFORE "Then proceed to §1 at the current tier".
     Both occurrences must be byte-identical across all 19 files (within each occurrence index).

Self-contained: no cross-imports from test_1m_context_precheck.
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
# Path resolver (self-contained copy — do NOT import from test_1m_context_precheck)
# ---------------------------------------------------------------------------

# IVG-118 T-05: derived from the filesystem (every skill with an adapter
# SKILL.md) instead of a hand-maintained literal — see D-02/D-03 in
# .workflow_artifacts/ivg-118-registration-manifest/current-plan.md.
MIGRATED_TO_ADAPTER: frozenset[str] = frozenset(
    p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()
)

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
SECTION0_SKILLS: list[str] = [t[0] for t in SECTION0_TARGETS]


def skill_md_path(skill_name: str) -> Path:
    """Return the canonical SKILL.md path for a given skill."""
    if skill_name in MIGRATED_TO_ADAPTER:
        return ADAPTER_SKILLS_DIR / skill_name / "SKILL.md"
    return LEGACY_SKILLS_DIR / skill_name / "SKILL.md"


# ---------------------------------------------------------------------------
# Marker constants
# ---------------------------------------------------------------------------

DECIDE_BEGIN     = "<!-- §0-1m-decide-begin -->"
DECIDE_END       = "<!-- §0-1m-decide-end -->"
CACHEWRITE_BEGIN = "<!-- §0-1m-cachewrite-begin -->"
CACHEWRITE_END   = "<!-- §0-1m-cachewrite-end -->"

# From test_1m_context_precheck (self-contained copy)
S0_WF_BEGIN_MARKER = "<!-- §0-worktree-fallback-begin -->"
S0_WF_END_MARKER   = "<!-- §0-worktree-fallback-end -->"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_block(text: str, begin_marker: str, end_marker: str) -> str:
    """Return the text between (exclusive) begin and end markers (first occurrence)."""
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


def _extract_all_blocks(text: str, begin_marker: str, end_marker: str) -> list[str]:
    """Return ALL non-overlapping text spans between begin and end markers.

    Scans linearly; collects all (begin, end) pairs; returns list of inner text
    for each pair. Orphaned begin markers (no matching end) are skipped.
    Returns empty list if no complete pairs found.
    """
    lines = text.splitlines()
    results: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if begin_marker in line:
            current = []
        elif end_marker in line:
            if current is not None:
                results.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    return results


# ---------------------------------------------------------------------------
# TestDecideBlockByteIdentity
# ---------------------------------------------------------------------------

class TestDecideBlockByteIdentity:

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_all_19_s0_files_have_decide_block(self, skill: str) -> None:
        """All 19 SKILL.md files contain the §0-1m-decide begin/end markers."""
        text = skill_md_path(skill).read_text()
        assert DECIDE_BEGIN in text, f"{skill}: missing {DECIDE_BEGIN}"
        assert DECIDE_END in text, f"{skill}: missing {DECIDE_END}"

    def test_all_19_s0_files_decide_block_byte_identical(self) -> None:
        """§0-1m-decide blocks are byte-identical across all 19 files."""
        blocks = [
            _extract_block(skill_md_path(skill).read_text(), DECIDE_BEGIN, DECIDE_END)
            for skill in SECTION0_SKILLS
        ]
        missing = [
            skill for skill, b in zip(SECTION0_SKILLS, blocks) if not b.strip()
        ]
        assert not missing, f"Skills with empty decide block: {missing}"
        reference = blocks[0]
        differing = [
            skill for skill, b in zip(SECTION0_SKILLS, blocks) if b != reference
        ]
        assert not differing, (
            f"Decide blocks differ from {SECTION0_SKILLS[0]} in: {differing}"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_decide_block_no_ask_user_question(self, skill: str) -> None:
        """The decide block must explicitly prohibit AskUserQuestion (R-09 guard).
        The phrase 'Do NOT call AskUserQuestion' must be present; no AskUserQuestion
        invocation (without the preceding 'Do NOT call') may appear."""
        text = skill_md_path(skill).read_text()
        block = _extract_block(text, DECIDE_BEGIN, DECIDE_END)
        # R-09: the prohibition phrase must be present
        assert "Do NOT call AskUserQuestion" in block, (
            f"{skill}: decide block must explicitly say 'Do NOT call AskUserQuestion'"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_decide_block_contains_safe_path_proceed(self, skill: str) -> None:
        """The decide block must contain the safe-path proceed instruction (§1/§0c literal)."""
        text = skill_md_path(skill).read_text()
        block = _extract_block(text, DECIDE_BEGIN, DECIDE_END)
        assert "§1/§0c" in block, (
            f"{skill}: decide block must contain literal '§1/§0c' proceed instruction"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_decide_block_no_hardcoded_tier_noun(self, skill: str) -> None:
        """The CLI call in the decide block must use '<declared_tier>' placeholder, not a
        hardcoded tier name. The example text '(e.g. \"sonnet\" or \"haiku\")' is acceptable
        as explanatory prose, but the --tier argument must reference <declared_tier>."""
        text = skill_md_path(skill).read_text()
        block = _extract_block(text, DECIDE_BEGIN, DECIDE_END)
        assert "--tier <declared_tier>" in block, (
            f"{skill}: decide block CLI call must use '--tier <declared_tier>' placeholder"
        )


# ---------------------------------------------------------------------------
# TestCachewriteBlockByteIdentity
# ---------------------------------------------------------------------------

class TestCachewriteBlockByteIdentity:

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_all_19_s0_files_have_two_cachewrite_blocks(self, skill: str) -> None:
        """All 19 files contain exactly two §0-1m-cachewrite begin/end marker pairs."""
        text = skill_md_path(skill).read_text()
        blocks = _extract_all_blocks(text, CACHEWRITE_BEGIN, CACHEWRITE_END)
        assert len(blocks) == 2, (
            f"{skill}: expected 2 cachewrite blocks, found {len(blocks)}"
        )

    def test_all_19_s0_files_cachewrite_block_occurrence0_byte_identical(self) -> None:
        """§0-1m-cachewrite occurrence 0 (success path, --result safe) is byte-identical
        across all 19 files."""
        all_blocks = [
            _extract_all_blocks(skill_md_path(skill).read_text(), CACHEWRITE_BEGIN, CACHEWRITE_END)
            for skill in SECTION0_SKILLS
        ]
        occurrence0 = [b[0] for b in all_blocks if len(b) >= 1]
        assert len(occurrence0) == len(SECTION0_SKILLS), "Some files missing cachewrite occurrence 0"
        reference = occurrence0[0]
        differing = [
            skill for skill, b in zip(SECTION0_SKILLS, occurrence0) if b != reference
        ]
        assert not differing, (
            f"Cachewrite occurrence 0 differs from {SECTION0_SKILLS[0]} in: {differing}"
        )

    def test_all_19_s0_files_cachewrite_block_occurrence1_byte_identical(self) -> None:
        """§0-1m-cachewrite occurrence 1 (IVG-89 leaf, --result unsafe) is byte-identical
        across all 19 files."""
        all_blocks = [
            _extract_all_blocks(skill_md_path(skill).read_text(), CACHEWRITE_BEGIN, CACHEWRITE_END)
            for skill in SECTION0_SKILLS
        ]
        occurrence1 = [b[1] for b in all_blocks if len(b) >= 2]
        assert len(occurrence1) == len(SECTION0_SKILLS), "Some files missing cachewrite occurrence 1"
        reference = occurrence1[0]
        differing = [
            skill for skill, b in zip(SECTION0_SKILLS, occurrence1) if b != reference
        ]
        assert not differing, (
            f"Cachewrite occurrence 1 differs from {SECTION0_SKILLS[0]} in: {differing}"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_cachewrite_occurrence0_contains_safe(self, skill: str) -> None:
        """Occurrence 0 (success path) must contain '--result safe'."""
        text = skill_md_path(skill).read_text()
        blocks = _extract_all_blocks(text, CACHEWRITE_BEGIN, CACHEWRITE_END)
        assert len(blocks) >= 1, f"{skill}: no cachewrite blocks found"
        assert "--result safe" in blocks[0], (
            f"{skill}: cachewrite occurrence 0 must contain '--result safe'"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_cachewrite_occurrence1_contains_unsafe(self, skill: str) -> None:
        """Occurrence 1 (leaf path) must contain '--result unsafe'."""
        text = skill_md_path(skill).read_text()
        blocks = _extract_all_blocks(text, CACHEWRITE_BEGIN, CACHEWRITE_END)
        assert len(blocks) >= 2, f"{skill}: fewer than 2 cachewrite blocks found"
        assert "--result unsafe" in blocks[1], (
            f"{skill}: cachewrite occurrence 1 must contain '--result unsafe'"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_cachewrite_occurrence0_precedes_wait_line(self, skill: str) -> None:
        """Occurrence 0 must appear AFTER 'Wait for the subagent.' (standalone) and BEFORE
        'Return its output as your final response. STOP.' in the file (m-06 ordering).
        The original compound line has been split by the propagation step.

        Scope note (IVG-117): 10 of the 19 §0 skills now also carry a §0‴ Minimum-tier
        guard block (mirrors the Opus §0″ template) that legitimately re-introduces the
        compound 'Wait for the subagent. Return its output as your final response. STOP.'
        literal as a required token — that block is unrelated to the §0 cachewrite
        propagation this test guards. Scope the "compound line must be split" assertion
        to the §0 block only (everything before the §0‴ heading, if present)."""
        text = skill_md_path(skill).read_text()
        mintier_sonnet_idx = text.find("## §0‴ Minimum-tier guard")
        s0_scope = text if mintier_sonnet_idx == -1 else text[:mintier_sonnet_idx]
        # Verify the COMPOUND line no longer exists WITHIN THE §0 BLOCK
        assert "Wait for the subagent. Return its output as your final response. STOP." not in s0_scope, (
            f"{skill}: compound 'Wait...STOP.' line must be split (m-06)"
        )
        pos_wait = text.find("      Wait for the subagent.\n")
        pos_cw_begin = text.find(CACHEWRITE_BEGIN)
        pos_return = text.find("      Return its output as your final response. STOP.")
        assert pos_wait != -1, f"{skill}: 'Wait for the subagent.' standalone line not found"
        assert pos_cw_begin != -1, f"{skill}: {CACHEWRITE_BEGIN} not found"
        assert pos_return != -1, f"{skill}: 'Return its output...' line not found"
        assert pos_wait < pos_cw_begin < pos_return, (
            f"{skill}: m-06 ordering violated: "
            f"wait={pos_wait} cw_begin={pos_cw_begin} return={pos_return}"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_cachewrite_occurrence1_inside_worktree_fallback(self, skill: str) -> None:
        """Occurrence 1 must appear INSIDE the §0-worktree-fallback block."""
        text = skill_md_path(skill).read_text()
        wf_block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert CACHEWRITE_BEGIN in wf_block, (
            f"{skill}: cachewrite occurrence 1 must be inside the worktree-fallback block"
        )


# ---------------------------------------------------------------------------
# TestExistingCompatibility
# ---------------------------------------------------------------------------

class TestExistingCompatibility:

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_1m_core_substring_still_in_worktree_fallback(self, skill: str) -> None:
        """Existing IVG-89 test invariant: the 1M core substring is still in the wf block
        after the cachewrite markers are inserted inside it."""
        text = skill_md_path(skill).read_text()
        wf_block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert "Usage credits required for 1M context" in wf_block, (
            f"{skill}: '1M context' core substring must remain in worktree-fallback block"
        )

    @pytest.mark.parametrize("skill", SECTION0_SKILLS)
    def test_no_ask_user_question_in_1m_branch_in_wf_block(self, skill: str) -> None:
        """The 1M-credit-class branch inside the wf block must say 'Do NOT call AskUserQuestion'."""
        text = skill_md_path(skill).read_text()
        wf_block = _extract_block(text, S0_WF_BEGIN_MARKER, S0_WF_END_MARKER)
        assert "Do NOT call AskUserQuestion" in wf_block, (
            f"{skill}: wf block must still say 'Do NOT call AskUserQuestion' in 1M branch"
        )
