---
name: run
description: "Default pipeline: size, then a direct fix or brief, context pack, Opus implement, one review; local commit. Use for: /run, 'do this task'. Tags: hard:, direct:/light: (force route), --autonomous."
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

**Phase markers.** At the start of each step, append a marker:
`python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" <phase>`, with phase one of
`setup`, `discover`, `frame`, `context`, `decompose`, `implement`, `verify`,
`review`, `review-fixes`, `commit`. Add `--skipped` when the step is skipped, and
`--stage N` for steps run inside stage N of a staged task. Chain it onto a Bash
call the step already makes rather than spending a separate call where you can,
and put `;` after the marker (never `&&`) so the step's own command runs even if
the marker cannot. The marker only appends to `.workflow_artifacts/<task>/phases.log`,
which benchmark runs read to split cost and time by phase; it changes nothing
else, a failure is only a warning, and the log is never staged or committed.
When the implementer tier guard dispatches, you write `implement` before the
spawn and `review-fixes` before sending it the findings; it writes none. (The
phase report counts a subagent's messages under the phase that spawned it, so
fixes made through that implementer show up as `implement`.)

## Step 0 — Route

Scan the input before anything else. The task name is the first token that is
not a flag or tag (`--resume`, `--autonomous`, `[autonomous]`, `[no-redispatch]`,
`hard:`, `strict:`, `small:`, `parallel:`, `direct:`, `light:`, `isolate:` and the like; a leading `[no-redispatch]` is
stripped and remembered for the implementer tier guard); when handing off to `run_hard`, always pass the original
input unchanged apart from removing `hard:`, `direct:` and `light:` (and `isolate:`).

1. **`hard:`** or **`strict:`** anywhere in the input → this task wants the full
   heavy pipeline. Invoke the `run_hard` skill with the input unchanged (keep
   `strict:`, strip only `hard:`) and stop; `run_hard` owns everything from there.
2. **`--resume`** (task name as above) → if `.workflow_artifacts/memory/run-state-<task>.json` or
   `.workflow_artifacts/memory/autonomous-run-<task>.marker` exists, the task was
   started by the heavy pipeline: invoke `run_hard` with the input unchanged and
   stop. Otherwise resume the light run: run Step 1's claim (item 2; it
   returns the task's worktree if it has one), read `.workflow_artifacts/<task>/brief.md`,
   check `git status`/`git log` on the task branch in `WT`, and continue from the first
   step whose output is missing. A brief whose `## Size` section says
   `route: direct` resumes on the Direct route instead; one with no `## Size`
   section is a light run, and a light or staged run (also
   **`stage N of <task>`**) first reads the light pipeline file, whose
   resume section covers staged tasks.
3. **`--autonomous`** (or an `[autonomous]` sentinel from a parent session) →
   strip it and set `AUTONOMOUS=true`: never ask the user anything; record
   assumptions in the brief instead.
4. **`small:` / `medium:` / `large:` / `fast:` / `max_rounds: N`** → strip them. They
   select heavy-pipeline profiles and have no effect here. If the user wrote
   `large:` interactively, mention in one line that `/run hard:` runs the full
   pipeline, then continue on the light path.
5. **`parallel:`** → strip it and set `PARALLEL=true`. It only changes how a
   staged task runs its stages (Step 4b, "Parallel waves", in the light
   pipeline file); a single-shape
   task ignores it. It combines with `--autonomous` and `--resume`.
6. **`QUOIN_RUN_PROFILE=bench-lean`** in the environment → set `LEAN=true`.
   This is a benchmark-only switch, never a tag: the benchmark harness sets it
   for its quoin arm to measure /run without discovery. Read it from the
   `profile=` line the Step 1 setup marker prints; a `--resume` that skips
   Step 1 reads it with `printenv QUOIN_RUN_PROFILE`. Under `LEAN`, Step 2 only
   logs its marker as skipped and Step 8 skips the lessons-learned append
   and the discovery-dirty marker; every other step runs as written.
7. **`direct:`** / **`light:`** → recognised only as whitespace-delimited
   tokens among the leading flags and tags, before the task text (so
   `redirect:` or a tag quoted in an issue body never counts). Strip them and
   remember the override for Step 1b: `direct:` forces the Direct route,
   `light:` forces the light pipeline (if both appear, `light:` wins). They
   apply under `--autonomous` too.
8. **`isolate:`** → recognised the same way as `direct:`/`light:`. Strip it and
   set `ISOLATE=true`: Step 1 works in a worktree even when no other run holds
   the checkout.

## Step 1 — Setup (inline)

1. Derive a kebab-case task name; create the task folder and log the first
   marker in one call:
   `mkdir -p ".workflow_artifacts/<task>" && python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" setup`
2. Isolation. Pick the task branch name and claim the checkout of the repo
   the change lands in:
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" claim --task "<task>" --branch "<branch>" --repo "<repo>" --project-root "$(pwd)"`
   (`<repo>` is the main checkout, never `WT`), plus `--isolate` under
   `ISOLATE`. Set `WT` to the `worktree` it prints.
   - `mode: main`: `WT` is the checkout itself; go on to item 3.
   - `mode: worktree`: another live run holds the checkout (`blocking`) or
     `isolate:` asked for it; the task branch is already checked out at
     `WT`. Skip item 3; the checkout's changes belong to the other run.
   - Exit 3: print its `error` and stop.
   - Script missing or any other failure: print
     `[quoin-run: run isolation unavailable; working in this checkout]` and
     go on with `WT` = the checkout.

   From here on all git, edits, tests and commits happen in `WT` (`git -C
   "<WT>"`, `(cd "<WT>" && <test command>)`, absolute paths), and every
   subagent gets `WT` as its absolute path. This session's cwd stays at the
   project root, with `.workflow_artifacts/` and the phase markers.
3. Git: if the tree has uncommitted changes (ignore untracked
   `.workflow_artifacts/`), ask what to do with them (interactive) or leave them
   alone and work on top of them (autonomous). Then create a fresh task branch,
   never committing to main/master: from an up-to-date default branch when a
   remote exists, otherwise from the current HEAD (for example a benchmark
   fixture with no remote). In a multi-repo project, do this in the repo the
   change lands in; a staged task creates the branch in each repo when its first
   stage there starts (Step 4b, in the light pipeline file).
4. Read `.workflow_artifacts/memory/lessons-learned.md` if present, keeping only
   entries relevant to this task.

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
same rule applies; never ask. Record the decision in the brief as:

```markdown
## Size
route: <direct | light>
reason: <one line; "forced by direct:" or "forced by light:" when a tag decided>
```

Light → read the light pipeline file (see "Light pipeline" below) and continue
at its Step 2; keep this section in the Step 3 brief.
Direct → follow the Direct route below: skip Steps 2, 3, 4, 4b and 7; run the
implementer tier guard, then Steps 5, 6 and 8 as the Direct route describes.

## Direct route (inline)

The direct route has no discovery, no context-pack subagent and no review
subagent. The implementer tier guard still applies: on Opus you implement
inline; otherwise one Opus implementer gets this brief in place of a context
pack.

Phase markers: log `discover`, `context` and `review` as `--skipped` and then
`frame` in one Bash call before writing the brief; `implement`, `verify` and
`commit` are logged at their steps as usual.

1. **Brief.** Write `.workflow_artifacts/<task>/brief.md`, about ten lines:
   `# <task>`, `## Goal` (one or two sentences), the `## Size` section, and
   `## Verification` (`pending` until step 3). Under `AUTONOMOUS`, add
   `## Assumptions` if you made any.
2. **Implement.** Find the code, the tests covering it and the command that
   runs them with targeted Grep/Read; that command is Step 6's test command
   and goes to a dispatched implementer in place of the context-pack path.
   Change the code following Step 5: minimal diff, about one focused
   regression test.
3. **Verify.** Step 6 as written. Replace `pending` with the command and its
   result (`pass`, `fail` or `not run: <why>`).
4. **Commit.** Step 8 as written, including the report.

**Escalate to light** (one way, never back) when the diff outgrows one module
plus its tests, verification fails twice, or the change turns out to need an
excluded category (that last trigger does not apply when `direct:` forced the
route). Two failed verifications here mean this escalation, not the
`/run hard:` recommendation under Escalation, which applies only to failures on
the light pipeline. Keep the working tree as it is: no reset, no stash. Read
the light pipeline file first (it holds the Step 3 brief template), then
rewrite the brief in one write as the full Step 3 brief, its Size section saying
`route: light` plus `escalated: from direct — <trigger>`, and drop
`## Verification`. Then continue at Step 2. Steps 5–7 build on the existing
diff (through the same Opus implementer, via SendMessage, when the guard
dispatched) and the review covers all of it.

## Light pipeline (Steps 2–4b and 7)

The light route's Steps 2 (discover), 3 (frame), 4 (context pack), 4b
(decompose, parallel waves) and 7 (review) live in
`__QUOIN_HOME__/skills/run/light-pipeline.md`. Read it once, with one Read,
before the first light-route step you run: when Step 1b picks light, when the Direct route
escalates, and when `--resume` or `stage N of <task>` continues a light or
staged run. The Direct route never needs it.

## Implementer tier guard (before Step 5)

On the Direct route the guard runs before Direct step 2.

Steps 5–6 and the light route's Step 7 fix pass (light pipeline file) always
run on **opus**. Everything else
(Steps 0–4, the review spawn, Step 8 and every user question) stays in this
session, so discovery, the context pack and the review keep working: never
wrap the whole of `/run` in one subagent.

1. **Opt-out.** Implement inline when the input started with
   `[no-redispatch]`.
2. **Detect.** Read your model from system context ("powered by the model
   named X") and run `python3 __QUOIN_HOME__/scripts/model_tier.py "<X>"`.
   `INLINE|opus`, or `INLINE|optout` (`QUOIN_DISABLE_MINTIER_GUARD=1` is
   set) → run Steps 5–6 (and, on the light route, the light pipeline file's
   Step 7 fixes) inline as written. `DISPATCH|<tier>` (haiku,
   sonnet, fable, mythos or unknown) → dispatch. Tiers above Opus dispatch
   too. If the script is missing, decide yourself: any model but Opus
   dispatches.
3. **Dispatch.** Spawn one general-purpose Agent with `model: "opus"`. Its
   prompt starts with `[no-redispatch]` and gives the brief text, the context
   pack path, the relevant lessons-learned entries, the absolute `WT` path and task
   branch, the test command, and the text of Steps 5 and 6 below (it cannot
   see this file). It tells the agent to do those two steps, not to commit
   and not to spawn subagents, and to return the files changed, the tests
   added, the verification result (command and pass/fail) and any blocker.
   It also says, verbatim: "Run every test command in the FOREGROUND with an
   explicit Bash `timeout: 600000`, and pass `--junitxml` (or `--junit-out`
   for affected_tests.py) a path under `.workflow_artifacts/cache/` containing
   the token `impl-<task>-<stamp>`. Never end your turn while a test run
   is pending; read its result first." The explicit timeout is load-bearing:
   without it the harness moves any call past ~120 s to the background, and
   the agent's turn then ends with the result unread.
   Its verification is Step 6's result. On this path you never edit project
   source files yourself. If it reports a blocker or failing verification,
   send it back once with SendMessage; if it still fails, that counts as two
   failed attempts under Escalation. Never fix it inline.
4. **Watchdog.** Your notification fires when the implementer's turn ends,
   not when its test process exits, so nobody is re-invoked when a
   backgrounded suite finishes. If the implementer returns waiting on tests
   (a run still pending, no result), do not wait for the user. Start this
   waiter with Bash `run_in_background: true`, filling in the `impl-...`
   token from its junit path (ask it with SendMessage if it did not say):

   ```bash
   i=0; while pgrep -f "[i]mpl-<task>-<stamp>" >/dev/null && [ "$i" -lt 360 ]; do sleep 10; i=$((i+1)); done
   if pgrep -f "[i]mpl-<task>-<stamp>" >/dev/null; then echo "WAITER|timeout"; else echo "WAITER|exited"; fi
   ```

   Write the token with its first character in brackets (`[i]mpl-...`) so the
   pattern never matches the waiter's own command line. Never key on a bare
   `pgrep -f pytest`, which matches other sessions' runs. The wait is bounded
   at one hour. When the waiter re-invokes you, nudge the implementer with
   SendMessage: `WAITER|exited` → "your test run finished; read its result
   from <junit path> and finish Step 6"; `WAITER|timeout` → "the run is
   still going after an hour; stop it and report a failed verification".
   Under `AUTONOMOUS` this runs with no user prompt at all. A headless child
   must not end its turn on a background waiter: run the same loop in the
   FOREGROUND with Bash `timeout: 600000`, with `54` in place of `360`, and
   repeat the call until it prints `WAITER|exited` or an hour has passed.
5. **Fail-open** (only when the Agent spawn itself errors). Classify the
   error in this order:
   - **Autonomous-class** (`AUTONOMOUS`): never ask. Print
     `[quoin-run-impl-autonomous: opus implementer dispatch failed; implementing inline at current tier]`,
     record it under Assumptions in the brief, then implement inline.
   - **1M-credit-class** (the error contains `Usage credits required for 1M context`)
     and **generic**: AskUserQuestion with two options, "Abort, switch with /model"
     and "Proceed at current tier". Abort → print
     `[quoin-run-impl: aborted; switch with /model, then /run --resume <task>]`
     and STOP. Proceed → print
     `[quoin-run-impl: opus implementer unavailable; implementing at current tier per user choice]`
     and implement inline.

## Step 5 — Implement (inline)

Marker: `implement`. Implement the brief in this session (or, when the implementer tier guard
dispatched, in the Opus implementer), using the context pack. Plan in your own
reasoning; do not write a plan file. Keep the diff minimal: no drive-by refactors,
no speculative abstractions.

**Tests, proportionate.** Prefer extending an existing test file over creating a
new one. Add about one focused regression test per behaviour change (for a bug,
the test that fails before the fix). Do not add tests for unchanged code, do not
duplicate coverage across files, and do not build fixtures larger than the change.

## Step 6 — Verify (inline)

Marker: `verify` (chain it before the test command). Run the tests that cover the change once, using the test command from the
context pack, in the FOREGROUND with an explicit Bash `timeout: 600000` (see
the dispatch rule above; inline runs follow it too). For a Python project you may instead use the affected-test helper:

```bash
python3 __QUOIN_HOME__/scripts/affected_tests.py --project-root "$(pwd)" --repo-root "<WT>" --format text
```

It only understands pytest: if it reports `ran_pytest=false`, no tests ran, so
do not record a green result; use the context-pack command instead. Fix real
failures and re-run.

If the test environment cannot be built (missing dependencies, sandbox denies
installs, no interpreter), say so in one line, record it in the brief under
Assumptions, and continue. Do not retry environment setup, create virtualenvs
outside the project, or install packages globally.

Light route: Step 7 (review, in the light pipeline file) comes next. The Direct
route goes straight on to Step 8.

## Step 8 — Commit (inline)

Marker: `commit` (chain it onto the `git add` call).

1. Stage the changed files by name, never with `git add -A`. Never stage
   `.workflow_artifacts/` content into a project that does not track it.
2. Commit with a conventional message (`type(scope): summary`, body says why).
   The message is plain engineering prose per
   `__QUOIN_HOME__/memory/clean-authored-content.md`: no artifact paths, finding
   IDs or review narration.
3. Unless `LEAN`, if the task surfaced a reusable gotcha, record one entry in
   `.workflow_artifacts/memory/lessons-learned.md` in the standard form
   (`## <date> — <task>`, then **What happened**, **Lesson**, **Applies to**).
   Skip it if an entry for this task is already there
   (`grep -q "^## .* — <task>$"`). Otherwise write it to a temp file and run
   `python3 __QUOIN_HOME__/scripts/lessons_guard.py --candidate-file <tmp> --lessons-file .workflow_artifacts/memory/lessons-learned.md --candidate-slug <task> --no-strict-attrib`
   (`--no-strict-attrib` so entries from this project's own earlier tasks
   count). Exit 1 means an existing entry is near-verbatim the same (it prints
   that entry's slug): skip the append and name it in the report, or, if this
   task adds something the old entry lacks, append with a first line
   `> Related: <matched slug>`. The guard only catches near-verbatim copies,
   so also skip a lesson you already saw, reworded, in Step 1's relevant
   entries. Any other exit, or a missing script, means append: pipe the temp
   file into
   `python3 __QUOIN_HOME__/scripts/append_artifact.py --blank-line "<path>"` (no prior Read needed).
4. Unless `LEAN`, mark discovery memory out of date when the commit changed
   the project's structure: it added, removed or renamed a top-level directory
   or package; changed a dependency manifest (`pyproject.toml`, `setup.py`,
   `requirements*.txt`, `package.json`, `go.mod`, `Cargo.toml`, `pom.xml`,
   `build.gradle*`, `Gemfile` and the like, not lockfiles alone); or changed a
   public interface other code or repos consume (an exported API, CLI, schema
   or service endpoint). Check with `git show --name-status --format= HEAD`, or
   `git diff --name-status <base>..HEAD` when the task or stage made more than
   one commit. If so, append one line,
   `<date> <task>: <reason>`, to `.workflow_artifacts/memory/discovery-dirty`;
   the next light run's Step 2 (light pipeline file) refreshes discovery and
   `/discover` removes the marker. Never stage the marker. Internal edits, tests and docs alone do not
   count.
5. Report in two or three sentences: what changed, test status, and the next step
   (`/pr` pushes the branch and opens a PR). For a staged task, report once after
   the last stage: one line per stage (repo, what changed, tests), and name every
   repo whose branch needs a PR.
6. Release the claim: a single task now, a staged task once after its last
   stage, for each repo it claimed (also when the run stops early):
   `python3 "__QUOIN_HOME__/scripts/run_isolation.py" release --task "<task>" --repo "<repo>"`.
   In worktree mode the report also names `WT` and the branch. Both stay;
   `/workspace teardown <task>` removes them after its safety checks.

## Escalation

Stop and recommend `/run hard: <task>` (interactive), or end with that
recommendation in the final report (autonomous), when:

- the review returns a blocker that is a design problem, not a local fix;
- two implementation attempts fail verification for reasons you do not understand;
- the task needs more than five stages, or a stage needs its own design work
  (a data migration strategy, a new public API with several consumers).

A task that spans several repos or needs a few stages is not by itself a reason
to escalate: the staged path in Step 4b (light pipeline file) handles it.

Before stopping, release the claim (Step 8 item 6) and, in worktree mode,
name `WT` and the branch: `/run hard:` does not isolate, so run it from `WT`.

Never switch to the heavy pipeline on your own. Escalating from the Direct
route to the light pipeline is a separate, one-way step (see Step 1b).

## Important behaviours

- One session from start to finish. Subagents only for discovery, the context
  pack and the review (steps whose job is to read a lot and return a conclusion,
  or to give a fresh read), plus the Opus implementer when the session is not
  on Opus. The Direct route uses none of them except that implementer.
- No cost-ledger rows, run-state records, checkpoints or progress sentinels: those
  belong to the heavy pipeline's multi-session orchestration.
- Never write quoin's own files (for example `quoin/dev/tests/...`) into another
  project. Task state lives only under `.workflow_artifacts/<task>/`.
