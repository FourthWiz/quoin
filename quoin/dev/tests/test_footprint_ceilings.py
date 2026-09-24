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
    "skill:capture_insight": 13196,  # S-4: post-description-trim 11996 * 1.10, monotonic vs prior 13396  S-5 ratchet: CANDIDATE-no-op (measured 11996 B, candidate 13196 B == prior ceiling 13196 B; unchanged).
    "skill:checkpoint": 77492,  # S-4: post-description-trim 70447 * 1.10, monotonic vs prior 77901  S-5 ratchet: HOLD (measured 72815 B, candidate 80097 B > prior ceiling 77492 B; unchanged).
    "skill:cleanup": 19157,  # S-4: post-description-trim 17415 * 1.10, monotonic vs prior 19388  S-5 ratchet: HOLD (measured 19107 B, candidate 21018 B > prior ceiling 19157 B; unchanged).
    "skill:continue_work": 16019,  # S-4: post-description-trim 14562 * 1.10, monotonic vs prior 16077  S-5 ratchet: CANDIDATE-no-op (measured 14562 B, candidate 16019 B == prior ceiling 16019 B; unchanged).
    "skill:cost_snapshot": 22204,  # S-4: HELD (D-12 monotonic) — candidate 20209*1.10=22230 > prior; file grew since 2026-08-02 derivation  S-5 ratchet: HOLD (measured 20209 B, candidate 22230 B > prior ceiling 22204 B; unchanged).
    "skill:end_of_day": 49834,  # S-4: post-description-trim 45303 * 1.10, monotonic vs prior 50058  S-5 ratchet: CANDIDATE-no-op (measured 45303 B, candidate 49834 B == prior ceiling 49834 B; unchanged).
    "skill:end_of_task": 55541,  # S-4: post-description-trim 50491 * 1.10, monotonic vs prior 55771  S-5 ratchet: HOLD (measured 53393 B, candidate 58733 B > prior ceiling 55541 B; unchanged).
    "skill:expand": 20921,  # S-4: post-description-trim 19019 * 1.10, monotonic vs prior 21229  S-5 ratchet: CANDIDATE-no-op (measured 19019 B, candidate 20921 B == prior ceiling 20921 B; unchanged).
    "skill:gate": 61580,  # S-4: post-description-trim 55981 * 1.10, monotonic vs prior 62430  S-5 ratchet: HOLD (measured 59458 B, candidate 65404 B > prior ceiling 61580 B; unchanged).
    "skill:implement": 54414,  # S-4: post-description-trim 49467 * 1.10, monotonic vs prior 54715  S-5 ratchet: HOLD (measured 52096 B, candidate 57306 B > prior ceiling 54414 B; unchanged).
    "skill:next_steps": 12998,  # R: post-slim 11914 * 1.10  S-5 ratchet: APPLY (measured 11816 B, candidate 12998 B <= prior ceiling 13106 B; only strict decrease in the whole task).
    "skill:pr": 20850,  # ratchet: check-4 pathspec-narrowing fix grew the file  S-5 ratchet: HOLD (measured 20818 B, candidate 22900 B > prior ceiling 20850 B; unchanged).
    # to 20784 (untracked-entry exclusion, script/judge file union, untrusted-
    # candidate-text note); 20371 no longer holds headroom for that fix
    "skill:revise-fast": 29330,  # S-4: post-description-trim 26663 * 1.10, monotonic vs prior 29521  S-5 ratchet: CANDIDATE-no-op (measured 26663 B, candidate 29330 B == prior ceiling 29330 B; unchanged).
    "skill:rollback": 23984,  # S-4: post-description-trim 21803 * 1.10, monotonic vs prior 24247  S-5 ratchet: CANDIDATE-no-op (measured 21803 B, candidate 23984 B == prior ceiling 23984 B; unchanged).
    "skill:sleep": 27739,  # S-4: HELD (D-12 monotonic) — candidate 25252*1.10=27778 > prior; file grew since 2026-08-02 derivation  S-5 ratchet: HOLD (measured 25252 B, candidate 27778 B > prior ceiling 27739 B; unchanged).
    "skill:start_of_day": 28944,  # S-4: post-description-trim 26312 * 1.10, monotonic vs prior 29155  S-5 ratchet: CANDIDATE-no-op (measured 26312 B, candidate 28944 B == prior ceiling 28944 B; unchanged).
    "skill:status": 9841,  # R: post-slim 8946 * 1.10  S-5 ratchet: CANDIDATE-no-op (measured 8946 B, candidate 9841 B == prior ceiling 9841 B; unchanged).
    "skill:triage": 34356,  # S-4: post-description-trim 31232 * 1.10, monotonic vs prior 34422  S-5 ratchet: CANDIDATE-no-op (measured 31232 B, candidate 34356 B == prior ceiling 34356 B; unchanged).
    "skill:weekly_review": 18633,  # S-4: post-description-trim 16939 * 1.10, monotonic vs prior 18803  S-5 ratchet: CANDIDATE-no-op (measured 16939 B, candidate 18633 B == prior ceiling 18633 B; unchanged).
    "skill:workspace": 18958,  # S-4: post-description-trim 17234 * 1.10, monotonic vs prior 19239  S-5 ratchet: CANDIDATE-no-op (measured 17234 B, candidate 18958 B == prior ceiling 18958 B; unchanged).
    "claude_md": 40726,  # T-12 ratchet: post-slim 37023 * 1.10
    # S-5 ratchet (T-11): measured 39256 B, candidate 39256*1.10=43182 B > prior
    # ceiling 40726 B -> HOLD. Net effect zero; recorded anyway per D-06 (a
    # surface with a recorded no-op decision differs from one nobody examined).

    # IVG-164 stage 1 T-12: _target_path returns the repo SOURCE file for the
    # "claude_md" key (QUOIN_DIR / "CLAUDE.md" — T-02 DOES change it, +59 B;
    # this is not the deployed-file ceiling round 1's plan text once assumed).
    # claude_md_slim ratchets the new generated CLAUDE.slim.md the same way:
    # measured post-generation size 9,161 B (T-04, well-formed blank-line
    # model) * 1.10 rounded up.
    "claude_md_slim": 10078,  # R: post-generation 9161 * 1.10 rounded up
    # S-5 ratchet (T-11): measured 9161 B, candidate 9161*1.10=10078 B (rounded
    # up) == prior ceiling 10078 B -> CANDIDATE, but a no-op. Net effect zero;
    # recorded anyway per D-06.
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
