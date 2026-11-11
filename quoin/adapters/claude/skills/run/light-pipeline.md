# Run — light pipeline steps

*Reference for `/run`, deployed as
`__QUOIN_HOME__/skills/run/light-pipeline.md`. `/run` reads this file once,
with one Read, when the task takes the light route: Step 1b picks light, the
Direct route escalates, or `--resume` (or `stage N of <task>`) continues a
light or staged run. Step numbers, phase markers, artifacts and the "as
written" rules of SKILL.md apply unchanged here.*

The light route runs, in order: Step 2, Step 3, Step 4 and (staged tasks)
Step 4b below; then the implementer tier guard and Steps 5 and 6 in SKILL.md;
then Step 7 below; then Step 8 in SKILL.md. "Steps 5–8" means that same
sequence.

## Resuming a light or staged run

Resume reads the brief (Step 0) and continues from the first step whose output
is missing. If `.workflow_artifacts/<task>/architecture.md` exists, the task is
staged: continue at the first stage still marked ⏳, from the first step of
that stage whose output under `stage-N/` is missing.
**`stage N of <task>`** works the same way but runs only stage N.

## Step 2 — Discover (subagent, conditional)

Discovery memory is refreshed when it is missing, older than 7 days
(`QUOIN_DISCOVERY_STALE_DAYS`), or marked out of date by an earlier task
(`.workflow_artifacts/memory/discovery-dirty`, written in Step 8):

```bash
if [ ! -f .workflow_artifacts/memory/discovery-dirty ] && [ -n "$(find .workflow_artifacts/cache/_staleness.md .workflow_artifacts/memory/repo-heads.md -mtime -7 2>/dev/null | head -1)" ]; then echo recent; python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" discover --skipped; else echo stale; python3 "__QUOIN_HOME__/scripts/run_phase.py" "<task>" discover; fi
```

- Under `LEAN`, do not run that check (nor read the dirty marker): log
  `run_phase.py "<task>" discover --skipped` and go on to Step 3. The context
  pack explores the repository itself.
- `recent` → skip and say so in one line.
- `stale` (missing, older than 7 days, or marked dirty) → spawn one subagent
  on **opus** whose prompt starts with `[no-redispatch]` and runs `/discover` for
  the project root (the prefix stops `/discover` from re-dispatching itself into
  a second subagent). Wait for it; its output files are the project memory the
  next steps read. `/discover` deletes the dirty marker when it finishes; if it
  failed, leave the marker so the next run retries.

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
## Size
route: light
reason: <the one-line reason from Step 1b>
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

Marker: `context`. Spawn one read-only subagent (Explore type, sonnet) with the brief's text, `WT` (the tree to explore) and the
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

A single task then goes on to the implementer tier guard and Steps 5 and 6 in
SKILL.md, then comes back here for Step 7; a staged one goes through Step 4b
first.

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

**Implementer tier guard, per stage.** Apply the guard per stage: one Opus
implementer per stage, given the stage brief. Multi-stage `PARALLEL` waves always use
one Opus implementer per stage, even when this session is on Opus.

**Then run Steps 5–8 once per stage, in order.** For stage N:

1. Write `.workflow_artifacts/<task>/stage-N/brief.md` from the stage entry: goal,
   acceptance criteria, non-goals (the other stages' scope), and the interfaces
   it must provide or may rely on.
2. In the stage's repo, make sure the task branch exists (the same branch name in
   every repo). The first time a stage touches a repo, run Step 1 items 2–3
   for it (claim; create the branch only in `mode: main`), and do the
   stage's git, test and commit work in that repo's `WT`.
   Under `PARALLEL`, a stage in a multi-stage wave instead works on a
   temporary stage branch in its own worktree, merged back into the task
   branch (see "Parallel waves").
3. If the stage's repo or area is not covered by the task's context pack, run
   Step 4 for the stage brief and save it to `stage-N/context.md`.
4. Steps 5–8 as written (Step 8 item 6, the release, waits for the last
   stage), with the stage brief in place of the task brief: the
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
`<repo>` below is the stage repo's task working tree: `WT` from that repo's
Step 1 claim, which is an isolated worktree when the run was isolated. Stage
worktrees then branch from that tree.

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
5. **Integrate one stage at a time, in file order.** From the task working
   tree (`<repo>`), on the task branch, run
   `PS merge --repo "<repo>" --into "<task branch>" --branch "<stage branch>"`.
   `.workflow_artifacts/` edits never block a merge.
   - **Exit 0 (merged):** run
     `PS cleanup --repo "<repo>" --into "<task branch>" --branch "<stage branch>"`
     and mark that stage ✅ at once. If cleanup reports `partial`, note the
     leftover worktree in the report and carry on. The stage implementer
     committed on its own branch, so run Step 8 items 3 and 4 here for that
     stage, on `git diff --name-status <base>..HEAD` (base from `PS worktree`).
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

**Review fixes through the implementer.** When the implementer tier guard
dispatched: after the Step 7 review, send the blockers and
should-fixes to the same implementer with SendMessage (load it through
ToolSearch if it is deferred). If SendMessage fails, spawn one more Opus
Agent with the brief, the findings and the diff instead; if that spawn
errors, apply the guard's fail-open item. The implementer applies the fixes,
re-runs Step 6 once and reports back. There is still only one review.

Then Step 8 in SKILL.md.
