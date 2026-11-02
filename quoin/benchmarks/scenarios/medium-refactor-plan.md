# Medium Refactor Plan

## Purpose

Evaluate planning quality for a refactor that is too large for a blind edit but
small enough to stage within one task.

## Starting State

Use a clean checkout of the fixture repository with a documented target module
or subsystem. The operator may provide a short refactor goal, but should not
provide an implementation plan.

## Prompt

Plan a medium refactor for the target subsystem. Identify the current structure,
proposed stages, affected files, correctness risks, validation strategy, and
explicit non-goals. Do not implement the refactor.

## Mode Notes

- Simple Claude: use Claude normally and return a plan.
- Quoin + Claude: use Quoin architecture and planning artifacts under
  `.workflow_artifacts/`.
- Simple Codex: use native Codex planning and return a plan.
- Quoin + Codex: use repo-local Quoin guidance to create portable architecture
  and current-plan artifacts under `.workflow_artifacts/`.
- Simple OpenCode: ask OpenCode for a refactor plan in its native session. Same
  model and endpoint as the Quoin OpenCode run.
- Quoin + OpenCode: use the Quoin planning commands from the installed scaffold,
  writing the plan under `.workflow_artifacts/`.

## Expected Evidence

- Refactor plan with stages or sequencing.
- Risk and test strategy.
- Relevant inspected files.
- For Quoin modes, `architecture.md` and `current-plan.md` evidence.

## Evaluation Notes

Score for whether the plan is actionable, staged, realistic, testable, and
honest about uncertainty. Penalize plans that overfit to runtime-specific
features or invent repository facts.
