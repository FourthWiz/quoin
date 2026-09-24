"""Byte-compare every live §0-family block against its renderer.

Parametrized over all 53 (skill, family) pairs recorded in the five roster
constants. For each, slices the live block from the adapter SKILL.md with the
SAME span rule `inject_pollution_dispatch.py` itself uses to replace that
block, and compares it byte-for-byte against the matching renderer's output.

Including `render_section0_block` is deliberate: §0 is the family with the
historical over-capture anomaly (see test_section0_provenance_guard.py), and
before this test its only byte check lived inside `--check`, which is a CLI
entry point, not a pytest-visible regression guard.

This is also the growth-side backstop the per-file share ceilings
(test_section0_provenance_guard.py::test_file_within_share_ceiling) do not
provide: any hand-added line inside a generated span reds here even when the
file's overall §0-family share stays under its ceiling, because the span
rule captures exactly the generator-owned region, not a percentage.

Complements, rather than duplicates, existing coverage:
  - test_section0_generator.py checks idempotence and marker shape
  - test_generator_autonomous_clause.py / test_plain_run_unchanged.py check
    token presence inside the templates
  - neither byte-compares live-vs-renderer across all five families
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_SCRIPTS_DIR = _TESTS_DIR.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import inject_pollution_dispatch as generator  # noqa: E402

ADAPTER_DIR = _TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"

SECTION0_TARGET_SKILLS = sorted(generator.SECTION0_TARGET_SKILLS)
POLLUTION_TARGET_SKILLS = sorted(generator.POLLUTION_TARGET_SKILLS)
MINTIER_TARGET_SKILLS = sorted(generator.MINTIER_TARGET_SKILLS)
MINTIER_SONNET_TARGET_SKILLS = sorted(generator.MINTIER_SONNET_TARGET_SKILLS)
ZC_SKILLS = sorted(generator.ZC_SKILLS)

# Every (family, skill) pair this test guards — 20 + 10 + 11 + 10 + 2 = 53.
ALL_PAIRS = (
    [("§0", s) for s in SECTION0_TARGET_SKILLS]
    + [("§0'", s) for s in POLLUTION_TARGET_SKILLS]
    + [("§0″", s) for s in MINTIER_TARGET_SKILLS]
    + [("§0‴", s) for s in MINTIER_SONNET_TARGET_SKILLS]
    + [("§0c", s) for s in ZC_SKILLS]
)


def _extract_replace_existing_block_span(text: str, heading: str) -> str | None:
    """Mirror inject_pollution_dispatch.py's own `_replace_existing_block`
    match exactly: from the heading line through (but not including) the
    next `^## ` line. Used for the four families replaced that way
    (§0', §0″, §0‴, §0c)."""
    pattern = re.compile(
        r"^" + re.escape(heading) + r".+?(?=^## )",
        flags=re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(0) if match else None


def _extract_section0_span(text: str) -> str | None:
    """Mirror the §0 family's own marker-anchored replacement span: from
    SECTION0_HEADING through SECTION0_END_MARKER inclusive, plus the
    marker's own trailing newline — exactly as `inject_blocks_into_file`
    and the `--check` drift detector both compute `region_end`."""
    heading = generator.SECTION0_HEADING
    marker = generator.SECTION0_END_MARKER
    if text.count(heading) != 1 or text.count(marker) != 1:
        return None
    heading_idx = text.index(heading)
    marker_idx = text.index(marker)
    region_end = marker_idx + len(marker) + 1  # include marker's trailing \n
    return text[heading_idx:region_end]


_RENDERERS = {
    "§0": generator.render_section0_block,
    "§0'": generator.render_pollution_block,
    "§0″": generator.render_mintier_block,
    "§0‴": generator.render_mintier_sonnet_block,
    "§0c": generator.render_zc_block,
}
_HEADINGS = {
    "§0'": generator.POLLUTION_HEADING,
    "§0″": generator.MINTIER_HEADING,
    "§0‴": generator.MINTIER_SONNET_HEADING,
    "§0c": generator.ZC_HEADING,
}


@pytest.mark.parametrize(
    "family,skill", ALL_PAIRS, ids=[f"{f}:{s}" for f, s in ALL_PAIRS]
)
def test_live_block_byte_matches_renderer(family, skill):
    path = ADAPTER_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    if family == "§0":
        live = _extract_section0_span(text)
    else:
        live = _extract_replace_existing_block_span(text, _HEADINGS[family])

    assert live is not None, (
        f"{skill}/SKILL.md: could not locate a {family} block span "
        "(heading missing, duplicated, or generator's own anchor invariant "
        "violated — see inject_pollution_dispatch.py's FAIL LOUD checks)"
    )

    expected = _RENDERERS[family](skill)

    assert live == expected, (
        f"{skill}/SKILL.md {family} block is not byte-identical to "
        f"{_RENDERERS[family].__name__}({skill!r}) "
        f"(live={len(live)} chars, rendered={len(expected)} chars). "
        "Run `bash quoin/install.sh` / re-run the generator, or this is a "
        "hand-edit inside a generated span that must be reverted."
    )


def test_all_53_pairs_covered():
    assert len(ALL_PAIRS) == 53
    assert len(set(ALL_PAIRS)) == 53, "duplicate (family, skill) pair in ALL_PAIRS"
