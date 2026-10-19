"""
Drift/generation tests for inject_pollution_dispatch.py (IVG-69 Stage A).

Asserts:
  - --check exits 0 on the freshly-generated tree (freshness gate)
  - Idempotence as byte-equality: running the generator twice yields identical bytes
  - Block-scoped token cross-guard (MAJ-2): all 6 REQUIRED_TOKENS from
    test_quoin_pollution_preamble.py + the 3 score-extraction strings from
    test_pollution_score_extraction.py appear INSIDE the extracted §0' block
  - Exactly one §0' heading in each of the 7 target adapter files
  - Zero §0' headings in the 14 exclusion-skill adapters (cheap-tier + orchestrators)

Per lesson 2026-04-23: NO live LLM calls — only deterministic string matching.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Add quoin/scripts/ to path so we can import inject_pollution_dispatch directly
# (mirrors how test_preamble_freshness.py imports build_preambles)
_TESTS_DIR = Path(__file__).parent
_SCRIPTS_DIR = _TESTS_DIR.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import inject_pollution_dispatch as generator  # noqa: E402


# ─── Shared test fixtures ─────────────────────────────────────────────────────

ADAPTER_DIR = _TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"

POLLUTION_HEADING = generator.POLLUTION_HEADING
TARGET_SKILLS = generator.POLLUTION_TARGET_SKILLS

# 12 cheap-tier skills — must NOT carry §0'.
CHEAP_TIER_SKILLS = [
    "gate",
    "end_of_day",
    "start_of_day",
    "capture_insight",
    "cost_snapshot",
    "weekly_review",
    "end_of_task",
    "implement",
    "rollback",
    "expand",
    "revise-fast",
    "triage",
]

# Orchestrators — must NOT carry §0'.
ORCHESTRATOR_SKILLS = ["run", "thorough_plan"]

# All 14 exclusion adapters
EXCLUSION_SKILLS = CHEAP_TIER_SKILLS + ORCHESTRATOR_SKILLS

# Required tokens inside §0' block (from test_quoin_pollution_preamble.py REQUIRED_TOKENS)
REQUIRED_TOKENS = [
    "[no-redispatch]",
    "[quoin-S-1: cannot extract per-skill dispatch contract; running in main]",
    "[quoin-S-1: pollution dispatch unavailable; proceeding in current session]",
    "pollution_guard.py",
    "OVER|",
    "POLLUTION_THRESHOLD",
    'model: "opus"',
]

# Required guard-detection strings (from test_pollution_score_extraction.py)
SCORE_EXTRACTION_STRINGS = [
    "pollution_guard.py",
    "OVER|",
    "OK|0|",
]

# Per-skill distinctive tokens (from test_quoin_pollution_preamble.py)
SKILL_DISTINCTIVE_TOKENS = {
    "architect": "repos-inventory.md",
    "plan": "architecture.md",
    "critic": "Target:",
    "revise": "critic-response",
    "review": "Branch:",
    "init_workflow": "project root",
    "discover": "project root",
    "specify": "spec.md",
    "security_review": "OWASP",
    "enrich": "enriched-prompt.md",
}

# MIN-A: literal default threshold value
THRESHOLD_TOKEN = "5000"


def _read_adapter(skill: str) -> str:
    return (ADAPTER_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _extract_pollution_block(text: str) -> str:
    """Return the §0' block content (heading through last line before next H2).

    Uses the SAME regex as test_quoin_pollution_preamble.py._extract_pollution_block
    for cross-guard validity.
    """
    match = re.search(
        r"^## §0' Pollution dispatch \(execute after §0 / §0c if present — before skill body\).+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(0)


# ─── 1. Freshness gate ────────────────────────────────────────────────────────

def test_check_exits_zero_on_fresh_tree():
    """--check must exit 0 after inject_pollution_dispatch.py has been run."""
    exit_code = generator.run_check()
    assert exit_code == 0, (
        "inject_pollution_dispatch --check returned non-zero on what should be a fresh tree. "
        "Run: python3 quoin/scripts/inject_pollution_dispatch.py to regenerate, then re-check."
    )


# ─── 2. Idempotence as byte-equality (MIN-3) ──────────────────────────────────

@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_generator_is_idempotent(skill):
    """Running the generator twice must produce byte-identical output."""
    skill_md = ADAPTER_DIR / skill / "SKILL.md"
    bytes_before = skill_md.read_bytes()

    # Run render+inject (the core logic, not the full I/O path)
    new_content = generator.inject_blocks_into_file(skill, skill_md)
    new_bytes = new_content.encode("utf-8")

    assert bytes_before == new_bytes, (
        f"{skill}: generator output is NOT byte-identical on second run. "
        "This means the inject logic is not idempotent — fix the replace/insert logic."
    )


def test_check_still_exits_zero_after_second_run():
    """--check must exit 0 even after running the generator a second time."""
    # Re-run generator (no-op content-wise)
    exit_code_inject = generator.run_inject()
    assert exit_code_inject == 0, f"run_inject() returned non-zero: {exit_code_inject}"
    exit_code_check = generator.run_check()
    assert exit_code_check == 0, "--check returned non-zero after a second generator run"


# ─── 3. Block-scoped token cross-guard (MAJ-2) ────────────────────────────────

@pytest.mark.parametrize("skill", TARGET_SKILLS)
@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_rendered_block_has_required_token(skill, token):
    """All 6 REQUIRED_TOKENS must appear inside the extracted §0' block."""
    text = _read_adapter(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}: §0' block is empty (heading present but block not extracted)"
    assert token in block, (
        f"{skill}: rendered §0' block missing REQUIRED_TOKEN {token!r}. "
        "This token is asserted by test_quoin_pollution_preamble.py — fix the generator template."
    )


@pytest.mark.parametrize("skill", TARGET_SKILLS)
@pytest.mark.parametrize("string", SCORE_EXTRACTION_STRINGS)
def test_rendered_block_has_score_extraction_string(skill, string):
    """The 3 guard-detection strings must appear inside the extracted §0' block."""
    text = _read_adapter(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}: §0' block is empty"
    assert string in block, (
        f"{skill}: rendered §0' block missing score-extraction string {string!r}. "
        "This string is asserted by test_pollution_score_extraction.py — fix the generator template."
    )


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_rendered_block_has_per_skill_token(skill):
    """Each skill's distinctive dispatch-contract token must appear inside the §0' block."""
    token = SKILL_DISTINCTIVE_TOKENS[skill]
    text = _read_adapter(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}: §0' block is empty"
    assert token in block, (
        f"{skill}: rendered §0' block missing per-skill distinctive token {token!r}. "
        "This token proves the dispatch contract is skill-specific."
    )


@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_rendered_block_has_threshold_value(skill):
    """MIN-A: literal threshold value '5000' must appear inside the §0' block."""
    text = _read_adapter(skill)
    block = _extract_pollution_block(text)
    assert block, f"{skill}: §0' block is empty"
    assert THRESHOLD_TOKEN in block, (
        f"{skill}: rendered §0' block missing literal threshold value {THRESHOLD_TOKEN!r}. "
        "The anti-drift cross-guard (MIN-A) requires this token inside the block."
    )


# ─── 5. Target adapters have exactly one §0' heading ────────────────────────

@pytest.mark.parametrize("skill", TARGET_SKILLS)
def test_each_target_adapter_has_exactly_one_pollution_heading(skill):
    """Each target adapter SKILL.md must contain the §0' heading exactly once."""
    text = _read_adapter(skill)
    count = text.count(POLLUTION_HEADING)
    assert count == 1, (
        f"{skill}/SKILL.md contains §0' heading {count} times (expected exactly 1). "
        "The generator's idempotent refresh logic has a bug if count > 1."
    )


# ─── 6. Exclusion adapters have zero §0' headings (d/e backstop) ────────────

@pytest.mark.parametrize("skill", EXCLUSION_SKILLS)
def test_exclusion_adapter_has_no_pollution_heading(skill):
    """Cheap-tier and orchestrator adapters must NOT carry §0'."""
    skill_md = ADAPTER_DIR / skill / "SKILL.md"
    if not skill_md.exists():
        pytest.skip(f"Adapter SKILL.md not found for {skill} (may not be migrated yet)")
    text = skill_md.read_text(encoding="utf-8")
    assert POLLUTION_HEADING not in text, (
        f"{skill} is a cheap-tier/orchestrator skill but its adapter SKILL.md contains §0'. "
        "The generator must only write to the 7 POLLUTION_TARGET_SKILLS."
    )


# ─── 8. Installer call-site ordering (MAJ-3) ─────────────────────────────────

def test_regenerate_pollution_dispatch_before_deploy_skills_in_cli():
    """T-06 MAJ-3: regenerate_pollution_dispatch must be called BEFORE deploy_skills in cli.py.

    The generator injects §0' into the SOURCE adapter files; deploy_skills then copies
    those files to the deploy root. If the order is reversed, the deployed SKILL.md
    would lack §0' until a second install run.
    """
    cli_path = _TESTS_DIR.parent.parent.parent / "src" / "quoin" / "cli.py"
    assert cli_path.exists(), f"cli.py not found at {cli_path}"
    text = cli_path.read_text(encoding="utf-8")

    # Both call-site patterns must be present (use "installer.X" to match the call, not the import)
    call_regen = "installer.regenerate_pollution_dispatch"
    call_deploy = "installer.deploy_skills"
    assert call_regen in text, (
        f"cli.py does not contain {call_regen!r}. "
        "The §0' generator must be wired into the installer (T-06)."
    )
    assert call_deploy in text, f"cli.py does not contain {call_deploy!r} (unexpected)"

    # regenerate_pollution_dispatch must appear BEFORE deploy_skills
    regen_idx = text.index(call_regen)
    deploy_idx = text.index(call_deploy)
    assert regen_idx < deploy_idx, (
        f"cli.py: {call_regen!r} (pos={regen_idx}) appears AFTER "
        f"{call_deploy!r} (pos={deploy_idx}). The generator must run BEFORE deploy_skills "
        "so the freshly-injected adapter SKILL.md is what gets deployed (T-06, R-11)."
    )


# ─── 9. Check mode fails on manual edit ─────────────────────────────────────

def test_check_fails_after_manual_edit(tmp_path, monkeypatch):
    """--check must return non-zero if a required token is removed from a target file.

    This test proves the anti-drift property: a future refactor that accidentally
    removes a token from the §0' block will be caught by --check.
    """
    import pathlib

    # Redirect adapter_dir to a temp dir with a manipulated file
    temp_skills = tmp_path / "adapters" / "claude" / "skills"
    for skill in TARGET_SKILLS:
        skill_dir = temp_skills / skill
        skill_dir.mkdir(parents=True)
        original = (ADAPTER_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        # Remove 'POLLUTION_THRESHOLD' from the content to simulate drift
        modified = original.replace("POLLUTION_THRESHOLD", "POLLUTION_THRESH_REMOVED")
        (skill_dir / "SKILL.md").write_text(modified, encoding="utf-8")

    # Monkeypatch _get_adapter_dir to return the temp dir
    monkeypatch.setattr(generator, "_get_adapter_dir", lambda: temp_skills)

    exit_code = generator.run_check()
    assert exit_code != 0, (
        "run_check() returned 0 even though POLLUTION_THRESHOLD was removed from §0' blocks. "
        "The anti-drift guard is not working."
    )


# ─── 10. Generator-side DISPATCH_CONTRACTS roster parity (round-4 CRIT-1 guard) ──

def test_dispatch_contracts_matches_target_skills():
    """DISPATCH_CONTRACTS must have an entry for every POLLUTION_TARGET_SKILLS member.

    render_pollution_block() does a bare DISPATCH_CONTRACTS[skill] subscript with no
    .get() fallback, so a missing entry KeyErrors the whole generator run before it
    injects a single block. This closes the enumeration/coverage-miss defect class
    permanently at the generator-side dispatch-contract map (found round-4, after the
    round-3 census's test-side sweep missed it — see IVG-128 plan Notes row 19).
    """
    assert set(generator.DISPATCH_CONTRACTS) == set(generator.POLLUTION_TARGET_SKILLS), (
        f"DISPATCH_CONTRACTS keys {set(generator.DISPATCH_CONTRACTS)} != "
        f"POLLUTION_TARGET_SKILLS {set(generator.POLLUTION_TARGET_SKILLS)}"
    )
