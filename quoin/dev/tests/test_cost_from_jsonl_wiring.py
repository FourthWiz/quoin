"""test_cost_from_jsonl_wiring.py — structural smoke tests for SKILL.md fallback wiring.

Four deterministic test cases (pure pathlib + string matching, no LLM, no subprocess):
  1. test_cost_snapshot_has_fallback_wiring   — cost_snapshot SKILL.md has correct wiring
  2. test_end_of_task_has_fallback_wiring     — end_of_task SKILL.md has correct wiring
  3. test_install_sh_deploys_cost_from_jsonl  — install.sh deploy + Stage 5 cleanup present
  4. test_no_fallback_in_other_skills         — no accidental wiring in other SKILL.md files
"""

import pathlib
import re

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # project root
DEV_WORKFLOW = REPO_ROOT / "quoin"
SKILLS_DIR = DEV_WORKFLOW / "adapters" / "claude" / "skills"
INSTALL_SH = DEV_WORKFLOW / "install.sh"

COST_SNAPSHOT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "cost_snapshot" / "SKILL.md"
END_OF_TASK_SKILL   = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"


# ---------------------------------------------------------------------------
# Test 1: cost_snapshot/SKILL.md fallback wiring
# ---------------------------------------------------------------------------
def test_cost_snapshot_has_fallback_wiring():
    text = COST_SNAPSHOT_SKILL.read_text()

    # Per-UUID mode must be referenced
    assert "cost_from_jsonl.py session -i" in text, (
        "cost_snapshot SKILL.md missing per-UUID fallback: "
        "'cost_from_jsonl.py session -i'"
    )
    # Bulk mode must be referenced
    assert "cost_from_jsonl.py session --json --since" in text, (
        "cost_snapshot SKILL.md missing bulk fallback: "
        "'cost_from_jsonl.py session --json --since'"
    )
    # Fallback notice template must appear
    assert "[fallback: cost_from_jsonl.py — prices as of" in text, (
        "cost_snapshot SKILL.md missing fallback notice template"
    )
    # Anchor-order: ccusage must appear BEFORE cost_from_jsonl.py
    ccusage_pos = text.find("ccusage")
    fallback_pos = text.find("cost_from_jsonl.py")
    assert ccusage_pos < fallback_pos, (
        f"cost_snapshot SKILL.md: 'ccusage' must appear BEFORE 'cost_from_jsonl.py' "
        f"(ccusage_pos={ccusage_pos}, fallback_pos={fallback_pos}) — "
        "ccusage-first ordering required per D-02a / architecture fallback-only constraint"
    )


# ---------------------------------------------------------------------------
# Test 2: end_of_task/SKILL.md fallback wiring
# ---------------------------------------------------------------------------
def test_end_of_task_has_fallback_wiring():
    text = END_OF_TASK_SKILL.read_text()

    # Per-UUID mode must be referenced
    assert "cost_from_jsonl.py session -i" in text, (
        "end_of_task SKILL.md missing per-UUID fallback: "
        "'cost_from_jsonl.py session -i'"
    )
    # Bulk mode must be referenced
    assert "cost_from_jsonl.py session --json --since" in text, (
        "end_of_task SKILL.md missing bulk fallback: "
        "'cost_from_jsonl.py session --json --since'"
    )
    # Fallback notice template must appear
    assert "[fallback: cost_from_jsonl.py — prices as of" in text, (
        "end_of_task SKILL.md missing fallback notice template"
    )
    # Anchor-order: ccusage must appear BEFORE cost_from_jsonl.py
    ccusage_pos = text.find("ccusage")
    fallback_pos = text.find("cost_from_jsonl.py")
    assert ccusage_pos < fallback_pos, (
        f"end_of_task SKILL.md: 'ccusage' must appear BEFORE 'cost_from_jsonl.py' "
        f"(ccusage_pos={ccusage_pos}, fallback_pos={fallback_pos}) — "
        "ccusage-first ordering required per D-02b / architecture fallback-only constraint"
    )
    # Missing-binary pre-flight must be present
    binary_check_present = (
        "Binary check" in text
        or "command -v npx" in text
        or "npx --version" in text
    )
    assert binary_check_present, (
        "end_of_task SKILL.md missing binary pre-flight check — "
        "expected 'Binary check', 'command -v npx', or 'npx --version'"
    )
    # Path-agnostic all-failed gate must be present
    # Assert at least ONE of these path-agnostic phrases appears near the gate text
    path_agnostic_phrases = [
        "regardless of",
        "whichever of",
        "path-agnostic",
    ]
    path_agnostic_present = any(phrase in text for phrase in path_agnostic_phrases)
    # OR: two separate gate references (per-UUID loop AND bulk call)
    two_gate_refs = (
        "per-UUID loop" in text or "per-UUID" in text
    ) and (
        "bulk call" in text or "bulk" in text
    )
    assert path_agnostic_present or two_gate_refs, (
        "end_of_task SKILL.md missing path-agnostic all-failed gate. "
        "Expected one of: " + str(path_agnostic_phrases) + " OR both "
        "'per-UUID' and 'bulk' references. "
        "A substring-only 'every UUID' check is insufficient — it cannot distinguish "
        "a path-agnostic gate from a per-UUID-loop-only gate."
    )


# ---------------------------------------------------------------------------
# Test 3: installer.py deploys cost_from_jsonl.py + Stage 5 cleanup constants
# ---------------------------------------------------------------------------
def test_install_sh_deploys_cost_from_jsonl():
    """installer.py DEPLOYED_SCRIPTS must include cost_from_jsonl.py; OBSOLETE_SCRIPTS canary."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from quoin import installer  # noqa: PLC0415

    # cost_from_jsonl.py must be in DEPLOYED_SCRIPTS constant
    assert "cost_from_jsonl.py" in installer.DEPLOYED_SCRIPTS, (
        "installer.DEPLOYED_SCRIPTS does not include 'cost_from_jsonl.py'. "
        "The Python installer must deploy all scripts that install.sh used to deploy."
    )

    # Stage 5 cleanup canary: OBSOLETE_SCRIPTS must still include the legacy scripts
    assert "summarize_for_human.py" in installer.OBSOLETE_SCRIPTS, (
        "installer.OBSOLETE_SCRIPTS missing 'summarize_for_human.py' — Stage 5 cleanup canary. "
        "This is the regression guard that ensures the cleanup for obsolete scripts is not dropped."
    )
    assert "with_env.sh" in installer.OBSOLETE_SCRIPTS, (
        "installer.OBSOLETE_SCRIPTS missing 'with_env.sh' — Stage 5 cleanup canary."
    )


# ---------------------------------------------------------------------------
# Test 4: no accidental cost_from_jsonl.py wiring in other SKILL.md files
# ---------------------------------------------------------------------------
def test_no_fallback_in_other_skills():
    excluded = {
        COST_SNAPSHOT_SKILL.resolve(),
        # END_OF_TASK_SKILL now resolves under adapters/; vestigial after Phase 14, kept for symmetry.
        END_OF_TASK_SKILL.resolve(),
    }
    contaminated = []
    for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
        if skill_md.resolve() in excluded:
            continue
        text = skill_md.read_text()
        if "cost_from_jsonl.py" in text:
            contaminated.append(str(skill_md.relative_to(REPO_ROOT)))

    assert not contaminated, (
        "cost_from_jsonl.py found in unexpected SKILL.md files "
        "(per architecture line 353: only cost_snapshot and end_of_task should "
        "reference the fallback script): " + ", ".join(contaminated)
    )
