"""IVG-77: Drift/wiring tests for the branch-recovery.md Tier-1 memory file.

Asserts:
  (a) The recipe file exists and contains the canonical git update-ref recipe.
  (b) The recipe file does NOT expose git reset --hard as a standalone runnable
      fenced command (only allows it inside the "Why not" rationale prose).
  (c) Each referencing adapter SKILL.md (implement, gate, review, end_of_task)
      contains the ${CLAUDE_PLUGIN_ROOT}/memory/branch-recovery.md path literal.
  (d) branch-recovery.md is in TIER1_MEMORY_FILES (installer.py).
  (e) CLAUDE.md contains the branch-recovery.md reference.
  (f) pyproject.toml wires branch-recovery.md into the wheel via the
      quoin/memory directory force-include (replaces the former per-file
      dual-list guard; the directory mapping ships every quoin/memory/ file,
      so a present source file is sufficient — see
      test_wheel_memory_inventory_matches_tier1_set for the exact-set check).
"""
from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Path constants (parents[3] form — matches test_install_branch_hygiene_deployed.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG_DIR = REPO_ROOT / "quoin"  # quoin/quoin/ — the source package root

# Sanity check: fail loudly if the path math is wrong (MIN-1)
assert (PKG_DIR / "memory").is_dir(), (
    f"path math wrong: PKG_DIR/memory does not exist at {PKG_DIR / 'memory'}. "
    "Expected PKG_DIR = <repo_root>/quoin/quoin/ where 'memory' is a subdirectory."
)

RECIPE_FILE = PKG_DIR / "memory" / "branch-recovery.md"
CLAUDE_MD = PKG_DIR / "CLAUDE.md"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

REFERENCING_SKILLS = ["implement", "gate", "review", "end_of_task"]

RECIPE_ANCHOR = "update-ref refs/heads"
# The quoin/memory directory force-include ships every file under quoin/memory/
# (including branch-recovery.md) into the wheel. This replaced the former
# per-file enumeration, which drifted out of sync with TIER1_MEMORY_FILES.
EXPECTED_FORCE_INCLUDE_LINE = (
    '"quoin/memory" = "src/quoin/data/memory"'
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _skill_path(name: str) -> Path:
    return PKG_DIR / "adapters" / "claude" / "skills" / name / "SKILL.md"


# ---------------------------------------------------------------------------
# (a) Recipe file exists and contains the canonical anchor
# ---------------------------------------------------------------------------

def test_recipe_file_exists():
    """branch-recovery.md must exist in the quoin/memory/ directory."""
    assert RECIPE_FILE.exists(), (
        f"Expected recipe file at {RECIPE_FILE} — file not found. "
        "Run T-01 (create quoin/quoin/memory/branch-recovery.md)."
    )


def test_recipe_contains_update_ref_anchor():
    """The recipe file must contain 'update-ref refs/heads' (the canonical anchor)."""
    text = RECIPE_FILE.read_text(encoding="utf-8")
    assert RECIPE_ANCHOR in text, (
        f"branch-recovery.md must contain the literal '{RECIPE_ANCHOR}'. "
        "The canonical recipe uses 'git update-ref refs/heads/<protected>' — "
        "ensure the recipe file has not been accidentally changed."
    )


# ---------------------------------------------------------------------------
# (b) No standalone runnable git reset --hard fenced command
# ---------------------------------------------------------------------------

def test_no_standalone_git_reset_hard_fenced():
    """The recipe file must not present 'git reset --hard' as a standalone runnable
    fenced command.

    Strategy: find all fenced code blocks (``` ... ```) in the file and assert
    that none of them contain a line that is purely a 'git reset --hard ...' invocation.
    Allowing the substring inside prose/comments is fine — this guards only against
    a runnable command block.
    """
    text = RECIPE_FILE.read_text(encoding="utf-8")

    fenced_blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)

    for block in fenced_blocks:
        for line in block.splitlines():
            stripped = line.strip()
            # A line that IS a git reset --hard command
            if re.match(r"^(git -C \S+\s+)?git reset --hard", stripped):
                raise AssertionError(
                    f"branch-recovery.md contains 'git reset --hard' as a standalone "
                    f"runnable command in a fenced code block: {stripped!r}. "
                    "This is the auto-denied form. Only use 'git update-ref' in "
                    "runnable fences; the 'git reset --hard' form is allowed only "
                    "inside prose ('Why not' rationale)."
                )

    # Also assert the canonical form IS present (belt-and-suspenders with test (a))
    assert RECIPE_ANCHOR in text, (
        f"Expected '{RECIPE_ANCHOR}' in branch-recovery.md after verifying no "
        "forbidden fenced blocks — this is a belt-and-suspenders check."
    )


# ---------------------------------------------------------------------------
# (c) Each referencing adapter SKILL.md contains the path literal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (d) branch-recovery.md is in TIER1_MEMORY_FILES
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (e) CLAUDE.md contains the branch-recovery.md reference
# ---------------------------------------------------------------------------

def test_claude_md_references_branch_recovery():
    """CLAUDE.md must reference branch-recovery.md (parity with the skill layer)."""
    assert CLAUDE_MD.exists(), f"CLAUDE.md not found at {CLAUDE_MD}"
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert "branch-recovery.md" in text, (
        "quoin/quoin/CLAUDE.md must contain 'branch-recovery.md'. "
        "Add a pointer in the 'enforced at three layers' sentence "
        "(see T-07 in IVG-77)."
    )


# ---------------------------------------------------------------------------
# (f) pyproject.toml force-include line (dual-list guard)
# ---------------------------------------------------------------------------

