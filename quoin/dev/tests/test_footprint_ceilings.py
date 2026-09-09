"""IVG-162 T-02: per-file byte-ceiling checks (provisional = current size).

Guards the 20 SECTION0_SKILLS deployed SKILL.md SOURCE files (the §0-carrying
skill set — see quoin/CLAUDE.md "### §0 Model dispatch preamble") plus the
quoin/CLAUDE.md SOURCE file, against a per-file byte ceiling.

Modeled on test_preamble_freshness.py (TestSizeBudget — parametrized per-file,
independent assertions) and test_claude_md_size_ceiling.py (source-guard, not
deployed — D-06: measuring SOURCE keeps this test deterministic and
env-independent; the footprint REPORT (footprint_report.py / T-01) measures
DEPLOYED copies separately for the token-saving narrative).

Ceilings are PROVISIONAL at Stage 1: each equals the file's CURRENT measured
size (captured alongside the T-01 footprint baseline
`.workflow_artifacts/ivg-162-token-optimization-wave1/footprint-baseline.json`)
— nothing fails yet. Stage 6 (T-12) ratchets every slimmed file's ceiling down
to post-slim size * 1.10 (rounded up), so this test starts guarding regrowth
only once the wave's slims have landed.

Each file is an INDEPENDENT parametrized assertion (hard constraint — not one
aggregate budget), so a regression in one file never masks a regression in
another, and a single ratchet edit (T-12) never has to touch every case.

Update (IVG-165, 2026-08-02): the 20 §0-carrying skill ceilings are no
longer provisional/unslimmed — they went through the full generator-
conversion arc (see the CEILINGS comment below) and are now FINAL post-slim
* 1.10 ratchets, same status as claude_md's existing ratchet.
"""

import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
QUOIN_DIR = HERE.parent.parent  # quoin/dev/tests -> quoin/dev -> quoin
SKILLS_DIR = QUOIN_DIR / "adapters" / "claude" / "skills"
CLAUDE_MD = QUOIN_DIR / "CLAUDE.md"
SLIM_CLAUDE_MD = QUOIN_DIR / "CLAUDE.slim.md"

# The 20 §0-carrying skills (single source of truth: quoin/CLAUDE.md "### §0
# Model dispatch preamble" skill list, byte-mirrored against
# test_quoin_stage1_worktree_fallback.py's SOURCE_MUTATING_WORKTREE_SKILLS
# subset for the 5 sidecar carriers within this set).
SECTION0_SKILLS = [
    "gate",
    "end_of_day",
    "start_of_day",
    "triage",
    "capture_insight",
    "cleanup",
    "cost_snapshot",
    "weekly_review",
    "end_of_task",
    "implement",
    "rollback",
    "expand",
    "revise-fast",
    "sleep",
    "next_steps",
    "checkpoint",
    "continue_work",
    "pr",
    "status",
    "workspace",
]

# Ceilings = current measured SOURCE size (Stage 1 / T-01 baseline
# date 2026-08-02) for files this wave did NOT slim; RATCHETED (T-12, Stage 6,
# 2026-08-02) to post-slim size * 1.10 (rounded up) for files that WERE slimmed
# this wave (checkpoint, claude_md — see T-06/T-07/T-08).
#
# The 20 §0-carrying skill ceilings went through the full IVG-165 §0
# generator-conversion arc on 2026-08-02 (Commits N1 -> N2 -> A -> B -> R):
# N1 added the generator-conversion boundary marker (+17 bytes/file BY
# CONSTRUCTION); N2 normalized 3 pre-existing off-axis prose residuals
# (cleanup/sleep +256 bytes each, implement -1 byte); A made
# inject_pollution_dispatch.py the generator-owner of §0 with a zero-diff
# gate (no byte change); B slimmed the Manual-kill-switch rationale sentence
# to a memory/dispatch-guide.md pointer (-bytes/file, see
# .workflow_artifacts/ivg-165-s0-generator-conversion/footprint-report-B.md).
# All 20 (INCLUDING checkpoint, re-derived from its NEW post-slim size, not
# skipped) are now FINAL post-slim * 1.10 ratchets, same discipline as the
# claude_md ratchet below — NOT provisional/unslimmed, NOT "not attempted."
# The generator (`inject_pollution_dispatch.py::render_section0_block`) is
# the SOURCE OF TRUTH for §0 content going forward; a failure here after this
# point means a file genuinely grew and needs to shrink back down (the
# authorized marker/residual/slim exceptions above are one-time, 2026-08-02
# only — do not hand-raise a ceiling to "fix" a future failure).
CEILINGS = {
    "skill:capture_insight": 13196,  # S-4: post-description-trim 11996 * 1.10, monotonic vs prior 13396
    "skill:checkpoint": 77492,  # S-4: post-description-trim 70447 * 1.10, monotonic vs prior 77901
    "skill:cleanup": 19157,  # S-4: post-description-trim 17415 * 1.10, monotonic vs prior 19388
    "skill:continue_work": 16019,  # S-4: post-description-trim 14562 * 1.10, monotonic vs prior 16077
    "skill:cost_snapshot": 22204,  # S-4: HELD (D-12 monotonic) — candidate 20209*1.10=22230 > prior; file grew since 2026-08-02 derivation
    "skill:end_of_day": 49834,  # S-4: post-description-trim 45303 * 1.10, monotonic vs prior 50058
    "skill:end_of_task": 55541,  # S-4: post-description-trim 50491 * 1.10, monotonic vs prior 55771
    "skill:expand": 20921,  # S-4: post-description-trim 19019 * 1.10, monotonic vs prior 21229
    "skill:gate": 61580,  # S-4: post-description-trim 55981 * 1.10, monotonic vs prior 62430
    "skill:implement": 54414,  # S-4: post-description-trim 49467 * 1.10, monotonic vs prior 54715
    "skill:next_steps": 13106,  # R: post-slim 11914 * 1.10
    "skill:pr": 20850,  # ratchet: check-4 pathspec-narrowing fix grew the file
    # to 20784 (untracked-entry exclusion, script/judge file union, untrusted-
    # candidate-text note); 20371 no longer holds headroom for that fix
    "skill:revise-fast": 29330,  # S-4: post-description-trim 26663 * 1.10, monotonic vs prior 29521
    "skill:rollback": 23984,  # S-4: post-description-trim 21803 * 1.10, monotonic vs prior 24247
    "skill:sleep": 27739,  # S-4: HELD (D-12 monotonic) — candidate 25252*1.10=27778 > prior; file grew since 2026-08-02 derivation
    "skill:start_of_day": 28944,  # S-4: post-description-trim 26312 * 1.10, monotonic vs prior 29155
    "skill:status": 9841,  # R: post-slim 8946 * 1.10
    "skill:triage": 34356,  # S-4: post-description-trim 31232 * 1.10, monotonic vs prior 34422
    "skill:weekly_review": 18633,  # S-4: post-description-trim 16939 * 1.10, monotonic vs prior 18803
    "skill:workspace": 18958,  # S-4: post-description-trim 17234 * 1.10, monotonic vs prior 19239
    "claude_md": 40726,  # T-12 ratchet: post-slim 37023 * 1.10

    # IVG-164 stage 1 T-12: _target_path returns the repo SOURCE file for the
    # "claude_md" key (QUOIN_DIR / "CLAUDE.md" — T-02 DOES change it, +59 B;
    # this is not the deployed-file ceiling round 1's plan text once assumed).
    # claude_md_slim ratchets the new generated CLAUDE.slim.md the same way:
    # measured post-generation size 9,161 B (T-04, well-formed blank-line
    # model) * 1.10 rounded up.
    "claude_md_slim": 10078,  # R: post-generation 9161 * 1.10 rounded up
}


def _target_path(key: str) -> pathlib.Path:
    if key == "claude_md":
        return CLAUDE_MD
    if key == "claude_md_slim":
        return SLIM_CLAUDE_MD
    assert key.startswith("skill:"), f"unrecognized ceiling key: {key}"
    skill = key.split(":", 1)[1]
    return SKILLS_DIR / skill / "SKILL.md"


def test_section0_skills_set_matches_ceiling_keys():
    """Guard against the 20-skill set and the ceiling dict silently drifting apart."""
    assert len(SECTION0_SKILLS) == 20, (
        f"expected exactly 20 §0-carrying skills, got {len(SECTION0_SKILLS)}"
    )
    skill_keys = {k for k in CEILINGS if k.startswith("skill:")}
    expected_keys = {f"skill:{s}" for s in SECTION0_SKILLS}
    assert skill_keys == expected_keys, (
        f"CEILINGS skill keys {skill_keys} do not match SECTION0_SKILLS {expected_keys}"
    )


@pytest.mark.parametrize("key", sorted(CEILINGS.keys()))
def test_file_within_byte_ceiling(key):
    path = _target_path(key)
    assert path.exists(), f"ceiling target missing on disk: {path}"
    size = len(path.read_bytes())
    ceiling = CEILINGS[key]
    assert size <= ceiling, (
        f"{path} is {size} bytes, exceeds provisional ceiling of {ceiling} bytes "
        f"(key={key}). If this is a deliberate wave slim overshoot, ratchet the "
        f"ceiling per T-12 (Stage 6) with a rationale, never silently."
    )
