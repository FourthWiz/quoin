"""Tests for IVG-246 `run/SKILL.md` fast-path-triage prose (T-05 onward).

T-05 covers `fast:` tag parsing: the token must be documented as stripped in
the same "Parse input and determine task profile" block as `strict:` /
`small:`/`medium:`/`large:` / `max_rounds:`, ORTHOGONAL to (composable with)
the profile tags, and stripped before the derived task name — the same
non-pollution treatment already given to `--autonomous` and `strict:`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_RUN_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
_REVIEW_SKILL = _SOURCE_ROOT / "adapters" / "claude" / "skills" / "review" / "SKILL.md"


@pytest.fixture(scope="module")
def run_skill_text() -> str:
    assert _RUN_SKILL.exists(), f"run/SKILL.md not found at {_RUN_SKILL}"
    return _RUN_SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def review_skill_text() -> str:
    assert _REVIEW_SKILL.exists(), f"review/SKILL.md not found at {_REVIEW_SKILL}"
    return _REVIEW_SKILL.read_text(encoding="utf-8")


def _parse_block(text: str) -> str:
    start = text.index("### Parse input and determine task profile")
    end = text.index("### Determine task name", start)
    return text[start:end]


# ─── T-06: Phase 1.6 section — routing, modes, evidence ladder, eligibility ──

def _phase_1_6_section(text: str) -> str:
    start = text.index("## Phase 1.6 — Fast-path triage (conditional)")
    end = text.index("## Phase 2 — Architect", start)
    return text[start:end]


# ---------------------------------------------------------------------------
# T-08: fast-route plan stub emitter + triage-decision.md
# ---------------------------------------------------------------------------


def _stub_prose(text: str) -> str:
    section = _phase_1_6_section(text)
    start = section.index("Fast-route plan stub")
    end = section.index("**Ledger row.**", start)
    return section[start:end]


def test_emitted_stub_fixture_passes_validate_artifact(tmp_path) -> None:
    """A stub emitted per the contract documented above must pass the real
    validate_artifact.py invocation with exit 0 — the ack requires this, not
    just prose review."""
    import subprocess
    import sys

    validator = _SOURCE_ROOT / "core" / "scripts" / "validate_artifact.py"
    assert validator.exists(), f"validate_artifact.py not found at {validator}"

    stub = tmp_path / "current-plan.md"
    stub.write_text(
        "---\n"
        "task: example-fast-task\n"
        "source: IVG-000\n"
        "date: 2026-08-06\n"
        "status: draft\n"
        "profile: Small\n"
        "provenance: fast-path-triage\n"
        "---\n"
        "## For human\n\n"
        "No planning phase ran for this task — it was routed through fast-path triage\n"
        "and this stub was mechanically derived from the evidence's acceptance criteria.\n"
        "Status: ready for implementation. Risk: low, single-module change. Next: /implement.\n\n"
        "## State\n\n"
        "```yaml\n"
        "task: example-fast-task\n"
        "profile: Small\n"
        "Task profile: Small\n"
        "Review shape: single-pass (fast-path)\n"
        "Route: fast\n"
        "```\n\n"
        "Convergence summary: Rounds: 0.\n\n"
        "## Tasks\n\n"
        "1. ⏳ **T-01 — Example change.** `src/example.py` — acceptance: the function returns\n"
        "   the documented value.\n\n"
        "## Risks\n\n"
        "None identified — bounded, single-module change.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(validator), str(stub)],
        capture_output=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"emitted stub fixture failed validate_artifact.py: "
        f"stdout={result.stdout.decode('utf-8', 'replace')} "
        f"stderr={result.stderr.decode('utf-8', 'replace')}"
    )


# ---------------------------------------------------------------------------
# T-09: fast-route skip conditions and .done writes for architect/thorough_plan
# ---------------------------------------------------------------------------


def _phase_span(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


# ---------------------------------------------------------------------------
# T-10: third branch on the formulation quality bar
# ---------------------------------------------------------------------------


def _formulation_bar_section(text: str) -> str:
    return _phase_span(
        text,
        "## Formulation quality bar (autonomous)",
        "## Phase 4 — Implement",
    )


# ---------------------------------------------------------------------------
# T-11: Opus /implement at all three spawn sites + route-conditional ledger
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-12: `Review shape:` precedence channel in /review/SKILL.md
# ---------------------------------------------------------------------------


def _profile_detection_section(text: str) -> str:
    return _phase_span(
        text,
        "## Profile detection and fan-out",
        "**Required-section ownership**",
    )


# ---------------------------------------------------------------------------
# T-13: mid-flight escalation — Checkpoint C + the two Phase 5 terminal branches
# ---------------------------------------------------------------------------


def _checkpoint_c_section(text: str) -> str:
    return _phase_span(text, "**Checkpoint C:**", "## Phase 5 — Review")


def _phase5_section(text: str) -> str:
    return _phase_span(text, "## Phase 5 — Review", "**Checkpoint D**")


# ---------------------------------------------------------------------------
# T-14: D-12a carve-outs — "orchestrate, don't perform" named exception
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-17: full-path near-bit-identity — the single most important new test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-18: Resume Step 0b — route recovery
# ---------------------------------------------------------------------------


def _resume_section(text: str) -> str:
    # "## Resume" also appears as an inline mention (e.g. "in `## Resume` below.")
    # before the real heading — find the REAL heading (line-anchored) explicitly.
    real_heading = text.index("\n## Resume\n")
    end = text.index("## Session state tracking", real_heading)
    return text[real_heading:end]


# ---------------------------------------------------------------------------
# Review-fix round 1 (IVG-246 review-1.md, CRITICAL 1 + MAJOR 1/2/3/4/5/7/8/9
# + MINOR 10/11/12/13/14/16/17/18/19/20/21/22)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review-fix round 2 (IVG-246 review-2.md MAJOR 1-7 + supervisor coverage gap)
# ---------------------------------------------------------------------------


