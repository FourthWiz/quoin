# run

Runtime-neutral intent for the run skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

The default way to take a task from a raw request to a local commit.
By default it is plain strong-model implementation with proportionate
tests; a scope triage adds a review, a stage plan or the heavy pipeline
only when the task carries that class's signal, because on ordinary
changes they add cost and latency without a measurable gain in
resolution. It does not run multi-round planning, critique loops,
architecture rounds or a separate gate; those live in `run_hard`.

## Sequence

Task artifacts live under `.workflow_artifacts/<task-name>/`. Each step
starts by appending a timestamped phase marker to `phases.log` (with
`skipped` when the step is skipped and a stage prefix inside a staged
task); markers change no behaviour and are never committed.

1. Route: `hard:`/`strict:` input, or a resume of a task the heavy
   pipeline started, hands off to `run_hard` unchanged.
2. Setup: task folder, fresh branch, relevant lessons. Before branching,
   the run claims the checkout in a per-checkout registry under an
   exclusive lock (`run_isolation.py`). When another live run already
   holds it, or the `isolate:` tag asks, the run gets its own git
   worktree on the task branch outside the project folder and does all
   later git, edit, test, review and commit work there; artifacts stay at
   the project root. A resume returns to the same worktree; the run
   releases its claim at the end and leaves the worktree and branch for
   workspace teardown. Without the helper the run works in place.
2a. Scope triage, inline (no helper, no extra model call): from the
   request (a referenced issue's title and body fetched first) plus at
   most one quick look at the code, put the task
   in exactly one class, checked from the top. A class above R0 needs a
   signal the session can name; without one, also when unsure, it is R0.
   - R3 Needs design: a novel architecture, a data-migration strategy, or
     an interface several consumers must agree on.
   - R2 Multi-part: more than one repository, or parts that must land in
     order.
   - R1 Risky: auth, permissions, crypto, input sanitising, a schema or
     migration, or a public API.
   - R0 Plain: everything else, one coherent low-risk change.
   Size, file count, a dependency bump or a long request are not signals.
   Each class adds exactly one thing: R0 Plain implements on the strong
   model, verifies and commits, skipping discovery (3), the context pack
   (5) and the review (8), with framing (4) a marker only; R1 adds the review; R2 adds a
   short stage plan (3–5b), each stage running as R0 or R1; R3 adds the
   heavy pipeline, on explicit opt-in only (interactive runs are asked
   once; autonomous runs never switch, run as R2 or R1 and recommend the
   heavy pipeline in the report). The `r0:`, `r1:` and `r2:` tags force a
   class, also in autonomous mode; `hard:` is the opt-in for R3. The
   request, verbatim, and the class with the signal that fired go into
   `brief.md` (`route:`, `signal:`) with the skipped markers in one call; the verification result and commit
   are appended at the end. A plain run reclassifies upward, never down,
   when the work shows a missed signal: to R1 by adding the review, to R2
   by moving to the staged steps while keeping the diff. The skill text a
   plain run loads is kept to a short core; tags, resume, isolation
   modes, the non-strong-model dispatch, lessons and escalation sit in an
   on-demand reference file, and the R2 steps in another.
3. R2 only. Discover, delegated, only when discovery memory is missing, older
   than the staleness threshold (7 days by default), or marked out of
   date by an earlier task's `discovery-dirty` marker (step 9). A benchmark-only
   lean profile (`QUOIN_RUN_PROFILE=bench-lean`, set by the benchmark
   harness, never a user tag) skips this step, the isolation claim and
   release, the lessons-learned read and append, and the dirty marker;
   every other step runs unchanged. The memory profile
   (`QUOIN_RUN_PROFILE=bench-memory`, for sequence suites whose workflow
   memory carries between tasks) skips only the isolation claim and release.
4. R2 only. Frame, inline: `brief.md` already holds the request and the
   scope; record further assumptions only.
5. R2 only. Context pack, delegated to a read-only helper that starts from the
   cached repo map when present: likely files and symbols, covering tests and how to run them, conventions, relevant
   lessons. Written to `context.md`.
5b. R2 only: write a short `architecture.md` whose
   `## Stage decomposition` section lists two to five stages (one repo
   each, with scope, class R0 or R1, dependencies and acceptance), confirm
   it with the user unless autonomous, then run steps 6–9 once per stage
   with a stage brief under `stage-N/`, a review for each R1 stage and one
   commit per stage,
   marking each stage done in `architecture.md`. Autonomous runs go
   through every stage in one session, stopping early only on an
   escalation condition. `--resume` continues at the first unfinished
   stage. With the `parallel:` tag, stages are grouped by their declared
   dependencies into waves (capped by `QUOIN_PARALLEL_MAX`, default 2, and
   dropped to one at a time once the optional daily spend budget
   `QUOIN_PARALLEL_BUDGET_USD` is reached). A stage with no declared
   dependency line waits for the one before it. The
   stages of a wave run concurrently, each in its own worktree on a temporary
   stage branch, and are then merged into the task branch one at a time,
   followed by one integration test run. A conflicting or refused merge is
   aborted with the task branch left at its pre-merge commit, and recovery is
   offered: resolve by hand, re-run the stage sequentially, or roll back.
6. Implement, with no plan file, always on the strong (implementation)
   model: inline when the session already runs on it, otherwise in one
   delegated implementer on that model, given the brief, the context
   pack, the branch and the test command. Any other tier, cheaper or
   more expensive, delegates. Orchestration, helpers and user questions
   stay in the parent. Opt out with a no-redispatch prefix or the
   runtime's min-tier knob. If delegation fails, proceed inline: silently
   in autonomous mode, otherwise only after the user picks abort or
   proceed. Staged tasks use one implementer per stage. Tests are
   proportionate: about one regression test per behaviour change,
   extending existing files.
7. Verify, in the same place as step 6: run affected tests once; if the
   environment cannot be built, record that and continue without retry
   loops. Test runs happen in the foreground with an explicit long tool
   timeout, and a delegated implementer never ends its turn with a run
   pending. If it does anyway, the parent starts its own bounded waiter on
   that specific test process (keyed on a unique token in its report path,
   never on any test process) so it is re-invoked when the run exits, then
   prompts the implementer to read the result. No user prompt is needed.
8. R1, and each R1 stage of R2, only. Review, delegated, one pass, strong model, given only the brief, the
   diff and the test result. Fixes are applied where step 6 ran (the
   same implementer when delegated); no second review.
9. Commit locally, then record the branch and commit in the brief (so
   `/cleanup` can find the PR). Never push or open a PR. A reusable lesson is
   checked against `lessons-learned.md` with the duplicate guard before it
   is appended. A script, not the model, reads the commit's name-status
   list: when it changed the project's structure (top-level directories or
   packages), a dependency manifest or an interface definition file, it
   appends a line to `.workflow_artifacts/memory/discovery-dirty` so the
   next run refreshes discovery; discovery deletes the marker. This step
   writes no session state, checkpoints, cost rows or run-state records.

## Escalation

The task outgrows this skill when the review finds a design-level
problem, two implementation attempts fail for unclear reasons, or the task
needs more than five stages or a stage needs its own design work. Spanning
several repositories alone is handled by the staged path. On a trigger the
run hands the task to `run_hard` itself: interactive runs ask once (switch,
continue as is, or stop); autonomous runs switch and record why in the
brief. The hand-off writes `pipeline-switch.md` at the task root and keeps
the brief, context pack, review notes, completed stages and the partial
diff (no reset or stash); the worktree and branch stay and `run_hard`
works in them. A `no-switch:` tag or `QUOIN_RUN_AUTO_SWITCH=0` keeps the
older behaviour: stop and recommend `run_hard`. A task switches pipelines
at most once; a resume follows the switch note's direction.

## Artifacts

`.workflow_artifacts/<task-name>/brief.md`, `context.md`, `review.md`,
`phases.log` (an R0 task writes only `brief.md` and `phases.log`).
Staged tasks add `architecture.md` at the task root and `brief.md`,
`review.md` (and `context.md` when needed) under each `stage-N/`. No
run-state record, checkpoints or progress sentinels. No cost ledger,
except that parallel stages record one row per stage implementer and
reviewer, tagged with the stage, so cost reports can break costs down by
stage.
