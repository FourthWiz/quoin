# Session state schema

The one shape every Claude Code skill writes to `.workflow_artifacts/memory/sessions/`. Skills point here instead of carrying their own section lists. The machine-checkable form is the `session` entry in `format-kit.sections.json`; this file adds the field rules, the merge rule and the reader map.

## Scope

- Governs session files written by Claude Code skills.
- Describes, but does not govern: the core-written orchestrator file (last section) and Codex handoff files that share the folder (those follow the Codex adapter's own shape).
- Legacy files already on disk are not migrated. Every reader tolerates the old shapes.

## File name

`<YYYY-MM-DD>-<task-key>.md`, local date. The task key may carry `-stage-N`. One file per task per day: later runs the same day rewrite it (see Merge rule).

## Canonical example

```markdown
---
date: 2026-01-02
task: demo
---

## Status
in_progress

## Current stage
implement task 2 of 4

## Completed in this session
- [plan] converged plan, 1 round
- [implement] schema doc written (abc1234)

## Unfinished work
- wire the writers (adapters/claude/skills/plan/SKILL.md first)
- schema test

## Next command
/implement

## Decisions made
- [plan] one schema file instead of per-skill lists: fewer places to drift

## Open questions
- none

## Cost
- Session UUID: 11111111-2222-3333-4444-555555555555
- Phase: implement
- Recorded in cost ledger: yes
- end_of_day_due: yes
- fallback_fires: 0
- verification_ran: no
- verification_mismatches: 0
```

Frontmatter is `date` and `task`. The value of each section goes on the line(s) after the heading, never on the heading line.

## Sections

Order is fixed. Cost goes last so an in-place flip that appends at end of file still lands inside `## Cost`.

| Heading | Required | Content rule | On same-day rewrite |
|---|---|---|---|
| `## Status` | required | one word: `in_progress`, `completed` or `blocked` | replace |
| `## Current stage` | required | one line: skill and position (`implement task 2 of 4`) | replace |
| `## Completed in this session` | required | one item per line, starts with the writer's phase token in brackets, commit hash when there is one | accumulate |
| `## Architectural design summary` | optional | architect only: the design in a few lines | replace by architect, otherwise preserve |
| `## Unfinished work` | required | one item per line with the exact resume point, at most 12 items | replace |
| `## Next command` | required by this schema (optional in the sidecar) | one line: the command to type next, stage in the command for stage-scoped tasks | replace |
| `## Decisions made` | optional | non-obvious choices with the reason, phase token first | accumulate |
| `## Open questions` | optional | decisions waiting on the user, at most 12 items; `none` when empty | replace |
| `## Cost` | required | fixed `- key: value` lines, next section | field rules below |

Keep the whole file near 4 KB; that is a target, not a check. No other headings: rationale goes into Decisions, finished steps into Completed, gotchas into the daily insights scratchpad.

## Cost fields

Lines are `- key: value`, in this order. The `- ` prefix is required (cost_snapshot's regex needs it). The order of fields does not matter to readers, because the finalize step inserts `finalized_by_end_of_task` next to the flag line.

| Field | Written by | On rewrite | Read by |
|---|---|---|---|
| `Session UUID` | the writing skill | set to this run's UUID | checkpoint_picker, end_of_day |
| `Phase` | the writing skill | set | end_of_day |
| `Recorded in cost ledger` | the writing skill | `yes` once the ledger row exists | end_of_day |
| `end_of_day_due` | the writing skill | always `yes`; `/end_of_day` flips it to `no` in place | sessionstart.sh, sessionend.sh, verify_claims.py, select_unprocessed_sessions.py, start_of_day, end_of_day |
| `fallback_fires` | created as `0`; incremented in place by dispatch retries | preserved, never decremented | cost_snapshot and end_of_day (`^- fallback_fires:\s*(\d+)\s*$`) |
| `verification_ran` | the verification step | reset to `no` | end_of_day, verify_claims.py |
| `verification_mismatches` | the verification step | reset to `0` | end_of_day, verify_claims.py |
| `finalized_by_end_of_task` (optional) | `/end_of_task` bookkeeping (a date) | preserved | select_unprocessed_sessions.py, task_bookkeeping.py |

Post-finalize rule: `/pr` (or any skill) that writes the same-day file after `/end_of_task` flipped it sets `end_of_day_due: yes` and keeps `finalized_by_end_of_task`. The next `/end_of_day` rolls the file up once more and its flip is idempotent. This combination is expected, not a bug.

## Timing

One write per skill run, at the skill's exit: complete, partial, blocked or needs-decision. `run_hard` (`/run hard:`) writes at each phase end. In-place field updates (the `fallback_fires` increment, the `/end_of_day` flip, the finalize flip) are not session writes and may happen at any time.

Trade-off: a run killed mid-way (context exhaustion, crash) leaves no session file for that run. Resume then rests on the plan's task glyphs and the run-state record, and the next run that exits writes the file.

## Merge rule

Rewrite the whole file through the atomic finalize; never append. If the file already exists (same date and task):

- Accumulate: keep every earlier item of Completed and Decisions, add this run's items after them, drop exact duplicates, no cap. Each new item starts with the writer's phase token in brackets.
- Replace: Status, Current stage, Unfinished work, Next command, Open questions. Architectural design summary is replaced only by architect.
- Cost: apply the field table (Session UUID, Phase and Recorded are this run's; `end_of_day_due: yes`; `fallback_fires` and `finalized_by_end_of_task` carried over; verification fields reset).

Worked example. The file after `/end_of_task`:

```text merge-before
---
date: 2026-01-02
task: demo
---

## Status
completed

## Current stage
end_of_task

## Completed in this session
- [implement] schema doc written (abc1234)
- [end_of_task] branch pushed (def5678)

## Unfinished work
- none

## Next command
/pr

## Decisions made
- [plan] one schema file instead of per-skill lists: fewer places to drift

## Cost
- Session UUID: 11111111-2222-3333-4444-555555555555
- Phase: end-of-task
- Recorded in cost ledger: yes
- end_of_day_due: no
- fallback_fires: 2
- verification_ran: no
- verification_mismatches: 0
- finalized_by_end_of_task: 2026-01-02
```

The same file after `/pr` runs:

```text merge-after
---
date: 2026-01-02
task: demo
---

## Status
completed

## Current stage
pr

## Completed in this session
- [implement] schema doc written (abc1234)
- [end_of_task] branch pushed (def5678)
- [pr] pull request opened

## Unfinished work
- merge the pull request, then switch to the target branch

## Next command
none after merge

## Decisions made
- [plan] one schema file instead of per-skill lists: fewer places to drift
- [pr] minor version bump: new memory file

## Cost
- Session UUID: 66666666-7777-8888-9999-000000000000
- Phase: pr
- Recorded in cost ledger: yes
- end_of_day_due: yes
- fallback_fires: 2
- verification_ran: no
- verification_mismatches: 0
- finalized_by_end_of_task: 2026-01-02
```

Every earlier Completed and Decisions item survives; the new run added one of each; `fallback_fires` and the finalize marker carried over; `end_of_day_due` is yes again.

## Content readers

- `/end_of_day` daily build: sessions-processed table from Current stage and Status; Completed today and Decisions log from the accumulated lists; carry-forward from Unfinished work, Open questions and Next command.
- `/sleep`: model-read context only. Its scorer reads `daily/insights-*.md`, not session files.
- `/gate`: no longer a profile source. The task profile comes from the plan's convergence summary or its frontmatter `profile:` field. Gate Step 1 still reads the session file for other facts.

## Not written

`## Close snapshot` (no reader), `autonomous: true` (the marker file carries the mode), `pollution_score:` (unwritten since stage 1), and appended narrative sections such as `## Context`, `## Notes` or per-phase plan blocks.

## Orchestrator variant

`<date>-<task>-orchestrator.md` is written by the portable `thorough_plan_checkpoint.py`, not by skills, and the session validator is not run on it. Its headings, as written: `## Active task`, `## Current stage`, `## Branch`, `## Session ID`, `## Status` (`in_progress`), `## Cost` (the same `- key: value` lines). Readers that list sessions skip it when looking for a skill's own state.
