# run

Runtime-neutral intent for the run skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

End-to-end orchestration of the development workflow from discovery
through finalization. The skill coordinates other skills in sequence —
discover, architect, plan, implement, review, and finalize — and
pauses at every phase boundary for an explicit user confirmation. It
does not perform the work of those phases itself; it sequences, hands
off, and gates.

## When to use

- When the user wants the full pipeline in one invocation rather than
  manually advancing through each phase.
- Never auto-invoked by any other skill; always a deliberate user
  choice.
- The orchestrator is the documented exception that may invoke the
  implementation and finalization skills on the user's behalf, but
  only after the user confirms at the relevant checkpoint. The user's
  invocation constitutes the conscious decision; checkpoint
  confirmations are the safety net.

## Inputs

- Task description (required).
- Profile and round-cap overrides parsed from the task description
  (optional; adapter-specific syntax).
- Prior discovery outputs under `.workflow_artifacts/memory/` (optional;
  adapter checks staleness and skips rediscovery when recent).
- Per-stage artifacts under `.workflow_artifacts/<task-name>/` (optional;
  used for resume support).
- Session-state file under `.workflow_artifacts/memory/sessions/` (optional;
  used to identify the last completed phase on resume).
- Knowledge cache under `.workflow_artifacts/cache/` (optional; advisory).

All reads MUST tolerate missing files. The stage-aware path resolver MUST
be honored when locating per-stage artifacts.

## Output

- A task folder at `.workflow_artifacts/<task-name>/` populated with all
  phase artifacts produced by the orchestrated skills.
- A cost ledger at `.workflow_artifacts/<task-name>/cost-ledger.md` with
  one row per phase session, including the orchestrator's own session.
- A session-state file under `.workflow_artifacts/memory/sessions/`
  tracking current phase, outcomes, and checkpoint history.
- A final completion report rendered to the user after finalization.

## Behavior contract

- Shipped authored content (comments, commit messages, PR descriptions) MUST follow the clean-authored-content rule: plain engineering language, no plan/decision/finding IDs, severities, review-round narration, gate verdicts, or planning-artifact paths.
- The skill MUST chain phases in this order, each conditional on the
  routing outcome determined earlier in the pipeline rather than an
  unconditional fixed sequence: discover (conditional — skip if
  recent), spec (conditional — skip if Small or if a task spec already
  exists), a routing step that decides between two paths, architect
  (conditional — skip if Small profile, or if the routing step chose
  the abbreviated path), planning (conditional — skip on the same
  terms as architect), implementation, review, finalization. The
  routing step MAY skip straight from spec to implementation, carrying
  a mechanically-derived plan of its own, when its eligibility criteria
  are met and the user (or, under autonomous mode, the formulation
  quality bar) confirms it.
- After discovery, when no task feature spec exists at
  `<task-root>/spec.md` and the task is not Small, the skill SHOULD
  offer/run the spec phase, gated at its own checkpoint; the skill
  MUST forward the task spec path to the downstream design and review
  phases when present; absence of a spec is always a valid,
  non-blocking outcome (grandfather).
- The skill MUST pause at every phase boundary and wait for explicit
  user confirmation before advancing to the next phase.
- The skill MUST treat each heavy phase as a separate session; the
  orchestrator's own context must remain lean across the full pipeline. A
  lightweight in-orchestrator routing step is permitted where the runtime
  needs an interactive prompt the orchestrator itself must raise.
- The skill MUST honor profile triage (Small / Medium / Large) when
  routing sub-skills.
- The skill MUST NOT auto-invoke implementation or finalization without
  a prior user confirmation at the dedicated checkpoint for each phase.
- The skill MUST preserve all artifacts if the user stops the pipeline
  at any checkpoint; partial results remain under
  `.workflow_artifacts/<task-name>/` and can be resumed.
- The skill MUST run finalization in a fresh context for unbiased
  assessment downstream.
- The skill MUST tolerate missing optional inputs (stale or absent
  discovery files, absent architecture, absent prior session state).
- The skill MUST update the session-state file after each phase
  completes so that a resume invocation can identify the next phase
  without rerunning completed work.
- Cost-ledger writes are mandatory when a task context is active.

## Out of scope

- Model tier per phase — adapter-specific.
- Subagent dispatch mechanism — adapter-specific.
- Gate invocation modes (inline vs. subagent) — adapter-specific.
- Session-age guard and long-session warnings — adapter-specific.
- Stream-idle timeout retry policy — adapter-specific.
- JSONL-based cost capture and per-runtime fallback chain —
  adapter-specific.
- CLI mechanics and project-hash derivation — adapter-specific.
- The specific names of the sub-skills invoked at runtime — those are
  adapter-specific.

## Autonomous mode (opt-in)

- Adapters MAY support an opt-in autonomous span: a single explicit
  user-supplied flag that, once accepted, carries the run from
  formulation through finalization with zero prompts.
- Entering the autonomous span still requires clearing a documented
  quality bar via a non-interactive formulation before execution
  begins. A formulation that fails the bar is a hard stop, not a
  silent proceed.
- Every hard stop already defined in this contract — a blocked
  downstream verdict, a failed check past its retry cap, a git
  conflict, a branch-hygiene violation, or a below-bar formulation —
  remains in force under the autonomous span. A hard stop halts the
  run and records the reason for a later resume rather than prompting.
- A hard stop records its reason in a stable sentinel file before the
  run exits. The sentinel schema is five one-line fields: `task`,
  `phase`, `reason`, `timestamp`, `resume_hint`. The sentinel's
  location is stable and lives outside the task-scoped artifact
  folder, so the record survives that folder's later archival.
- The autonomous span MUST NOT create a pull request under any
  circumstance; PR creation stays a separate, explicit user action
  outside this skill's scope, in both interactive and autonomous
  modes.
- Which prompt each downstream phase auto-resolves to, and how
  formulation confidence is scored, is adapter-specific and documented
  in the adapter's own reference material.

## Autonomous durability contract (opt-in supervisor)

An adapter MAY implement an external supervisor that relaunches fresh
sessions to carry an autonomous span across a context-window boundary
that a single session cannot fit in. When it does, the relaunch and
resume MUST agree on the following sentinel path templates, all of
which resolve under `.workflow_artifacts/memory/` — deliberately
outside the task-scoped artifact folder, so each sentinel survives
that folder's later archival:

- **Marker** — `autonomous-run-{task}.marker`, one line, written once
  at autonomous-span entry, before the first phase begins. A resumed
  session reads this marker first, before its own first decision
  point, to re-establish the autonomous span rather than reverting to
  an interactive default and stalling.
- **Per-phase completion sentinels** — `autonomous-progress-{task}/{phase}.done`,
  one file per completed phase. The phase set is the full resumable
  roster — discover, enrich, specify, fast_path_triage, architect,
  thorough_plan, implement, review, end_of_task — documented in full,
  with each phase's skip/run condition, by the adapter's own
  phase-sequence description; every phase in that roster gets a
  completion sentinel, none silently excluded. Finer
  progress within a single long-running phase MAY additionally write
  `autonomous-progress-{task}/{phase}.{subphase}.done`. The counting
  glob `autonomous-progress-{task}/*.done` is the union of both forms,
  so either kind of new sentinel counts as forward progress for a
  supervisor's no-progress guard.
- **Done sentinel** — `autonomous-done-{task}.md`, written last, after
  finalization's other side effects complete, so a relaunch after a
  kill mid-finalization can tell the span already reached its terminal
  step and stop without redoing that work. Because a kill anywhere
  inside finalization re-runs the whole terminal phase on relaunch,
  every side-effecting finalization step MUST be individually
  idempotent — each check-and-skip guards its own already-done work
  (a push is a no-op when the branch already matches its remote at the
  same commit; a lessons/cost step skips when its own completion
  sentinel is present; an archive move skips when the target already
  exists) — not only the push and the archive.
- **Halt sentinel** — the hard-stop record already defined above
  (`autonomous-halt-{task}.md`); a resuming supervisor checks it after
  the done sentinel, before deciding whether to relaunch again.

A phase MAY record a completion marker (`{phase}.tasks.done`) meaning its
work is done and its gate is pending. The marker counts toward the union
glob, is never a resumable sub-phase, and never substitutes for
`{phase}.done`. A resume into a phase whose plan reports every task done
runs that phase's gate directly and writes `{phase}.done` only after the
gate passes. A spent automatic gate retry survives a relaunch and is reset
only when the run halts for a human.

An adapter supervisor SHOULD give each headless child a session id it
chose itself, record that id before the child starts, and offer a
stop-then-attach verb (for example `quoin run --takeover <task>`) that
confirms the child is stopped before printing the command that resumes it
interactively. Halt files MAY carry a `takeover_hint:` line pointing at
that verb; the five-field halt schema is otherwise unchanged.

This document fixes only the path templates and the phase-roster
coverage rule, so independently implemented supervisors and resumers
agree on the contract shape; the supervisor loop mechanics themselves
(relaunch cap, backoff, launcher) are adapter-specific.

A within-phase mechanism complements the cross-phase relaunch above: a
phase subagent MUST exchange only paths and short summaries with the
orchestrator, never raw content, so the orchestrator's own context stays
bounded across the whole span. A phase subagent nearing its own limit
writes a checkpoint to disk and reports a partial-completion signal
instead of exhausting itself; the orchestrator answers by dispatching a
fresh subagent to continue the same phase from that checkpoint, repeating
until the phase reports normal completion. Whether a subagent detects its
own nearness-to-limit or falls back to a fixed work-unit cap is
adapter-specific.

The channel a spawned phase uses to report its own completion or partial
progress back to the orchestrator is a return envelope, whose full field
set is declared in `quoin/core/workflow/handoff-format.md`, the
return-channel contract shared by every adapter this document constrains.
The partial-completion signal named above is a `status` value of that
same return shape, carrying the checkpoint path the orchestrator resumes
from. This paragraph describes that success-path return only; a phase
that fails closed is a separate, adapter-owned channel this document does
not describe.

A resume that honors this contract MUST read the marker before any
other decision point, so a relaunch never reverts to an interactive
default and stalls; MUST derive the next phase from the completion
sentinels rather than from prose state alone, treating a present
sentinel as never-re-run and an absent one as never-skip; and MAY use
sub-phase sentinels to resume partway through a single long phase
rather than restarting it. Each resumable phase is responsible for
writing its own completion sentinel once its work is done, so the
reader and writer sides of this contract stay independently
verifiable.

## Resume record (plain, non-autonomous runs)

The sentinel contract above (marker, per-phase `.done` files, halt/done
sentinels) only exists under the opt-in autonomous supervisor. A plain,
non-autonomous invocation of this skill — no supervisor, no relaunch —
writes none of those files, so it needs an independent, always-on
resumability record: a single JSON file,
`run-state-{task}.json`, alongside its append-only companion notes log
(`run-notes-{task}.md`), both under `.workflow_artifacts/memory/` for the
same archival-survival reason the sentinels live there.

The orchestrator writes this record at every phase boundary — unconditional
on autonomous mode, and unconditional on whether the phase itself ran or
was skipped by its own skip condition — recording, at minimum, which phase
just completed and what to start next. A resume reads it back only when
the sentinel-derived answer is unavailable (no per-phase `.done` sentinels
exist for the task), refining rather than overriding whatever the sentinel
contract already decided. The record additionally supports resuming
partway through a single long phase, at whatever granularity that phase's
own boundary writes checkpoint to — the shape is a phase/sub-phase/step
triple that a resume applies in that order, most-specific first, falling
back one level whenever the next-more-specific answer is absent or stale.
An entry whose age exceeds an adapter-defined freshness window is treated
as absent, never as a decision.

The record also carries the id of the session that created it — an anchor a
runtime's compaction-time machinery may use to recognize the record's owner.
Because that creator session is dead after any resume, the one sanctioned
migration of this field is a resume-entry adoption write that re-anchors the
record to the resuming session; it runs only after the resume's
freshness-gated reads of the record, and it is refused outright for a record
the freshness window already treats as absent, so adoption can never revive
a stale entry. Ordinary refresh writes preserve the stored id unchanged.

A reader determines what to resume from `next_action`, never from `phase`
— `phase` names the last-completed phase (or, in the sub-phase case, the
phase currently in flight), never the resume target, so keying resume
logic on it conflates "already ran" with "run next." Two values of
`next_action` are documented terminal markers that deliberately do not
parse against the phase-name format at all: one for run completion
(written immediately before the record is cleared) and one for a
blocking verdict that must not be silently resumed past. A reader that
meets either marker, or any other `next_action` value it cannot parse
against a known phase name, treats the record as if it had returned
nothing and falls through to the next resumability source in the tier
order above, rather than misreading a marker as a phase to resume into.

Because this record is the ONLY resumability source on a plain run, an
adapter implementing it MUST write it only once the outcome it records is
actually known — never before the gate or verdict that determines what
comes next — so a session lost mid-phase never resumes into a decision
that was never actually made. The full write-ordering and quoting
constraints this implies are adapter-specific (see the Claude adapter's
own `run/SKILL.md` for the worked contract); this document fixes only the
record's existence, its file-path shape, and the "never before the
decision it records" ordering rule, so an independently implemented
resumer and writer agree on the same guarantee.

## Notes

- The orchestrator owns coordination only, never artifact content;
  runtime adapters own model tiers, dispatch mechanisms, cost plumbing,
  and any retry or fail-OPEN behavior.
- Checkpoints are non-optional even when the user invoked the skill
  with a "run everything" intent. Every phase boundary requires a
  conscious confirmation.
- All artifacts produced before a stop are preserved under
  `.workflow_artifacts/<task-name>/` and can be resumed individually or
  via a resume subcommand.
