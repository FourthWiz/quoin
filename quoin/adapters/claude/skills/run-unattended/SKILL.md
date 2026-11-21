---
name: run-unattended
description: "Starts a task's stages as an unattended background run. Use for: /run-unattended, 'run it overnight'."
model: haiku
allowed-tools: Read, Bash(python3:*)
---

# Run unattended

*Portable intent doc: `quoin/core/skills/run-unattended.md`*

You start a task's unattended run in the background and hand control back. You do not wait for it, watch it, or change code.

## Inputs

`/run-unattended <task> [--model <model>] [--max-budget-usd <n>]`. The task is the folder name under `.workflow_artifacts/`. With no task, use the active task from the brief or architecture you were working from; if there is none, ask for it.

## Step 1: Check the task is ready

Read `.workflow_artifacts/<task>/architecture.md`. It needs a `## Stage decomposition` whose rows each have `Repo:` and `Acceptance:` lines, and a `Branch:` line naming the task branch. Without an architecture.md, `.workflow_artifacts/<task>/brief.md` must exist (the run then treats the whole task as one goal). If neither is there, say what is missing and stop.

## Step 2: Launch

From the project root (the folder holding `.workflow_artifacts/`), run this one command, with no `cd` or `&&` chain:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/launch_unattended.py --task "<task>" --project-root "<project root>" [--model <model>] [--max-budget-usd <n>]
```

It prints JSON. On `"started": false`, report its `error` and stop.

## Step 3: Report and end the turn

Reply in three or four lines: the run started (pid), each unfinished stage runs as its own `/goal` session, progress is in `goal-run.log` and output in `unattended.log` (give both paths), and rerunning `/run-unattended <task>` after it stops continues at the first unfinished stage (a usage limit stops it with exit code 75). Then end your turn.
