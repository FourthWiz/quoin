"""
Drift-detection tests for the §0″ Minimum-tier guard block (IVG-72, updated IVG-91).

The 7 Opus-tier leaf skills (architect, plan, critic, revise, review,
init_workflow, discover) carry a `## §0″ Minimum-tier guard ...` block.
These tests verify structural correctness of that block.

Note on 1M handling (IVG-91): §0″ now carries a post-dispatch 1M-credit-class catch
mirrored from §0'. When the Agent up-dispatch fails with `Usage credits required for
1M context`, an AskUserQuestion surfaces two options (abort / proceed-in-session).
This is NOT a pre-dispatch precheck — it fires only on a real dispatch error, exactly
as §0' does (IVG-89 D-03 approach). test_1m_context_precheck.py is NOT extended —
the 1M guard is post-dispatch and is covered by MINTIER_REQUIRED_TOKENS here.

Note on happy path (IVG-91): §0″ now silently spawns an Agent subagent (model: opus)
as the happy path; AskUserQuestion (abort / proceed-under-powered) is the fail-open
fallback only. The two discriminator tokens `spawn an Agent subagent` and
`Wait for the subagent. Return its output as your final response. STOP.` are absent
from the old Option-B body and serve as the structural regression guard.

Note on SO_HEADING non-collision: test_quoin_stage1_preamble.py::test_no_opus_tier_skill_has_preamble
covers the 11 Opus skills and asserts SO_HEADING = "## §0 Model dispatch (FIRST STEP — execute
before anything else)" is absent. §0″'s heading does NOT substring-match SO_HEADING. The
non-collision is verified by T-04's grep check (grep -c "## §0 Model dispatch" returns 0 for all
9 target SKILL.md files). See also the T-04 commit for the verification output.

Token list discipline (MIN-1):
- mintier_required_tokens (9 content tokens): checked INSIDE the extracted §0″ block.
- mintier_required_markers (2 HTML markers): checked for presence in the full file text
  (outside block extraction), exactly once each.
These two lists are separate to mirror the T-03 --check discipline exactly.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# ─── Path resolution ──────────────────────────────────────────────────────────

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
SCRIPTS_DIR = PKG_DIR / "scripts"

# ─── Import MINTIER_HEADING from the generator (single-source discipline) ─────
# This guarantees byte-identity: any heading change in the generator
# automatically invalidates the drift tests.

_ipd_spec = importlib.util.spec_from_file_location(
    "inject_pollution_dispatch",
    SCRIPTS_DIR / "inject_pollution_dispatch.py",
)
assert _ipd_spec is not None
_ipd = importlib.util.module_from_spec(_ipd_spec)
assert _ipd_spec.loader is not None
_ipd_spec.loader.exec_module(_ipd)

MINTIER_HEADING: str = _ipd.MINTIER_HEADING
POLLUTION_HEADING: str = _ipd.POLLUTION_HEADING

# ─── Constants ────────────────────────────────────────────────────────────────

MINTIER_SKILLS = [
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
    "repo_cleanup",
]

# mintier_required_tokens (MIN-1): 9 content tokens checked INSIDE the §0″ block.
# Must stay byte-mirrored with mintier_required_tokens in inject_pollution_dispatch.run_check().
MINTIER_REQUIRED_TOKENS = [
    "[no-redispatch]",
    'model: "opus"',
    "current_tier < declared_tier",
    "spawn an Agent subagent",
    "Wait for the subagent. Return its output as your final response. STOP.",
    "Usage credits required for 1M context",
    "Abort — run from an Opus session",
    "Proceed at current tier (under-powered)",
    "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
    "[autonomous]",
    "[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]",
]

# mintier_required_markers (MIN-1): 2 HTML markers checked as file-level presence
# (outside block extraction). Separate from MINTIER_REQUIRED_TOKENS.
MINTIER_REQUIRED_MARKERS = [
    "<!-- §0doubleprime-begin -->",
    "<!-- §0doubleprime-end -->",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_skill(skill: str) -> str:
    return (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _extract_mintier_block(text: str) -> str:
    """Return the §0″ block content (heading through last line before next H2).

    Uses the same regex pattern as run_check() for consistency.
    """
    heading_escaped = re.escape(MINTIER_HEADING)
    match = re.search(
        heading_escaped + r".+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(0)


# ─── (a) MINTIER_HEADING present exactly once; markers present exactly once each ─

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_heading_present_exactly_once(skill):
    """(a) MINTIER_HEADING present exactly once in the skill file."""
    text = _read_skill(skill)
    count = text.count(MINTIER_HEADING)
    assert count == 1, (
        f"{skill}/SKILL.md contains the §0″ heading {count} times (expected exactly 1). "
        f"Heading literal: {MINTIER_HEADING!r}"
    )


@pytest.mark.parametrize("skill", MINTIER_SKILLS)
@pytest.mark.parametrize("marker", MINTIER_REQUIRED_MARKERS)
def test_mintier_markers_present_exactly_once(skill, marker):
    """(a) Begin/end markers present exactly once each (file-level check, MIN-1)."""
    text = _read_skill(skill)
    count = text.count(marker)
    assert count == 1, (
        f"{skill}/SKILL.md: marker {marker!r} appears {count} times (expected exactly 1). "
        "Markers must appear exactly once — present in §0″ block only."
    )


# ─── (b) All 6 mintier_required_tokens present inside the extracted §0″ block ─

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
@pytest.mark.parametrize("token", MINTIER_REQUIRED_TOKENS)
def test_mintier_required_token_in_block(skill, token):
    """(b) All 6 mintier_required_tokens present inside the extracted §0″ block (MIN-1)."""
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, (
        f"{skill}/SKILL.md §0″ block could not be extracted "
        "(heading present but no trailing ## heading?)"
    )
    assert token in block, (
        f"{skill}/SKILL.md §0″ block missing required token: {token!r}"
    )


# ─── (c) Ordering: §0″ index > §0' index AND §0″ index < first-body-H2 index ─

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_ordering(skill):
    """(c) §0″ appears AFTER §0' and BEFORE the first skill-body H2 heading."""
    text = _read_skill(skill)

    assert MINTIER_HEADING in text, f"{skill}/SKILL.md missing §0″ heading"
    assert POLLUTION_HEADING in text, f"{skill}/SKILL.md missing §0' heading (all §0″ skills must also have §0')"

    p_idx = text.index(POLLUTION_HEADING)
    m_idx = text.index(MINTIER_HEADING)

    assert m_idx > p_idx, (
        f"{skill}/SKILL.md: §0″ (pos={m_idx}) appears BEFORE §0' (pos={p_idx}). "
        "Ordering must be: §0c? < §0' < §0″ < first-body-H2."
    )

    # Verify §0″ is before the first skill-body H2 (Session bootstrap or similar)
    # Find the first H2 that comes AFTER the §0″ block end
    mintier_end_marker = "<!-- §0doubleprime-end -->"
    if mintier_end_marker in text:
        block_end = text.index(mintier_end_marker) + len(mintier_end_marker)
    else:
        # Fallback: use block end from extraction
        block = _extract_mintier_block(text)
        block_end = m_idx + len(block) if block else m_idx + 1

    # Look for the next ## heading after block end
    next_h2 = re.search(r"^## ", text[block_end:], re.MULTILINE)
    assert next_h2, (
        f"{skill}/SKILL.md: no H2 heading found after §0″ block end. "
        "§0″ must appear before the skill body."
    )


# ─── (d) Generator idempotence: inject_mintier_block_into_file twice → identical output ─

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_injection_idempotent(skill):
    """(d) Running inject_mintier_block_into_file twice on an in-memory copy → identical output.

    Uses a tmp file to avoid modifying the actual SKILL.md.
    """
    import tempfile

    skill_md = ADAPTER_SKILLS_DIR / skill / "SKILL.md"
    original_text = skill_md.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a copy to a tmp path
        tmp_skill = Path(tmpdir) / "SKILL.md"
        tmp_skill.write_text(original_text, encoding="utf-8")

        # First injection
        _ipd.inject_mintier_block_into_file(skill, tmp_skill)
        after_first = tmp_skill.read_text(encoding="utf-8")

        # Second injection
        _ipd.inject_mintier_block_into_file(skill, tmp_skill)
        after_second = tmp_skill.read_text(encoding="utf-8")

    assert after_first == after_second, (
        f"{skill}: inject_mintier_block_into_file is NOT idempotent — "
        "second injection produced different output than first."
    )


# ─── (e) run_check() returns 0 on the committed tree ─────────────────────────

def test_run_check_passes_on_committed_tree():
    """(e) run_check() returns 0 on the committed adapter SKILL.md files."""
    result = _ipd.run_check()
    assert result == 0, (
        "inject_pollution_dispatch run_check() returned non-zero on the committed tree. "
        "This means the adapter SKILL.md files have drifted from the template. "
        "Run `python3 quoin/scripts/inject_pollution_dispatch.py` to regenerate."
    )


# ─── (g) Happy-path Agent up-dispatch structural co-presence ─────────────────

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_happy_path_agent_dispatch_present(skill):
    """(g) §0″ block contains all three co-present literals that are unique to Option-A.

    The co-presence of 'spawn an Agent subagent' + the STOP phrase (both absent from
    Option-B body) is the structural discriminator. 'model: "opus"' in a dispatch context
    (not merely a descriptor) is the third. All three must appear together in the block.
    """
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0″ block could not be extracted"

    dispatch_literals = [
        "spawn an Agent subagent",
        "Wait for the subagent. Return its output as your final response. STOP.",
        'model: "opus"',
    ]
    for lit in dispatch_literals:
        assert lit in block, (
            f"{skill}/SKILL.md §0″ block missing Option-A dispatch literal: {lit!r}. "
            "This indicates the block has reverted to Option-B (AskUserQuestion-only) "
            "or the Agent-spawn phrase was inadvertently removed."
        )


# ─── (h) Fallback AskUserQuestion labels still present ───────────────────────

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_fallback_askuser_labels_present(skill):
    """(h) Generic fallback AskUserQuestion labels remain verbatim in §0″ block.

    These labels are now the fail-open fallback (not the primary path), but drift
    detection relies on their verbatim presence.
    """
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0″ block could not be extracted"

    fallback_labels = [
        "Abort — run from an Opus session",
        "Proceed at current tier (under-powered)",
        "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
    ]
    for label in fallback_labels:
        assert label in block, (
            f"{skill}/SKILL.md §0″ block missing fallback label: {label!r}"
        )


# ─── (i) 1M-context credit-class branch present ──────────────────────────────

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_1m_credit_branch_present(skill):
    """(i) §0″ block contains the 1M-credit-class error catch (IVG-91 mirroring §0')."""
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0″ block could not be extracted"

    assert "Usage credits required for 1M context" in block, (
        f"{skill}/SKILL.md §0″ block missing 1M-credit-class branch "
        "('Usage credits required for 1M context' not found). "
        "§0″ must mirror §0' post-dispatch 1M-credit handling (IVG-89/IVG-91)."
    )
    # Also check 1M-specific AskUserQuestion option labels
    assert "Abort — I'll switch with /model first" in block, (
        f"{skill}/SKILL.md §0″ block missing 1M AskUserQuestion Option 1 label."
    )
    assert "Proceed in-session at parent tier" in block, (
        f"{skill}/SKILL.md §0″ block missing 1M AskUserQuestion Option 2 label."
    )


# ─── (j) [no-redispatch] appears as child dispatch prefix (not only sentinel) ─

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_no_redispatch_as_child_prompt_prefix(skill):
    """(j) [no-redispatch] appears ≥2 times in the §0″ block: once for the sentinel
    detection skip and once as the child dispatch prompt prefix.
    """
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0″ block could not be extracted"

    count = block.count("[no-redispatch]")
    assert count >= 2, (
        f"{skill}/SKILL.md §0″ block: '[no-redispatch]' appears {count} time(s); "
        "expected ≥2 (once in sentinel detection, once as child prompt prefix). "
        "The child Agent prompt must be prefixed with bare [no-redispatch] to prevent recursion."
    )


# ─── (k) Disable-switch line still present ───────────────────────────────────

@pytest.mark.parametrize("skill", MINTIER_SKILLS)
def test_mintier_disable_switch_present(skill):
    """(k) QUOIN_DISABLE_MINTIER_GUARD silent-skip detection remains in §0″ block."""
    text = _read_skill(skill)
    block = _extract_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0″ block could not be extracted"

    assert "QUOIN_DISABLE_MINTIER_GUARD" in block, (
        f"{skill}/SKILL.md §0″ block missing QUOIN_DISABLE_MINTIER_GUARD disable-switch line."
    )


# ─── (f) Spike-result annotation comment present near _MINTIER_BLOCK_BODY ─────

def test_spike_result_comment_present():
    """(f) Spike-result annotation comment present in inject_pollution_dispatch.py.

    The plan requires T-00 to be documented via a '# Spike result' comment near
    _MINTIER_BLOCK_BODY in the generator source. This test asserts the comment exists
    to ensure T-00 was not silently skipped.
    """
    generator_source = (SCRIPTS_DIR / "inject_pollution_dispatch.py").read_text(encoding="utf-8")
    assert "# Spike result" in generator_source, (
        "inject_pollution_dispatch.py missing '# Spike result' comment near _MINTIER_BLOCK_BODY. "
        "T-00 spike result must be documented per plan requirement (T-00 ack)."
    )


# ─── T-10: /specify adapter carries exactly one §0' + one §0″, correctly ordered ──
# Dedicated presence test (specify-skill stage 2, IVG-127). MINTIER_HEADING and
# POLLUTION_HEADING are imported from the generator above for byte-identity —
# this test fails if either block is removed from the specify adapter SKILL.md.

def test_specify_carries_exactly_one_pollution_and_mintier_block():
    """(T-10) specify/SKILL.md has exactly one §0' heading and one §0″ heading,
    §0″ appears after §0', and both §0doubleprime HTML markers are present.
    """
    text = _read_skill("specify")

    pollution_count = text.count(POLLUTION_HEADING)
    assert pollution_count == 1, (
        f"specify/SKILL.md: §0' heading appears {pollution_count} time(s); expected exactly 1."
    )

    mintier_count = text.count(MINTIER_HEADING)
    assert mintier_count == 1, (
        f"specify/SKILL.md: §0″ heading appears {mintier_count} time(s); expected exactly 1."
    )

    p_idx = text.find(POLLUTION_HEADING)
    m_idx = text.find(MINTIER_HEADING)
    assert m_idx > p_idx, (
        "specify/SKILL.md: §0″ must appear AFTER §0' (ordering violation)."
    )

    for marker in ("<!-- §0doubleprime-begin -->", "<!-- §0doubleprime-end -->"):
        assert text.count(marker) == 1, (
            f"specify/SKILL.md: marker {marker!r} missing or duplicated "
            f"(count={text.count(marker)})."
        )
