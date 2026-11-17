# Run — reference

*Reference for `/run`, deployed as `__QUOIN_HOME__/skills/run/reference.md`.
`/run` reads this file once, with one Read, only when SKILL.md points here: a
flag or tag beyond the core ones, an isolation result other than `mode: main`,
a non-Opus session, a lesson to record, or an escalation. Step numbers, phase
markers and artifacts of SKILL.md apply unchanged.*

## Step 0 — Tags and resume

Scan the input before anything else. The task name is the first token that is
not a flag or tag (`--resume`, `--autonomous`, `[autonomous]`, `[no-redispatch]`,
`[from-hard]`, `hard:`, `strict:`, `small:`, `parallel:`, `r0:`, `r1:`, `r2:`, `no-switch:`, `isolate:` and the like; a leading `[no-redispatch]` is
stripped and remembered for the implementer tier guard); when handing off to `run_hard`, always pass the original
input unchanged apart from removing `hard:`, `r0:`, `r1:` and `r2:` (and `isolate:`),
except the Escalation switch, which passes its own input.

1. **`hard:`** or **`strict:`** anywhere in the input → this task wants the full
   heavy pipeline. Invoke the `run_hard` skill with the input unchanged (keep
   `strict:`, strip only `hard:`) and stop; `run_hard` owns everything from there.
2. **`--resume`** (task name as above) → if `.workflow_artifacts/<task>/pipeline-switch.md`
   exists, its `to:` line decides alone: `hard` → invoke `run_hard` with the input
   unchanged and stop; `light` → resume the light run below, ignoring run_hard's
   files. Otherwise, if `.workflow_artifacts/memory/run-state-<task>.json` or
   `.workflow_artifacts/memory/autonomous-run-<task>.marker` exists, the task was
   started by the heavy pipeline: invoke `run_hard` with the input unchanged and
   stop. Otherwise resume the light run: run Step 1's claim (item 2; it
   returns the task's worktree if it has one), read `.workflow_artifacts/<task>/brief.md`,
   check `git status`/`git log` on the task branch in `WT`, and continue from the first
   step whose output is missing. The brief's `## Scope` decides the route:
   `route: r0` or `r1` (or `runs-as: r1`) resumes on the plain route, `r2`
   (or `runs-as: r2`) on the staged route, and an `r3` with no `runs-as:`
   line repeats Step 1b's r3 handling. A task with no brief, or a brief with
   no `## Scope`, restarts at Step 1b, keeping any diff on its task branch;
   Step 1b then appends `## Scope` to an existing brief instead of rewriting
   it. An older brief's `## Size` maps `route: direct` to r0 and
   `route: light` to r1, or to r2 when `architecture.md` exists. A staged run
   (also **`stage N of <task>`**) first reads the light pipeline file, whose
   resume section covers staged tasks.
3. **`--autonomous`** (or an `[autonomous]` sentinel from a parent session) →
   strip it and set `AUTONOMOUS=true`: never ask the user anything; record
   assumptions in the brief instead.
4. **`small:` / `medium:` / `large:` / `fast:` / `max_rounds: N`** → strip them. They
   select heavy-pipeline profiles and have no effect here. If the user wrote
   `large:` interactively, mention in one line that `/run hard:` runs the full
   pipeline, then continue in `/run`.
5. **`parallel:`** → strip it and set `PARALLEL=true`. It only changes how an
   r2 task runs its stages (Step 4b, "Parallel waves", in the light
   pipeline file); an r0 or r1
   task ignores it. It combines with `--autonomous` and `--resume`.
6. **`QUOIN_RUN_PROFILE`** (benchmark only, never a tag):
   `bench-lean` sets `LEAN`, `bench-memory` sets `MEMORY`. Read it from the
   Step 1 marker's `profile=` line (`printenv QUOIN_RUN_PROFILE` on a
   `--resume` that skips Step 1). `LEAN` skips isolation, lessons and the
   dirty marker (Step 1 items 2 and 4, Step 8 items 3, 4 and 6), and Step 2
   only logs its marker as skipped; `MEMORY` skips only isolation (Step 1
   item 2, Step 8 item 6). All else as written.
7. **`r0:`** / **`r1:`** / **`r2:`** → recognised only as whitespace-delimited
   tokens among the leading flags and tags, before the task text (so
   `hdr0:` or a tag quoted in an issue body never counts). Strip them and
   remember the override for Step 1b: each forces its class, with signal
   `forced by <tag>` (if several appear, the highest wins). They apply under
   `--autonomous` too. A forced class is never reclassified. There is no
   `r3:` tag: `hard:` is the opt-in to the heavy pipeline.
8. **`isolate:`** → recognised the same way as `r0:`/`r1:`/`r2:`. Strip it and
   set `ISOLATE=true`: Step 1 works in a worktree even when no other run holds
   the checkout.
9. **`no-switch:`** → recognised the same way; strip it and set `NO_SWITCH=true`
   (also when `QUOIN_RUN_AUTO_SWITCH=0`; read it with `printenv` when a trigger
   fires): Escalation then stops instead of switching to `run_hard`.
10. **`[from-hard]`** → `run_hard` handed this task over (its
   `pipeline-switch.md` says `to: light`). Strip it and keep the given task
   name. Step 1 keeps the task branch `run_hard` created (no new branch); the
   note's `evidence:` file is the request: Step 1b points the brief's
   `## Request` at it and classifies from it as usual,
   except that r3 runs as r2 or r1 without asking. The task never switches
   again.

## Isolation modes

Step 1 item 2's claim, in full:
`python3 "__QUOIN_HOME__/scripts/run_isolation.py" claim --task "<task>" --branch "<branch>" --repo "<repo>" --project-root "$(pwd)"`
(`<repo>` is the main checkout, never `WT`), plus `--isolate` under
`ISOLATE`. Set `WT` to the `worktree` it prints. `LEAN`/`MEMORY`: no claim,
`WT` is the checkout.

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
project root, with `.workflow_artifacts/` and the phase markers. In a
multi-repo project, claim and branch in the repo the change lands in; a staged
task creates the branch in each repo when its first stage there starts (Step
4b, in the light pipeline file). In worktree mode the final report names `WT`
and the branch. Both stay; `/workspace teardown <task>` removes them after its
safety checks.

## Implementer tier guard (dispatch)

SKILL.md's guard sent you here because the session is not on Opus
(`DISPATCH|<tier>`: haiku, sonnet, fable, mythos or unknown; tiers above Opus
dispatch too). Steps 5–6 and the Step 7 fix pass always run on **opus**.
Everything else (Steps 0–4, the review spawn, Step 8 and every user question)
stays in this session, so discovery, the context pack and the review keep
working: never wrap the whole of `/run` in one subagent. On the plain route
the guard runs before its step 1.

When the implementer tier guard dispatches, you write `implement` before the
spawn and `review-fixes` before sending it the findings; it writes none. (The
phase report counts a subagent's messages under the phase that spawned it, so
fixes made through that implementer show up as `implement`.)

1. **Dispatch.** Spawn one general-purpose Agent with `model: "opus"`. Its
   prompt starts with `[no-redispatch]` and gives the brief text, the context
   pack path, the relevant lessons-learned entries, the absolute `WT` path and task
   branch, the test command, and the text of Steps 5 and 6 of SKILL.md (the
   implementer sees neither file). It tells the agent to do those two steps, not to commit
   and not to spawn subagents, and to return the files changed, the tests
   added, the verification result (command and pass/fail) and any blocker.
   On the plain route (r0, r1) it gets the brief and the test command, with
   no context-pack path. It also says, verbatim: "Run every test command in the FOREGROUND with an
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
2. **Watchdog.** Your notification fires when the implementer's turn ends,
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
3. **Review fixes.** After the Step 7 review, send the blockers and
   should-fixes to the same implementer with SendMessage (load it through
   ToolSearch if it is deferred). If SendMessage fails, spawn one more Opus
   Agent with the brief, the findings and the diff instead; if that spawn
   errors, apply the fail-open item. The implementer applies the fixes,
   re-runs Step 6 once and reports back. There is still only one review.
4. **Fail-open** (only when the Agent spawn itself errors). Classify the
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

## Lessons

Step 8 item 3, unless `LEAN`: if the task surfaced a reusable gotcha, record one entry in
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

For the discovery-dirty marker (Step 8 item 4), the full detail: check the commit with
`git show --name-status --format= HEAD`, or `git diff --name-status <base>..HEAD`
when the task or stage made more than one commit. Dependency manifests are
`pyproject.toml`, `setup.py`, `requirements*.txt`, `package.json`, `go.mod`,
`Cargo.toml`, `pom.xml`, `build.gradle*`, `Gemfile` and the like. The next
r2 run's Step 2 (light pipeline file) refreshes discovery and `/discover`
removes the marker. Internal edits, tests and docs alone do not count.

## Reclassify to r2

A plain run (r0 or r1, not forced by a tag) moves to r2, one way, when the
change turns out to span repositories or to need parts that land in order.
Keep the working tree as it is: no reset, no stash. Read the light pipeline
file, then in one Edit set the brief's `## Scope` to `route: r2` with a line
`escalated: <r0 | r1> → r2 — <signal>`, keeping `## Request`, and
continue at its Step 2. The stages build on the existing diff (through the
same Opus implementer, via SendMessage, when the guard dispatched); the first
stage's review covers it if that stage is r1.

## Escalation

The task is bigger than `/run` when:

- the review returns a blocker that is a design problem, not a local fix;
- two implementation attempts fail verification for reasons you do not understand;
- the task needs more than five stages, or a stage needs its own design work
  (a data migration strategy, a new public API with several consumers).

A task that spans several repos or needs a few stages is not by itself a reason
to escalate: r2's Step 4b (light pipeline file) handles it.

When one fires, switch to `run_hard` through the switch procedure in the light
pipeline file: it asks once (interactive), keeps the brief, context
pack and working tree, and hands over in this session. Do not switch, and
instead stop and recommend `/run hard: <task>` (interactive) or end with that
recommendation in the final report (autonomous), when `NO_SWITCH` is set, when
`.workflow_artifacts/<task>/pipeline-switch.md` already exists (a task switches
at most once), or when the user picks stop. Before stopping, release the claim
(Step 8 item 6) and, in worktree mode, name `WT` and the branch: `/run hard:`
does not isolate, so run it from `WT`.
