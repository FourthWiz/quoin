# /cleanup task bookkeeping pass — reference

Full interactive procedure for the standalone `/cleanup` task bookkeeping pass.
The cleanup SKILL.md only carries a short pointer to this doc; read this file
for the exact steps, wording, and decision-gate markers.

## Purpose

Sorts `.workflow_artifacts/` task folders into five buckets — done, abandoned,
nearly-done, in-progress, not-a-task — using `task_bookkeeping.py classify`,
then offers to archive or trash the ones that are safe to move. Every move
goes through the classifier's move subcommand, one task at a time, only
after the person confirms that specific task.

## Inputs

Read from `python3 __QUOIN_HOME__/scripts/task_bookkeeping.py classify --format json`:

- `rows[].task`, `rows[].bucket`, `rows[].evidence` — what to show the person.
- `rows[].options` — the exact, ordered option list for that row's
  `AskUserQuestion` (already ≤4; never re-derive this list).
- `rows[].recommended_action`, `rows[].prompt` — whether the row is even
  worth asking about (non-idle in-progress and not-a-task rows are `report`/
  `skip` and are never prompted).
- `rows[].next_commands` — the `/pr` / `/end_of_task …` lines to print, both
  as an option label and in the final summary.
- `rows[].fingerprint` — passed back as the `--expect` value when a move is
  confirmed, so a stale prompt can never move a folder that changed after
  `classify` ran.
- `gh.status` — informational only; never gates whether the pass runs.

## Report-only conditions and exact message

The pass is report-only when any of: `--dry-run`, `[no-interactive]`,
`[autonomous]`, or no `AskUserQuestion` tool is available in the running
context. Print exactly:

```
[cleanup] task bookkeeping: report-only (<reason>); no folders moved
```

`<reason>` is one of `--dry-run`, `no-interactive`, `autonomous`, or
`no-AskUserQuestion`.

When `<reason>` is `no-AskUserQuestion` — the common case where `/cleanup` was
dispatched to an Agent-tool subagent, which is never given `AskUserQuestion`
(see `decision-gate-guard.md`'s POC finding) — also print a resume hint so the
person has a next step instead of a silent dead end:

```
to act on these rows, run: [no-redispatch] /cleanup
```

This is a known, accepted limitation of the interactive flow: the pass still
fails safe (nothing moves) when it can't ask.

## Interactive flow

When an interactive `AskUserQuestion` channel is available, walk the buckets
in order: done → abandoned → nearly-done → idle in-progress. Non-idle
in-progress and not-a-task rows are never prompted (`report`/`skip`).

For each bucket with 2 or more promptable rows, ask one mode question first:

<!-- decision-gate: best-effort site=cleanup-tasks-mode -->
```
AskUserQuestion(
  question="<bucket> has N tasks. Decide per task, or the same answer for all N?",
  options=[
    {label: "Decide per task", description: "Ask about each task individually."},
    {label: "Same answer for all N", description: "Apply one choice to every task in this bucket."}
  ]
)
```

"Same answer for all N" asks once using the bucket's common option list; any
row that lacks the chosen option (e.g. a row with `archive_blocked: true`)
falls back to being asked per-task instead.

Per-task questions use each row's `options` list verbatim, batched up to 4
questions per `AskUserQuestion` call:

<!-- decision-gate: best-effort site=cleanup-tasks-row -->
```
AskUserQuestion(
  question="<task> — <bucket>. <first evidence item>. What do you want to do?",
  options=<row.options, verbatim, in order>
)
```

## Applying answers

For each row the person confirmed an archive or trash action for:

```
python3 __QUOIN_HOME__/scripts/task_bookkeeping.py apply <archive|trash> <task> --expect <row.fingerprint>
```

On a non-zero exit, print that task's JSON error line and continue to the
next confirmed row — one failed move never blocks the rest of the batch.

### Option label -> action mapping

Every option label in `rows[].options` maps to exactly one of these; there is
no other action an answer can produce:

| option label | action |
|---|---|
| `Archive` | `apply archive <task> --expect <row.fingerprint>` |
| `Archive anyway` | `apply archive <task> --expect <row.fingerprint>` |
| `Trash` | `apply trash <task> --expect <row.fingerprint>` |
| `Leave` | no action — skip this row |
| `Print /pr` | no action — collect `/pr` into the closing summary's Next commands block |
| `Print /end_of_task <task>` | no action — collect `/end_of_task <task>` into the closing summary's Next commands block |

## Summary

After all confirmed moves:

```
[cleanup] archived N task(s) -> .workflow_artifacts/finalized/
[cleanup] trashed M task(s) -> .workflow_artifacts/trash/<date>/ (recover: mv .workflow_artifacts/trash/<date>/<task> .workflow_artifacts/)
```

Then a block collecting every `/pr` and `/end_of_task …` line the person
chose to print (from nearly-done rows), for them to run themselves:

```
Next commands (type these yourself):
/pr
/end_of_task <task>
```

## Trash location

Trashed task folders land in the top-level `.workflow_artifacts/trash/<date>/`,
not under `memory/trash/`. This is a deliberate departure from the original
spec assumption: top-level `trash/` already holds task-shaped trees and is
excluded from every scanner (`status_graph`, `dashboard_model`,
`task_bookkeeping`); putting task trees under `memory/` would perturb
`memory_version_key`'s recursive walk and memory browsing instead. Folders
here are never deleted — recover with a plain `mv`.

## What this pass never does

- Never invokes `/pr` or `/end_of_task` — it only prints the command for the
  person to run themselves.
- Never commits, pushes, or touches any repo.
- Never touches Linear or any other external tracker.
- Never re-derives a row's bucket or options — those come from `classify`
  verbatim.
- Never moves a folder without that specific folder's confirmation.
