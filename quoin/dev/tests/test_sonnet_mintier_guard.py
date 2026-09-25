"""
Drift-detection tests for the §0‴ Minimum-tier guard (Sonnet tier) block (IVG-117).

The 13 Sonnet-declared cheap-tier skills (checkpoint, cleanup, continue_work,
end_of_day, end_of_task, expand, gate, implement, pr, revise-fast, rollback,
sleep, workspace) carry a `## §0‴ Minimum-tier guard ...` block. These tests
verify structural correctness of that block, mirroring test_mintier_guard.py's
structure for the Opus §0″ block.

§0‴ anchors on SECTION0_HEADING (the hand-authored §0 block, always present in
these 13 files) rather than on §0'/§0″ (which only exist on the disjoint Opus-10
target set). Zero edits to the existing Opus template/constants (D-07) — the 10
deployed Opus files and test_mintier_guard.py remain byte-frozen.

Template-parity guard (this file): _MINTIER_SONNET_BLOCK_BODY is a tier-swapped
derivative of _MINTIER_BLOCK_BODY. The ONE net-new element is the MIN-2
recursion-contract line (keyed on the literal substring "[no-redispatch:N]"),
which documents that §0 (earlier in the file) already handles the recursion
abort case. normalize() strips that line, then applies the tier-word
substitution map, before asserting semantic equality between the two bodies.
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
from pathlib import Path

import pytest

# ─── Path resolution ──────────────────────────────────────────────────────────

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
SCRIPTS_DIR = PKG_DIR / "scripts"
REPO_ROOT = PKG_DIR.parent  # quoin/ (git repo root)

# ─── Import generator constants (single-source discipline, byte-identity) ─────

_ipd_spec = importlib.util.spec_from_file_location(
    "inject_pollution_dispatch",
    SCRIPTS_DIR / "inject_pollution_dispatch.py",
)
assert _ipd_spec is not None
_ipd = importlib.util.module_from_spec(_ipd_spec)
assert _ipd_spec.loader is not None
_ipd_spec.loader.exec_module(_ipd)

SECTION0_HEADING: str = _ipd.SECTION0_HEADING
MINTIER_SONNET_HEADING: str = _ipd.MINTIER_SONNET_HEADING
MINTIER_HEADING: str = _ipd.MINTIER_HEADING
_MINTIER_SONNET_BLOCK_BODY: str = _ipd._MINTIER_SONNET_BLOCK_BODY
_MINTIER_BLOCK_BODY: str = _ipd._MINTIER_BLOCK_BODY

# ─── Constants ────────────────────────────────────────────────────────────────

SONNET_MINTIER_SKILLS = [
    "checkpoint",
    "cleanup",
    "continue_work",
    "end_of_day",
    "end_of_task",
    "expand",
    "gate",
    "implement",
    "pr",
    "revise-fast",
    "rollback",
    "sleep",
    "workspace",
]

# Tier-swapped mirror of MINTIER_REQUIRED_TOKENS PLUS "[no-redispatch:N]" (MIN-2).
MINTIER_SONNET_REQUIRED_TOKENS = [
    "[no-redispatch]",
    'model: "sonnet"',
    "current_tier < declared_tier",
    "spawn an Agent subagent",
    "Wait for the subagent. Return its output as your final response. STOP.",
    "Usage credits required for 1M context",
    "Abort — run from a Sonnet session",
    "Proceed at current tier (under-powered)",
    "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
    "[autonomous]",
    "[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]",
    "[no-redispatch:N]",
]

MINTIER_SONNET_REQUIRED_MARKERS = [
    "<!-- §0tripleprime-begin -->",
    "<!-- §0tripleprime-end -->",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_skill(skill: str) -> str:
    return (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _extract_sonnet_mintier_block(text: str) -> str:
    """Return the §0‴ block content (heading through last line before next H2)."""
    heading_escaped = re.escape(MINTIER_SONNET_HEADING)
    match = re.search(
        heading_escaped + r".+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return ""
    return match.group(0)


def _normalize(body: str, keep_recursion_line: bool = False) -> str:
    """Normalize a §0″/§0‴ block body to a canonical tier-agnostic form.

    (1) Removes the single recursion-contract line (keyed on the literal
        substring "[no-redispatch:N]") unless keep_recursion_line is True.
    (2) Applies the tier-word substitution map (both directions) so that
        the Opus body and the Sonnet body converge to the same text.
    """
    lines = body.splitlines(keepends=True)
    if not keep_recursion_line:
        lines = [ln for ln in lines if "[no-redispatch:N]" not in ln]
    text = "".join(lines)

    # Canonicalize tier words both directions -> use a neutral placeholder
    # sequence so "an Opus" / "a Sonnet" and "Opus" / "Sonnet" and
    # "opus" / "sonnet" and "§0″" / "§0‴" and "doubleprime" / "tripleprime"
    # all collapse to the same canonical string.
    text = text.replace("an Opus", "<TIER-ARTICLE> <TIER>")
    text = text.replace("a Sonnet", "<TIER-ARTICLE> <TIER>")
    text = text.replace("Opus", "<TIER>")
    text = text.replace("Sonnet", "<TIER>")
    text = text.replace("opus", "<tier>")
    text = text.replace("sonnet", "<tier>")
    text = text.replace("§0″", "<SIGIL>")
    text = text.replace("§0‴", "<SIGIL>")
    text = text.replace("doubleprime", "<primeword>")
    text = text.replace("tripleprime", "<primeword>")
    return text


# ─── (a) MINTIER_SONNET_HEADING present exactly once; markers present exactly once each ─

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_heading_present_exactly_once(skill):
    text = _read_skill(skill)
    count = text.count(MINTIER_SONNET_HEADING)
    assert count == 1, (
        f"{skill}/SKILL.md contains the §0‴ heading {count} times (expected exactly 1). "
        f"Heading literal: {MINTIER_SONNET_HEADING!r}"
    )


@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
@pytest.mark.parametrize("marker", MINTIER_SONNET_REQUIRED_MARKERS)
def test_sonnet_mintier_markers_present_exactly_once(skill, marker):
    text = _read_skill(skill)
    count = text.count(marker)
    assert count == 1, (
        f"{skill}/SKILL.md: marker {marker!r} appears {count} times (expected exactly 1)."
    )


# ─── (b) All required tokens present inside the extracted §0‴ block ───────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
@pytest.mark.parametrize("token", MINTIER_SONNET_REQUIRED_TOKENS)
def test_sonnet_mintier_required_token_in_block(skill, token):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"
    assert token in block, (
        f"{skill}/SKILL.md §0‴ block missing required token: {token!r}"
    )


# ─── (c) Ordering: §0‴ index > §0 index; §0‴ is the FIRST H2 after the §0 block ─

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_ordering(skill):
    text = _read_skill(skill)

    assert SECTION0_HEADING in text, f"{skill}/SKILL.md missing §0 heading"
    assert MINTIER_SONNET_HEADING in text, f"{skill}/SKILL.md missing §0‴ heading"

    s_idx = text.index(SECTION0_HEADING)
    t_idx = text.index(MINTIER_SONNET_HEADING)
    assert t_idx > s_idx, (
        f"{skill}/SKILL.md: §0‴ (pos={t_idx}) appears BEFORE §0 (pos={s_idx})."
    )

    # §0‴ must be the FIRST H2 heading strictly after the §0 heading line.
    next_h2 = re.search(r"^## ", text[s_idx + len(SECTION0_HEADING):], re.MULTILINE)
    assert next_h2, f"{skill}/SKILL.md: no H2 heading found after §0"
    first_h2_after_s0_idx = s_idx + len(SECTION0_HEADING) + next_h2.start()
    assert first_h2_after_s0_idx == t_idx, (
        f"{skill}/SKILL.md: §0‴ is not the first H2 after §0 "
        f"(first H2 at {first_h2_after_s0_idx}, §0‴ at {t_idx})."
    )


# ─── (d) Generator idempotence ────────────────────────────────────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_injection_idempotent(skill):
    skill_md = ADAPTER_SKILLS_DIR / skill / "SKILL.md"
    original_text = skill_md.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skill = Path(tmpdir) / "SKILL.md"
        tmp_skill.write_text(original_text, encoding="utf-8")

        _ipd.inject_mintier_sonnet_block_into_file(skill, tmp_skill)
        after_first = tmp_skill.read_text(encoding="utf-8")

        _ipd.inject_mintier_sonnet_block_into_file(skill, tmp_skill)
        after_second = tmp_skill.read_text(encoding="utf-8")

    assert after_first == after_second, (
        f"{skill}: inject_mintier_sonnet_block_into_file is NOT idempotent."
    )


# ─── (e) run_check() returns 0 on the committed tree ─────────────────────────

def test_run_check_passes_on_committed_tree():
    result = _ipd.run_check()
    assert result == 0, (
        "inject_pollution_dispatch run_check() returned non-zero on the committed tree. "
        "Run `python3 quoin/scripts/inject_pollution_dispatch.py` to regenerate."
    )


# ─── (g) Happy-path Agent up-dispatch structural co-presence ─────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_happy_path_agent_dispatch_present(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    dispatch_literals = [
        "spawn an Agent subagent",
        "Wait for the subagent. Return its output as your final response. STOP.",
        'model: "sonnet"',
    ]
    for lit in dispatch_literals:
        assert lit in block, (
            f"{skill}/SKILL.md §0‴ block missing Option-A dispatch literal: {lit!r}."
        )


# ─── (h) Fallback AskUserQuestion labels still present ───────────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_fallback_askuser_labels_present(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    fallback_labels = [
        "Abort — run from a Sonnet session",
        "Proceed at current tier (under-powered)",
        "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
    ]
    for label in fallback_labels:
        assert label in block, (
            f"{skill}/SKILL.md §0‴ block missing fallback label: {label!r}"
        )


# ─── (i) 1M-context credit-class branch present ──────────────────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_1m_credit_branch_present(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    assert "Usage credits required for 1M context" in block, (
        f"{skill}/SKILL.md §0‴ block missing 1M-credit-class branch."
    )
    assert "Abort — I'll switch with /model first" in block, (
        f"{skill}/SKILL.md §0‴ block missing 1M AskUserQuestion Option 1 label."
    )
    assert "Proceed in-session at parent tier" in block, (
        f"{skill}/SKILL.md §0‴ block missing 1M AskUserQuestion Option 2 label."
    )


# ─── (j) [no-redispatch] appears as child dispatch prefix (not only sentinel) ─

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_no_redispatch_as_child_prompt_prefix(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    count = block.count("[no-redispatch]")
    assert count >= 2, (
        f"{skill}/SKILL.md §0‴ block: '[no-redispatch]' appears {count} time(s); expected ≥2."
    )


# ─── (k) Disable-switch line still present ───────────────────────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_disable_switch_present(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    assert "QUOIN_DISABLE_MINTIER_GUARD" in block, (
        f"{skill}/SKILL.md §0‴ block missing QUOIN_DISABLE_MINTIER_GUARD disable-switch line."
    )


# ─── (l) MIN-2: recursion token present ──────────────────────────────────────

@pytest.mark.parametrize("skill", SONNET_MINTIER_SKILLS)
def test_sonnet_mintier_recursion_token_present(skill):
    text = _read_skill(skill)
    block = _extract_sonnet_mintier_block(text)
    assert block, f"{skill}/SKILL.md §0‴ block could not be extracted"

    assert "[no-redispatch:N]" in block, (
        f"{skill}/SKILL.md §0‴ block missing MIN-2 recursion-contract token "
        "'[no-redispatch:N]'."
    )


# ─── Set-equality guards (lesson 2026-07-20 — structural invariant) ──────────

def _load_skills_json() -> dict:
    skills_json_path = REPO_ROOT / "quoin" / "core" / "workflow" / "skills.json"
    if not skills_json_path.exists():
        pytest.skip(f"skills.json not found at {skills_json_path}")
    return json.loads(skills_json_path.read_text(encoding="utf-8"))


def test_sonnet_mintier_target_skills_matches_skills_json_roster():
    """(a) set(MINTIER_SONNET_TARGET_SKILLS) == skills.json sonnet && section_0 roster."""
    data = _load_skills_json()
    entries = data.get("skills", data) if isinstance(data, dict) else data
    if isinstance(entries, dict):
        entries = list(entries.values())

    derived = {
        e.get("name") or e.get("id")
        for e in entries
        if e.get("claude_model") == "sonnet" and e.get("section_0") is True
    }
    derived.discard(None)

    assert set(_ipd.MINTIER_SONNET_TARGET_SKILLS) == derived, (
        f"MINTIER_SONNET_TARGET_SKILLS diverges from skills.json roster.\n"
        f"generator={sorted(_ipd.MINTIER_SONNET_TARGET_SKILLS)}\n"
        f"skills.json={sorted(derived)}"
    )


def test_sonnet_mintier_target_skills_disjoint_from_opus_and_haiku_rosters():
    """(b) MINTIER_SONNET_TARGET_SKILLS is disjoint from the Opus-10 and Haiku roster."""
    sonnet_set = set(_ipd.MINTIER_SONNET_TARGET_SKILLS)
    opus_set = set(_ipd.MINTIER_TARGET_SKILLS)
    assert sonnet_set.isdisjoint(opus_set), (
        f"MINTIER_SONNET_TARGET_SKILLS overlaps MINTIER_TARGET_SKILLS (Opus): "
        f"{sonnet_set & opus_set}"
    )

    data = _load_skills_json()
    entries = data.get("skills", data) if isinstance(data, dict) else data
    if isinstance(entries, dict):
        entries = list(entries.values())
    haiku_section0 = {
        e.get("name") or e.get("id")
        for e in entries
        if e.get("claude_model") == "haiku" and e.get("section_0") is True
    }
    haiku_section0.discard(None)
    assert sonnet_set.isdisjoint(haiku_section0), (
        f"MINTIER_SONNET_TARGET_SKILLS overlaps the Haiku section_0 roster: "
        f"{sonnet_set & haiku_section0}"
    )


# ─── Template-parity guard ────────────────────────────────────────────────────

def test_sonnet_and_opus_mintier_templates_are_semantically_equivalent():
    """Assert normalize(_MINTIER_SONNET_BLOCK_BODY) == normalize(_MINTIER_BLOCK_BODY).

    Keeps the two templates from drifting semantically while permitting the one
    documented recursion-contract-line delta (MIN-2).
    """
    sonnet_norm = _normalize(_MINTIER_SONNET_BLOCK_BODY)
    opus_norm = _normalize(_MINTIER_BLOCK_BODY)
    assert sonnet_norm == opus_norm, (
        "Sonnet §0‴ template has drifted semantically from the Opus §0″ template "
        "beyond the documented tier-word substitutions and the MIN-2 recursion line."
    )


def test_sonnet_mintier_template_has_exactly_one_recursion_line():
    """The recursion-contract line is the ONE net-new line vs the Opus body."""
    recursion_lines = [
        ln for ln in _MINTIER_SONNET_BLOCK_BODY.splitlines() if "[no-redispatch:N]" in ln
    ]
    assert len(recursion_lines) == 1, (
        f"Expected exactly 1 recursion-contract line containing '[no-redispatch:N]' "
        f"in _MINTIER_SONNET_BLOCK_BODY, found {len(recursion_lines)}."
    )
    assert "[no-redispatch:N]" not in _MINTIER_BLOCK_BODY, (
        "_MINTIER_BLOCK_BODY (Opus) unexpectedly contains '[no-redispatch:N]' — "
        "this token must be Sonnet-only (MIN-2 delta)."
    )
