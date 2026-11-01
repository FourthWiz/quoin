---
name: run
description: "Default pipeline: brief, context pack, inline implement, one review, local commit. Use for: /run, 'do this task'. Tags: hard: (full pipeline), --autonomous."
model: opus
---

# Run — Light Task Pipeline

*Portable intent doc: `quoin/core/skills/run.md`*

You take a task from a raw request to a reviewed local commit in one session. The
value you add over a plain session is a sharper task statement, the right context
loaded before coding, proportionate tests and one independent review. You do NOT
run multi-round planning, critic loops, architecture rounds or a separate gate:
benchmarks showed they multiply cost and wall time without improving resolution.

**You ARE an explicit user invocation.** `/run` may commit locally on the user's
behalf. It never pushes and never opens a PR.

## Step 0 — Route

Scan the input before anything else. The task name is the first token that is
not a flag or tag (`--resume`, `--autonomous`, `[autonomous]`, `hard:`, `strict:`,
`small:` and the like); when handing off to `run_hard`, always pass the original
input unchanged apart from removing `hard:`.

1. **`hard:`** or **`strict:`** anywhere in the input → this task wants the full
   heavy pipeline. Invoke the `run_hard` skill with the input unchanged (keep
   `strict:`, strip only `hard:`) and stop; `run_hard` owns everything from there.
2. **`--resume`** (task name as above) → if `.workflow_artifacts/memory/run-state-<task>.json` or
   `.workflow_artifacts/memory/autonomous-run-<task>.marker` exists, the task was
   started by the heavy pipeline: invoke `run_hard` with the input unchanged and
   stop. Otherwise resume the light run: read `.workflow_artifacts/<task>/brief.md`,
   check `git status`/`git log` on the task branch, and continue from the first
   step whose output is missing. If `.workflow_artifacts/<task>/architecture.md`
   exists, the task is staged: continue at the first stage still marked ⏳, from
   the first step of that stage whose output under `stage-N/` is missing.
   **`stage N of <task>`** works the same way but runs only stage N.
3. **`--autonomous`** (or an `[autonomous]` sentinel from a parent session) →
   strip it and set `AUTONOMOUS=true`: never ask the user anything; record
   assumptions in the brief instead.
4. **`small:` / `medium:` / `large:` / `fast:` / `max_rounds: N`** → strip them. They
   select heavy-pipeline profiles and have no effect here. If the user wrote
   `large:` interactively, mention in one line that `/run hard:` runs the full
   pipeline, then continue on the light path.

## Step 1 — Setup (inline)

1. Derive a kebab-case task name; create `.workflow_artifacts/<task>/`.
2. Git: if the tree has uncommitted changes (ignore untracked
   `.workflow_artifacts/`), ask what to do with them (interactive) or leave them
   alone and work on top of them (autonomous). Then create a fresh task branch,
   never committing to main/master: from an up-to-date default branch when a
   remote exists, otherwise from the current HEAD (for example a benchmark
   fixture with no remote). In a multi-repo project, do this in the repo the
   change lands in; a staged task creates the branch in each repo when its first
   stage there starts (Step 4b).
3. Read `.workflow_artifacts/memory/lessons-learned.md` if present, keeping only
   entries relevant to this task.

## Step 2 — Discover (subagent, conditional)

Discovery memory is refreshed when it is missing or older than 7 days
(`QUOIN_DISCOVERY_STALE_DAYS`):

```bash
find .workflow_artifacts/cache/_staleness.md .workflow_artifacts/memory/repo-heads.md -mtime -7 2>/dev/null | head -1
```

- Non-empty output (recent) → skip and say so in one line.
- Empty output (missing or older than 7 days) → spawn one subagent on **opus**
  whose prompt starts with `[no-redispatch]` and runs `/discover` for the project
  root (the prefix stops `/discover` from re-dispatching itself into a second
  subagent). Wait for it; its output files are the project memory the next steps
  read.

## Step 3 — Frame (inline)

Write `.workflow_artifacts/<task>/brief.md`, at most about 40 lines:

```markdown
# <task name>
## Goal
<one or two sentences: the outcome, in the user's terms>
## Acceptance criteria
- <observable, checkable behaviours; include the failing case if this is a bug>
## Non-goals
- <what not to touch>
## Assumptions
- <each one you made because the request did not say>
## Open questions
- <none, or the questions asked and their answers>
## Shape
<single | staged> — <one-line reason>
```

This replaces `/enrich` and `/specify` on the light path. Restate the request
precisely; do not invent requirements.

**Shape.** Decide whether the task is one change or several. Choose **staged**
when any of these hold:

- the change lands in more than one repository;
- parts must land in order and each deserves its own review (a schema or API
  change before its consumers, a shared library before its callers);
- it clearly will not fit one coherent diff (roughly more than three subsystems
  or fifteen files).

Otherwise, and whenever you are unsure, choose **single**. A single task goes
straight on to Step 4; a staged one goes through Step 4 and then Step 4b.

**Clarification.** If the request is unclear in a way that changes what you would
build — two plausible readings, a missing target, conflicting constraints — ask
the user (one `AskUserQuestion` call, at most a few focused questions) before
going further. If it is only unclear in ways that do not change the work, pick the
reasonable reading and list it under Assumptions. Under `AUTONOMOUS`, never ask:
record the assumption and continue.

## Step 4 — Context pack (subagent)

Spawn one read-only subagent (Explore type, sonnet) with the brief's text and the
paths of the discovery memory files. Its job is to return the pack below (at
most about 60 lines), which you then save as `.workflow_artifacts/<task>/context.md`
(the subagent is read-only):

- the files and symbols the change most likely touches, with `path:line`;
- the existing tests covering them, and the command that runs those tests;
- conventions the change must follow (error handling, naming, similar prior code);
- relevant lessons-learned entries;
- anything surprising (generated files, duplicated logic, a second call site).

Tell the subagent to prefer Serena symbol tools when available, to read excerpts
rather than whole files, and to report conclusions, not file dumps. You keep only
its summary in context.

## Step 4b — Decompose (staged tasks only, inline)

Using the brief and the context pack, write `.workflow_artifacts/<task>/architecture.md`
(at most about 60 lines). Keep the stage rows in exactly this form, which the
stage resolver parses:

```markdown
# <task name> — stages
## Overview
<two to four lines: why the task is staged and the order the stages land in>
## Stage decomposition
1. ⏳ S-01: <short stage name>
   - Repo: <repo path; one repo per stage>
   - Scope: <what this stage changes>
   - Depends on: <none | S-0N>
   - Acceptance: <observable checks for this stage>
2. ⏳ S-02: <short stage name>
   ...
## Interfaces between stages
- <each contract a later stage relies on: function signature, API shape, schema>
```

Two to five stages. Each stage must leave every repo it touches building and
passing its tests on its own. If the task needs more than five stages, or a
stage needs its own design work (a data migration strategy, a new public API
with several consumers), stop and escalate instead (see Escalation).

**Confirm.** Interactive: show the stage list and ask once (`AskUserQuestion`):
proceed with these stages, run it as a single change instead, or switch to
`/run hard:`. Apply edits the user asks for. Under `AUTONOMOUS`, proceed and
note the decomposition under Assumptions in the brief.

**Then run Steps 5–8 once per stage, in order.** For stage N:

1. Write `.workflow_artifacts/<task>/stage-N/brief.md` from the stage entry: goal,
   acceptance criteria, non-goals (the other stages' scope), and the interfaces
   it must provide or may rely on.
2. In the stage's repo, make sure the task branch exists (the same branch name in
   every repo, created as in Step 1 the first time a stage touches that repo).
3. If the stage's repo or area is not covered by the task's context pack, run
   Step 4 for the stage brief and save it to `stage-N/context.md`.
4. Steps 5–8 as written, with the stage brief in place of the task brief: the
   review gets the stage brief, the `## Interfaces between stages` section and
   the stage's diff only (base = the commit the stage started from), and its
   notes go to `stage-N/review.md`. One commit per stage.
5. Mark the stage ✅ in `architecture.md` (replace ⏳). If the stage showed the
   decomposition was wrong, update the later stages and add a one-line note to
   the Overview saying why, rather than forcing the old plan.

Continue to the next stage without asking unless an escalation condition hits.
If this session's context is getting heavy after a few stages, stop after a
completed stage and tell the user to continue with `/run --resume <task>` in a
fresh session; resume starts at the first ⏳ stage.

## Step 5 — Implement (inline)

Implement the brief in this session, using the context pack. Plan in your own
reasoning; do not write a plan file. Keep the diff minimal: no drive-by refactors,
no speculative abstractions.

**Tests, proportionate.** Prefer extending an existing test file over creating a
new one. Add about one focused regression test per behaviour change (for a bug,
the test that fails before the fix). Do not add tests for unchanged code, do not
duplicate coverage across files, and do not build fixtures larger than the change.

## Step 6 — Verify (inline)

Run the tests that cover the change once, using the test command from the
context pack. For a Python project you may instead use the affected-test helper:

```bash
python3 __QUOIN_HOME__/scripts/affected_tests.py --project-root "$(pwd)" --format text
```

It only understands pytest: if it reports `ran_pytest=false`, no tests ran, so
do not record a green result; use the context-pack command instead. Fix real
failures and re-run.

If the test environment cannot be built (missing dependencies, sandbox denies
installs, no interpreter), say so in one line, record it in the brief under
Assumptions, and continue. Do not retry environment setup, create virtualenvs
outside the project, or install packages globally.

## Step 7 — Review (subagent, one pass)

Spawn one general-purpose review subagent on **opus**. Give it only the brief, the
full diff of the task against its base, and the verification result. The work is
not committed yet, so first mark new files with `git add -N <new files>` and pass
`git diff <base>` (base = the commit the task branch started from); a plain
`git diff` leaves out new files.
Do not pass your reasoning or the context pack, so its read is not anchored on
yours. Ask it for at most about 10 findings, each with `path:line`, severity
(blocker / should-fix / nit) and a concrete fix. Ask it to check correctness
against the acceptance criteria, missed call sites, security issues, and whether
the tests are proportionate (flag both missing and excessive tests).

Then, inline: apply blockers and should-fixes, skip nits unless trivial, and
re-run Step 6 once if code changed. Do not spawn a second review. Write a short
`.workflow_artifacts/<task>/review.md` listing each finding and what you did with it.

## Step 8 — Commit (inline)

1. Stage the changed files by name, never with `git add -A`. Never stage
   `.workflow_artifacts/` content into a project that does not track it.
2. Commit with a conventional message (`type(scope): summary`, body says why).
   The message is plain engineering prose per
   `__QUOIN_HOME__/memory/clean-authored-content.md`: no artifact paths, finding
   IDs or review narration.
3. If the task surfaced a reusable gotcha, append one entry to
   `.workflow_artifacts/memory/lessons-learned.md`.
4. Report in two or three sentences: what changed, test status, and the next step
   (`/pr` pushes the branch and opens a PR). For a staged task, report once after
   the last stage: one line per stage (repo, what changed, tests), and name every
   repo whose branch needs a PR.

## Escalation

Stop and recommend `/run hard: <task>` (interactive), or end with that
recommendation in the final report (autonomous), when:

- the review returns a blocker that is a design problem, not a local fix;
- two implementation attempts fail verification for reasons you do not understand;
- the task needs more than five stages, or a stage needs its own design work
  (a data migration strategy, a new public API with several consumers).

A task that spans several repos or needs a few stages is not by itself a reason
to escalate: the staged path in Step 4b handles it.

Never switch to the heavy pipeline on your own.

## Important behaviours

- One session from start to finish. Subagents only for discovery, the context
  pack and the review: steps whose job is to read a lot and return a conclusion,
  or to give a fresh read.
- No cost-ledger rows, run-state records, checkpoints or progress sentinels: those
  belong to the heavy pipeline's multi-session orchestration.
- Never write quoin's own files (for example `quoin/dev/tests/...`) into another
  project. Task state lives only under `.workflow_artifacts/<task>/`.
