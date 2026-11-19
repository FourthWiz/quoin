---
name: run
description: "Default pipeline: scope triage (r0 plain, r1 +review, r2 staged, r3 opt-in heavy), Opus implement, local commit. Use for: /run, 'do this task'. Tags: hard:, r0:/r1:/r2:, --autonomous."
model: opus
---

# Run — Light Task Pipeline

*Portable intent doc: `quoin/core/skills/run.md`*

One session from request to local commit: plain Opus, plus a review, stage
plan or heavy pipeline only when scope triage says so. **You ARE an explicit
user invocation:** `/run` may commit locally; it never pushes or opens a PR.
Read `__QUOIN_HOME__/skills/run/routes.md` (the routes file: r1–r3) and
`__QUOIN_HOME__/skills/run/reference.md` (rare tags, isolation, other tiers,
lessons, escalation, important behaviours) only where pointed, once, with one Read.

**Phase markers.** At each step's start chain
`python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" <phase>` onto a Bash
call the step makes anyway, with `;` (never `&&`): `setup`, `discover`,
`frame`, `context`, `decompose`, `implement`, `verify`, `review`,
`review-fixes`, `commit`; `--skipped` for a skipped step, `--stage N` in
stage N. It appends to `.workflow_artifacts/<task>/phases.log`; never stage it.

## Step 0 — Route

The task name is the first token that is not a flag or tag.

1. **`hard:`** or **`strict:`** anywhere → invoke the `run_hard` skill with the
   input, removing `hard:`, `r0:`, `r1:`, `r2:` and `isolate:` (keep `strict:`); stop.
2. **`--autonomous`** / `[autonomous]` → strip; `AUTONOMOUS=true`: never ask;
   note assumptions (brief, or the r0 report).
3. Leading **`r0:`** / **`r1:`** / **`r2:`** → strip; forces that class in Step 1b (the highest wins).
4. **`QUOIN_RUN_PROFILE`** (benchmark only; the setup marker prints
   `profile=`): `bench-lean` sets `LEAN` (no isolation or lessons; the record script
   drops the discovery check itself), `bench-memory` sets `MEMORY` (no isolation).
5. Strip a leading `[no-redispatch]` (keep it in mind for the tier guard) and
   `small:`, `medium:`, `large:`, `fast:`, `max_rounds: N`.
6. **Any other flag or tag** (`--resume`, `stage N of <task>`, `[from-hard]`,
   `parallel:`, `isolate:`, `no-switch:`) → reference.md "Step 0 — Tags and resume".

## Step 1 — Setup (inline)

In as few Bash calls as possible:

1. `mkdir -p ".workflow_artifacts/<task>" && python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" setup` (kebab-case task).
2. Unless `LEAN`/`MEMORY` (then `WT` is the checkout):
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" claim --task "<task>" --branch "<branch>" --repo "<repo>" --project-root "$(pwd)"`;
   `WT` = its `worktree`. Not `mode: main` → reference.md "Isolation modes".
   All git, edits, tests and commits happen in `WT`; cwd stays the project root.
3. Uncommitted changes (ignore untracked `.workflow_artifacts/`) → ask, or
   work on top of them under `AUTONOMOUS`. Create a fresh task branch, never
   on main/master: from an up-to-date default branch if a remote exists, else HEAD.
4. Unless `LEAN`, read `.workflow_artifacts/memory/lessons-learned.md` if
   present; keep only relevant entries.

## Step 1b — Scope triage (inline)

Put the task in exactly one class, from the request (fetch a referenced issue first) plus at most one
quick look at the code (a Grep or one file read). No subagent, no extra model call. Check from the
top. A class above r0 needs a signal you can name; with none, the task is
**r0**, also when unsure:

- **r3 Needs design:** a novel architecture (a new subsystem with no
  precedent in the codebase), a data-migration strategy (existing stored data
  must be moved or transformed), or an interface several consumers must agree on.
- **r2 Multi-part:** more than one repository, or parts that must land in
  order (a schema or API change before its consumers, a library before its callers).
- **r1 Risky:** touches auth, permissions, crypto, input sanitising, a schema
  or migration, or a public API (an exported interface, CLI or endpoint that
  other code consumes).
- **r0 Plain:** everything else, one coherent low-risk change.

Size, file count, a dependency bump or a long request are not signals; a tag
decides instead; `AUTONOMOUS` never asks. **r0** → the plain route below: no brief,
no skipped-step markers, no other file. **r1, r2, r3** → read the routes file
and follow it.

## Plain route (r0)

No discovery, context pack, brief or review. The implementer tier guard
still applies. Find the code, its tests and their
command with targeted Grep/Read, then Steps 5, 6 and 8. **Reclassify upward** (never down; not when a tag forced the
class) if the diff touches an r1 signal or must span repos or land in parts:
the routes file, "Reclassify upward". Two failed verifications are an Escalation trigger.

## Implementer tier guard (before Step 5)

Steps 5–6 (and Step 7 fixes) run on **opus**. Chain
`python3 __QUOIN_HOME__/scripts/model_tier.py "<X>"` (X: "powered by the model
named X") onto setup. `INLINE|opus`, `INLINE|optout` or `[no-redispatch]` →
inline. `DISPATCH|<tier>` (script missing → any model but Opus) or multi-stage
`PARALLEL` waves → reference.md "Implementer tier guard (dispatch)". Never
wrap the whole of `/run` in one subagent.

## Step 5 — Implement (inline)

Marker: `implement`. Implement the request (plus the brief on r1, the context
pack on r2); plan in your reasoning, no plan file. Minimal diff: no drive-by
refactors or speculative abstractions. **Tests, proportionate:** extend an
existing test file; about one focused regression test per behaviour change
(for a bug, one that fails before the fix); none for unchanged code, no
duplicate coverage, no fixtures larger than the change.

## Step 6 — Verify (inline)

Marker: `verify`, chained before the test command. Run the covering tests
once in the FOREGROUND with an explicit Bash `timeout: 600000` (else calls past ~120 s go
to the background). Python may use
`python3 __QUOIN_HOME__/scripts/affected_tests.py --project-root "$(pwd)" --repo-root "<WT>" --format text`
(`ran_pytest=false`: nothing ran; run the test command). Fix real failures and
re-run. If the test environment cannot be built, say so in one line, note it
as an assumption and go on: no retries, no virtualenvs outside the project,
no global installs. Then Step 7 (the routes file) for r1 and each r1 stage; Step 8 otherwise.

## Step 8 — Commit (inline)

1. Chain the `commit` marker onto `git add` of the changed files by name
   (never `git add -A`, never untracked-by-project `.workflow_artifacts/`).
2. Commit: `type(scope): summary` and a short why-body, plain prose per
   `__QUOIN_HOME__/memory/clean-authored-content.md` (no artifact paths,
   finding IDs or review narration). Chain with `&&`:
   `python3 "__QUOIN_HOME__/scripts/run_record.py" --task "<task>" --repo "<WT>" --base <base> --project-root "$(pwd)" --request '<the request verbatim, or its issue id>' --verify '<test command>: <pass | fail | not run: why>'`
   (base: where the task or stage started). It writes `## Verification` and
   `## Commit` to the brief (creating the r0 brief) and marks discovery stale
   when needed; no judgement from you. Single-quote both values. No commit →
   run it with `--no-commit`.
3. Unless `LEAN`, only for a reusable gotcha: one lesson per reference.md "Lessons".
4. Report in two or three sentences: what changed, test status, next step
   (`/pr` pushes and opens a PR). A staged task reports once, one line per
   stage, naming every repo needing a PR.
5. Release a Step 1 claim (also when stopping early):
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" release --task "<task>" --repo "<repo>"`;
   in worktree mode the report names `WT` and the branch.

## Escalation

A design blocker from the review, two failed verifications you do not
understand, more than five stages or a stage needing its own design →
reference.md "Escalation". Several repos or a few stages alone are not a reason.

Always: one session (subagents only as reference.md "Important behaviours"
lists), no ledgers, run-state or checkpoints, and never quoin's own files in
another project.
