"""IVG-258 T-12: reader-contract harness for the D-02 four-tier resume
algorithm (relabeled from "resume-precedence tests" -- the original name
overstated what this module actually guards).

The four-tier decision algorithm itself lives in `run/SKILL.md` prose (an
LLM instruction followed at `/run` resume time, not executable code) --
there is no production Python function to import for it, so nothing here
can execute that algorithm end to end. What this module DOES pin, real and
behavioural against `run_state.py` via subprocess:
  - the WIRE CONTRACT the algorithm depends on being able to trust --
    lowercase `true`/`false` string rendering, empty stdout on a stale or
    schema-forward record, `--require-existing` as a genuine no-op that
    never resurrects a cleared record and never touches mtime.
  - that `run/SKILL.md`'s own Step 1b prose contains the algorithm's load-
    bearing invariant SENTENCES verbatim (see
    `test_step_1b_prose_carries_verbatim_invariant_pins` below), not merely
    the four tier names in some order.

The `_resolve_*` helpers are a literal, test-only transcription of the
documented algorithm, used ONLY to drive the wire-contract assertions above
against real `run_state.py` output -- they are not shipped anywhere and
must never be imported by production code. Because they are derived FROM
the prose rather than independently, they are tautological with respect to
the algorithm itself: rewriting Step 1b to invert a rule (record overrides
sentinels, drop the `at_stage_boundary` precondition, ordering instead of
string equality) would not fail a single `_resolve_*`-driven test, since
those helpers would simply be re-derived to match. The invariant-pin test
is what actually guards the documented algorithm against that class of
regression -- it is not a lesser check bolted on alongside the tier tests,
it is the ONLY one of the two halves that reads `run/SKILL.md`'s own words.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "run_state.py"
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"

PY = sys.executable

RESUMABLE_PHASES = (
    "discover",
    "enrich",
    "specify",
    "fast_path_triage",
    "architect",
    "thorough_plan",
    "implement",
    "review",
    "end_of_task",
)

ALL_FIELDS = (
    "schema,active,phase,phase_index,subphase,step,at_stage_boundary,"
    "next_action,notes_path,resume_command,updated_at"
)


def test_local_roster_matches_supervisor_roster():
    """This module's own `RESUMABLE_PHASES` duplicate
    (used only to drive the `_resolve_*` test-only oracle above) must stay
    in step with `quoin.supervisor.RESUMABLE_PHASES`, the single source of
    truth -- test_run_state_wiring.py pins the SKILL.md prose copy against
    the same source separately."""
    supervisor = importlib.import_module("quoin.supervisor")
    assert RESUMABLE_PHASES == supervisor.RESUMABLE_PHASES


def _run(args):
    return subprocess.run(
        [PY, str(SCRIPT)] + args, capture_output=True, text=True, timeout=30
    )


def _write(root, task, **kwargs):
    args = ["--write", "--project-root", str(root), "--task", task]
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        if key == "require_existing":
            if value:
                args.append(flag)
            continue
        if isinstance(value, list):
            for v in value:
                args += [flag, v]
        elif isinstance(value, bool):
            args += [flag, "true" if value else "false"]
        else:
            args += [flag, str(value)]
    return _run(args)


def _clear(root, task):
    return _run(["--clear", "--project-root", str(root), "--task", task])


def _read(root, task, max_age_days=None, fields=ALL_FIELDS):
    args = ["--read", "--project-root", str(root), "--task", task, "--fields", fields]
    if max_age_days is not None:
        args += ["--max-age-days", str(max_age_days)]
    result = _run(args)
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _record_path(root, task) -> Path:
    return Path(root) / ".workflow_artifacts" / "memory" / f"run-state-{task}.json"


# ---------------------------------------------------------------------------
# Test-only oracle -- see module docstring.
# ---------------------------------------------------------------------------


def _resolve_phase(sentinel_done_phases, record):
    """Tier 1: which phase.

    `sentinel_done_phases` is `None` when no `autonomous-progress-{task}/`
    directory exists at all; otherwise it is the set of phases whose
    `.done` sentinel is present. When it is `None`, a fresh active record's
    `next_action` decides instead -- NEVER `phase`, which names the last
    completed phase (or, at `at_stage_boundary: false`, the phase in
    flight) and is never the resume target. `next_action` only counts as a
    decision when it matches the fixed format `start <phase>` against a
    known phase name; anything else (a terminal marker, a value with no
    parseable phase) is treated as no decision, same as an absent record.
    """
    if sentinel_done_phases is not None:
        for phase in RESUMABLE_PHASES:
            if phase not in sentinel_done_phases:
                return phase
        return None
    if record and record.get("active") == "true":
        m = re.match(r"^start (\S+)$", record.get("next_action") or "")
        if m and m.group(1) in RESUMABLE_PHASES:
            return m.group(1)
    return None


class _NoCandidates:
    """Tier-2 sentinel distinguishing "this phase has no known sub-phase
    sentinel NAMES at all" from "every known sub-phase is done" (both would
    otherwise collapse to `None`). Only the former is what production
    actually hits today -- see `_resolve_subphase`'s docstring."""

    def __repr__(self):
        return "_NO_CANDIDATES"


_NO_CANDIDATES = _NoCandidates()


def _resolve_subphase(candidate_subphases_in_order, done_subphases):
    """Tier 2: which sub-phase. Sentinels ALWAYS decide -- the record is
    never consulted here.

    Returns `_NO_CANDIDATES` when `candidate_subphases_in_order` is empty --
    i.e. the selected phase has no `{phase}.{subphase}.done` sentinel NAMES
    written for it anywhere in the tree at all. This is the real production
    shape for `thorough_plan`: no phase writes `thorough_plan.round-N-*.done`
    sentinels, so its tier-2 candidate set is always empty (distinct from
    every candidate being done, which returns `None` below as before).
    """
    if not candidate_subphases_in_order:
        return _NO_CANDIDATES
    for subphase in candidate_subphases_in_order:
        if subphase not in done_subphases:
            return subphase
    return None


def _resolve_step(selected_subphase, record):
    """Tier 3: where inside the first incomplete sub-phase.

    Re-entry requires an active record and `at_stage_boundary: false`, plus
    one of: STRING EQUALITY against the tier-2 selection, OR -- in the
    no-sub-phase-sentinel case (`selected_subphase is _NO_CANDIDATES`) --
    the record's own `subphase` is accepted directly, since there is no
    tier-2 selection to compare it against.
    """
    if not record:
        return None
    if selected_subphase is not _NO_CANDIDATES and record.get("subphase") != selected_subphase:
        return None
    if record.get("active") != "true":
        return None
    if record.get("at_stage_boundary") != "false":
        return None
    return record.get("step")


def _resolve_next_action(record):
    """Tier 4: what to do next -- `next_action`/`notes_path` are the ONLY
    source."""
    if not record:
        return None, None
    return record.get("next_action"), record.get("notes_path")


def _resume_slice() -> str:
    text = RUN_SKILL.read_text(encoding="utf-8")
    heading = re.search(r"^## Resume$", text, re.M)
    assert heading, "run/SKILL.md must have a '## Resume' heading"
    start = heading.start()
    rest = text[heading.end() :]
    m = re.search(r"\n## [A-Z]", rest)
    end = heading.end() + (m.start() if m else len(rest))
    return text[start:end]


# ---------------------------------------------------------------------------
# Tier 1 -- which phase
# ---------------------------------------------------------------------------


def test_tier1_progress_dir_present_sentinels_decide_phase():
    sl = _resume_slice()
    assert "never re-run it" in sl
    assert "A phase whose `{phase}.done` sentinel EXISTS is finished" in sl
    # Behavioural: even when a record disagrees, the sentinel roster wins.
    done = {"discover", "enrich"}
    assert _resolve_phase(done, {"active": "true", "phase": "review"}) == "specify"


def test_tier1_no_progress_dir_fresh_record_decides_phase(tmp_path):
    # `phase` (last completed) and `next_action` (what to start next)
    # deliberately disagree here -- proves tier 1 reads `next_action`,
    # never `phase`, matching the field invariant.
    _write(
        tmp_path,
        "t1",
        phase="architect",
        phase_index=2,
        at_stage_boundary=True,
        next_action="start thorough_plan",
    )
    record = _read(tmp_path, "t1")
    assert _resolve_phase(None, record) == "thorough_plan"


def test_tier1_no_progress_dir_unparseable_next_action_is_no_decision(tmp_path):
    # A `next_action` that does not match `start <known-phase>` (e.g. a
    # terminal marker, or a value whose phase detail lives in
    # `subphase`/`step` instead) must NOT be misread as a phase name --
    # this is the exact regression class of round-2 issue 1.
    _write(
        tmp_path,
        "t1b",
        phase="end_of_task",
        phase_index=6,
        at_stage_boundary=True,
        next_action="run complete",
    )
    record = _read(tmp_path, "t1b")
    assert _resolve_phase(None, record) is None


def test_tier1_no_progress_dir_no_record_falls_back_to_session_state(tmp_path):
    record = _read(tmp_path, "ghost")
    assert record == {}
    assert _resolve_phase(None, record) is None


def test_no_record_falls_to_session_state_prose(tmp_path):
    record = _read(tmp_path, "ghost2", fields=ALL_FIELDS)
    assert record == {}


# ---------------------------------------------------------------------------
# Tier 2 -- which sub-phase
# ---------------------------------------------------------------------------


def test_tier2_subphase_sentinels_win_over_record(tmp_path):
    _write(tmp_path, "t2", phase="implement", subphase="batch-1", at_stage_boundary=False)
    record = _read(tmp_path, "t2")
    # Sentinels say batch-1 is done and batch-2 is not -> tier 2 selects
    # batch-2 regardless of the record's own (stale) claim of batch-1.
    selected = _resolve_subphase(["batch-1", "batch-2"], done_subphases={"batch-1"})
    assert selected == "batch-2"
    assert record["subphase"] == "batch-1"


# ---------------------------------------------------------------------------
# Tier 3 -- where inside the first incomplete sub-phase
# ---------------------------------------------------------------------------


def test_tier3_no_sub_phase_sentinels_accepts_the_record_subphase_directly(tmp_path):
    """The real production pair: `/thorough_plan`'s
    round-boundary write is the sole writer of a non-empty `subphase`
    anywhere in the tree, and no phase writes `thorough_plan.round-N-*.done`
    sentinels -- so `thorough_plan`'s tier-2 candidate set is always empty.
    Tier 3 must accept the record's own `subphase` directly in that case,
    rather than discarding it for failing to string-equal a selection that
    was never made.
    """
    _write(
        tmp_path,
        "t4",
        phase="thorough_plan",
        subphase="round-3-critic",
        step="critic round 3 dispatched",
        at_stage_boundary=False,
    )
    record = _read(tmp_path, "t4")
    selected = _resolve_subphase([], done_subphases=set())
    assert selected is _NO_CANDIDATES
    assert _resolve_step(selected, record) == "critic round 3 dispatched"


# The four tests below (batch-N identifiers) pin the tier-2/tier-3 sub-phase
# resolution algorithm in the abstract -- they are NOT production-reachable
# today, since no phase in the tree currently writes `{phase}.{subphase}.done`
# sentinels (the only production `subphase` writer is `/thorough_plan`'s
# round boundary, covered by the no-sub-phase-sentinel test above). They
# remain here as a contract pin for the day a phase does checkpoint at
# sub-phase granularity with real sentinels.


def test_tier3_subphase_with_existing_done_sentinel_is_discarded(tmp_path):
    _write(tmp_path, "t3", subphase="batch-1", step="stale", at_stage_boundary=False)
    record = _read(tmp_path, "t3")
    selected = _resolve_subphase(["batch-1", "batch-2"], done_subphases={"batch-1"})
    assert _resolve_step(selected, record) is None


def test_tier3_subphase_naming_an_unknown_subphase_is_discarded(tmp_path):
    _write(tmp_path, "t5", subphase="batch-99", step="ghost step", at_stage_boundary=False)
    record = _read(tmp_path, "t5")
    selected = _resolve_subphase(["batch-1", "batch-2"], done_subphases={"batch-1"})
    assert _resolve_step(selected, record) is None


def test_tier3_comparison_is_string_equality_not_ordering(tmp_path):
    _write(tmp_path, "t6", subphase="batch-10", step="should apply", at_stage_boundary=False)
    record = _read(tmp_path, "t6")
    # batch-10 IS the tier-2 selection here -> string-equal, obeyed.
    selected = _resolve_subphase(["batch-3", "batch-10"], done_subphases={"batch-3"})
    assert selected == "batch-10"
    assert _resolve_step(selected, record) == "should apply"
    # Flip which sub-phase is finished: batch-3 is now selected, and the
    # SAME record naming "batch-10" is discarded -- proving equality, not
    # a numeric/lexical rank, decides ("batch-10" is not "ahead of"
    # "batch-3" in any computed sense; it is simply a different string).
    selected2 = _resolve_subphase(["batch-3", "batch-10"], done_subphases={"batch-10"})
    assert selected2 == "batch-3"
    assert _resolve_step(selected2, record) is None


# ---------------------------------------------------------------------------
# Tier 4 -- what to do next
# ---------------------------------------------------------------------------


def test_tier4_next_action_and_notes_path_are_the_only_source(tmp_path):
    _write(tmp_path, "t7", next_action="start review", artifact="/x/current-plan.md")
    record = _read(tmp_path, "t7")
    next_action, notes_path = _resolve_next_action(record)
    assert next_action == "start review"
    assert notes_path == record.get("notes_path")


def test_tier4_tolerates_a_missing_notes_path(tmp_path):
    _write(tmp_path, "t8", next_action="start review", at_stage_boundary=True)
    record = _read(tmp_path, "t8")
    notes_file = Path(record["notes_path"])
    assert not notes_file.exists()
    next_action, notes_path = _resolve_next_action(record)
    assert next_action == "start review"
    assert notes_path == record["notes_path"]


# ---------------------------------------------------------------------------
# Cross-tier guards
# ---------------------------------------------------------------------------


def test_at_stage_boundary_true_record_does_not_trigger_reentry(tmp_path):
    _write(tmp_path, "t9", subphase="batch-1", step="x", at_stage_boundary=True)
    record = _read(tmp_path, "t9")
    assert _resolve_step("batch-1", record) is None


def test_stale_record_outside_freshness_window_is_ignored(tmp_path):
    _write(tmp_path, "t10", step="x")
    path = _record_path(tmp_path, "t10")
    old = time.time() - (3 * 86400)
    os.utime(path, (old, old))
    record = _read(tmp_path, "t10", max_age_days=1)
    assert record == {}


def test_standalone_thorough_plan_creates_no_record(tmp_path):
    result = _write(tmp_path, "t11", require_existing=True, step="round 1")
    assert result.returncode == 0
    assert not _record_path(tmp_path, "t11").exists()
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    assert not memory_dir.exists() or not any(memory_dir.iterdir())


def test_standalone_thorough_plan_after_a_completed_run_leaves_the_record_inactive(tmp_path):
    _write(tmp_path, "t12", phase="end_of_task", step="run finished")
    _clear(tmp_path, "t12")
    path = _record_path(tmp_path, "t12")
    before = _read(tmp_path, "t12")
    before_mtime = path.stat().st_mtime
    _write(
        tmp_path,
        "t12",
        require_existing=True,
        subphase="round-1-plan",
        step="round 1 plan returned",
    )
    after = _read(tmp_path, "t12")
    after_mtime = path.stat().st_mtime
    assert after["active"] == before["active"] == "false"
    assert after["phase"] == before["phase"] == "end_of_task"
    assert after_mtime == before_mtime


def test_round_boundary_write_updates_an_existing_record(tmp_path):
    _write(
        tmp_path,
        "t13",
        phase="thorough_plan",
        phase_index=3,
        subphase="round-1-plan",
        step="round 1 plan returned",
    )
    _write(
        tmp_path,
        "t13",
        require_existing=True,
        subphase="round-2-critic",
        step="round 2 critic returned",
    )
    record = _read(tmp_path, "t13")
    assert record["subphase"] == "round-2-critic"
    assert record["step"] == "round 2 critic returned"
    assert record["phase_index"] == "3"


def test_inactive_record_is_ignored(tmp_path):
    _write(tmp_path, "t14", step="x")
    _clear(tmp_path, "t14")
    record = _read(tmp_path, "t14")
    assert record["active"] == "false"
    assert _resolve_phase(None, record) is None


def test_schema_forward_record_falls_to_next_tier(tmp_path):
    _write(tmp_path, "t15", step="x")
    path = _record_path(tmp_path, "t15")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"schema": 1', '"schema": 2'),
        encoding="utf-8",
    )
    record = _read(tmp_path, "t15")
    assert record == {}
    assert _resolve_phase(None, record) is None


# ---------------------------------------------------------------------------
# Step 1b documented-contract checks
# ---------------------------------------------------------------------------


def test_step_1b_documented_between_step_1_and_step_2():
    sl = _resume_slice()
    step1 = sl.index("**Step 1 (")
    step1b = sl.index("**Step 1b (")
    step2 = sl.index("**Step 2 —")
    assert step1 < step1b < step2


def test_step_1b_documents_all_four_tiers():
    sl = _resume_slice()
    body = sl[sl.index("**Step 1b (") :]
    assert "which phase" in body
    assert "which sub-phase" in body
    assert "where inside the first incomplete sub-phase" in body
    assert "what to do next" in body


def test_step_1b_read_call_passes_max_age_days():
    sl = _resume_slice()
    body = sl[sl.index("**Step 1b (") :]
    assert "run_state.py --read" in body
    assert "--max-age-days" in body


def test_step_1_prose_carries_verbatim_tier1_invariant_pins():
    """Round-2 issue 4: the tier-1 fallback (`_resolve_phase` above, used
    when no `autonomous-progress-{task}/` directory exists) reads
    `next_action`, never `phase`. That contract lives in Step 1's Field
    invariant bullet and its extended fallback bullet -- both BEFORE
    Step 1b, so they are out of `test_step_1b_prose_carries_verbatim_invariant_pins`'s
    scope. Pin them here so a revert of the tier-1 fix (re-keying the
    fallback on `phase`) fails this harness too, not just the behavioural
    `_resolve_phase` tests above."""
    sl = _resume_slice()
    assert "names the LAST COMPLETED phase" in sl
    assert "--fields active,next_action" in sl


def test_step_1b_prose_carries_verbatim_invariant_pins():
    """The three checks above (tier ordinal position, tier-name substrings,
    flag presence) do not discriminate the documented algorithm from an
    inverted one: rewriting Step 1b to say the record OVERRIDES the
    sentinels, dropping the `at_stage_boundary: false` precondition, or
    swapping string equality for ordering would still pass every one of
    them, and every `_resolve_*`-driven tier test above (issue 8 -- those
    helpers are transcribed FROM this same prose, so they cannot catch a
    change to it). Pin the exact invariant sentences instead."""
    sl = _resume_slice()
    body = sl[sl.index("**Step 1b (") :]
    assert "The record NEVER overrides them." in body
    assert "STRING EQUALITY" in body
    assert "at_stage_boundary: false" in body
    assert "is DISCARDED, not obeyed" in body
    # The no-sub-phase-sentinel case must be stated, not
    # left implicit -- without it, tier 3's only production writer
    # (`/thorough_plan`'s round boundary) can never re-enter, since its
    # phase has no known sub-phase sentinel names to string-equal against.
    assert "No-sub-phase-sentinel case" in body
    assert "tier 2 has\n   no candidate set to select from" in body


def test_step_1b_fields_argument_is_complete_and_matches_the_oracle():
    """`--fields` completeness assert (issue 8): the exact field list Resume
    Step 1b requests from `run_state.py --read` must match this module's
    own `ALL_FIELDS` oracle constant exactly, so a field silently dropped
    from either side -- the doc's `--read` call, or the fixture helpers
    above that build and read records through that same field set -- fails
    a test instead of drifting unnoticed."""
    sl = _resume_slice()
    body = sl[sl.index("**Step 1b (") :]
    m = re.search(r"--fields ([a-z_,]+)", body)
    assert m, "Step 1b's run_state.py --read call must carry a --fields argument"
    assert m.group(1) == ALL_FIELDS
