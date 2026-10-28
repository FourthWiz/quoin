r"""T-08 (IVG-141): budget-roster set-equality census over the four adapter SKILL.md files.

Import-free / bare-checkout runnable — pure text scan of the adapter SKILL.md
files. NEVER `import quoin` (lessons 2026-07-22).

Census derivation (PINNED to the T-02 spike outcome — MINOR closeout)
--------------------------------------------------------------------
The T-02 subagent-transcript spike PASSED (`leaf-measure`: every one of the four
skills — including the three leaf skills — resolves its own transcript and calls
`context_budget_guard.py`). So the roster is keyed on the `context_budget_guard.py`
INVOCATION token: `SKILLS_WITH_BUDGET_CHECK` = the set of adapter skill dirs whose
SKILL.md text contains `context_budget_guard.py`. The set MUST equal
`{run, implement, thorough_plan, review}`.

Marker-keyed fallback (documented, NOT active): had the spike FAILED (leaf skills
not self-measuring), this census would instead key on the boundary MARKER token
`site=phase-budget-<skill>` so the four-skill invariant would still hold
regardless of who measures. Because the spike PASSED, the invocation-token
derivation is the active one; the `site=phase-budget-` marker is asserted ABSENT
(the over-budget path is non-blocking — no decision-gate site is introduced).

Structural-invariant discipline mirrors `test_decision_gate_census` / the IVG-128
set-equality lesson: a dropped or silently-added skill fails the suite.
"""
from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"

BUDGET_TOKEN = "context_budget_guard.py"
EXPECTED = {"run_hard", "implement", "thorough_plan", "review"}

# (start_anchor, end_anchor) bounding each skill's IVG-141 budget wiring region.
# The region is the ONLY place this feature touches; AUQ/marker/[no-interactive]
# assertions are scoped to it so unrelated site AUQ tokens in the same file don't
# false-trip the census.
WIRING_REGIONS = {
    "run_hard": (
        "## Pre-phase context budget (per heavy phase spawn) — IVG-141",
        "## On-behalf cost capture",
    ),
    "implement": (
        "Pre-phase context budget at task/batch boundaries (IVG-141)",
        "## §0b Branch-hygiene precheck",
    ),
    "thorough_plan": (
        "Pre-round context budget (IVG-141)",
        "### Boundary triggers",
    ),
    "review": (
        "Pre-fan-out context budget (IVG-141)",
        "**Medium/Large — parallel dimension fan-out.**",
    ),
}


def _skill_text(name: str) -> str:
    return (ADAPTER_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _all_skill_dirs():
    return [p.name for p in ADAPTER_SKILLS_DIR.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()]


def _skills_with_budget_check():
    return {name for name in _all_skill_dirs()
            if BUDGET_TOKEN in _skill_text(name)}


def _wiring_region(name: str) -> str:
    start, end = WIRING_REGIONS[name]
    text = _skill_text(name)
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# ─── Set-equality census ────────────────────────────────────────────────────

def test_four_skills_have_budget_check():
    assert _skills_with_budget_check() == EXPECTED


def test_census_bites_on_dropped_skill():
    """Mutation: simulate a skill dropping the guard call — census must bite."""
    mutated = _skills_with_budget_check() - {"review"}
    assert mutated != EXPECTED


def test_census_bites_on_added_skill():
    """Mutation: simulate a 5th skill acquiring the guard call — census must bite."""
    mutated = _skills_with_budget_check() | {"architect"}
    assert mutated != EXPECTED


# ─── No-new-AUQ + block-knob documented (Checkpoint-B design) ───────────────

def test_no_new_auq_site_in_wirings():
    """None of the four IVG-141 wiring regions introduces an AskUserQuestion(
    call token or a phase-budget decision-gate marker (non-blocking design)."""
    for name in EXPECTED:
        region = _wiring_region(name)
        assert "AskUserQuestion(" not in region, f"{name}: new AUQ token in budget wiring"
        assert "site=phase-budget-" not in region, f"{name}: phase-budget decision-gate marker"
    # Belt-and-suspenders: the marker must not appear anywhere in the four files.
    for name in EXPECTED:
        assert "site=phase-budget-" not in _skill_text(name)


def test_block_knob_documented_at_each_guard():
    """Each of the four wirings documents BOTH the default PROCEED behavior and
    the opt-in QUOIN_PHASE_BUDGET_BLOCK stop path (both non-AUQ)."""
    for name in EXPECTED:
        region = _wiring_region(name)
        assert BUDGET_TOKEN in region, f"{name}: guard call missing from region"
        assert "QUOIN_PHASE_BUDGET_BLOCK" in region, f"{name}: block knob undocumented"
        assert "PROCEED" in region, f"{name}: default PROCEED behavior undocumented"


def test_no_new_no_interactive_parsing_in_leaf_wirings():
    """Neither /review nor /thorough_plan adds [no-interactive]/_INTERACTIVE
    parsing FOR THIS FEATURE (the non-blocking path never prompts)."""
    for name in ("review", "thorough_plan"):
        region = _wiring_region(name)
        assert "_INTERACTIVE" not in region, f"{name}: budget wiring must not parse _INTERACTIVE"


def test_run_no_interactive_injection_unchanged():
    """Regression (dropped MAJ-1/MAJ-2 wiring stays out): /run's existing
    [no-interactive] injection / /thorough_plan exclusion prose is UNCHANGED —
    the IVG-141 feature must not have touched it."""
    run_text = _skill_text("run_hard")
    assert "## Non-interactive fail-closed propagation" in run_text
    assert "`/thorough_plan` is excluded:" in run_text
