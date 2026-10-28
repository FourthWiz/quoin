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
   non-goals, assumptions). Ask the user when the request is unclear in
   a way that changes the work; never ask in autonomous mode.
5. Context pack, delegated to a read-only helper: likely files and
   symbols, covering tests and how to run them, conventions, relevant
   lessons. Written to `context.md`.
6. Implement, inline, with no plan file. Tests are proportionate: about
   one regression test per behaviour change, extending existing files.
7. Verify, inline: run affected tests once; if the environment cannot
   be built, record that and continue without retry loops.
8. Review, delegated, one pass, strong model, given only the brief, the
   diff and the test result. Fixes are applied inline; no second review.
9. Commit locally. Never push or open a PR.

## Escalation

Recommend `run_hard` when the review finds a design-level problem, two
implementation attempts fail for unclear reasons, or the task spans
several repositories. Never switch automatically.

## Artifacts

`.workflow_artifacts/<task-name>/brief.md`, `context.md`, `review.md`. No
cost ledger, run-state record, checkpoints or progress sentinels.
