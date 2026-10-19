"""Text-structural guard over `/run`'s `## Hook cooperation (autonomous)` section.

The section no longer carries a self-checkpoint bullet: context saves come
from the Pre-phase over-budget `boundary_checkpoint.py` call only, and
between boundaries the backstop is native auto-compaction plus the
`SessionStart` compact re-entry and the Stop-hook continuation. This is a
text-structural check over the slice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run" / "SKILL.md"

SLICE_START = "## Hook cooperation (autonomous)"
SLICE_END = "## Gate boundaries reference"
NEW_BULLET = "- **Context saves at phase boundaries only.**"


@pytest.fixture(scope="module")
def slice_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    text = RUN_SKILL.read_text(encoding="utf-8")
    start = text.index(SLICE_START)
    end = text.index(SLICE_END, start)
    return text[start:end]


def test_stage2_pinned_literals_present(slice_text: str) -> None:
    for literal in (
        "NEVER writes to any file under",
        "hooks/",
        "NEVER modifies or lowers a `QUOIN_*_BPS` constant",
    ):
        assert literal in slice_text, f"stage-2 pinned literal missing: {literal!r}"


def test_no_line_co_occurs_autonomous_and_hooks_dir(slice_text: str) -> None:
    offenders = [
        (i, line)
        for i, line in enumerate(slice_text.splitlines(), start=1)
        if "autonomous" in line.lower() and "hooks/" in line
    ]
    assert not offenders, (
        f"line(s) in the Hook cooperation slice mention both 'autonomous' "
        f"and 'hooks/': {offenders}"
    )


def test_self_checkpoint_bullets_are_gone(slice_text: str) -> None:
    for gone in (
        "Self-checkpoint trigger",
        "Self-checkpoint at very high fill",
        "SELF_CHECKPOINT_DUE",
        "run_state_probe",
        "COMPACT_FIRST_BPS",
    ):
        assert gone not in slice_text, f"removed self-checkpoint wording is back: {gone!r}"


def test_boundary_bullet_is_first_and_names_the_backstop(slice_text: str) -> None:
    assert slice_text.count(NEW_BULLET) == 1
    first_bullet = next(line for line in slice_text.splitlines() if line.startswith("- **"))
    assert first_bullet.startswith(NEW_BULLET)
    bullet_start = slice_text.index(NEW_BULLET)
    bullet = slice_text[bullet_start:slice_text.index("- **No prompt hook reports context.**", bullet_start)]
    assert "boundary_checkpoint.py" in bullet
    assert "native auto-compaction (Claude Code setting" in bullet
    assert "`autoCompactEnabled`" in bullet
    assert "`SessionStart` compact" in bullet
    assert "Stop-hook continuation" in bullet


def test_no_block_to_catch_pins_are_kept(slice_text: str) -> None:
    assert "- **No prompt hook reports context.**" in slice_text
    assert "must not be reintroduced" in slice_text
    assert "there is\n  no block to catch" in slice_text or "no block to catch" in " ".join(slice_text.split())
    assert "- **Hooks fail open; the orchestrator never assumes otherwise.**" in slice_text
    assert "- **No yield with pending work.**" in slice_text
    assert "- **Hard constraint.**" in slice_text
