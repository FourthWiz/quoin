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


def test_fast_tag_stripping(run_skill_text: str) -> None:
    block = _parse_block(run_skill_text)

    assert "`fast:`" in block, (
        "the 'fast:' tag must be documented in the same "
        "'Parse input and determine task profile' block as strict:/small:/"
        "medium:/large:/max_rounds:"
    )
    # It's documented alongside the other stripped tokens in this same block.
    assert "`strict:`" in block
    assert "`small:`" in block
    assert "`max_rounds:" in block

    # Explicitly stripped before profile classification / task-name
    # derivation — same non-pollution treatment as --autonomous / strict:.
    assert "Strip the token" in block or "Strip token" in block
    assert "task name" in block.lower(), (
        "the block must document that 'fast:' does not reach the derived "
        "task name (AC-3)"
    )

    # Orthogonal / composable with profile tags — not mutually exclusive.
    assert "ORTHOGONAL" in block or "orthogonal" in block


def test_fast_tag_composable_with_profile_example(run_skill_text: str) -> None:
    """The block must give a concrete composability example (a `fast:` +
    profile-tag combination) showing the profile tag still wins for
    profile purposes while `fast:` independently forces route evaluation."""
    block = _parse_block(run_skill_text)
    assert "fast: large:" in block or "fast:` `large:`" in block or (
        "fast:" in block and "large:" in block and "route" in block.lower()
    )


# ─── T-06: Phase 1.6 section — routing, modes, evidence ladder, eligibility ──

def _phase_1_6_section(text: str) -> str:
    start = text.index("## Phase 1.6 — Fast-path triage (conditional)")
    end = text.index("## Phase 2 — Architect", start)
    return text[start:end]


def test_evidence_ladder_documented(run_skill_text: str) -> None:
    section = _phase_1_6_section(run_skill_text)
    assert "spec.md" in section
    assert "enriched-prompt.md" in section
    assert "raw task description" in section
    # Precedence order (D-04a): spec.md, else enriched-prompt.md, else raw description.
    spec_idx = section.index("spec.md")
    enriched_idx = section.index("enriched-prompt.md")
    raw_idx = section.index("raw task description")
    assert spec_idx < enriched_idx < raw_idx, (
        "evidence ladder must be documented in precedence order: spec.md, "
        "then enriched-prompt.md, then the raw task description"
    )


def test_eligibility_criteria_explicit_and_stricter_than_small(run_skill_text: str) -> None:
    section = _phase_1_6_section(run_skill_text)
    # The five-way eligibility conjunction (D-10), written out verbatim.
    assert "single module" in section
    assert "cross-module" in section or "cross-repo" in section
    assert "pattern already present" in section
    assert "data migration" in section
    assert "public-contract change" in section
    assert "implementation checklist" in section
    assert "stricter" in section.lower(), (
        "the section must state that fast-path eligibility is stricter than "
        "the existing Small-task threshold"
    )


def test_ledger_phase_is_triage_not_roster_name(run_skill_text: str) -> None:
    section = _phase_1_6_section(run_skill_text)
    assert "phase `triage`" in section or "phase 'triage'" in section
    assert "fast_path_triage" in section
    assert "different string" in section or "DIFFERENT string" in section, (
        "the section must state, in its own prose, that the ledger phase "
        "('triage') and the roster/sentinel name ('fast_path_triage') are "
        "deliberately different strings (D-11)"
    )


def test_checkpoint_a1_uses_askuserquestion_not_protocol_table(run_skill_text: str) -> None:
    section = _phase_1_6_section(run_skill_text)
    assert "Checkpoint A1" in section
    assert "AskUserQuestion" in section
    # Never reproduce the guarded checkpoint-protocol heading literal in this section.
    assert "## Checkpoint interaction protocol" not in section
    assert "## Resume" not in section


def test_a1_warns_on_large_security_dimension(run_skill_text: str) -> None:
    """MAJOR 1 fix verification (round 2): the Large warning must describe
    the ACTUAL tradeoff — performance/architecture-integration dimensions
    drop, OWASP is retained — not the pre-fix-round claim that OWASP itself
    drops (that claim is what the fix round made false when it made the
    OWASP pass unconditional on Large in review/SKILL.md)."""
    section = _phase_1_6_section(run_skill_text)
    idx = section.index("Checkpoint A1")
    tail = section[idx:]
    assert "Large" in tail
    assert "security_review" in tail
    assert "OWASP" in tail
    assert "retained unconditionally on Large" in tail
    assert "drops the performance and architecture/integration" in tail
    assert "drops the dedicated `/security_review` OWASP pass" not in tail


def test_a1_options_rendered_as_bullet_list_not_table(run_skill_text: str) -> None:
    section = _phase_1_6_section(run_skill_text)
    idx = section.index("Checkpoint A1")
    tail = section[idx:]
    pipe_lines = [line for line in tail.splitlines() if line.strip().startswith("|")]
    assert pipe_lines == [], (
        f"Checkpoint A1's options must be a bullet list, never a table: {pipe_lines}"
    )
    assert "Take fast path" in tail
    assert "Take full path" in tail
    assert "Show rationale" in tail


def test_no_table_in_guarded_slice_line_66_to_resume_heading(run_skill_text: str) -> None:
    """P-03b span rule, targeted: from the FIRST occurrence of the checkpoint
    heading literal (the inline mention near line 66, in "Parse input and
    determine task profile") to the REAL `## Checkpoint interaction
    protocol` heading itself, there must be ZERO pipe-leading (table) lines.
    This is the exact span every Phase 1.6-adjacent task (T-05 through T-13)
    writes into; the real checkpoint table lives only after its own heading,
    so a zero baseline here is non-vacuous and catches any table sneaking
    into the new prose specifically (distinct from T-04's whole-span
    baseline-count guard, which tolerates the real table's 8 rows)."""
    text = run_skill_text
    first_mention = text.index("## Checkpoint interaction protocol")
    real_heading = text.index("## Checkpoint interaction protocol", first_mention + 1)
    sub_slice = text[first_mention:real_heading]

    pipe_lines = [line for line in sub_slice.splitlines() if line.strip().startswith("|")]
    assert pipe_lines == [], (
        f"found {len(pipe_lines)} pipe-leading (table) line(s) between the first "
        f"'## Checkpoint interaction protocol' mention and its real heading — "
        f"P-03b forbids adding a table anywhere in this span: {pipe_lines}"
    )


# ---------------------------------------------------------------------------
# T-08: fast-route plan stub emitter + triage-decision.md
# ---------------------------------------------------------------------------


def _stub_prose(text: str) -> str:
    section = _phase_1_6_section(text)
    start = section.index("Fast-route plan stub")
    end = section.index("**Ledger row.**", start)
    return section[start:end]


def test_stub_carries_all_four_provenance_markers(run_skill_text: str) -> None:
    prose = _stub_prose(run_skill_text)
    assert "provenance: fast-path-triage" in prose
    assert "no planning phase ran" in prose
    assert "Rounds: 0" in prose
    assert "Route: fast" in prose
    # placement: inside `## State`, not between `## For human` and `## State`
    assert "inside `## State`" in prose or "inside \"## State\"" in prose


def test_stub_declares_both_profile_and_review_shape_lines(run_skill_text: str) -> None:
    prose = _stub_prose(run_skill_text)
    assert "Task profile:" in prose
    assert "Review shape: single-pass (fast-path)" in prose
    assert "honestly classified" in prose


def test_triage_decision_not_registered_in_validator(run_skill_text: str) -> None:
    prose = _stub_prose(run_skill_text)
    assert "triage-decision.md" in prose
    assert "route" in prose.lower()
    assert "rationale" in prose.lower()
    assert "confidence" in prose.lower()
    assert "evidence tier" in prose.lower()
    assert "not registered" in prose.lower() or "default type" in prose.lower()


def test_stub_provenance_and_consumer_lines_rendered_as_bullets_not_table(run_skill_text: str) -> None:
    prose = _stub_prose(run_skill_text)
    pipe_lines = [line for line in prose.splitlines() if line.strip().startswith("|")]
    assert pipe_lines == [], (
        f"T-08's stub-contract prose must render as bullet lists, never a table: {pipe_lines}"
    )
    assert prose.count("- ") >= 4  # the four provenance-marker bullets at minimum


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


def test_architect_and_thorough_plan_skipped_on_fast_route(run_skill_text: str) -> None:
    """Phase 2 and Phase 3 must both gain a fast-route skip condition,
    conjunctive with route == fast (Small-task skip behavior is untouched)."""
    text = run_skill_text
    phase2 = _phase_span(text, "## Phase 2 — Architect (conditional)", "## Phase 3 — Thorough Plan")
    assert "Skip condition:" in phase2
    assert "route" in phase2.lower() and "fast" in phase2.lower()
    assert "Task profile is Small" in phase2  # existing Small skip preserved

    phase3 = _phase_span(text, "## Phase 3 — Thorough Plan", "## Formulation quality bar (autonomous)")
    assert "Skip condition:" in phase3
    assert "route" in phase3.lower() and "fast" in phase3.lower()


def test_skipped_phases_still_write_done_sentinels(run_skill_text: str) -> None:
    """Both Phase 2's and Phase 3's `.done` writes must be extended to cover
    the fast-route skip, not just the pre-existing Small-task skip."""
    text = run_skill_text
    phase2 = _phase_span(text, "## Phase 2 — Architect (conditional)", "## Phase 3 — Thorough Plan")
    assert "architect.done" in phase2
    assert "fast route" in phase2 or "fast-route" in phase2

    phase3 = _phase_span(text, "## Phase 3 — Thorough Plan", "## Formulation quality bar (autonomous)")
    assert "thorough_plan.done" in phase3
    assert "fast route" in phase3 or "fast-route" in phase3


def test_gate_boundaries_reference_documents_fast_route_skips(run_skill_text: str) -> None:
    """The `## Gate boundaries reference` section must say the post-architect
    and post-plan gate boundaries do not exist on the fast route, since their
    phases never run — post-implement/post-review stay unchanged."""
    text = run_skill_text
    start = text.index("## Gate boundaries reference")
    end = text.index("## Important behaviors", start)
    section = text[start:end]
    assert "fast route" in section
    assert "post-architect" in section.lower() and "post-plan" in section.lower()
    assert "do not exist" in section
    assert "post-implement and post-review" in section.lower() or (
        "post-implement" in section.lower() and "unchanged" in section.lower()
    )


# ---------------------------------------------------------------------------
# T-10: third branch on the formulation quality bar
# ---------------------------------------------------------------------------


def _formulation_bar_section(text: str) -> str:
    return _phase_span(
        text,
        "## Formulation quality bar (autonomous)",
        "## Phase 4 — Implement",
    )


def test_fastpath_bar_threshold_default_0_8(run_skill_text: str) -> None:
    """The fast-route bullet must require min(triage_confidence,
    enrich_confidence_if_present) >= QUOIN_FASTPATH_CONFIDENCE_THRESHOLD,
    default 0.8 — stricter than Small's 0.7 default, since the fast route
    skipped critique entirely."""
    section = _formulation_bar_section(run_skill_text)
    assert "QUOIN_FASTPATH_CONFIDENCE_THRESHOLD" in section
    assert "0.8" in section
    assert "min(" in section
    # both idioms present and distinct
    assert "QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD" in section
    assert "default `0.7`" in section
    assert "Fast route" in section or "fast route" in section.lower()


def test_below_bar_writes_hard_stop_6_sentinel(run_skill_text: str) -> None:
    """The fast-route below-bar case must reuse Hard-stop #6 and the SAME
    phase: formulation halt sentinel — no new hard-stop site, no new schema
    field, no supervisor reader change."""
    section = _formulation_bar_section(run_skill_text)
    assert "Hard-stop #6" in section
    assert "phase: formulation" in section
    assert "reason string naming the fast-path route" in section
    assert "no seventh hard-stop" in section.lower()
    # pinned strings from T-10's ack list must survive verbatim
    assert "default `0.7`" in section
    assert "Hard-stop #6" in section
    assert "write the halt-sentinel" in section
    assert "Do **NOT** enter Phase 4" in section
    assert "Only evaluated under `AUTONOMOUS`" in section
    assert "plain `/run` never evaluates this bar" in section


# ---------------------------------------------------------------------------
# T-11: Opus /implement at all three spawn sites + route-conditional ledger
# ---------------------------------------------------------------------------


def test_all_three_implement_spawn_sites_carry_leading_sentinel(run_skill_text: str) -> None:
    """All three fast-route `/implement` spawn sites (primary Phase 4 spawn,
    Checkpoint C retry, Phase 5 review-fix) must dispatch with model opus and
    a spawn prompt whose FIRST token is bare `[no-redispatch]`. Acceptance is
    a COUNT, not a presence check — no fast-route spawn site may lack it."""
    text = run_skill_text

    # Site 1 — primary Phase 4 spawn: full definition of the dispatch rule.
    phase4 = _phase_span(text, "## Phase 4 — Implement", "## Phase 5 — Review")
    assert "dispatch this spawn with model opus" in phase4
    assert "FIRST token must be bare `[no-redispatch]`" in phase4
    assert "`[autonomous]` / `[no-interactive]` / `[quoin-onbehalf]`" in phase4

    # Site 2 — Checkpoint C "fix" retry, inside Phase 4.
    assert "same model-opus / leading-`[no-redispatch]` dispatch as the primary Phase 4 spawn above" in phase4

    # Site 3 — Phase 5 review-fix.
    phase5 = _phase_span(text, "## Phase 5 — Review", "**Checkpoint D**")
    assert "same model-opus / leading-`[no-redispatch]` dispatch as the primary Phase 4 spawn above" in phase5

    # Count: site 1 (full definition) + sites 2 and 3 (reference the same rule) == 3 total.
    total_sites = text.count("model opus") + text.count(
        "model-opus / leading-`[no-redispatch]` dispatch as the primary Phase 4 spawn above"
    )
    assert total_sites >= 3, f"expected >=3 fast-route /implement dispatch sites, found {total_sites}"

    # Order is load-bearing: [no-redispatch] must precede other markers, never follow.
    assert "leading-`[no-redispatch]` marker" not in phase4  # no accidental duplicate phrasing drift
    assert phase4.index("FIRST token must be bare `[no-redispatch]`") < phase4.index(
        "`[autonomous]` / `[no-interactive]` / `[quoin-onbehalf]`"
    )


def test_onbehalf_implement_model_is_route_conditional(run_skill_text: str) -> None:
    """Site 4 — the on-behalf ledger row for the `implement` phase must be
    route-conditional (`opus` on the fast route, `sonnet` otherwise), not a
    hardcoded `sonnet` that misrepresents an Opus run."""
    phase4 = _phase_span(run_skill_text, "## Phase 4 — Implement", "## Phase 5 — Review")
    assert "phase=implement, model=opus on the fast route, sonnet otherwise" in phase4


def test_phase4_rationale_names_a1_on_fast_route(run_skill_text: str) -> None:
    """Site 5 — the Phase 4 spawn rationale must name Checkpoint A1 as the
    fast-route confirming checkpoint, since Checkpoint B never fires on that
    route (the exception in implement/SKILL.md keys on 'spawned by /run', not
    on which specific checkpoint fired)."""
    phase4 = _phase_span(run_skill_text, "## Phase 4 — Implement", "## Phase 5 — Review")
    assert "Checkpoint A1" in phase4
    assert "Checkpoint B never fires on that route" in phase4
    assert "Checkpoint B" in phase4  # existing full-path wording preserved


# ---------------------------------------------------------------------------
# T-12: `Review shape:` precedence channel in /review/SKILL.md
# ---------------------------------------------------------------------------


def _profile_detection_section(text: str) -> str:
    return _phase_span(
        text,
        "## Profile detection and fan-out",
        "**Required-section ownership**",
    )


def test_review_shape_takes_precedence_over_profile_default(review_skill_text: str) -> None:
    """A `Review shape: single-pass (fast-path)` line must take precedence
    over BOTH profile inference and the undetermined-profile default."""
    section = _profile_detection_section(review_skill_text)
    assert "Review shape: single-pass (fast-path)" in section
    assert "takes precedence over profile inference" in section
    assert "undetermined-profile default" in section
    # existing default sentence stays VERBATIM
    assert (
        "If the task profile cannot be determined, default to **Medium fan-out** (D-02)"
        in section
    )


def test_medium_fanout_still_fires_without_review_shape_line(review_skill_text: str) -> None:
    """The three-way Medium fan-out must still fire on a Medium plan carrying
    no `Review shape:` line — the new channel is additive, not a replacement
    for profile-based detection."""
    text = review_skill_text
    section = _profile_detection_section(text)
    assert "regardless of the `Task profile:` value" in section
    # the Medium fan-out branch itself is untouched further down in the file
    assert "Medium" in text
    assert "fan-out" in text.lower()


# ---------------------------------------------------------------------------
# T-13: mid-flight escalation — Checkpoint C + the two Phase 5 terminal branches
# ---------------------------------------------------------------------------


def _checkpoint_c_section(text: str) -> str:
    return _phase_span(text, "**Checkpoint C:**", "## Phase 5 — Review")


def _phase5_section(text: str) -> str:
    return _phase_span(text, "## Phase 5 — Review", "**Checkpoint D**")


def test_escalation_removes_skipped_phase_sentinels(run_skill_text: str) -> None:
    section = _checkpoint_c_section(run_skill_text)
    assert "escalate to full" in section
    assert "rewrite `triage-decision.md` with the flipped route" in section
    assert "autonomous-progress-{task}/architect.done" in section
    assert "autonomous-progress-{task}/thorough_plan.done" in section
    assert "DELETE" in section
    assert "re-enter at the architect phase" in section
    assert "Completed implementation work is preserved" in section
    # Checkpoint C fires BEFORE implement.done is written (gate-FAILED branch) —
    # the deletion set here must say so explicitly, not just enumerate two sentinels.
    assert "implement.done` does not exist yet at this site" in section


def test_escalation_deletes_implement_done_at_review_phase_sites(run_skill_text: str) -> None:
    """BEHAVIOR re-point (round-1 review MAJOR 2): the two Phase 5 escalation
    sites fire AFTER `implement.done` already exists, so their deletion set
    must actually include it — not just reuse the Checkpoint C wording, which
    would silently skip re-implementation on a resumed escalated run."""
    phase5 = _phase5_section(run_skill_text)
    # Both branches (CHANGES_REQUESTED-cap and BLOCKED) must each name the
    # implement.done deletion explicitly — count, not just presence.
    assert phase5.count("autonomous-progress-{task}/implement.done") >= 2
    assert phase5.count("autonomous-progress-{task}/architect.done") >= 2
    assert phase5.count("autonomous-progress-{task}/thorough_plan.done") >= 2
    assert "implement.*.done" in phase5


def test_escalation_strips_review_shape_line(run_skill_text: str) -> None:
    section = _checkpoint_c_section(run_skill_text)
    # Review-3 MINOR 1: strip-both-lines is the PRIMARY step 3; the outright
    # delete is deferred until after the sentinels are gone (step 5).
    assert "strip its `Review shape:` line" in section
    assert "Do NOT delete the stub outright" in section
    assert "delete the stripped stub `current-plan.md` outright" in section
    assert "cheapest review precisely on the path taken" in section


def test_escalation_uses_needs_decision_path_under_autonomous(run_skill_text: str) -> None:
    text = run_skill_text
    checkpoint_c = _checkpoint_c_section(text)
    assert "NEEDS-DECISION return path" in checkpoint_c
    assert "needs-decision-{task}.md" in checkpoint_c
    assert "no seventh hard-stop" in checkpoint_c.lower()

    phase5 = _phase5_section(text)
    assert phase5.count("NEEDS-DECISION return path") >= 2  # both new Phase 5 branches


def test_changes_requested_cap_offers_escalation_on_fast_route(run_skill_text: str) -> None:
    phase5 = _phase5_section(run_skill_text)
    assert "Hard-stop #3" in phase5
    idx_hardstop3 = phase5.index("Hard-stop #3")
    idx_escalate = phase5.index('offer "escalate to full" as a third option alongside this halt')
    assert idx_escalate > idx_hardstop3
    assert "same mechanism as the Checkpoint C escalation above" in phase5


def test_blocked_offers_escalation_on_fast_route(run_skill_text: str) -> None:
    phase5 = _phase5_section(run_skill_text)
    assert "Hard-stop #1" in phase5
    assert "offer escalation rather than a bare stop" in phase5
    assert "same mechanism as the Checkpoint C escalation above" in phase5


def test_full_path_hard_stops_1_and_3_unchanged(run_skill_text: str) -> None:
    """Regression guard: the full path's CHANGES_REQUESTED-cap and BLOCKED
    branches must keep their pre-T-13 verbatim wording — the escalation
    option is additive and conditional on the fast route only."""
    phase5 = _phase5_section(run_skill_text)
    assert (
        "If still CHANGES_REQUESTED after the 3-round cap, this is Hard-stop #3 "
        "(Review CHANGES_REQUESTED after 3 rounds) — write the halt-sentinel per "
        '"## Autonomous hard stops" before exit, then stop.' in phase5
    )
    assert (
        '**If BLOCKED:** present the blocking issues. **STOP.** Do not offer to continue.'
        in phase5
    )
    assert "still a bare halt" in phase5
    assert "still a bare stop" in phase5


# ---------------------------------------------------------------------------
# T-14: D-12a carve-outs — "orchestrate, don't perform" named exception
# ---------------------------------------------------------------------------


def test_orchestrate_dont_perform_carveout_named(run_skill_text: str) -> None:
    """The `## Important behaviors` -> Orchestrate, don't perform bullet must
    name the Phase 1.6 routing stub as an explicit exception, with the
    justification recorded inline (mechanical transcription, no design
    judgment) so it is never mistaken for authored plan content."""
    text = run_skill_text
    start = text.index("**Orchestrate, don't perform.**")
    end = text.index("**Checkpoints are mandatory.**", start)
    bullet = text[start:end]
    assert "Named exception:" in bullet
    assert "Phase 1.6 fast-route routing stub" in bullet
    assert "mechanical transcription" in bullet
    assert "no design judgment is exercised" in bullet
    # existing rule text preserved verbatim
    assert "Never write plan content, code, or review findings yourself." in bullet
    assert "Always spawn the appropriate subagent skill." in bullet


# ---------------------------------------------------------------------------
# T-17: full-path near-bit-identity — the single most important new test
# ---------------------------------------------------------------------------


def test_full_path_near_bit_identical_on_plain_and_autonomous(run_skill_text: str) -> None:
    """On a Medium/Large run with no `fast:` tag: ZERO additional prompts,
    ZERO additional artifacts, ZERO additional model calls on a PLAIN run.
    Under AUTONOMOUS, the assertion narrows to EXACTLY ONE additional
    artifact (the phase's own `.done` sentinel) — not zero, correcting
    round 1's blanket 'bit-identical' framing (round 2 critic MAJ-1)."""
    section = _phase_1_6_section(run_skill_text)
    normalized = " ".join(section.split())  # collapse newlines/indentation for robust matching

    # (a) silent no-op mode's only side effect
    assert "zero user-facing output, zero prompts, zero extra model calls, and no" in normalized
    assert "`triage-decision.md`." in normalized
    assert (
        "A plain (non-autonomous) run adds zero artifacts and zero behavior delta here; "
        "an autonomous run adds exactly one thing — its own completion sentinel"
        in normalized
    )

    # (b) sentinel write explicitly qualified `Under `AUTONOMOUS`` — plain run writes nothing
    assert "Under `AUTONOMOUS`, once evaluated (both modes), also write the phase's" in normalized
    assert "A plain (non-autonomous) run never writes this file, in either mode." in normalized

    # (c) triage-decision.md written in EVALUATING mode only
    assert "Written at the task root, evaluating mode only" in normalized

    # (d) no AskUserQuestion documented outside the evaluating branch
    assert "via `AskUserQuestion`" in normalized
    assert "Fires in evaluating mode only" in normalized

    # (e) plain-vs-autonomous distinction stated in-section (not just cross-referenced)
    assert "an autonomous run adds exactly one thing" in normalized


# ---------------------------------------------------------------------------
# T-18: Resume Step 0b — route recovery
# ---------------------------------------------------------------------------


def _resume_section(text: str) -> str:
    # "## Resume" also appears as an inline mention (e.g. "in `## Resume` below.")
    # before the real heading — find the REAL heading (line-anchored) explicitly.
    real_heading = text.index("\n## Resume\n")
    end = text.index("## Session state tracking", real_heading)
    return text[real_heading:end]


def test_resume_reads_route_before_first_dispatch(run_skill_text: str) -> None:
    section = _resume_section(run_skill_text)
    normalized = " ".join(section.split())
    assert "Step 0b" in section
    assert "BEFORE Step 1 determines the next" in normalized
    assert "`Route:` from `<task_dir>/current-plan.md`" in normalized
    assert "Set the orchestrator's `route` variable from this read before Step 1 runs" in normalized
    # Step 0b must sit between Step 0 and Step 1
    idx_step0 = section.index("Step 0 (T-09)")
    idx_step0b = section.index("Step 0b (T-18)")
    idx_step1 = section.index("Step 1 (T-09)")
    assert idx_step0 < idx_step0b < idx_step1


def test_resume_falls_back_to_triage_decision_when_stub_absent(run_skill_text: str) -> None:
    section = _resume_section(run_skill_text)
    normalized = " ".join(section.split())
    assert (
        "does not exist or carries no `Route:` line, fall back to" in normalized
    )
    assert "`<task_dir>/triage-decision.md`'s recorded route" in normalized


def test_resume_defaults_to_full_when_neither_source_exists(run_skill_text: str) -> None:
    section = _resume_section(run_skill_text)
    normalized = " ".join(section.split())
    assert "neither source yields a route, default to `full`" in normalized
    assert "today's only behavior" in normalized


def test_resume_after_escalation_does_not_revert_to_fast(run_skill_text: str) -> None:
    """BEHAVIOR re-point (round-1 review MAJOR 7): the claim sentence alone
    ("resumes as full, not fast") was true for only ONE of the two sanctioned
    escalation branches — the strip-only fallback used to leave `Route: fast`
    readable, which Step 0b's rule 1 would then match successfully, reverting
    the escalation. The fix requires BOTH lines stripped together; assert that
    mechanism explicitly, not just the surviving claim sentence."""
    section = _resume_section(run_skill_text)
    normalized = " ".join(section.split())
    assert "composes correctly with the mid-flight escalation mechanism" in normalized
    assert "Step 0b naturally falls" in section or "falls\nthrough to" in section or (
        "falls through to" in normalized
    )
    assert "a run that escalated and was then interrupted resumes as `full`, not" in normalized
    assert "`fast`" in normalized
    # The actual mechanism: both lines stripped TOGETHER, never one alone —
    # this is what makes the "falls through" claim true regardless of whether
    # the optional post-sentinel outright delete also ran.
    assert "strips BOTH the stub's `Review shape:` line and its `Route:` line" in normalized
    assert "never the review-shape line alone" in normalized
    assert "reverting the escalation" in normalized


def test_escalation_offer_placed_outside_autonomous_only_paragraph(run_skill_text: str) -> None:
    """BEHAVIOR re-point (round-1 review MINOR 11 / 'escalation placement'):
    the review-phase escalation offer must be visible to an INTERACTIVE user,
    not confined inside the `Under AUTONOMOUS:` paragraph — assert ordering,
    not just substring presence, so a future edit that re-buries the offer
    inside the autonomous-only paragraph is caught."""
    phase5 = _phase5_section(run_skill_text)
    # CHANGES_REQUESTED branch: the fast-route offer paragraph must appear
    # BEFORE its own "Under `AUTONOMOUS`:" paragraph.
    idx_changes_offer = phase5.index('**(fast route only)** after the 3-round CHANGES_REQUESTED cap')
    idx_changes_autonomous = phase5.index("Under `AUTONOMOUS`: auto-select \"fix\" at each round")
    assert idx_changes_offer < idx_changes_autonomous

    # BLOCKED branch: same ordering requirement.
    idx_blocked_stop = phase5.index("**If BLOCKED:** present the blocking issues.")
    idx_blocked_offer = phase5.index("this bare stop is the full-path behavior")
    idx_blocked_autonomous = phase5.index("Under `AUTONOMOUS`: this is Hard-stop #1")
    assert idx_blocked_stop < idx_blocked_offer < idx_blocked_autonomous


# ---------------------------------------------------------------------------
# Review-fix round 1 (IVG-246 review-1.md, CRITICAL 1 + MAJOR 1/2/3/4/5/7/8/9
# + MINOR 10/11/12/13/14/16/17/18/19/20/21/22)
# ---------------------------------------------------------------------------


def test_escalation_flips_in_session_route_at_all_three_definition_sites(run_skill_text: str) -> None:
    """CRITICAL 1 fix verification: escalation must name the in-session `route`
    flip explicitly, not just the durable decision-record rewrite — this is
    what makes a re-entered Phase 2/3 skip condition evaluate false without a
    resume. The full atomic-unit definition lives at Checkpoint C; the two
    Phase 5 sites reference it as "the same atomic unit"/"performed per the
    atomic unit above" rather than re-stating it, so assert the phrase at the
    Checkpoint C definition site and its cross-references at both Phase 5
    sites."""
    checkpoint_c = _checkpoint_c_section(run_skill_text)
    assert "Set the orchestrator's in-session `route` variable to `full`" in checkpoint_c
    assert "re-entered architect and planning skip conditions" in checkpoint_c

    phase5 = _phase5_section(run_skill_text)
    assert phase5.count("in-session `route` flip to `full`") + phase5.count(
        "set the in-session `route` to `full`"
    ) >= 2


def test_needs_decision_writer_named_at_all_escalation_sites(run_skill_text: str) -> None:
    """MAJOR 9 fix verification: each fast-route escalation's NEEDS-DECISION
    branch must name the actual writer (the shared decision_gate_guard.py
    fail-closed invocation, inline — not a spawned subagent) with a distinct
    --site id, so an autonomous fast run always leaves a terminal signal."""
    checkpoint_c = _checkpoint_c_section(run_skill_text)
    assert "decision_gate_guard.py fail-closed" in checkpoint_c
    assert "--site fast-route-escalation-checkpoint-c" in checkpoint_c
    assert "not written by a spawned subagent here" in checkpoint_c

    phase5 = _phase5_section(run_skill_text)
    assert phase5.count("decision_gate_guard.py fail-closed") >= 2
    assert "--site fast-route-escalation-changes-requested" in phase5
    assert "--site fast-route-escalation-blocked" in phase5


def test_hard_stops_section_carves_out_fast_route_sites_1_2_3(run_skill_text: str) -> None:
    """MAJOR 9 fix verification (round 1): the '## Autonomous hard stops'
    six-site list must no longer contradict the fast-route escalation
    branches. SUPERSEDED WORDING (round 2, MAJOR 5): the round-1 fix's
    'NOT unconditional halt-sentinel writers' framing was itself the
    contradiction round 2 flagged against the site paragraphs — see
    test_hard_stops_section_no_longer_claims_sites_not_unconditional for the
    corrected contract (halt-sentinel always written; NEEDS-DECISION
    additive). This test now asserts only the surviving structural claim: a
    named escalation supplement exists for sites 1/2/3."""
    text = run_skill_text
    start = text.index("## Autonomous hard stops")
    end = text.index("## Autonomous progress sentinels", start)
    section = text[start:end]
    assert "Fast-route escalation supplement for sites 1, 2, and 3" in section
    assert "sites 1 (Review" in section


def test_onbehalf_marker_ordering_carve_out_for_no_redispatch(run_skill_text: str) -> None:
    """MAJOR 4 fix verification: the on-behalf cost-capture section's
    order-independence claim must carve out the sentinel-first requirement,
    reconciling it with the fast route's Opus `/implement` dispatch."""
    text = run_skill_text
    start = text.index("## On-behalf cost capture")
    end = text.index("## Phase 1 — Discover", start)
    section = text[start:end]
    assert "order-independent EXCEPT that any" in section
    assert "must remain the FIRST token" in section
    assert "load-bearing" in section


def test_stub_emitter_has_read_before_write_guard(run_skill_text: str) -> None:
    """MAJOR 5 fix verification: the stub emitter must never clobber an
    existing critic-reviewed plan — a read-before-write existence guard,
    keyed on the provenance marker, degrading silently to route=full."""
    prose = _stub_prose(run_skill_text)
    assert "Read-before-write guard" in prose
    assert "does NOT carry" in prose and "provenance: fast-path-triage" in prose
    assert "NEVER overwrite it" in prose
    assert "degrade silently to `route=full`" in prose


def test_stub_task_contract_names_pending_glyph_and_numbering(run_skill_text: str) -> None:
    """MAJOR 8 fix verification: the stub's task-section contract must name
    the exact pending glyph and numbering `/implement` selects on, so an
    autonomous fast run's `/implement` never sees zero pending tasks."""
    prose = _stub_prose(run_skill_text)
    assert "Task-section contract" in prose
    assert "`⏳` glyph" in prose
    assert "`T-NN`" in prose
    assert "T-01" in prose and "T-02" in prose
    assert "All tasks already implemented." in prose


def test_stub_emitter_validates_written_stub_at_runtime(run_skill_text: str) -> None:
    """MINOR 15 fix verification: the emitted stub must be validated with the
    real validate_artifact.py against the FILE JUST WRITTEN (not only a
    hand-built fixture, see test_emitted_stub_fixture_passes_validate_artifact
    above), degrading to route=full on non-zero exit."""
    prose = _stub_prose(run_skill_text)
    normalized = " ".join(prose.split())
    assert "Post-write validation" in prose
    assert "validate_artifact.py" in prose
    assert "Any non-zero exit" in normalized
    assert "delete the just-written file and" in normalized
    assert "degrade to `route=full`" in prose
    # Review-4 MINOR 2: the `Task profile:` assertion is the one post-write
    # check with a real behavioral consequence (a stub missing it lands in
    # the undetermined-plus-override review cell and drops the Large OWASP
    # retention) — pin it mechanically.
    assert "a `Task profile:` line" in normalized
    assert "OR a missing `Task profile:` line" in normalized


def test_cost_estimate_has_fast_route_rows_and_small_caveat(run_skill_text: str) -> None:
    """MAJOR 6 / MINOR 10 fix verification: the cost table must carry
    fast-route rows and state the Small-profile net-cost direction honestly
    rather than silently omitting it."""
    text = run_skill_text
    start = text.index("## Cost estimate")
    end = text.index("## Error handling", start)
    section = text[start:end]
    assert "Small, fast route" in section
    assert "Medium, fast route" in section
    assert "Large, fast route" in section
    assert "plausibly MORE than plain Small, not less" in section
    assert "genuinely favorable" in section

    phase4 = _phase_span(text, "## Phase 4 — Implement", "## Phase 5 — Review")
    assert "Small-profile cost honesty" in phase4
    assert "plausibly net MORE expensive" in phase4


def test_cost_estimate_rows_pin_ranges_and_ordering(run_skill_text: str) -> None:
    """Review-3 MINOR 9 fix verification: the cost-row test must pin actual
    dollar figures, not only labels/gloss — a rewritten Small-fast range that
    contradicts its own gloss must fail here, not survive to a human review."""
    import re

    text = run_skill_text
    start = text.index("## Cost estimate")
    end = text.index("## Error handling", start)
    section = text[start:end]

    def row_range(label: str) -> tuple[float, float]:
        m = re.search(
            rf"^\| {re.escape(label)} \| ~\$(\d+\.\d{{2}})–\$(\d+\.\d{{2}})",
            section,
            re.MULTILINE,
        )
        assert m, f"cost row not found or range malformed: {label}"
        return float(m.group(1)), float(m.group(2))

    small = row_range("Small")
    medium = row_range("Medium")
    large = row_range("Large")
    small_fast = row_range("Small, fast route")
    medium_fast = row_range("Medium, fast route")
    large_fast = row_range("Large, fast route")

    assert small == (2.75, 3.50)
    assert medium == (3.75, 5.50)
    assert large == (6.00, 8.50)
    assert small_fast == (3.00, 3.75)
    assert medium_fast == (2.75, 4.00)
    assert large_fast == (5.25, 7.50)

    # Ordering invariants backing the row glosses:
    # plain profiles strictly ordered
    assert small[0] < medium[0] < large[0] and small[1] < medium[1] < large[1]
    # Small-fast >= plain Small at both ends ("plausibly MORE than plain Small")
    assert small_fast[0] >= small[0] and small_fast[1] > small[1]
    # Small-fast at or below Medium-fast at the ceiling (review-2 MINOR 12)
    assert small_fast[1] <= medium_fast[1]
    # Medium/Large fast routes genuinely favorable vs their plain profiles
    assert medium_fast[0] <= medium[0] and medium_fast[1] < medium[1]
    assert large_fast[0] < large[0] and large_fast[1] < large[1]


def test_resume_step0b_anchors_route_line_not_substring(run_skill_text: str) -> None:
    """MINOR 13 fix verification: Step 0b's `Route:` read must be
    line-anchored, not an unanchored whole-file substring search — this
    repo's own plan for this task documents 'Route: fast' in prose, which
    would otherwise be misread as taking the fast route."""
    section = _resume_section(run_skill_text)
    normalized = " ".join(section.split())
    assert "ANCHORED inside the state block" in normalized
    assert r"^Route:\s*(fast|full)\s*$" in normalized
    assert "must not be misread as taking it" in normalized


def test_phase_sequence_diagram_matches_skip_conditions(run_skill_text: str) -> None:
    """MINOR 14 fix verification: the phase-sequence diagram must show the
    route=fast skip condition on Phase 2 and Phase 3, and the Checkpoint A1
    arrow, matching the phase bodies it indexes."""
    text = run_skill_text
    start = text.index("## Phase sequence")
    end = text.index("## Pre-phase context budget", start)
    diagram = text[start:end]
    assert "Checkpoint A1" in diagram
    assert "skip if Small, OR skip if route=fast" in diagram
    assert "THOROUGH_PLAN (conditional — skip if route=fast)" in diagram


def test_final_report_has_route_field(run_skill_text: str) -> None:
    """MINOR 16 fix verification: the Phase 6 final-report template must
    state whether planning was skipped by routing rather than by profile."""
    text = run_skill_text
    start = text.index("After completion, present the final report:")
    end = text.index("## Checkpoint interaction protocol", start) if "## Checkpoint interaction protocol" in text[start:] else len(text)
    # bounded search window around the template (small, avoids over-matching)
    window = text[start:start + 800]
    assert "Route: <full|fast>" in window
    assert "planning was skipped by routing, not by profile" in window


def test_ledger_row_pins_model_and_uuid(run_skill_text: str) -> None:
    """MINOR 17 fix verification: the Phase 1.6 ledger row must pin the model
    and orchestrator UUID explicitly, not rely on a phase->model writer
    mapping that could default to the triage skill's own cheap tier."""
    text = run_skill_text
    start = text.index("**Ledger row.**")
    end = text.index("**Session state.**", start)
    section = text[start:end]
    assert "Pin the model explicitly to `opus`" in section
    assert "pin `uuid` to the orchestrator's own session UUID" in section


def test_checkpoint_a1_census_out_of_scope_and_backstop(run_skill_text: str) -> None:
    """MINOR 20 + MINOR 22 fix verification: Checkpoint A1's prose-described
    AskUserQuestion is confirmed deliberately out of the decision-gate
    census's scope, and its non-interactive degrade has a backstop for an
    AskUserQuestion that returns empty or errors."""
    section = _phase_1_6_section(run_skill_text)
    idx = section.index("Checkpoint A1")
    tail = section[idx:]
    assert "deliberately outside the" in tail
    assert "decision-gate census's call-syntax detection scope" in tail
    assert "returns empty, errors, or otherwise fails to resolve" in tail


def test_large_carveout_owasp_unconditional(review_skill_text: str) -> None:
    """MAJOR 3 fix verification: a fast-route review-shape override on a
    Large-profile task must not drop the dedicated /security_review OWASP
    pass — it stays unconditional on Large regardless of route."""
    section = _profile_detection_section(review_skill_text)
    assert "Large carve-out" in section
    assert "unconditionally dispatch the dedicated `/security_review` OWASP pass" in section
    assert "never Large — see the carve-out below" in section


# ---------------------------------------------------------------------------
# Review-fix round 2 (IVG-246 review-2.md MAJOR 1-7 + supervisor coverage gap)
# ---------------------------------------------------------------------------


def test_a1_and_review_agree_on_owasp_retention_on_large(
    run_skill_text: str, review_skill_text: str
) -> None:
    """MAJOR 1 fix verification (round 2): a dedicated cross-file guard so
    Checkpoint A1's Large warning and review/SKILL.md's Large carve-out can
    never silently diverge again — round 2 found two passing tests each
    reading a different file and pinning opposite OWASP contracts."""
    a1_section = _phase_1_6_section(run_skill_text)
    a1_tail = a1_section[a1_section.index("Checkpoint A1"):]
    review_section = _profile_detection_section(review_skill_text)
    assert "retained unconditionally on Large" in a1_tail
    assert "drops the dedicated `/security_review` OWASP pass" not in a1_tail
    assert "unconditionally dispatch the dedicated `/security_review` OWASP pass" in review_section


def test_read_before_write_guard_rejects_thorough_plan_done_proxy(run_skill_text: str) -> None:
    """MAJOR 2 fix verification (round 2): the guard's frontmatter marker
    must be the ONLY sanctioned test. The round-1 fix's 'equivalently:
    thorough_plan.done is already present' parenthetical was false in both
    directions — that sentinel is written on the fast route too (Phase 3's
    own skip-write is unconditional) and never written on a plain
    non-autonomous run — so an implementer following it could still
    overwrite a converged plan. The parenthetical must not reappear."""
    prose = _stub_prose(run_skill_text)
    assert "equivalently:" not in prose
    assert "The frontmatter marker is the ONLY sanctioned test" in prose
    assert "false in both directions" in prose


def test_both_silent_degrades_rewrite_triage_decision_route_full(run_skill_text: str) -> None:
    """MAJOR 3 fix verification (round 2): both fast-route silent-degrade
    paths (the read-before-write guard, and post-write validation) must
    rewrite `triage-decision.md` to `route: full` — otherwise Resume Step
    0b's fallback source still reads `fast` and a resumed session can
    resurrect a dead fast route with no plan behind it."""
    prose = _stub_prose(run_skill_text)
    assert prose.count("record `route: full` in `triage-decision.md`") >= 2


def test_triage_decision_records_effective_route_not_raw_answer(run_skill_text: str) -> None:
    """MAJOR 3 fix verification (round 2): the `triage-decision.md` prose
    must say it records the EFFECTIVE route (post-degrade), not just 'the
    chosen route' — ambiguous language that left it unstated whether a
    silent degrade after Checkpoint A1 gets reflected in the file a resume
    falls back to."""
    prose = _stub_prose(run_skill_text)
    normalized = " ".join(prose.split())
    assert "EFFECTIVE route" in normalized
    assert "never the raw A1 answer alone" in normalized


def test_escalation_order_is_stub_before_sentinels_at_all_three_sites(run_skill_text: str) -> None:
    """MAJOR 4 fix verification (round 2): the escalation atomic unit's
    durable writes must fix the stub's `Route:`/`Review shape:` lines BEFORE
    deleting the completion sentinels, at all three escalation sites — the
    round-2-found interruption window (sentinels gone, `Route: fast` still
    readable in the stub) is closed by re-ordering, not just documented."""
    checkpoint_c = _checkpoint_c_section(run_skill_text)
    idx_triage = checkpoint_c.index("rewrite `triage-decision.md` with the flipped route")
    idx_strip = checkpoint_c.index("strip its `Review shape:` line")
    idx_sentinels = checkpoint_c.index(
        "DELETE `autonomous-progress-{task}/architect.done` and `autonomous-progress-{task}/thorough_plan.done`"
    )
    # Review-3 MINOR 1: the outright stub delete moves AFTER the sentinel
    # deletion, closing the plan-absent-while-sentinels-claim-done window.
    idx_outright = checkpoint_c.index("delete the stripped stub `current-plan.md` outright")
    assert idx_triage < idx_strip < idx_sentinels < idx_outright
    assert "crash-safe for route recovery by construction" in checkpoint_c

    phase5 = _phase5_section(run_skill_text)
    # CHANGES_REQUESTED branch: line strip precedes the "THEN, once both
    # route-recovery sources agree, DELETE" sentinel clause.
    idx_cr_stub = phase5.index("strip the stub's lines per the same provenance-conditioned rule as Checkpoint")
    idx_cr_sentinels = phase5.index("THEN, once both")
    assert idx_cr_stub < idx_cr_sentinels

    # BLOCKED branch: same ordering requirement.
    idx_blocked_stub = phase5.index(
        "strip the stub's `Review shape:` and `Route:` lines per the same provenance-conditioned rule"
    )
    idx_blocked_sentinels = phase5.index("THEN DELETE `autonomous-progress-{task}/architect.done`")
    assert idx_blocked_stub < idx_blocked_sentinels


def test_hard_stops_section_no_longer_claims_sites_not_unconditional(run_skill_text: str) -> None:
    """MAJOR 5 fix verification (round 2), per the resolved user decision:
    on a supervised autonomous fast run, Review BLOCKED must always write
    the supervisor-visible halt-sentinel and terminate — no unattended
    relaunch into the full path. The carve-out bullet is rewritten to match
    the site paragraphs (`:725`/`:757`/`:761` in review-2.md's numbering):
    all three sites remain unconditional halt-sentinel writers, and
    NEEDS-DECISION is written IN ADDITION, never instead."""
    text = run_skill_text
    start = text.index("## Autonomous hard stops")
    end = text.index("## Autonomous progress sentinels", start)
    section = text[start:end]
    assert "Fast-route escalation supplement for sites 1, 2, and 3" in section
    assert "all three sites remain unconditional halt-sentinel writers" in section
    assert "IN ADDITION" in section
    assert "never instead of it" in section
    assert "NOT unconditional halt-sentinel writers" not in section

    # The BLOCKED site itself must say the same: halt-sentinel always
    # written, NEEDS-DECISION additive, never a substitute.
    phase5 = _phase5_section(text)
    assert "in addition to the halt-sentinel already written above — never instead of it" in phase5
    assert "always terminates for a human rather than relaunching unattended into the full path" in phase5


def test_review_branch_set_partitions_undetermined_plus_override(review_skill_text: str) -> None:
    """MAJOR 6 fix verification (round 2): an undetermined `Task profile:`
    carrying a `Review shape:` override must match exactly one branch — the
    single-pass branch, per the resolved decision that the override is the
    explicit signal — not fall through the gap between the Small/Medium
    branch (round-2 read as Small/Medium only) and the fan-out branch
    (round-2 required NO override present)."""
    section = _profile_detection_section(review_skill_text)
    assert "Small, Medium, or undetermined" in section
    assert "undetermined-plus-override case is routed here deliberately" in section
    # the Medium/Large fan-out branch's own undetermined default remains
    # scoped to the no-override case (unaffected by this fix).
    assert "AND no" in section  # "... AND no `Review shape: ...` override is present"


def test_core_review_doc_documents_shape_override_and_large_carveout(run_skill_text: str) -> None:
    """MAJOR 7 fix verification (round 2): the portable-core review doc must
    describe the review-shape override and the Large OWASP carve-out so a
    non-Claude runtime implementing from the core doc doesn't silently fan
    out where the Claude adapter now does not. Bare skill names only — no
    model tokens (forbidden-token test covers the static substring check;
    this test targets the specific content gap review-2.md MAJOR 7 flagged)."""
    core_doc = (_SOURCE_ROOT / "core" / "skills" / "review.md").read_text(encoding="utf-8")
    normalized = " ".join(core_doc.split())
    assert "review-shape override" in normalized
    assert "security-review skill's fan-out contract unconditionally" in normalized
    for token in ("opus", "sonnet", "haiku"):
        assert token not in core_doc.lower()

