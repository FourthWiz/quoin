"""Non-regression guard for plain `/run` (no `--autonomous`) — IVG-153, T-20(a).

Two independent claims, both asserted at the SKILL.md-lint level (grep the
source, mirroring the repo's existing SKILL.md-lint test style):

  1. `run/SKILL.md`'s `## Checkpoint interaction protocol` table still pauses
     at every checkpoint for plain (non-autonomous) invocations — the five
     interactive rows (yes/no/show/skip/other) plus the "never proceed
     without explicit confirmation" sentence are byte-unchanged from their
     pre-autonomous wording. The autonomous work (T-03) only ADDED a sixth
     "Autonomous" row; it must not have touched the other five.
  2. A leaf skill invoked WITHOUT the `[autonomous]` sentinel takes the
     unchanged interactive dispatch path: the T-23 generator clause (§0'/§0″)
     and the T-25 hand-synced clause (§0-worktree-fallback) are both
     conditional on the sentinel being PRESENT — so their "Otherwise (no
     `[autonomous]` sentinel — non-autonomous behavior unchanged)" fallback
     still reaches the original `AskUserQuestion` prompts. This test asserts
     that conditional guard is present and that the original interactive
     branches (with their original AskUserQuestion option labels) still
     exist verbatim alongside it.

FAILS-without-the-guard: if a future edit made the autonomous branch
unconditional (always fail-OPEN, sentinel or not), the "Otherwise" fallback
text and/or the original AskUserQuestion option labels would disappear from
the block, and this test would catch it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
SCRIPTS_DIR = PKG_DIR / "scripts"
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
RUN_SKILL = ADAPTER_SKILLS_DIR / "run_hard" / "SKILL.md"
CRITIC_SKILL = ADAPTER_SKILLS_DIR / "critic" / "SKILL.md"
REVISE_FAST_SKILL = ADAPTER_SKILLS_DIR / "revise-fast" / "SKILL.md"

_ipd_spec = importlib.util.spec_from_file_location(
    "inject_pollution_dispatch_plain_run_unchanged_test",
    SCRIPTS_DIR / "inject_pollution_dispatch.py",
)
assert _ipd_spec is not None
_ipd = importlib.util.module_from_spec(_ipd_spec)
assert _ipd_spec.loader is not None
_ipd_spec.loader.exec_module(_ipd)


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert RUN_SKILL.exists(), f"run/SKILL.md not found at {RUN_SKILL}"
    return RUN_SKILL.read_text(encoding="utf-8")


# ─── (1) Checkpoint protocol table: non-autonomous rows byte-unchanged ───────

# Verbatim pre-autonomous wording (five interactive rows + confirmation line).
# These strings pre-date IVG-153 T-03; T-03 was scoped to ADD a sixth
# "Autonomous" row, never to touch these.
NON_AUTONOMOUS_PROTOCOL_ROWS = [
    "| `yes` / `y` / `continue` / `go` | Proceed to next phase |",
    "| `no` / `n` / `stop` | Halt workflow; preserve all artifacts; tell user how to resume manually |",
    "| `show <artifact>` | Display the artifact (architecture / plan / changes / review / discover), then re-ask |",
    "| `skip` | Skip the next phase (only valid for optional phases: discover, specify, architect) |",
    "| Any other input | Treat as feedback or clarification; answer and re-ask |",
]

NON_AUTONOMOUS_CONFIRMATION_SENTENCE = (
    "**Never proceed without explicit confirmation** (non-autonomous mode)."
)


def test_checkpoint_protocol_non_autonomous_rows_byte_unchanged(run_skill_text: str) -> None:
    text = run_skill_text
    assert "## Checkpoint interaction protocol" in text

    for row in NON_AUTONOMOUS_PROTOCOL_ROWS:
        assert row in text, (
            f"Checkpoint interaction protocol row changed or missing: {row!r} — "
            "plain /run must still pause at every checkpoint with this exact wording"
        )

    assert NON_AUTONOMOUS_CONFIRMATION_SENTENCE in text, (
        "The non-autonomous 'never proceed without explicit confirmation' sentence "
        "changed or is missing"
    )


def test_checkpoint_protocol_row_count_unchanged(run_skill_text: str) -> None:
    """The protocol table has exactly 5 non-autonomous rows + 1 autonomous row
    (6 data rows total) — a sixth row was ADDED (T-03), none of the original
    five were removed or merged."""
    text = run_skill_text
    start = text.index("## Checkpoint interaction protocol")
    end = text.index("## Resume", start)
    table_slice = text[start:end]

    # Count pipe-delimited data rows (excludes the header + separator rows,
    # which start with "| Response" / "|---").
    data_rows = [
        line
        for line in table_slice.splitlines()
        if line.strip().startswith("|")
        and not line.strip().startswith("| Response")
        and not line.strip().startswith("|--")
        and not line.strip().startswith("|-")
    ]
    assert len(data_rows) == 6, (
        f"expected exactly 6 protocol data rows (5 non-autonomous + 1 autonomous), "
        f"found {len(data_rows)}: {data_rows}"
    )


# ─── (2) Leaf skill without [autonomous] sentinel: unchanged dispatch path ───

AUTONOMOUS_SENTINEL_TOKEN = "[autonomous]"


def test_mintier_block_non_autonomous_fallback_preserved() -> None:
    """The T-23 §0″ generator clause is conditional on the [autonomous]
    sentinel — without it, the original 1M-credit-class and generic
    AskUserQuestion branches (with their original option labels) must still
    be reachable, verbatim."""
    block = _ipd.render_mintier_block("critic")

    assert AUTONOMOUS_SENTINEL_TOKEN in block, "autonomous clause missing entirely"
    assert "checked FIRST" in block, (
        "autonomous branch must be classified first but still be a BRANCH, not "
        "an unconditional replacement"
    )

    # Original (pre-autonomous) interactive branches must still be present,
    # unconditionally reachable for non-autonomous invocations.
    assert "1M-credit-class:" in block
    assert 'label: "Abort — I\'ll switch with /model first"' in block
    assert 'label: "Proceed in-session at parent tier"' in block
    assert 'label: "Abort — run from an Opus session"' in block
    assert 'label: "Proceed at current tier (under-powered)"' in block

    # Live in the actual regenerated file too (not just the template).
    text = CRITIC_SKILL.read_text(encoding="utf-8")
    heading = "## §0″ Minimum-tier guard"
    assert heading in text
    idx = text.index(heading)
    end_idx = text.index("\n## ", idx + len(heading))
    live_block = text[idx:end_idx]
    assert AUTONOMOUS_SENTINEL_TOKEN in live_block
    assert 'label: "Abort — run from an Opus session"' in live_block


def test_pollution_block_non_autonomous_fallback_preserved() -> None:
    """Same as above for the §0' Pollution dispatch block."""
    block = _ipd.render_pollution_block("critic")

    assert AUTONOMOUS_SENTINEL_TOKEN in block
    assert "checked FIRST" in block
    assert "1M-credit-class:" in block


def test_worktree_fallback_non_autonomous_fallback_preserved() -> None:
    """The T-25 §0-worktree-fallback clause is conditional on the
    [autonomous] sentinel — without it, the original worktree-class
    AskUserQuestion (option (c) proceed-current-tier) must still fire."""
    assert REVISE_FAST_SKILL.exists()
    text = REVISE_FAST_SKILL.read_text(encoding="utf-8")

    start = text.index("<!-- §0-worktree-fallback-begin -->")
    end = text.index("<!-- §0-worktree-fallback-end -->", start)
    block = text[start:end]

    assert AUTONOMOUS_SENTINEL_TOKEN in block
    assert "checked FIRST" in block
    assert (
        "Otherwise (no\n      `[autonomous]` sentinel — non-autonomous behavior unchanged):"
        in block
        or "Otherwise (no `[autonomous]` sentinel — non-autonomous behavior unchanged):"
        in block
    ), "conditional 'Otherwise (no sentinel)' fallback guard is missing"

    # Original interactive branch (unconditionally reachable without the
    # sentinel) still present verbatim.
    assert "Use the AskUserQuestion tool to present the user with one" in block
    assert "proceed-current-tier" in block
