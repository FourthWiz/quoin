"""IVG-111 stage 3, T-07: structural exactly-one-writer guard for on-behalf cost capture.

Covers:
  - HARD assertion (the sole population invariant, D-5/D-11): self_writers ==
    onbehalf_skip, where both sides are derived by filesystem census of the
    co-located `<!-- quoin:ledger-self-write -->` sentinel and the distinctive
    `SKIP this cost-ledger self-write` predicate phrase installed by T-06. No
    hand-maintained skill roster or frozen exclusion list survives here — rounds
    1-2 hand-listed the self-writer population (9, then 15 + a frozen exclusion
    of 6) and both were wrong on the live tree. This round closes the class
    structurally: the sentinel IS the population.
  - Orchestrator side: `architect`/`thorough_plan`/`run` are the closed T-03/T-04/
    T-05 deliverable (a legitimate literal — a fixed set of files this very plan
    modifies, not a growing/guessed population) and must carry the on-behalf CLI
    invocation + the `QUOIN_INLINE_COST_CAPTURE` flag gate.
  - WARN-only drift backstop (never a hard assertion — see D-5/D-11): a broad
    regex finds candidate self-write mentions; anything sentineled-but-not-a-
    candidate or candidate-but-not-sentineled is a human-classification signal,
    not a test failure. Exercised via `pytest.warns` on synthetic fixtures per
    the round-3 critic MIN-2 note (`warnings.warn` alone does not fail default
    pytest, so the backstop needs an explicit assertion that it actually fires).
  - Mutation fixtures prove the guard is load-bearing: sentinel-without-predicate
    and predicate-without-sentinel both FAIL set-equality; an unstamped
    "append ... cost ledger" mention only WARNs.

RG-CENSUS safety: every skill-name collection here is a FUNCTION-LOCAL lowercase
variable (never a module-level ALL-CAPS roster), per the plan's explicit
instruction — check_registration.py's `_discover_rosters` heuristic only flags
module-level ALL-CAPS assigns whose members are a subset of CANONICAL_SKILLS.
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_DEV_DIR = _TESTS_DIR.parent
_PKG_DIR = _DEV_DIR.parent  # quoin/quoin/ (the inner package root)
SKILLS_DIR = _PKG_DIR / "adapters" / "claude" / "skills"

SENTINEL = "<!-- quoin:ledger-self-write -->"
PREDICATE_PHRASE = "SKIP this cost-ledger self-write"
ORCH_TOKEN = "agent_transcript_cost.py"
CANDIDATE_RE = re.compile(r"append[^\n]{0,60}cost.?ledger", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Reusable census helpers — parameterized by directory so they can run against
# both the live tree and synthetic tmp_path fixtures.
# ---------------------------------------------------------------------------

def _census(skills_dir: Path, predicate) -> set[str]:
    found = set()
    for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        if predicate(text):
            found.add(path.parent.name)
    return found


def _self_writers(skills_dir: Path) -> set[str]:
    return _census(skills_dir, lambda t: SENTINEL in t)


def _onbehalf_skip(skills_dir: Path) -> set[str]:
    return _census(skills_dir, lambda t: PREDICATE_PHRASE in t)


def _candidates(skills_dir: Path) -> set[str]:
    return _census(skills_dir, lambda t: CANDIDATE_RE.search(t) is not None)


def assert_writer_set_equality(skills_dir: Path) -> set[str]:
    """The sole hard population invariant. Returns self_writers on success."""
    self_writers = _self_writers(skills_dir)
    onbehalf_skip = _onbehalf_skip(skills_dir)
    assert self_writers == onbehalf_skip, (
        f"asymmetric drift: sentinel-only={sorted(self_writers - onbehalf_skip)}, "
        f"predicate-only={sorted(onbehalf_skip - self_writers)}"
    )
    return self_writers


def warn_drift_backstop(skills_dir: Path) -> None:
    """WARN-only residual — NO assertion on membership or count (D-11)."""
    self_writers = _self_writers(skills_dir)
    candidates = _candidates(skills_dir)
    for name in sorted(self_writers - candidates):
        warnings.warn(
            f"{name}: sentineled but not a broad-regex match — widen regex or "
            "check sentinel placement",
            stacklevel=1,
        )
    for name in sorted(candidates - self_writers):
        warnings.warn(
            f"{name}: mentions append+cost-ledger but no sentinel — classify at "
            "review (add sentinel if genuine self-writer, else prose ref)",
            stacklevel=1,
        )


def _write_skill(base: Path, name: str, body: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Live-tree assertions
# ---------------------------------------------------------------------------

def test_self_writers_equal_onbehalf_skip_live_tree():
    self_writers = assert_writer_set_equality(SKILLS_DIR)
    assert self_writers, "expected at least one genuine self-writer on the live tree"


def test_orchestrators_are_exactly_the_scoped_four():
    """RHS is the scoped orchestrators — a closed literal, not an open-world census
    (D-5). /review joined the architect/thorough_plan/run set because its parallel
    review fan-out now writes on-behalf cost rows. Triple-membership with
    self_writers/onbehalf_skip is intentional: the asserts operate on independent
    tokens (D-9)."""
    orchestrators = _census(SKILLS_DIR, lambda t: ORCH_TOKEN in t)
    assert orchestrators == {"architect", "thorough_plan", "review", "run"}
    for name in orchestrators:
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "QUOIN_INLINE_COST_CAPTURE" in text, f"{name}: missing flag gate"


def test_self_writers_document_marker_strip():
    self_writers = _self_writers(SKILLS_DIR)
    for name in sorted(self_writers):
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "strip" in text.lower() and "[quoin-onbehalf]" in text, (
            f"{name}: missing marker-strip documentation"
        )


def test_broad_regex_drift_backstop_live_tree_visible(recwarn):
    """Exercise the backstop against the real tree. Any residual (e.g. a self-writer
    phrased outside the 60-char regex window, or a candidate classified as prose-only)
    is surfaced for human review, never asserted on — that IS the enumeration-class
    closure (D-11). This test only proves the mechanism runs cleanly, not a count."""
    warn_drift_backstop(SKILLS_DIR)
    for w in recwarn.list:
        print(f"[drift-backstop] {w.message}")


# ---------------------------------------------------------------------------
# Mutation fixtures — prove the guard is load-bearing (T-07 acceptance)
# ---------------------------------------------------------------------------

def test_mutation_sentinel_without_predicate_fails_equality(tmp_path):
    _write_skill(
        tmp_path,
        "fakeskill",
        f"## Session bootstrap\n\nAppend your session to the cost ledger.\n{SENTINEL}\n",
    )
    with pytest.raises(AssertionError):
        assert_writer_set_equality(tmp_path)


def test_mutation_predicate_without_sentinel_fails_equality(tmp_path):
    _write_skill(
        tmp_path,
        "fakeskill",
        f"## Session bootstrap\n\n{PREDICATE_PHRASE}\n\nAppend your session to the cost ledger.\n",
    )
    with pytest.raises(AssertionError):
        assert_writer_set_equality(tmp_path)


def test_mutation_both_tokens_present_passes_equality(tmp_path):
    _write_skill(
        tmp_path,
        "fakeskill",
        f"## Session bootstrap\n\n{PREDICATE_PHRASE}\n\nAppend your session to the "
        f"cost ledger.\n{SENTINEL}\n",
    )
    self_writers = assert_writer_set_equality(tmp_path)
    assert self_writers == {"fakeskill"}


def test_mutation_unstamped_candidate_warns_not_fails(tmp_path):
    """A fixture with an 'append ... cost ledger' mention and no sentinel produces a
    WARN naming the file, never a hard failure (this is the enumeration-class fix —
    a human classifies it, CI does not silently freeze it into a roster)."""
    _write_skill(
        tmp_path,
        "fakeskill",
        "## Session bootstrap\n\nAppend your session to the cost ledger somewhere.\n",
    )
    # Set-equality holds trivially (both sides empty) — no assertion failure.
    self_writers = assert_writer_set_equality(tmp_path)
    assert self_writers == set()

    with pytest.warns(UserWarning, match="fakeskill"):
        warn_drift_backstop(tmp_path)
