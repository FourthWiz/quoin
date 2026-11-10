---
name: run
description: "Default pipeline: brief, context pack, implement on Opus, one review, local commit. Use for: /run, 'do this task'. Tags: hard: (full pipeline), --autonomous."
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
`hard:`, `strict:`, `small:`, `parallel:` and the like; a leading `[no-redispatch]` is
stripped and remembered for the implementer tier guard); when handing off to `run_hard`, always pass the original
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
5. **`parallel:`** → strip it and set `PARALLEL=true`. It only changes how a
   staged task runs its stages (Step 4b, "Parallel waves"); a single-shape
   task ignores it. It combines with `--autonomous` and `--resume`.
6. **`QUOIN_RUN_PROFILE=bench-lean`** in the environment → set `LEAN=true`.
   This is a benchmark-only switch, never a tag: the benchmark harness sets it
   for its quoin arm to measure /run without discovery. Read it from the
   `profile=` line the Step 1 setup marker prints; a `--resume` that skips
   Step 1 reads it with `printenv QUOIN_RUN_PROFILE`. Under `LEAN`, Step 2 only
   logs its marker as skipped and Step 8 skips the lessons-learned append;
   every other step runs as written.

## Step 1 — Setup (inline)

1. Derive a kebab-case task name; create the task folder and log the first
   marker in one call:
   `mkdir -p ".workflow_artifacts/<task>" && python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" setup`
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
if [ -n "$(find .workflow_artifacts/cache/_staleness.md .workflow_artifacts/memory/repo-heads.md -mtime -7 2>/dev/null | head -1)" ]; then echo recent; python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" discover --skipped; else echo stale; python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" discover; fi
```

- Under `LEAN`, do not run that check: log
  `run_phase.py "<task>" discover --skipped` and go on to Step 3. The context
  pack explores the repository itself.
- `recent` → skip and say so in one line.
- `stale` (missing or older than 7 days) → spawn one subagent on **opus**
  whose prompt starts with `[no-redispatch]` and runs `/discover` for the project
  root (the prefix stops `/discover` from re-dispatching itself into a second
  subagent). Wait for it; its output files are the project memory the next steps
  read.

## Step 3 — Frame (inline)

Marker: `frame`. Write `.workflow_artifacts/<task>/brief.md`, at most about 40 lines:

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

Marker: `context`. Spawn one read-only subagent (Explore type, sonnet) with the brief's text and the
paths of the discovery memory files (none under `LEAN`: it explores the repo from scratch). Its job is to return the pack below (at
most about 60 lines), which you then save as `.workflow_artifacts/<task>/context.md`
(the subagent is read-only):

- the files and symbols the change most likely touches, with `path:line`;
- the existing tests covering them, and the command that runs those tests;
- conventions the change must follow (error handling, naming, similar prior code);
- relevant lessons-learned entries;
- anything surprising (generated files, duplicated logic, a second call site).

Tell the subagent to start from `.workflow_artifacts/cache/<repo>/repo-map.md`
(a ranked symbol map; skip it if absent or its `hash:` is not the current HEAD)
to locate likely files, to prefer Serena
symbol tools when available, to read excerpts rather than whole files, and to
report conclusions, not file dumps. You keep only
its summary in context.

## Step 4b — Decompose (staged tasks only, inline)

Marker: `decompose`. Using the brief and the context pack, write `.workflow_artifacts/<task>/architecture.md`
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
   Under `PARALLEL`, a stage in a multi-stage wave instead works on a
   temporary stage branch in its own worktree, merged back into the task
   branch (see "Parallel waves").
3. If the stage's repo or area is not covered by the task's context pack, run
   Step 4 for the stage brief and save it to `stage-N/context.md`.
4. Steps 5–8 as written, with the stage brief in place of the task brief: the
   review gets the stage brief, the `## Interfaces between stages` section and
   the stage's diff only (base = the commit the stage started from), and its
   notes go to `stage-N/review.md`. One commit per stage.
5. Mark the stage ✅ in `architecture.md` (replace ⏳). If the stage showed the
   decomposition was wrong, update the later stages and add a one-line note to
   the Overview saying why, rather than forcing the old plan.

**Parallel waves (`PARALLEL` only).** Stages run in dependency waves instead
of strict file order. Below, `PS` means
`python3 __QUOIN_HOME__/scripts/parallel_stages.py`. Each command prints one
JSON object. Double-quote every path, branch and task value you substitute.

1. **Plan the next wave.** If `QUOIN_PARALLEL_BUDGET_USD` (a dollar budget
   for today's project spend) is set, read `today_usd` from
   `python3 __QUOIN_HOME__/scripts/spend_monitor.py --once --json --scope project`
   and pass it as `--spent-usd`. Then run
   `PS plan --task-dir ".workflow_artifacts/<task>" [--spent-usd <usd>]`.
   - It groups the ⏳ stages into `waves`, at most `cap` stages each
     (`QUOIN_PARALLEL_MAX`, default 2).
   - Only an explicit `Depends on: none` makes a stage independent. A stage
     with no such line waits for the stage before it.
   - Once the budget is reached the cap is 1, so the run carries on
     sequentially.
   - Run only `waves[0]`, then re-plan, because ✅ marks and spend change.
   - Exit 2 (unknown dependency or cycle): fix the `Depends on:` lines if the
     mistake is obvious, else escalate.
2. **One-stage wave:** run it exactly as the sequential loop above.
3. **Multi-stage wave.**
   - For each stage, do sub-steps 1–3 above in this session, then run
     `PS worktree --repo "<repo>" --task-branch "<task branch>" --stage S-0N`.
     It prints `branch`, `path` and `base`. The worktree goes inside the repo
     under `.claude/worktrees/`, which is kept out of `git status`. On resume
     it reuses an existing stage branch or worktree and prints
     `status: exists`.
   - Spawn one Opus implementer per stage, all in a single message so they
     run concurrently. Use the implementer tier guard's dispatch prompt (even
     on Opus), and add four rules:
     - work and run tests only inside `path`, never in the main checkout;
     - use the junit token `impl-<task>-S-0N-<stamp>`;
     - commit on the stage branch with a conventional message;
     - return the commit SHA.
   - Review each stage (Step 7) on `git diff <base>...<stage branch>`, send
     fixes to that stage's implementer, and write `stage-N/review.md`.
4. **Cost rows.** When `QUOIN_INLINE_COST_CAPTURE` is not `0`, append one
   on-behalf row per stage implementer and per stage reviewer to
   `.workflow_artifacts/<task>/cost-ledger.md`. Follow the recipe in
   `__QUOIN_HOME__/memory/cost-ledger-format.md`:
   - `AID` and `TUID` are that agent's `agentId` and tool-use id;
   - PHASE is `implement` or `review`;
   - NOTE is `on-behalf: <phase> via /run (stage S-0N)`;
   - append `;stage=S-0N` to `ATTR`.

   Write the implementer's row only after its fix pass, so that row covers
   the fixes too. These are the only ledger rows the light run writes, and
   `/cost_snapshot` uses them for its per-stage breakdown.
5. **Integrate one stage at a time, in file order.** From the main checkout,
   on the task branch, run
   `PS merge --repo "<repo>" --into "<task branch>" --branch "<stage branch>"`.
   `.workflow_artifacts/` edits never block a merge.
   - **Exit 0 (merged):** run
     `PS cleanup --repo "<repo>" --into "<task branch>" --branch "<stage branch>"`
     and mark that stage ✅ at once. If cleanup reports `partial`, note the
     leftover worktree in the report and carry on.
   - **Integration gate:** after the wave's last merge, run Step 6 once on the
     task branch. Fix any failure in a separate commit.
   - **Exit 3 (`dirty-tree` or `wrong-branch`):** nothing was merged.
     Interactive: show the files and ask whether to commit, stash or stop.
     Autonomous: stop and escalate.
   - **Exit 4:** the merge was aborted.
     - If `restored` is false, stop at once and report `pre_merge`. Do not
       touch the tree.
     - Otherwise the task branch is back at `pre_merge`. `reason` is
       `conflict` (files are listed in `conflicts`) or `merge-failed` (git
       refused before merging; `detail` says why, for example an untracked
       file in the way).
     - Interactive: ask once (`AskUserQuestion`) with three options:
       - resolve it by hand (merge the stage branch yourself, then
         `/run --resume <task>`);
       - re-run the stage sequentially on top of the task branch (`PS cleanup`
         with `--discard`; the stage stays ⏳);
       - undo with `/rollback`.
     - Autonomous: keep the worktree and branch, end the run, and report the
       reason, the files and those options.
   - **Exit 2 from merge:** stop and report the error.

   Never resolve a conflict silently, and never use `-X ours`/`-X theirs`.
   On `--resume`, re-plan first. A ⏳ stage whose stage branch already has a
   commit goes straight to review and merge.

Continue to the next stage without asking unless an escalation condition hits.
Under `AUTONOMOUS`, run every remaining stage in this session (only stage N
for `stage N of <task>`): never stop between stages
to suggest a fresh session or `/run --resume` (the harness compacts long
contexts); only an escalation condition ends the run early. Interactive only:
if this session's context is getting heavy after a few stages, stop after a
completed stage and tell the user to continue with `/run --resume <task>` in a
fresh session; resume starts at the first ⏳ stage.

## Implementer tier guard (before Step 5)

Steps 5–6 and the Step 7 fix pass always run on **opus**. Everything else
(Steps 0–4, the review spawn, Step 8 and every user question) stays in this
session, so discovery, the context pack and the review keep working: never
wrap the whole of `/run` in one subagent.

1. **Opt-out.** Implement inline when the input started with
   `[no-redispatch]`.
2. **Detect.** Read your model from system context ("powered by the model
   named X") and run `python3 __QUOIN_HOME__/scripts/model_tier.py "<X>"`.
   `INLINE|opus`, or `INLINE|optout` (`QUOIN_DISABLE_MINTIER_GUARD=1` is
   set) → run Steps 5–7 inline as written. `DISPATCH|<tier>` (haiku,
   sonnet, fable, mythos or unknown) → dispatch. Tiers above Opus dispatch
   too. If the script is missing, decide yourself: any model but Opus
   dispatches.
3. **Dispatch.** Spawn one general-purpose Agent with `model: "opus"`. Its
   prompt starts with `[no-redispatch]` and gives the brief text, the context
   pack path, the relevant lessons-learned entries, the repo path and task
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
5. **Review fixes.** After the Step 7 review, send the blockers and
   should-fixes to the same implementer with SendMessage (load it through
   ToolSearch if it is deferred). If SendMessage fails, spawn one more Opus
   Agent with the brief, the findings and the diff instead; if that spawn
   errors, apply item 7. The implementer applies the fixes, re-runs Step 6
   once and reports back. There is still only one review.
6. **Staged tasks.** Apply the guard per stage: one Opus implementer per
   stage, given the stage brief. Multi-stage `PARALLEL` waves always use
   one Opus implementer per stage, even when this session is on Opus.
7. **Fail-open** (only when the Agent spawn itself errors). Classify the
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

Marker: `review` (chain it onto the `git add -N` call below). Spawn one general-purpose review subagent on **opus**. Give it only the brief, the
full diff of the task against its base, and the verification result. The work is
not committed yet, so first mark new files with `git add -N <new files>` and pass
`git diff <base>` (base = the commit the task branch started from); a plain
`git diff` leaves out new files.
Do not pass your reasoning or the context pack, so its read is not anchored on
yours. Ask it for at most about 10 findings, each with `path:line`, severity
(blocker / should-fix / nit) and a concrete fix. Ask it to check correctness
against the acceptance criteria, missed call sites, security issues, and whether
the tests are proportionate (flag both missing and excessive tests).

Then log `review-fixes` and, inline on Opus (or through the Opus implementer when the guard
dispatched), apply blockers and should-fixes, skip nits unless trivial, and
re-run Step 6 once if code changed. Do not spawn a second review. Write a short
`.workflow_artifacts/<task>/review.md` listing each finding and what you did with it.

## Step 8 — Commit (inline)

Marker: `commit` (chain it onto the `git add` call).

1. Stage the changed files by name, never with `git add -A`. Never stage
   `.workflow_artifacts/` content into a project that does not track it.
2. Commit with a conventional message (`type(scope): summary`, body says why).
   The message is plain engineering prose per
   `__QUOIN_HOME__/memory/clean-authored-content.md`: no artifact paths, finding
   IDs or review narration.
3. Unless `LEAN`, if the task surfaced a reusable gotcha, append one entry to
   `.workflow_artifacts/memory/lessons-learned.md` by piping it into
   `python3 __QUOIN_HOME__/scripts/append_artifact.py --blank-line "<path>"` (no prior Read needed).
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
  pack and the review (steps whose job is to read a lot and return a conclusion,
  or to give a fresh read), plus the Opus implementer when the session is not
  on Opus.
- No cost-ledger rows, run-state records, checkpoints or progress sentinels: those
  belong to the heavy pipeline's multi-session orchestration.
- Never write quoin's own files (for example `quoin/dev/tests/...`) into another
  project. Task state lives only under `.workflow_artifacts/<task>/`.
