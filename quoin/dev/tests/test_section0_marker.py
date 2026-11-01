"""
IVG-165 T-02 (Commit N1) — §0 region end-marker placement invariant.

Verifies `<!-- §0-end -->` (SECTION0_END_MARKER, D-01) is present exactly once
in every §0-carrying SKILL.md, and sits at the pinned byte-offset relative to
the region per proc:marker-place / D-01b:
  - on its own line
  - exactly one `\\n` after the final line of the §0 variant closer (or, for
    `pr`/`workspace`, the `§0b: intentionally omitted` comment line)
  - followed by the SAME single trailing blank line that existed before the
    marker was added, then whatever follows the region (the next `## `
    heading for 19 files; `### Step 1a` for `start_of_day`, which is NOT a
    `## ` boundary — the confirmed sole boundary anomaly this marker fixes
    by construction)

This is a plain-structural test (deterministic pathlib + regex only, no LLM
calls) mirroring the discipline of test_quoin_stage1_worktree_fallback.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
ADAPTER_SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"

SECTION0_HEADING = "## §0 Model dispatch (FIRST STEP — execute before anything else)"
SECTION0_END_MARKER = "<!-- §0-end -->"

# The 19 §0-carrying skills — mirrors test_footprint_ceilings.py::SECTION0_SKILLS.
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
    "continue_work",
    "pr",
    "status",
    "workspace",
]


def _skill_md_text(skill: str) -> str:
    path = ADAPTER_SKILLS_DIR / skill / "SKILL.md"
    assert path.is_file(), f"{skill}: adapter SKILL.md missing at {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", SECTION0_SKILLS)
def test_section0_end_marker_present_exactly_once(skill: str) -> None:
    text = _skill_md_text(skill)
    count = text.count(SECTION0_END_MARKER)
    assert count == 1, f"{skill}: SECTION0_END_MARKER count={count} (expected exactly 1)"


@pytest.mark.parametrize("skill", SECTION0_SKILLS)
def test_section0_end_marker_on_own_line(skill: str) -> None:
    text = _skill_md_text(skill)
    idx = text.index(SECTION0_END_MARKER)
    # Character immediately before the marker must be a newline (own line, no
    # trailing whitespace on the closer line before it).
    assert idx > 0 and text[idx - 1] == "\n", (
        f"{skill}: marker is not on its own line (preceding char is {text[idx - 1]!r})"
    )
    # Marker line ends immediately at the marker's own newline.
    after = text[idx + len(SECTION0_END_MARKER):]
    assert after.startswith("\n"), (
        f"{skill}: marker line has trailing content: {after[:40]!r}"
    )


@pytest.mark.parametrize("skill", SECTION0_SKILLS)
def test_section0_end_marker_offset_uniform(skill: str) -> None:
    """Marker sits exactly one blank line before whatever follows the region.

    For 19 files that's the next `## ` heading; `start_of_day` abuts
    `### Step 1a` (H3, not a `## ` boundary) — the confirmed sole anomaly the
    marker fixes by construction (architecture.md Current state).
    """
    text = _skill_md_text(skill)
    idx = text.index(SECTION0_END_MARKER)
    after_marker = text[idx + len(SECTION0_END_MARKER):]

    if skill == "start_of_day":
        m = re.match(r"^\n\n### Step 1a", after_marker)
        assert m, (
            f"{skill}: expected exactly one blank line then '### Step 1a' after the "
            f"marker; got {after_marker[:60]!r}"
        )
    else:
        m = re.match(r"^\n\n## ", after_marker)
        assert m, (
            f"{skill}: expected exactly one blank line then a '## ' heading after the "
            f"marker; got {after_marker[:60]!r}"
        )


def test_section0_skills_roster_is_20() -> None:
    assert len(SECTION0_SKILLS) == 19
    assert len(set(SECTION0_SKILLS)) == 19
