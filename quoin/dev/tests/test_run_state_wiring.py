"""IVG-258 T-12: a live census of every /run and /thorough_plan run-state
writer call.

Never a frozen count -- everything here is derived from the live
`run/SKILL.md` / `thorough_plan/SKILL.md` text and cross-checked against
`quoin.supervisor.RESUMABLE_PHASES`, so a future added phase or escalation
site without a writer call fails CLOSED rather than being silently missed.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
THOROUGH_PLAN_SKILL = (
    REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "thorough_plan" / "SKILL.md"
)

WRITE_CALL = "run_state.py --write"
CLEAR_CALL = "run_state.py --clear"
# The escalation rewind sets `phase` to the last phase whose output survives
# the escalation (fast_path_triage, since the rewind deletes the architect /
# thorough_plan / implement sentinels), never to the re-entry target
# (architect) itself -- see the "Field invariant" paragraph in
# run/SKILL.md's Resume Step 1.
ESCALATION_REWIND = "--phase fast_path_triage --phase-index 1"
REWIND_PHRASE = "rewind the run-state record"
REENTER_PHRASE = "re-enter at the architect phase"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Call-site census -- an oracle rewrite alone does not catch a
# `--next-action` value that never matches a known phase name, or a
# call-site quoting regression; this section adds a live grep of every
# such value against the phase roster, plus a quoting assertion over every
# writer call site.
# ---------------------------------------------------------------------------

# Two terminal `next_action` markers that are documented NOT to parse against
# `start <phase>` -- both fall through by design (Resume Step 1's extended
# fallback bullet: "treat this tier as if it had returned nothing").
TERMINAL_MARKERS = {"run complete", "run blocked"}

_CALL_RE = re.compile(r"run_state\.py --(write|clear)\b.*?\|\| true", re.S)
_NEXT_ACTION_RE = re.compile(r'--next-action "([^"]*)"')
_QUOTED_FLAG_RE = {
    flag: re.compile(rf'--{flag} "[^"]*"') for flag in ("project-root", "task", "artifact")
}
_BARE_FLAG_RE = {
    flag: re.compile(rf"--{flag}\s+(?!\")\S") for flag in ("project-root", "task", "artifact")
}


def _call_blocks(text: str) -> list:
    """Every `run_state.py --write ... || true` / `--clear ... || true`
    call site, verbatim, in file order. Every real writer call in both
    SKILL.md files ends with `|| true` (the fail-open convention) -- this is
    what lets the non-greedy match bound each call without over-capturing
    into the next one.
    """
    return [m.group(0) for m in _CALL_RE.finditer(text)]


def _known_phase_roster() -> tuple:
    supervisor = importlib.import_module("quoin.supervisor")
    return supervisor.RESUMABLE_PHASES


# ---------------------------------------------------------------------------
# Slice helpers -- definitions restated once, matching T-08/T-12's own prose.
# ---------------------------------------------------------------------------


def _phase_heading_slices(text: str) -> dict:
    """Slice the doc by its `## Phase N[.M] — ...` headings, in file order.

    Bounded to the NEXT `## ` (any) heading, or EOF for the last one. Zipped
    positionally against `quoin.supervisor.RESUMABLE_PHASES` -- both are
    ordered identically by construction (the doc's own Phase sequence).
    """
    supervisor = importlib.import_module("quoin.supervisor")
    phase_headings = [
        m.start() for m in re.finditer(r"^## Phase [\d.]+ — .*$", text, re.M)
    ]
    all_headings = [m.start() for m in re.finditer(r"^## .*$", text, re.M)]
    assert len(phase_headings) == len(supervisor.RESUMABLE_PHASES), (
        f"expected {len(supervisor.RESUMABLE_PHASES)} '## Phase' headings, "
        f"found {len(phase_headings)}"
    )
    slices = {}
    for name, start in zip(supervisor.RESUMABLE_PHASES, phase_headings):
        later = [h for h in all_headings if h > start]
        end = min(later) if later else len(text)
        slices[name] = text[start:end]
    return slices


def _escalation_slices(text: str) -> list:
    """The three escalation atomic units: from each `DELETE ...architect.done`
    occurrence forward to the first line matching `^Under `AUTONOMOUS``,
    `^\\*\\*If `, or `^## `. Definition restated verbatim from T-08/T-12.
    """
    delete_marker = "DELETE `autonomous-progress-{task}/architect.done`"
    starts = [m.start() for m in re.finditer(re.escape(delete_marker), text)]
    assert len(starts) == 3, f"expected 3 escalation DELETE sites, found {len(starts)}"
    boundary_re = re.compile(r"^(Under `AUTONOMOUS`|\*\*If |## )", re.M)
    slices = []
    for start in starts:
        m = boundary_re.search(text, start)
        end = m.start() if m else len(text)
        slices.append(text[start:end])
    return slices


def _autonomy_slices(text: str) -> list:
    """Every span from a line matching `^Under `AUTONOMOUS`` to the next
    blank line -- used to prove a writer call is never gated on autonomy.
    """
    lines = text.splitlines()
    slices = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("Under `AUTONOMOUS`"):
            j = i
            while j < len(lines) and lines[j].strip() != "":
                j += 1
            slices.append("\n".join(lines[i:j]))
            i = j
        else:
            i += 1
    return slices


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


def test_every_stage_transition_site_calls_the_writer():
    text = _text(RUN_SKILL)
    slices = _phase_heading_slices(text)
    missing = [name for name, sl in slices.items() if WRITE_CALL not in sl]
    assert not missing, f"phases missing a {WRITE_CALL!r} call: {missing}"


def test_writer_call_is_not_gated_on_autonomous():
    text = _text(RUN_SKILL)
    for sl in _autonomy_slices(text):
        assert "run_state.py" not in sl, (
            "a run_state.py occurrence falls inside an 'Under `AUTONOMOUS`' "
            f"paragraph:\n{sl}"
        )


def test_escalation_sites_rewind_the_record():
    text = _text(RUN_SKILL)
    for sl in _escalation_slices(text):
        assert WRITE_CALL in sl, f"escalation slice has no rewind write:\n{sl}"
        assert ESCALATION_REWIND in sl, (
            f"escalation slice does not rewind phase to fast_path_triage:\n{sl}"
        )


def test_escalation_sites_frame_and_order_the_rewind():
    """Each escalation slice must (a) carry a framing sentence introducing the
    rewind -- not a bare code block -- and (b) perform the rewind BEFORE
    transferring control with "re-enter at the architect phase". A rewind
    ordered after the re-enter instruction is scheduled after control has
    already left the atomic unit and, on an interactive run with no
    sentinels, is never reached.
    """
    text = _text(RUN_SKILL)
    for sl in _escalation_slices(text):
        assert REWIND_PHRASE in sl, f"escalation slice has no rewind framing sentence:\n{sl}"
        assert REENTER_PHRASE in sl, f"escalation slice has no re-enter instruction:\n{sl}"
        assert sl.index(REWIND_PHRASE) < sl.index(REENTER_PHRASE), (
            f"rewind must precede re-enter in escalation slice:\n{sl}"
        )


def test_escalation_rewind_at_the_blocked_site_is_inline_on_one_line():
    text = _text(RUN_SKILL)
    slices = _escalation_slices(text)
    single_line = [sl for sl in slices if "\n" not in sl.strip()]
    assert len(single_line) == 1, (
        f"expected exactly one single-physical-line escalation site, found {len(single_line)}"
    )
    sl = single_line[0]
    assert WRITE_CALL in sl
    assert sl.strip() != ""


def test_phase_six_calls_clear():
    text = _text(RUN_SKILL)
    slices = _phase_heading_slices(text)
    assert CLEAR_CALL in slices["end_of_task"]


def test_phase_six_write_precedes_clear():
    text = _text(RUN_SKILL)
    slices = _phase_heading_slices(text)
    phase6 = slices["end_of_task"]
    assert phase6.index(WRITE_CALL) < phase6.index(CLEAR_CALL)


def test_thorough_plan_round_boundary_calls_writer():
    text = _text(THOROUGH_PLAN_SKILL)
    assert WRITE_CALL in text


def test_thorough_plan_writer_is_require_existing():
    tp_text = _text(THOROUGH_PLAN_SKILL)
    assert tp_text.count("run_state.py --write --require-existing") == 1
    # The round-boundary refresh must never adopt: it runs in a planning
    # subagent whose id must not displace the record's creator id.
    assert "--adopt-session" not in tp_text
    run_text = _text(RUN_SKILL)
    # /run's ONLY require-existing write is the Resume entry re-anchor, and
    # it must carry --adopt-session on the same call; every other /run
    # write site stays a plain --write.
    assert run_text.count("--require-existing") == 1
    assert run_text.count("run_state.py --write --require-existing --adopt-session") == 1


def test_thorough_plan_writer_call_follows_the_ivg98_checkpoint():
    text = _text(THOROUGH_PLAN_SKILL)
    checkpoint_idx = text.index("thorough_plan_checkpoint.py")
    writer_idx = text.index("run_state.py --write --require-existing")
    assert checkpoint_idx < writer_idx


GATE_SENTENCE_ARCHITECT = "spawn `/gate` as a subagent session (architecture gate"
GATE_SENTENCE_IMPLEMENT = (
    "run `/gate` inline (read `/gate/SKILL.md` from the same session and "
    "execute the gate process directly — do not spawn a subagent)"
)
GATE_SENTENCE_REVIEW = "run `/gate` inline (Full level, post-review"
END_OF_TASK_NEXT_ACTION = '--next-action "start end_of_task"'


def test_boundary_write_follows_its_gate_sentence():
    """A phase-boundary run-state write positioned BEFORE the gate/verdict
    that determines it lets a plain `/run` resume skip the gate -- and, on
    Phase 5, re-enter `end_of_task` after a CHANGES_REQUESTED or BLOCKED
    review. Pin each write's offset strictly after its gate sentence's
    offset within the same phase slice.

    Both offsets are computed from position 0 of the slice, never chained
    from the gate offset -- `slice.index(X, gate_idx)` only ever finds a
    match AT OR AFTER `gate_idx` by construction, which makes `write_idx >
    gate_idx` true whenever *any* matching call sits later in the slice,
    including one that is not the write under test (the implement slice's
    fast-route escalation rewind sits between its gate and its real
    boundary write and shares the generic `WRITE_CALL` substring; the
    review slice's "accept" branch write shares `END_OF_TASK_NEXT_ACTION`
    with the APPROVED branch's own write). Locating each write by its own
    unique `--next-action` value from position 0 avoids both: a value that
    is unique within its slice can only be found at the real write's
    offset, so a mutation that moves that write earlier is caught as a
    genuine `write_idx < gate_idx` failure instead of being masked by a
    different call site satisfying the assertion.
    """
    text = _text(RUN_SKILL)
    slices = _phase_heading_slices(text)

    architect_slice = slices["architect"]
    gate_idx = architect_slice.index(GATE_SENTENCE_ARCHITECT)
    write_idx = architect_slice.index(WRITE_CALL, gate_idx)
    assert write_idx > gate_idx, "architect boundary write precedes its gate sentence"

    implement_slice = slices["implement"]
    gate_idx = implement_slice.index(GATE_SENTENCE_IMPLEMENT)
    write_idx = implement_slice.index('--next-action "start review"')
    assert write_idx > gate_idx, "implement boundary write precedes its gate sentence"

    review_slice = slices["review"]
    gate_idx = review_slice.index(GATE_SENTENCE_REVIEW)
    write_idx = review_slice.index(END_OF_TASK_NEXT_ACTION)
    assert write_idx > gate_idx, (
        "review's 'start end_of_task' write precedes the post-review gate sentence"
    )


def test_review_next_action_is_verdict_conditional():
    """The review-verdict branches must each write a distinct,
    verdict-appropriate `next_action` -- never a single unconditional write
    made before the verdict is known. The APPROVED and "accept" branches
    both resolve through Checkpoint D, so their write is deferred past the
    checkpoint rather than each carrying its own copy: a session lost
    between the verdict/gate resolving and the checkpoint answer, or a
    user who answers `no`, must not leave `next_action: start end_of_task`
    on disk for a continuation nobody confirmed.
    """
    text = _text(RUN_SKILL)
    slices = _phase_heading_slices(text)
    review_slice = slices["review"]

    approved_idx = review_slice.index("**If APPROVED:**")
    changes_idx = review_slice.index("**If CHANGES_REQUESTED:**")
    blocked_idx = review_slice.index("**If BLOCKED:**")
    checkpoint_d_idx = review_slice.index("**Checkpoint D**")
    assert approved_idx < changes_idx < blocked_idx < checkpoint_d_idx

    approved_block = review_slice[approved_idx:changes_idx]
    changes_block = review_slice[changes_idx:blocked_idx]
    blocked_block = review_slice[blocked_idx:checkpoint_d_idx]
    post_checkpoint_d_block = review_slice[checkpoint_d_idx:]

    # Neither pre-Checkpoint-D branch claims `end_of_task` is next -- that
    # write is deferred until Checkpoint D itself resolves to continue.
    assert END_OF_TASK_NEXT_ACTION not in approved_block
    assert END_OF_TASK_NEXT_ACTION not in changes_block
    assert END_OF_TASK_NEXT_ACTION not in blocked_block
    assert '--next-action "start implement"' in changes_block
    assert '--next-action "run blocked"' in blocked_block
    assert '--next-action "start implement"' not in blocked_block

    # The APPROVED-or-accepted write fires exactly once, after Checkpoint D
    # resolves to continue -- never before either path's precondition (an
    # APPROVED verdict with the gate PASSED, or an "accept" decision) has
    # actually been confirmed.
    assert post_checkpoint_d_block.count(END_OF_TASK_NEXT_ACTION) == 1


def test_next_action_roster_census():
    """Every `--next-action "..."` value written anywhere in either
    SKILL.md must parse as `start <phase>` against the known-phase roster,
    or be one of the two documented terminal markers. Reverting
    `/thorough_plan` to `--next-action "start round {N+1}"` fails here --
    `round` is not a phase name and the whole value never matches
    `start <phase>` against the roster either.
    """
    roster = _known_phase_roster()
    blocks = _call_blocks(_text(RUN_SKILL)) + _call_blocks(_text(THOROUGH_PLAN_SKILL))
    checked = 0
    for block in blocks:
        for value in _NEXT_ACTION_RE.findall(block):
            checked += 1
            if value in TERMINAL_MARKERS:
                continue
            assert value.startswith("start "), (
                f"--next-action value {value!r} is neither 'start <phase>' "
                f"nor a documented terminal marker:\n{block}"
            )
            phase = value[len("start ") :]
            assert phase in roster, (
                f"--next-action names unknown phase {phase!r} (not in "
                f"RESUMABLE_PHASES):\n{block}"
            )
    # Sanity: the census must actually have examined call sites, not
    # vacuously passed because the regex matched nothing.
    assert checked >= 15, f"expected >=15 --next-action call sites, found {checked}"


def test_every_writer_call_site_quotes_its_path_flags():
    """A `run_state.py` call site that leaves `--project-root`, `--task`,
    or `--artifact` unquoted breaks on any project path containing a
    space. Every `--write`/`--clear` call site that carries one of these
    three flags must quote its value.
    """
    blocks = _call_blocks(_text(RUN_SKILL)) + _call_blocks(_text(THOROUGH_PLAN_SKILL))
    assert blocks, "no run_state.py --write/--clear call sites found"
    for block in blocks:
        for flag, bare_re in _BARE_FLAG_RE.items():
            assert not bare_re.search(block), (
                f"--{flag} is unquoted in call site:\n{block}"
            )
        for flag, quoted_re in _QUOTED_FLAG_RE.items():
            if f"--{flag}" in block:
                assert quoted_re.search(block), (
                    f"--{flag} present but not in quoted 'flag \"value\"' form:\n{block}"
                )


def test_resume_fallthrough_sentence_is_pinned_verbatim():
    """Pin the reader's fall-through sentence verbatim alongside the four
    existing invariant pins, so deleting it fails this test directly
    rather than only behaviourally.
    """
    text = _text(RUN_SKILL)
    assert (
        'treat this tier as if it had returned nothing and fall through' in text
    ), "Resume Step 1's fall-through sentence is missing or reworded"


def test_step1_prose_roster_matches_supervisor_roster():
    """Step 1's phase roster is hand-copied into prose
    (the 'known phase names' parenthetical) and duplicated again as a local
    `RESUMABLE_PHASES` tuple in test_run_state_resume_precedence.py.
    Nothing previously asserted the two stay in step. Parse the prose
    roster and pin it against `quoin.supervisor.RESUMABLE_PHASES`, the
    single source of truth both copies are meant to mirror.
    """
    text = _text(RUN_SKILL)
    m = re.search(
        r"known phase names \((.*?)\), parse the phase name out of it",
        text,
        re.S,
    )
    assert m, "could not find the 'known phase names' roster sentence in Step 1"
    prose_roster = tuple(re.findall(r"`([a-z_]+)`", m.group(1)))
    roster = _known_phase_roster()
    assert prose_roster == roster, (
        f"Step 1's prose roster {prose_roster} has drifted from "
        f"quoin.supervisor.RESUMABLE_PHASES {roster}"
    )


def test_no_writer_call_uses_literal_tilde_claude():
    for path in (RUN_SKILL, THOROUGH_PLAN_SKILL):
        text = _text(path)
        for line in text.splitlines():
            if "run_state.py" in line:
                assert "~/.claude" not in line, f"{path}: literal tilde-claude in: {line}"


def test_run_resume_adopt_follows_the_staleness_gated_reads():
    run_text = _text(RUN_SKILL)
    adopt_idx = run_text.index("run_state.py --write --require-existing --adopt-session")
    read_idx = run_text.index("--fields schema,active,phase")
    # The adopt write must sit BELOW the staleness-gated reads: it refreshes
    # the record mtime, so running first would revive a record the reads'
    # freshness window had already excluded.
    assert read_idx < adopt_idx
    # And it must pass the same validated freshness window the reads use.
    tail = run_text[adopt_idx:adopt_idx + 400]
    assert '--max-age-days "$_RUN_STATE_STALE_DAYS"' in tail
