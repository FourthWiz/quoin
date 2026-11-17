# Run — light pipeline steps

*Reference for `/run`, deployed as
`__QUOIN_HOME__/skills/run/light-pipeline.md`. `/run` reads this file once,
with one Read, when the task is r2 (multi-part): the routes file picks r2 (or r3 runs
as r2), a plain run reclassifies to r2, or `--resume` (or `stage N of <task>`)
continues a staged run. It also holds the run_hard switch procedure. Step
numbers, phase markers, artifacts and the "as written" rules of SKILL.md apply
unchanged here.*

An r2 task runs, in order: Step 2, Step 3, Step 4 and Step 4b below; then, per
stage, the implementer tier guard and Steps 5, 6, 7 (r1 stages only; Step 7
is in the routes file) and 8 in SKILL.md. "Steps 5–8" means that same sequence.

## Resuming a staged run

Resume reads the brief (Step 0) and continues from the first step whose output
is missing. If `.workflow_artifacts/<task>/architecture.md` exists, the task is
staged: continue at the first stage still marked ⏳, from the first step of
that stage whose output under `stage-N/` is missing.
**`stage N of <task>`** works the same way but runs only stage N.

## Switch to run_hard

Used by Escalation in SKILL.md once a trigger fires and switching is allowed,
and by the routes file for an interactive r3 task (trigger `design`).

1. **Ask once** (interactive, one AskUserQuestion): "Switch to run_hard"
   (recommended; keeps the brief, context pack and diff), "Continue as is"
   (stay in `/run` and record the choice under Assumptions; failing
   code is still never committed, so a verification trigger ends with a report
   instead; for `design`, add `runs-as: r2` or `runs-as: r1` to `## Scope` and
   go on as that class) or "Stop" (the
   Escalation stop). Under `AUTONOMOUS`, switch without asking and add the
   trigger and reason under Assumptions in the brief; trigger `design` never
   reaches this procedure under `AUTONOMOUS`, since r3 is opt-in only.
2. **Write `.workflow_artifacts/<task>/pipeline-switch.md`**: lines
   `from: light`, `to: hard`, `trigger: <review-blocker | verification |
   stages | design>`, `reason: <one line>`, `mode: <interactive | autonomous>`,
   `worktree: <WT, or none>`, `branch: <task branch>`, and `reuse:` listing
   whichever of `brief.md`, `context.md`, `review.md` exist, and `claim:
   <repo>`. Add
   `switched: to hard — <trigger>` to the brief's `## Scope` section.
3. **Keep the work.** No reset, stash or commit of the partial diff; stages
   already committed stay. A staged task renames, with
   `python3 __QUOIN_HOME__/scripts/fsops.py mv`, `architecture.md` to
   `light-architecture.md` and each `stage-N/` to `light-stage-N/`, so
   run_hard's architect starts clean and can read them.
4. **Keep the claim.** run_hard continues the run in this session and releases
   it when it ends, so no other run builds on the partial diff. It works in
   `WT`; the worktree and branch stay.
5. **Hand over.** Invoke the `run_hard` skill with `[from-light] <task>`, plus
   `--autonomous` under `AUTONOMOUS`, and stop: run_hard owns the task from here.

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

Marker: `frame`, unless a plain run already logged it before reclassifying to r2.
The routes file already wrote `.workflow_artifacts/<task>/brief.md`: the request
and `## Scope` with the r2 signal. Do not restate the request
or ask about it again; record any further assumption you make under
`## Assumptions`. Go on to Step 4 and then Step 4b.

## Step 4 — Context pack (subagent)

Marker: `context`. Spawn one read-only subagent (Explore type, sonnet) with the brief's text, `WT` (the tree to explore) and the
paths of the discovery memory files (none under `LEAN`: it explores the repo from scratch). Its job is to return the pack below (at
most about 60 lines), which you then save as `.workflow_artifacts/<task>/context.md`
(the subagent is read-only):

- the files and symbols the change most likely touches, with `path:line`;
- the existing tests covering them, and the command that runs those tests;
- conventions the change must follow (error handling, naming, similar prior code);
- relevant lessons-learned entries (none under `LEAN`);
- anything surprising (generated files, duplicated logic, a second call site).

Tell the subagent to start from `.workflow_artifacts/cache/<repo>/repo-map.md`
(a ranked symbol map; skip it if absent or its `hash:` is not the current HEAD)
to locate likely files, to prefer Serena
symbol tools when available, to read excerpts rather than whole files, and to
report conclusions, not file dumps. You keep only
its summary in context.

Then Step 4b.

## Step 4b — Decompose (inline)

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
   - Class: <r0 | r1>
   - Depends on: <none | S-0N>
   - Acceptance: <observable checks for this stage>
2. ⏳ S-02: <short stage name>
   ...
## Interfaces between stages
- <each contract a later stage relies on: function signature, API shape, schema>
```

A stage's class is r1 when its scope hits an r1 signal (SKILL.md Step 1b),
otherwise r0; only r1 stages get the Step 7 review. Two to five stages. Each stage must leave every repo it touches building and
passing its tests on its own. If the task needs more than five stages, or a
stage needs its own design work (a data migration strategy, a new public API
with several consumers), stop and escalate instead (see Escalation).

**Confirm.** Interactive: show the stage list and ask once (`AskUserQuestion`):
proceed with these stages, run it as a single change instead (the plain
route, as r1 when an r1 signal holds, else r0; set `## Scope` to match), or switch to
`run_hard` (the switch procedure above, with no second question). When
Escalation does not allow switching (`NO_SWITCH`, or `pipeline-switch.md`
exists), the third option is stop and recommend `/run hard:` instead. Apply edits the user asks for. Under `AUTONOMOUS`, proceed and
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
   stage), with the stage brief in place of the task brief. Step 7 runs only
   for an r1 stage (an r0 stage logs `review --skipped --stage N`): its
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
   - Review each r1 stage (Step 7) on `git diff <base>...<stage branch>`,
     send fixes to that stage's implementer, and write `stage-N/review.md`.
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
     stage, item 4 with the base from `PS worktree`.
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
