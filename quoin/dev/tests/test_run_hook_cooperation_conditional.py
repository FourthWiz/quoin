"""Text-structural guard over `/run`'s conditioned self-checkpoint bullet — IVG-258 S-6, T-06.

`## Hook cooperation (autonomous)` in `run/SKILL.md` conditions its
self-checkpoint bullet on `run_state_probe` (`hooks/_lib.sh`) rather than
firing unconditionally at `COMPACT_FIRST_BPS`. This is a text-structural
check over the slice — it can prove the prose states the right contract, not
that the sourced call behaves that way at runtime (the executable
counterpart is `test_stage6_checkpoint_probe_wiring_canary.sh` Case B2).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run" / "SKILL.md"

SLICE_START = "## Hook cooperation (autonomous)"
SLICE_END = "## Gate boundaries reference"


@pytest.fixture(scope="module")
def slice_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    text = RUN_SKILL.read_text(encoding="utf-8")
    start = text.index(SLICE_START)
    end = text.index(SLICE_END, start)
    return text[start:end]


def _self_checkpoint_bullet(slice_text: str) -> str:
    """Isolate just the first bullet (the self-checkpoint one), not the
    whole section, for the clauses that must co-occur WITHIN one bullet."""
    bullet_start = slice_text.index("- **Self-checkpoint at very high fill.**")
    next_bullet = slice_text.index(
        "- **No prompt hook reports context.**", bullet_start
    )
    return slice_text[bullet_start:next_bullet]


# (a) the three stage-2 pinned literals are still present verbatim
def test_stage2_pinned_literals_present(slice_text: str) -> None:
    for literal in (
        "NEVER writes to any file under",
        "hooks/",
        "NEVER modifies or lowers a `QUOIN_*_BPS` constant",
    ):
        assert literal in slice_text, f"stage-2 pinned literal missing: {literal!r}"


# (b) no single line in the slice contains both "autonomous" (case-insensitive)
# and "hooks/" — asserted locally so a reflow fails here first.
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


# (c) the self-checkpoint bullet states the save fires when an active
# run-state record exists, names the trigger it depends on, carries no
# checkpoint-defer marker (that marker lost its only reader), and does NOT
# contain `at_stage_boundary`.
def test_self_checkpoint_bullet_states_condition(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    assert "SELF_CHECKPOINT_DUE" in bullet
    assert "checkpoint-defer" not in bullet, (
        "the checkpoint-defer marker has no reader any more; the bullet must "
        "not describe writing or skipping it"
    )
    assert "at_stage_boundary" not in bullet, (
        "self-checkpoint bullet must not gate on at_stage_boundary"
    )


# (d) the slice still contains COMPACT_FIRST_BPS
def test_slice_contains_compact_first_bps(slice_text: str) -> None:
    assert "COMPACT_FIRST_BPS" in slice_text


# (e) the bullet contains the literal string `run_state_probe`
def test_bullet_names_run_state_probe(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    assert "run_state_probe" in bullet, (
        "self-checkpoint bullet must name run_state_probe explicitly — "
        "otherwise an implementer is sent to the wrong, non-gating "
        "run_state.py --read reader (MAJ-3 round-2 regression)"
    )


# (f) the line index of `. __QUOIN_HOME__/hooks/_lib.sh` inside the bullet is
# STRICTLY LESS than the line index of `command -v run_state_probe`.
def test_source_line_precedes_availability_guard(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    lines = bullet.splitlines()
    source_idx = next(
        (i for i, ln in enumerate(lines) if '. "__QUOIN_HOME__/hooks/_lib.sh"' in ln),
        None,
    )
    guard_idx = next(
        (i for i, ln in enumerate(lines) if "command -v run_state_probe" in ln),
        None,
    )
    assert source_idx is not None, "bullet must source _lib.sh"
    assert guard_idx is not None, "bullet must check command -v run_state_probe"
    assert source_idx < guard_idx, (
        "the _lib.sh source line must appear before the command -v guard "
        "(MAJ-1 round-3 regression: naming the probe without sourcing it first)"
    )


# (g) the bullet contains "self-checkpoint fires" co-occurring with "guard is
# absent" WITHIN THE SAME BULLET (round-5 MIN-6: scoped to the bullet).
def test_fail_safe_direction_pinned(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    assert "self-checkpoint fires" in bullet
    assert "guard is absent" in bullet


# (h) the bullet contains the literal expression `resolve_project_root
# "$(pwd)"` AND does NOT contain a bare `{root}` placeholder (round-5
# MAJ-1 round-4 regression guard).
def test_root_resolution_present_no_bare_placeholder(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    assert 'resolve_project_root "$(pwd)"' in bullet, (
        "bullet must resolve its own root via resolve_project_root \"$(pwd)\" "
        "rather than leaving the probe argument unresolvable"
    )
    assert "{root}" not in bullet, (
        "bullet must not contain a bare {root} placeholder — the probe "
        "argument must be a resolvable expression"
    )


# (i) the three token literals live in exactly one fenced ```sh block within
# the bullet, and the block emits no other token (round-5 critic MIN-1:
# T-13 Case B2's extraction target must be well-defined, mirroring T-08
# clause (h) for T-07's block).
def test_single_fenced_block_emits_only_three_tokens(slice_text: str) -> None:
    bullet = _self_checkpoint_bullet(slice_text)
    # Leading whitespace before the fence markers is expected — run/SKILL.md's
    # in-bullet fences are indented (unlike checkpoint/SKILL.md's column-0
    # fences), so the regex must tolerate that (round-5 critic MIN-2).
    fences = re.findall(r"[ \t]*```sh\n(.*?)\n[ \t]*```", bullet, re.DOTALL)
    assert len(fences) == 1, (
        f"expected exactly one fenced ```sh block in the self-checkpoint "
        f"bullet (needed for T-13 Case B2's well-defined extraction target), "
        f"found {len(fences)}"
    )
    block = fences[0]
    assert "command -v run_state_probe" in block
    for token in ("GUARD_UNAVAILABLE", "PROBE_ACTIVE", "PROBE_INACTIVE"):
        assert token in block, f"block missing token literal {token!r}"
    echoed_tokens = set(re.findall(r"echo\s+(\S+)", block))
    assert echoed_tokens == {"GUARD_UNAVAILABLE", "PROBE_ACTIVE", "PROBE_INACTIVE"}, (
        f"block echoes tokens beyond the three defined literals: {echoed_tokens}"
    )
