# run-unattended

Runtime-neutral intent for the run-unattended skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

Start the unattended run of a task (`quoin run --autonomous <task>`) in the
background from inside a session and hand control back at once. Each
unfinished stage then runs as its own headless `/goal` session; the session
that launched it does not wait.

## When to use

- When the person asks to run a task's stages unattended or overnight.
- When the staged route's stage confirmation picks "run the stages unattended".

## Inputs

- The task name, whose folder `.workflow_artifacts/<task>/` holds the stage
  list (`architecture.md`) or, for a single change, `brief.md`.
- Optional: a model and a per-stage spending cap.

## Outputs

- A detached `quoin run --autonomous` process, its pid in
  `.workflow_artifacts/<task>/unattended.pid` and its output in
  `.workflow_artifacts/<task>/unattended.log`; per-stage results go to
  `goal-run.log` in the same folder.
- A short reply saying what started and where to watch it.

## Rules

- Never waits for the run and never edits code.
- Refuses to start a second run of a task whose first is still running.
