---
name: run
description: "Default pipeline: size, then a direct fix or brief, context pack, Opus implement, one review; local commit. Use for: /run, 'do this task'. Tags: hard:, direct:/light: (force route), --autonomous."
model: opus
---

# Run — Light Task Pipeline

*Portable intent doc: `quoin/core/skills/run.md`*

You take a task from a raw request to a local commit in one session. The Direct
route implements, verifies and commits with minimal bookkeeping; the light route
adds a brief, a context pack and one review. **You ARE an explicit user
invocation:** `/run` may commit locally. It never pushes and never opens a PR.

`__QUOIN_HOME__/skills/run/reference.md` holds the rarely needed rest: read it
once, with one Read, only where this file points to it.

**Phase markers.** At the start of each step, append a marker:
`python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" <phase>`, with phase one of
`setup`, `discover`, `frame`, `context`, `decompose`, `implement`, `verify`,
`review`, `review-fixes`, `commit`. Add `--skipped` when the step is skipped, and
`--stage N` inside stage N. Chain it onto a Bash call the step already makes,
with `;` after it (never `&&`). It only appends to
`.workflow_artifacts/<task>/phases.log`; never stage it.

## Step 0 — Route

The task name is the first token that is not a flag or tag.

1. **`hard:`** or **`strict:`** anywhere in the input → invoke the `run_hard`
   skill with the input unchanged apart from removing `hard:`, `direct:`,
   `light:` and `isolate:` (keep `strict:`) and stop.
2. **`--autonomous`** (or an `[autonomous]` sentinel) → strip it and set
   `AUTONOMOUS=true`: never ask the user anything; record assumptions in the
   brief instead.
3. **`direct:`** / **`light:`** among the leading tags → strip; `direct:` forces
   the Direct route, `light:` the light pipeline (`light:` wins if both).
4. **`QUOIN_RUN_PROFILE`** (benchmark only, never a tag), read from the Step 1
   marker's `profile=` line: `bench-lean` sets `LEAN`, `bench-memory` sets
   `MEMORY`. `LEAN` skips isolation, lessons and the dirty marker, and Step 2
   only logs its marker as skipped; `MEMORY` skips only isolation.
5. Strip a leading `[no-redispatch]` (remember it for the implementer tier
   guard) and `small:`, `medium:`, `large:`, `fast:`, `max_rounds: N` (heavy
   pipeline profiles; no effect here).
6. **Any other flag or tag** (`--resume`, `stage N of <task>`, `[from-hard]`,
   `parallel:`, `isolate:`, `no-switch:`) → read reference.md
   "Step 0 — Tags and resume" first and follow it.

## Step 1 — Setup (inline)

In as few Bash calls as possible:

1. Derive a kebab-case task name and log the first marker:
   `mkdir -p ".workflow_artifacts/<task>" && python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" setup`
2. Isolation (skip under `LEAN`/`MEMORY`; `WT` is then the checkout). Claim the
   checkout of the repo the change lands in:
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" claim --task "<task>" --branch "<branch>" --repo "<repo>" --project-root "$(pwd)"`
   and set `WT` to the `worktree` it prints. `mode: main` → go on to item 3.
   Any other result (`mode: worktree`, exit 3, a failure) → reference.md
   "Isolation modes". From here on all git, edits, tests and commits happen in
   `WT`; this session's cwd stays at the project root.
3. Git: uncommitted changes (ignore untracked `.workflow_artifacts/`) → ask
   what to do (interactive) or work on top of them (autonomous). Create a fresh
   task branch, never committing to main/master: from an up-to-date default
   branch when a remote exists, otherwise from the current HEAD.
4. Unless `LEAN`, read `.workflow_artifacts/memory/lessons-learned.md` if
   present, keeping only entries relevant to this task.

## Step 1b — Size (inline)

Decide the route yourself, from the request and at most a quick targeted look
at the code (a Grep or one file read). No subagent, no extra model call. Take
the **direct** route only when all of these hold:

- a concrete defect or change with clear expected behaviour (a failing case, an
  error message, a named function and what it should do);
- the fix should touch one module plus its tests;
- no public API change, schema or migration, security-sensitive code (auth,
  permissions, crypto, input sanitising), dependency change or cross-repo change.

Otherwise, and whenever you are unsure, take the **light** route. A `direct:` or
`light:` tag from Step 0 decides instead of the criteria. Under `AUTONOMOUS` the
same rule applies; never ask. The brief records the decision as:

```markdown
## Size
route: <direct | light>
reason: <one line; "forced by direct:" or "forced by light:" when a tag decided>
```

Light → read the light pipeline file and continue at its Step 2.
Direct → the Direct route below.

## Direct route (inline)

Implement and verify; nothing else. No discovery, context pack or review
subagent; the implementer tier guard still applies. In one Bash call, log
`discover`, `context`, `review` as `--skipped` and `frame`, and write
`.workflow_artifacts/<task>/brief.md` with a heredoc: `# <task>`, `## Goal`
(one or two sentences), the `## Size` section, and `## Assumptions` under
`AUTONOMOUS` if you made any. `implement`, `verify` and `commit` are logged at
their steps.

1. **Implement.** Find the code, the tests covering it and the command that
   runs them with targeted Grep/Read; that command is Step 6's test command.
   Then Step 5: minimal diff, about one focused regression test.
2. **Verify.** Step 6.
3. **Commit.** Step 8 items 1–2, appending `## Verification` (the command and
   `pass`, `fail` or `not run: <why>`) and `## Commit` to the brief in one call;
   then items 3–6. A run that ends without a commit still appends
   `## Verification`.

**Escalate to light** (one way, never back) when the diff outgrows one module
plus its tests, verification fails twice, or the change needs an excluded
category (not when `direct:` forced the route): reference.md "Direct route
escalation".

## Light pipeline (Steps 2–4b and 7)

The light route's steps live in `__QUOIN_HOME__/skills/run/light-pipeline.md`.
Read it once, with one Read, before the first light-route step you run: when
Step 1b picks light, when the Direct route
escalates, and when `--resume` or `stage N of <task>` continues a light or
staged run.

## Implementer tier guard (before Step 5)

Steps 5–6 (and the light route's Step 7 fixes, light pipeline file) always run
on **opus**. Read
your model from system context ("powered by the model named X") and run
`python3 __QUOIN_HOME__/scripts/model_tier.py "<X>"`. `INLINE|opus` or
`INLINE|optout`, or an input that started with `[no-redispatch]` → implement
inline. `DISPATCH|<tier>` (any non-Opus tier; script missing → any model but
Opus), and multi-stage `PARALLEL` waves even on Opus → reference.md
"Implementer tier guard (dispatch)". Never wrap the whole
of `/run` in one subagent.

## Step 5 — Implement (inline)

Marker: `implement`. Implement the brief (or, on the light route, using the
context pack). Plan in your own reasoning; do not write a plan file. Keep the
diff minimal: no drive-by refactors, no speculative abstractions.

**Tests, proportionate.** Prefer extending an existing test file over creating a
new one. Add about one focused regression test per behaviour change (for a bug,
the test that fails before the fix). Do not add tests for unchanged code, do not
duplicate coverage across files, and do not build fixtures larger than the change.

## Step 6 — Verify (inline)

Marker: `verify` (chain it before the test command). Run the tests that cover
the change once, in the FOREGROUND with an explicit Bash `timeout: 600000`
(without it the harness backgrounds calls past ~120 s). For a Python project you
may instead use the affected-test helper:

```bash
python3 __QUOIN_HOME__/scripts/affected_tests.py --project-root "$(pwd)" --repo-root "<WT>" --format text
```

If it reports `ran_pytest=false`, no tests ran: run the test command instead.
Fix real failures and re-run. If the test environment cannot be built, say so
in one line, record it under Assumptions, and continue; do not retry setup,
create virtualenvs outside the project or install packages globally.

Light route: Step 7 (review, light-pipeline.md) comes next. Direct: Step 8.

## Step 8 — Commit (inline)

Marker: `commit` (chain it onto the `git add` call).

1. Stage the changed files by name, never with `git add -A`. Never stage
   `.workflow_artifacts/` content into a project that does not track it.
2. Commit with a conventional message (`type(scope): summary`, body says why),
   in plain engineering prose per `__QUOIN_HOME__/memory/clean-authored-content.md`:
   no artifact paths, finding IDs or review narration. Record `branch:` and
   `commit:` lines in the brief's `## Commit` section (`/cleanup` finds the PR
   by it).
3. Unless `LEAN`, only if the task surfaced a reusable gotcha: record one
   lesson per reference.md "Lessons".
4. Unless `LEAN`, if the commit changed the project's structure (added, removed
   or renamed a top-level directory or package; changed a dependency manifest,
   not lockfiles alone; or changed a public interface other code consumes: an
   exported API, CLI, schema or endpoint; check with
   `git show --name-status --format= HEAD`; tests and docs alone do not count),
   append `<date> <task>: <reason>` to `.workflow_artifacts/memory/discovery-dirty`.
   Never stage the marker.
5. Report in two or three sentences: what changed, test status, and the next
   step (`/pr` pushes the branch and opens a PR). A staged task reports once
   after the last stage, one line per stage, naming every repo needing a PR.
6. Release the claim, if Step 1 made one (also when the run stops early):
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" release --task "<task>" --repo "<repo>"`.
   In worktree mode the report also names `WT` and the branch.

## Escalation

The task is bigger than the light pipeline when the review returns a design
blocker, two implementation attempts fail verification for reasons you do not
understand, or the task needs more than five stages or a stage needs its own
design work. Then follow reference.md "Escalation". Several repos or a few
stages alone are not a reason: Step 4b (light-pipeline.md) handles them.

## Important behaviours

- One session. Subagents only for the light route's discovery, context pack and
  review, plus the Opus implementer on other tiers.
- No cost-ledger rows, run-state records, checkpoints or progress sentinels.
  Task state lives only under `.workflow_artifacts/<task>/`.
- Never write quoin's own files (for example `quoin/dev/tests/...`) into
  another project.
