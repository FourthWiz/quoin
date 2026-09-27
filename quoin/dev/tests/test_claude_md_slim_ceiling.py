"""IVG-164 stage 1 T-08: slim marker-block ceiling guard.

CLAUDE.slim.md is merged into a POSSIBLY PRE-EXISTING project CLAUDE.md by
`merge_workflow_rules` — the marker block (`_MARKER_START` line + content +
`_MARKER_END` line) is quoin's to bound; the surrounding project file is not.
This test therefore ceilings the MARKER BLOCK, not the whole deployed file
(that would conflate quoin's own budget with user-owned content — see the
advisory whole-file projection at the bottom, which is deliberately loose).

All addends are pinned exactly (plan MIN-1 r2 / MAJ-2 r3: every term in the
projection is a named, re-derivable constant, no `~` approximations). Live
figures re-derived at implement time (round-4 critic's well-formed blank-line
model, MIN-1 of critic-response-4.md): keep set 7,311 B, generated header
338 B (pinned verbatim in T-04), pointer index 1,452 B (140 B header + 1,311 B
of 24 rows + 1 blank) -> CLAUDE.slim.md source == 9,101 B exactly (re-measured
IVG-262: keep set shrank 60 B when the finalization-rule bullet was shortened). This
matches the live committed file byte-for-byte (T-08 does not hand-transcribe
this figure a second time; it reads the committed file directly).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ (repo root)
_SOURCE_ROOT = _REPO_ROOT / "quoin"

SLIM = _SOURCE_ROOT / "CLAUDE.slim.md"

QUOIN_HOME_PLACEHOLDER = "__QUOIN_HOME__"

# Frame = "# === DEV WORKFLOW START ===\n" + ... + "# === DEV WORKFLOW END ===\n"
# measured (same convention/value as test_claude_md_size_ceiling.py's
# MARKER_FRAME): opening + closing install.sh marker lines.
MARKER_FRAME = 56

SLIM_MARKER_BLOCK_CEILING = 10_240

# Fixed, environment-independent worst-case project `.claude` path (same
# discipline as the sibling ceiling test's HOME_STANDIN: a literal, not
# os.path.expanduser, so the projection is a true upper bound regardless of
# which machine runs the test). Modeled on a realistic Drive-synced project
# path; exactly 104 chars, matching the plan's "worst case, ~104 chars" term.
PROJECT_STANDIN = (
    "/Users/example/Library/CloudStorage/GoogleDrive-user@example.com/"
    "My Drive/Storage/project-name-x/.claude"
)
assert len(PROJECT_STANDIN) == 104, len(PROJECT_STANDIN)

# Advisory whole-file projection basis (NOT the marker-block guard above).
# Re-derived from THIS repo's own project CLAUDE.md (5,541 B measured at
# implement time) plus headroom for growth — explicitly NOT the user-scope
# PRE_MARKER_BUDGET=700 in test_claude_md_size_ceiling.py, which is
# calibrated for a different destination (the user's ~/.claude/CLAUDE.md).
PROJECT_PRE_MARKER_BUDGET = 6_144
DEPLOYED_CEILING = 40_000


def test_slim_source_marker_block_below_ceiling():
    """The committed CLAUDE.slim.md, framed by the install.sh markers, fits the ceiling."""
    source_len = len(SLIM.read_bytes())
    marker_block = source_len + MARKER_FRAME
    assert marker_block <= SLIM_MARKER_BLOCK_CEILING, (
        f"CLAUDE.slim.md marker block is {marker_block} B "
        f"(source {source_len} + frame {MARKER_FRAME}); "
        f"ceiling is {SLIM_MARKER_BLOCK_CEILING}. Re-derive the ceiling with "
        "written justification (spec A-5); never trim keep content to fit."
    )


def test_slim_deployed_projection_below_ceiling_exact_figures():
    """Substituted (worst-case project path) marker block matches the plan's pinned figures.

    Asserted against exact figures, not a tolerance (plan T-08: "the test
    asserts against these exact figures"). If any addend changes, this test
    is meant to go red — that is the point of pinning arithmetic instead of
    an inequality-only check.
    """
    slim_text = SLIM.read_text(encoding="utf-8")
    source_len = len(slim_text.encode("utf-8"))
    assert source_len == 9_101, (
        f"CLAUDE.slim.md source is {source_len} B, expected 9,101 B exactly "
        "(7,311 B keep set + 338 B pinned header + 1,452 B pointer index). "
        "If this changed intentionally, re-pin every downstream figure in "
        "this file and in T-12's claude_md_slim ceiling."
    )

    marker_block_source = source_len + MARKER_FRAME
    assert marker_block_source == 9_157, marker_block_source

    substituted = slim_text.replace(QUOIN_HOME_PLACEHOLDER, PROJECT_STANDIN)
    substituted_len = len(substituted.encode("utf-8"))
    placeholder_count = slim_text.count(QUOIN_HOME_PLACEHOLDER)
    assert placeholder_count == 2, placeholder_count
    sub_delta = placeholder_count * (len(PROJECT_STANDIN) - len(QUOIN_HOME_PLACEHOLDER))
    assert substituted_len == source_len + sub_delta

    marker_block_substituted = substituted_len + MARKER_FRAME
    assert marker_block_substituted == 9_337, marker_block_substituted

    headroom = SLIM_MARKER_BLOCK_CEILING - marker_block_substituted
    assert headroom == 903, headroom
    assert marker_block_substituted <= SLIM_MARKER_BLOCK_CEILING, (
        f"CLAUDE.slim.md substituted marker block is {marker_block_substituted} B "
        f"on a {len(PROJECT_STANDIN)}-char worst-case project path; "
        f"ceiling is {SLIM_MARKER_BLOCK_CEILING}."
    )


def test_project_whole_file_advisory_projection_below_40k():
    """Advisory: slim marker block + a realistic project pre-marker budget stays under 40k.

    This is deliberately loose (the surrounding project CLAUDE.md is
    user-owned, not quoin's to bound) — it exists only to catch a gross
    regression, not to replace the marker-block ceiling above.
    """
    slim_text = SLIM.read_text(encoding="utf-8")
    substituted = slim_text.replace(QUOIN_HOME_PLACEHOLDER, PROJECT_STANDIN)
    projected = PROJECT_PRE_MARKER_BUDGET + MARKER_FRAME + len(substituted.encode("utf-8"))
    assert projected < DEPLOYED_CEILING, (
        f"Projected deployed project CLAUDE.md is {projected} B; "
        f"ceiling is {DEPLOYED_CEILING}. Re-derive PROJECT_PRE_MARKER_BUDGET "
        "or the slim keep set."
    )
