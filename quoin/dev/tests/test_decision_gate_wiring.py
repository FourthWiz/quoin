"""S-2 wiring / behavioral tests for the fail-closed decision-gate contract (IVG-150, T-16).

Verifies each of the 6 fail-closed sites documents the helper call + `[no-interactive]`
(AC-1/AC-5), the `[autonomous]` arm is evaluated before the fail-closed branch where a site
has one (AC-4), `/run` injects `[no-interactive]` on non-autonomous phase spawns only and
routes NEEDS-DECISION (AC-5), and best-effort sites stay unwired (AC-6). Reuses the census
derivation so sweep and census agree by construction.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"

# Reuse the census module's single-source derivation.
_census_spec = importlib.util.spec_from_file_location(
    "_dg_census", TESTS_DIR / "test_decision_gate_census.py"
)
_census = importlib.util.module_from_spec(_census_spec)
_census_spec.loader.exec_module(_census)

# The 6 fail-closed sites → owning skill file.
FAIL_CLOSED_SITES = {
    "garbage-files": "end_of_task",
    "commit-decision": "end_of_task",
    "archive-type": "end_of_task",
    "gate-approval": "gate",
    "branch-hygiene": "implement",
    "destructive-undo": "rollback",
    # session-age (IVG-146) is a 7th in-scope fail-closed gate
    "session-age": "end_of_task",
}


def _scope_text_for_site(skill: str, site: str) -> str:
    text = (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    stripped_lines = _census._strip_generated(text).split("\n")
    markers, _tokens = _census._genuine_decision_sites(text)
    ranges = _census._owned_ranges(markers, len(stripped_lines))
    for mk, start, stop in ranges:
        if mk["site"] == site:
            return "\n".join(stripped_lines[start:stop])
    return ""


@pytest.mark.parametrize("site,skill", sorted(FAIL_CLOSED_SITES.items()))
def test_each_fail_closed_site_documents_helper_call(site, skill):
    scope = _scope_text_for_site(skill, site)
    assert scope, f"no fail-closed marker scope found for site={site} in {skill}"
    assert "decision_gate_guard.py fail-closed" in scope, (
        f"site={site} does not reference the helper 'decision_gate_guard.py fail-closed'"
    )
    assert "[no-interactive]" in scope, f"site={site} does not mention [no-interactive]"


def test_guard_inert_when_autonomous():
    """Sites WITH an autonomous arm evaluate it BEFORE the fail-closed helper branch."""
    # end_of_task's 3 AUQ sites + session-age have an autonomous / pinned-autonomous arm.
    text = (ADAPTER_SKILLS_DIR / "end_of_task" / "SKILL.md").read_text(encoding="utf-8")
    for site in ("garbage-files", "commit-decision", "archive-type"):
        scope = _scope_text_for_site("end_of_task", site)
        auto_idx = scope.find("_AUTONOMOUS")
        helper_idx = scope.find("decision_gate_guard.py")
        assert auto_idx != -1 and helper_idx != -1, f"site={site} missing an arm"
        assert auto_idx < helper_idx, (
            f"site={site}: autonomous arm must precede the fail-closed branch"
        )
    # gate: Step 3.6 autonomous auto-approve precedes Step 4 fail-closed in document order.
    gate = (ADAPTER_SKILLS_DIR / "gate" / "SKILL.md").read_text(encoding="utf-8")
    assert gate.find("Step 3.6") < gate.find("decision_gate_guard.py fail-closed --task <task-name> --skill gate")


def test_run_injects_no_interactive_non_autonomous_only():
    run = (ADAPTER_SKILLS_DIR / "run_hard" / "SKILL.md").read_text(encoding="utf-8")
    assert "[no-interactive]" in run
    assert "Non-interactive fail-closed propagation" in run
    # thorough_plan explicitly excluded
    assert "thorough_plan" in run and "excluded" in run.lower()
    # inline gates excluded
    assert "INLINE-GATE EXCLUSION" in run
    # mutual exclusivity with [autonomous]
    assert "mutually exclusive" in run.lower()


def test_run_routes_needs_decision():
    run = (ADAPTER_SKILLS_DIR / "run_hard" / "SKILL.md").read_text(encoding="utf-8")
    assert "NEEDS-DECISION" in run
    assert "gate-result: NEEDS-DECISION" in run


def test_best_effort_sites_not_wired():
    """AC-6: best-effort sites are marked best-effort and do NOT reference the helper."""
    # enrich gap-questions is best-effort; sample end_of_task lessons-prompt too.
    for skill in ("enrich", "end_of_task", "implement"):
        text = (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        markers, _ = _census._genuine_decision_sites(text)
        best = [m for m in markers if m["cls"] == "best-effort"]
        for mk in best:
            scope = _scope_text_for_site(skill, mk["site"])
            assert "decision_gate_guard.py" not in scope, (
                f"best-effort site={mk['site']} in {skill} must NOT reference the helper"
            )


def test_existing_autonomous_coverage_unchanged():
    """AC-4: the pre-existing autonomous census + thorough_plan autonomous tests stay green."""
    for name in ("test_autonomous_askuserquestion_coverage.py", "test_thorough_plan_autonomous.py"):
        target = TESTS_DIR / name
        if not target.exists():
            pytest.skip(f"{name} not present")
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(target), "-q"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{name} regressed:\n{r.stdout[-2000:]}\n{r.stderr[-1000:]}"
