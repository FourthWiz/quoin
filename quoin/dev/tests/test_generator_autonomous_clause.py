"""
Drift-detection test for the T-23 generator autonomous fail-OPEN clause (IVG-153).

`inject_pollution_dispatch.py` is the SOURCE-OF-TRUTH generator for the §0'
Pollution dispatch and §0″ Minimum-tier guard blocks across the 11 Opus-tier
leaf skills. Under `--autonomous` `/run`, those blocks must fail-OPEN (proceed
at current tier, no `AskUserQuestion`) whenever the incoming prompt carries the
`[autonomous]` sentinel — otherwise an unattended dispatch-failure/1M-credit
error would stall on a prompt nobody is present to answer.

This test asserts:
  (a) both `render_pollution_block` and `render_mintier_block` emit the
      `[autonomous]`-aware fail-OPEN clause;
  (b) a representative regenerated skill (`critic/SKILL.md`) carries the
      clause inside its §0″ block;
  (c) `build_preambles.py --check` stays clean — §0'/§0″ content is not part
      of the preamble.md payload, so this generator change must not touch it.

FAILS-without-the-change: reverting the template clause (in either render
function) reintroduces an unbranched dispatch prompt that would stall an
autonomous run on AskUserQuestion.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
SCRIPTS_DIR = PKG_DIR / "scripts"
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
REPO_ROOT = PKG_DIR.parent  # quoin/ (git root)

_ipd_spec = importlib.util.spec_from_file_location(
    "inject_pollution_dispatch_autonomous_clause_test",
    SCRIPTS_DIR / "inject_pollution_dispatch.py",
)
assert _ipd_spec is not None
_ipd = importlib.util.module_from_spec(_ipd_spec)
assert _ipd_spec.loader is not None
_ipd_spec.loader.exec_module(_ipd)

AUTONOMOUS_SENTINEL_TOKEN = "[autonomous]"
POLLUTION_AUTONOMOUS_ADVISORY = (
    "[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]"
)
MINTIER_AUTONOMOUS_ADVISORY = (
    "[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]"
)
NO_ASKUSERQUESTION_PHRASE = "DO NOT call `AskUserQuestion`"


def test_render_pollution_block_emits_autonomous_clause():
    """(a) render_pollution_block emits the [autonomous]-aware fail-OPEN clause."""
    block = _ipd.render_pollution_block("critic")
    assert AUTONOMOUS_SENTINEL_TOKEN in block, (
        "render_pollution_block output missing the [autonomous] sentinel check"
    )
    assert POLLUTION_AUTONOMOUS_ADVISORY in block, (
        "render_pollution_block output missing the autonomous fail-OPEN advisory line"
    )
    assert NO_ASKUSERQUESTION_PHRASE in block, (
        "render_pollution_block output missing the 'DO NOT call AskUserQuestion' clause text"
    )


def test_render_mintier_block_emits_autonomous_clause():
    """(a) render_mintier_block emits the [autonomous]-aware fail-OPEN clause."""
    block = _ipd.render_mintier_block("critic")
    assert AUTONOMOUS_SENTINEL_TOKEN in block, (
        "render_mintier_block output missing the [autonomous] sentinel check"
    )
    assert MINTIER_AUTONOMOUS_ADVISORY in block, (
        "render_mintier_block output missing the autonomous fail-OPEN advisory line"
    )
    assert NO_ASKUSERQUESTION_PHRASE in block, (
        "render_mintier_block output missing the 'DO NOT call AskUserQuestion' clause text"
    )


def test_autonomous_clause_checked_before_1m_and_generic_branches():
    """The autonomous branch must be classified BEFORE the 1M-credit / generic branches
    so it can short-circuit them (order matters for a human reading the block, and for
    any future parser that walks branches in document order)."""
    pollution_block = _ipd.render_pollution_block("critic")
    autonomous_idx = pollution_block.index(AUTONOMOUS_SENTINEL_TOKEN)
    credit_idx = pollution_block.index("1M-credit-class")
    assert autonomous_idx < credit_idx, (
        "§0' autonomous clause must appear before the 1M-credit-class branch"
    )

    mintier_block = _ipd.render_mintier_block("critic")
    autonomous_idx_m = mintier_block.index(AUTONOMOUS_SENTINEL_TOKEN)
    credit_idx_m = mintier_block.index("1M-credit-class")
    assert autonomous_idx_m < credit_idx_m, (
        "§0″ autonomous clause must appear before the 1M-credit-class branch"
    )


def test_critic_skill_contains_autonomous_clause_in_mintier_block():
    """(b) A representative regenerated skill (critic/SKILL.md) carries the autonomous
    clause inside its live §0″ block after regeneration."""
    text = (ADAPTER_SKILLS_DIR / "critic" / "SKILL.md").read_text(encoding="utf-8")

    mintier_heading_escaped = re.escape(_ipd.MINTIER_HEADING)
    match = re.search(
        mintier_heading_escaped + r".+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match, "critic/SKILL.md: §0″ block could not be extracted"
    block = match.group(0)

    assert AUTONOMOUS_SENTINEL_TOKEN in block, (
        "critic/SKILL.md §0″ block missing the [autonomous] sentinel check — "
        "regeneration did not propagate the new clause"
    )
    assert MINTIER_AUTONOMOUS_ADVISORY in block, (
        "critic/SKILL.md §0″ block missing the autonomous fail-OPEN advisory line"
    )


def test_critic_skill_contains_autonomous_clause_in_pollution_block():
    """(b) critic/SKILL.md also carries the autonomous clause inside its §0' block."""
    text = (ADAPTER_SKILLS_DIR / "critic" / "SKILL.md").read_text(encoding="utf-8")

    match = re.search(
        r"^## §0' Pollution dispatch \(execute after §0 / §0c if present — before skill body\).+?(?=^## )",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match, "critic/SKILL.md: §0' block could not be extracted"
    block = match.group(0)

    assert AUTONOMOUS_SENTINEL_TOKEN in block, (
        "critic/SKILL.md §0' block missing the [autonomous] sentinel check — "
        "regeneration did not propagate the new clause"
    )
    assert POLLUTION_AUTONOMOUS_ADVISORY in block, (
        "critic/SKILL.md §0' block missing the autonomous fail-OPEN advisory line"
    )


def test_inject_pollution_dispatch_check_clean_after_regeneration():
    """run_check() (--check) must pass on the regenerated tree — the run_check() token
    lists were updated alongside the templates."""
    result = _ipd.run_check()
    assert result == 0, (
        "inject_pollution_dispatch --check reports drift after T-23 regeneration. "
        "Re-run `python3 quoin/scripts/inject_pollution_dispatch.py` and ensure "
        "run_check()'s required_tokens/mintier_required_tokens lists include the "
        "new autonomous clause tokens."
    )


def test_build_preambles_check_stays_clean():
    """(c) build_preambles.py --check stays clean — §0'/§0″ is not part of the
    preamble.md payload, so this generator change (T-23, scoped to
    inject_pollution_dispatch.py only) must not have touched any preamble.md file."""
    build_preambles = SCRIPTS_DIR / "build_preambles.py"
    result = subprocess.run(
        [sys.executable, str(build_preambles), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "build_preambles.py --check reported drift, but T-23 does not edit "
        "build_preambles.py or any preamble.md file. stdout:\n"
        f"{result.stdout}\nstderr:\n{result.stderr}"
    )
