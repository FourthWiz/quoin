# run

Runtime-neutral intent for the run skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

The default way to take a task from a raw request to a reviewed local
commit. It adds four things over a plain session: a precise task brief,
a context pack gathered before coding, proportionate tests, and one
independent review. It does not run multi-round planning, critique
loops, architecture rounds or a separate gate; those live in `run_hard`.

## Sequence

Task artifacts live under `.workflow_artifacts/<task-name>/`.

1. Route: `hard:`/`strict:` input, or a resume of a task the heavy
   pipeline started, hands off to `run_hard` unchanged.
2. Setup: task folder, fresh branch, relevant lessons.
3. Discover, delegated, only when discovery memory is missing or older
   than the staleness threshold (7 days by default).
4. Frame, inline: write `brief.md` (goal, acceptance criteria,
   non-goals, assumptions, shape). Ask the user when the request is
   unclear in a way that changes the work; never ask in autonomous mode.
   Shape is `single` by default and `staged` when the change spans
   several repositories, has parts that must land in order, or will not
   fit one coherent diff.
5. Context pack, delegated to a read-only helper: likely files and
   symbols, covering tests and how to run them, conventions, relevant
   lessons. Written to `context.md`.
5b. Staged tasks only: write a short `architecture.md` whose
   `## Stage decomposition` section lists two to five stages (one repo
   each, with scope, dependencies and acceptance), confirm it with the
   user unless autonomous, then run steps 6–9 once per stage with a
   stage brief under `stage-N/`, one review and one commit per stage,
   marking each stage done in `architecture.md`. `--resume` continues at
   the first unfinished stage.
6. Implement, inline, with no plan file. Tests are proportionate: about
   one regression test per behaviour change, extending existing files.
7. Verify, inline: run affected tests once; if the environment cannot
   be built, record that and continue without retry loops.
8. Review, delegated, one pass, strong model, given only the brief, the
   diff and the test result. Fixes are applied inline; no second review.
9. Commit locally. Never push or open a PR.

## Escalation

Recommend `run_hard` when the review finds a design-level problem, two
implementation attempts fail for unclear reasons, or the task needs more
than five stages or a stage needs its own design work. Spanning several
repositories alone is handled by the staged path. Never switch
automatically.

## Artifacts

`.workflow_artifacts/<task-name>/brief.md`, `context.md`, `review.md`.
Staged tasks add `architecture.md` at the task root and `brief.md`,
`review.md` (and `context.md` when needed) under each `stage-N/`. No
cost ledger, run-state record, checkpoints or progress sentinels.
