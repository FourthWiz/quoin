"""
IVG-165 T-05 (Commit A) — §0 Model dispatch generator unit tests.

Covers the 4 acceptance-criteria test classes for the generator's new §0
ownership (inject_pollution_dispatch.py):
  (i)   idempotency — double-run output == single-run output byte-for-byte
  (ii)  count==1 FAIL LOUD — SECTION0_HEADING and SECTION0_END_MARKER each
        exactly once per file, else raise
  (iii) roster (HARD completeness gate, NOT covered by the empty-diff check
        alone — critic MIN-1): SECTION0_TARGET_SKILLS must equal the OTHER
        live 19-skill rosters (test_1m_context_precheck.py's per-skill
        (skill, tier, proceed_ref) table; CLAUDE.md's "§0 Model dispatch
        preamble" prose list), so a single-anchor drift or roster OMISSION
        can't slip through silently (a skill accidentally missing from
        SECTION0_TARGET_SKILLS is never regenerated, so its diff would be
        trivially empty and the empty-diff gate alone would miss it).
  (iv)  marker placement — regenerated marker offset matches proc:marker-place
        (mirrors test_section0_marker.py's assertions, applied to freshly
        rendered text rather than the live file).

Deterministic pathlib/string tests only, no LLM calls (lesson 2026-04-23).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
SCRIPTS_DIR = TESTS_DIR.parent.parent / "scripts"
ADAPTER_SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"
CLAUDE_MD = TESTS_DIR.parent.parent / "CLAUDE.md"


def _load_generator():
    """Import inject_pollution_dispatch.py as a standalone module (mirrors
    the dynamic-import pattern used by test_propagate_1m_s0_edit.py — the
    generator is deliberately standalone, D-06, not a package member)."""
    path = SCRIPTS_DIR / "inject_pollution_dispatch.py"
    spec = importlib.util.spec_from_file_location("inject_pollution_dispatch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


@pytest.fixture(scope="module")
def m1_ctx():
    """The test_1m_context_precheck.py module, for cross-roster assertion."""
    path = TESTS_DIR / "test_1m_context_precheck.py"
    spec = importlib.util.spec_from_file_location("test_1m_context_precheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── (ii) count==1 FAIL LOUD ────────────────────────────────────────────────

def test_section0_end_marker_constant_value(gen):
    assert gen.SECTION0_END_MARKER == "<!-- §0-end -->"


def test_inject_raises_on_missing_heading(gen, tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# Foo\n\nno §0 heading here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SECTION0_HEADING appears 0 times"):
        gen.inject_section0_block_into_file("gate", skill_md)


def test_inject_raises_on_duplicate_heading(gen, tmp_path):
    skill_md = tmp_path / "SKILL.md"
    text = (
        f"# Foo\n\n{gen.SECTION0_HEADING}\nbody\n{gen.SECTION0_END_MARKER}\n\n"
        f"## Other\n{gen.SECTION0_HEADING}\nbody2\n"
    )
    skill_md.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="SECTION0_HEADING appears 2 times"):
        gen.inject_section0_block_into_file("gate", skill_md)


def test_inject_raises_on_missing_marker(gen, tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(f"# Foo\n\n{gen.SECTION0_HEADING}\nbody, no marker\n\n## Next\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SECTION0_END_MARKER appears 0 times"):
        gen.inject_section0_block_into_file("gate", skill_md)


def test_inject_raises_on_duplicate_marker(gen, tmp_path):
    skill_md = tmp_path / "SKILL.md"
    text = (
        f"# Foo\n\n{gen.SECTION0_HEADING}\nbody\n{gen.SECTION0_END_MARKER}\n\n"
        f"stray extra marker below\n{gen.SECTION0_END_MARKER}\n"
    )
    skill_md.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="SECTION0_END_MARKER appears 2 times"):
        gen.inject_section0_block_into_file("gate", skill_md)


# ── real per-skill parametrization (module-level, roster from the generator) ──

def _skill_list():
    gen = _load_generator()
    return sorted(gen.SECTION0_TARGET_SKILLS)


SECTION0_SKILLS_FOR_PARAM = _skill_list()


@pytest.mark.parametrize("skill", SECTION0_SKILLS_FOR_PARAM)
def test_section0_heading_and_marker_count_one_in_live_corpus(gen, skill):
    skill_md = ADAPTER_SKILLS_DIR / skill / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert text.count(gen.SECTION0_HEADING) == 1, f"{skill}: heading count != 1"
    assert text.count(gen.SECTION0_END_MARKER) == 1, f"{skill}: marker count != 1"


# ── (i) idempotency ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("skill", SECTION0_SKILLS_FOR_PARAM)
def test_render_section0_block_idempotent(gen, skill):
    once = gen.render_section0_block(skill)
    twice = gen.render_section0_block(skill)
    assert once == twice, f"{skill}: render_section0_block is non-deterministic"


@pytest.mark.parametrize("skill", SECTION0_SKILLS_FOR_PARAM)
def test_inject_section0_double_run_is_noop(gen, skill, tmp_path):
    """Copy a live SKILL.md, inject twice, assert the second run is a byte-for-byte no-op."""
    src = (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(src, encoding="utf-8")

    gen.inject_section0_block_into_file(skill, skill_md)
    after_first = skill_md.read_text(encoding="utf-8")

    gen.inject_section0_block_into_file(skill, skill_md)
    after_second = skill_md.read_text(encoding="utf-8")

    assert after_first == after_second, f"{skill}: second inject run is not a no-op"


# ── (iv) marker placement (mirrors test_section0_marker.py, on rendered text) ──

@pytest.mark.parametrize("skill", SECTION0_SKILLS_FOR_PARAM)
def test_rendered_block_ends_with_marker_on_own_line(gen, skill):
    block = gen.render_section0_block(skill)
    assert block.count(gen.SECTION0_END_MARKER) == 1
    idx = block.index(gen.SECTION0_END_MARKER)
    assert block[idx - 1] == "\n", f"{skill}: marker not on its own line"
    after = block[idx + len(gen.SECTION0_END_MARKER):]
    assert after == "\n", f"{skill}: unexpected trailing content after marker: {after!r}"


# ── (iii) roster — HARD completeness gate (critic MIN-1) ───────────────────

def test_section0_target_skills_roster_is_19(gen):
    assert len(gen.SECTION0_TARGET_SKILLS) == 19
    assert len(set(gen.SECTION0_TARGET_SKILLS)) == 19


def test_section0_target_skills_matches_1m_context_precheck_roster(gen, m1_ctx):
    """Cross-asserts names AND (tier, proceed_ref) against the independently
    authored test_1m_context_precheck.py::SECTION0_TARGETS roster — not only
    a name-set comparison, so a tier/proceed_ref drift is also caught."""
    generator_names = set(gen.SECTION0_TARGET_SKILLS)
    other_names = set(m1_ctx.SECTION0_SKILLS)
    assert generator_names == other_names, (
        f"roster mismatch vs test_1m_context_precheck.py::SECTION0_SKILLS: "
        f"only in generator={generator_names - other_names}, "
        f"only in 1m-precheck={other_names - generator_names}"
    )
    for skill in generator_names:
        gen_tier, gen_proc, _variant, _clause, _comment = gen.SECTION0_TARGET_SKILLS[skill]
        assert gen_tier == m1_ctx.SKILL_DECLARED_TIER[skill], f"{skill}: tier mismatch"
        assert gen_proc == m1_ctx.SKILL_PROCEED_REF[skill], f"{skill}: proceed_ref mismatch"


def test_section0_target_skills_matches_claude_md_preamble_list(gen):
    """Cross-asserts against the CLAUDE.md '### §0 Model dispatch preamble'
    prose list (the other independently-maintained 19-skill roster)."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    marker = "### §0 Model dispatch preamble"
    body_start = text.index(marker) + len(marker) + 2  # skip heading's own "\n\n" separator
    para_end = text.index("\n\n", body_start)
    paragraph = text[body_start:para_end]

    list_match = re.search(r"The 19 cheap-tier skills \(([^)]+)\)", paragraph)
    assert list_match, "could not find the '19 cheap-tier skills (...)' list in CLAUDE.md"
    claude_md_names = {s.strip() for s in list_match.group(1).split(",")}

    generator_names = set(gen.SECTION0_TARGET_SKILLS)
    assert generator_names == claude_md_names, (
        f"roster mismatch vs CLAUDE.md §0 preamble list: "
        f"only in generator={generator_names - claude_md_names}, "
        f"only in CLAUDE.md={claude_md_names - generator_names}"
    )
