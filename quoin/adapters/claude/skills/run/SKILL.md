---
name: run
description: "Default pipeline: scope triage (r0 plain, r1 +review, r2 staged, r3 opt-in heavy), Opus implement, local commit. Use for: /run, 'do this task'. Tags: hard:, r0:/r1:/r2:, --autonomous."
model: opus
---

# Run — Light Task Pipeline

*Portable intent doc: `quoin/core/skills/run.md`*

You take a task from a raw request to a local commit in one session: plain
Opus, plus a review, stage plan or heavy pipeline only when scope triage's
signal fires. **You ARE an explicit user invocation:**
`/run` may commit locally. It never pushes and never opens a PR.

Two files hold the rest; read each once, with one Read, only where this file
points to it: `__QUOIN_HOME__/skills/run/routes.md` (the routes file: r1, r2
and r3) and `__QUOIN_HOME__/skills/run/reference.md` (rare tags, isolation,
other tiers, lessons, escalation).

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
   skill with the input unchanged apart from removing `hard:`, `r0:`, `r1:`,
   `r2:` and `isolate:` (keep `strict:`) and stop.
2. **`--autonomous`** (or an `[autonomous]` sentinel) → strip it and set
   `AUTONOMOUS=true`: never ask the user anything; note assumptions in the
   brief (r0: in the report).
3. **`r0:`** / **`r1:`** / **`r2:`** among the leading tags → strip; each
   forces that class in Step 1b (the highest wins if several appear).
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

## Step 1b — Scope triage (inline)

Put the task in exactly one class, from the request (fetch a referenced
issue's title and body first) plus at most one quick look at the code (a Grep
or one file read). No subagent, no extra model call.
Check from the top. A class above r0 needs a signal you can name from the
request or that look; with none, the task is **r0**, also when unsure:

- **r3 Needs design:** a novel architecture (a new subsystem with no
  precedent in the codebase), a data-migration strategy (existing stored data
  must be moved or transformed), or an interface several consumers must agree on.
- **r2 Multi-part:** more than one repository, or parts that must land in
  order (a schema or API change before its consumers, a library before its callers).
- **r1 Risky:** touches auth, permissions, crypto, input sanitising, a schema
  or migration, or a public API (an exported interface, CLI or endpoint that
  other code consumes).
- **r0 Plain:** everything else, one coherent low-risk change.

Size, file count, a dependency bump or a long request are not signals. An
`r0:`/`r1:`/`r2:` tag decides instead. Under `AUTONOMOUS` the same rule
applies; never ask.

**r0** → the plain route below: no brief, no skipped-step markers, no other
file. **r1, r2, r3** → read the routes file and follow it (it writes the
brief and holds the r1 review, the r2 stage plan and r3's opt-in switch).

## Plain route (r0)

No discovery, context pack, brief or review. The implementer tier guard still
applies.

1. **Implement.** Find the code, the tests covering it and the command that
   runs them with targeted Grep/Read; that command is Step 6's test command.
   Then Step 5: minimal diff, about one focused regression test.
2. **Verify.** Step 6.
3. **Commit.** Step 8.

**Reclassify upward** (never down; not when a tag forced the class) when the
work shows a signal Step 1b missed: the diff touches an r1 signal, or the
change must span repos or land in parts. Then read the routes file and
follow its "Reclassify upward". Two failed verifications are an Escalation
trigger.

## Implementer tier guard (before Step 5)

Steps 5–6 (and the Step 7 fixes) always run on **opus**. Read
your model from system context ("powered by the model named X") and run
`python3 __QUOIN_HOME__/scripts/model_tier.py "<X>"`. `INLINE|opus` or
`INLINE|optout`, or an input that started with `[no-redispatch]` → implement
inline. `DISPATCH|<tier>` (any non-Opus tier; script missing → any model but
Opus), and multi-stage `PARALLEL` waves even on Opus → reference.md
"Implementer tier guard (dispatch)". Never wrap the whole
of `/run` in one subagent.

## Step 5 — Implement (inline)

Marker: `implement`. Implement the request (the brief on r1, plus the context
pack on r2). Plan in your own reasoning; do not write a plan file. Keep the
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
in one line, note it as an assumption, and continue; do not retry setup,
create virtualenvs outside the project or install packages globally.

Then Step 7 (the routes file) for r1 and each r1 stage; Step 8 otherwise.

## Step 8 — Commit (inline)

Marker: `commit` (chain it onto the `git add` call).

1. Stage the changed files by name, never with `git add -A`. Never stage
   `.workflow_artifacts/` content into a project that does not track it.
2. Commit with a conventional message (`type(scope): summary`, body says why),
   in plain engineering prose per `__QUOIN_HOME__/memory/clean-authored-content.md`:
   no artifact paths, finding IDs or review narration. Then, in one call,
   record the run in `.workflow_artifacts/<task>/brief.md` (`/cleanup` finds
   the PR by it): `## Verification` (the command and `pass`, `fail` or
   `not run: <why>`) and `## Commit` with `branch:` and `commit:` lines,
   appended to the brief, or on r0 written as a new brief that holds only
   `# <task>`, `## Request` (the request verbatim, or the issue id it came
   from), `## Scope` with `route: r0`, and those two sections. A run that
   ends without a commit still records `## Verification`.
3. Unless `LEAN`, only if the task surfaced a reusable gotcha: record one
   lesson per reference.md "Lessons".
4. Unless `LEAN`, chain onto the commit call with `&&`, so a rejected commit skips it:
   `python3 "__QUOIN_HOME__/scripts/discovery_dirty.py" --repo "<WT>" --task "<task>" --base <base> --project-root "$(pwd)"`
   (base: where the task or stage started). It decides from the commit's name-status list, with no judgement from you,
   and appends to `.workflow_artifacts/memory/discovery-dirty` when the commit
   changed structure, a dependency manifest or an interface file. Never stage the marker.
5. Report in two or three sentences: what changed, test status, and the next
   step (`/pr` pushes the branch and opens a PR). A staged task reports once
   after the last stage, one line per stage, naming every repo needing a PR.
6. Release the claim, if Step 1 made one (also when the run stops early):
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" release --task "<task>" --repo "<repo>"`.
   In worktree mode the report also names `WT` and the branch.

## Escalation

The task is bigger than `/run` when the review returns a design blocker, two
implementation attempts fail verification for reasons you do not understand,
or the task needs more than five stages or a stage needs its own design work.
Then follow reference.md "Escalation". Several repos or a few stages alone are
not a reason: r2 handles them.

## Important behaviours

- One session. Subagents only for r2's discovery and context pack, the r1
  review, plus the Opus implementer on other tiers.
- No cost-ledger rows, run-state records, checkpoints or progress sentinels.
  Task state lives only under `.workflow_artifacts/<task>/`.
- Never write quoin's own files (for example `quoin/dev/tests/...`) into
  another project.
