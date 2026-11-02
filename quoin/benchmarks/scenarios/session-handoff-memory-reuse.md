# Session Handoff / Memory Reuse

## Purpose

Evaluate whether the workflow can continue from prior-session evidence without
repeating avoidable discovery or losing important constraints.

## Starting State

Use a fixture repository with a prior run note, partial plan, or handoff file.
For Quoin modes, place the prior evidence under the documented Quoin memory or
task artifact layout. For simple modes, provide an equivalent plain handoff note
outside Quoin-specific paths.

## Prompt

Continue from the provided handoff. Summarize the relevant prior context, state
the next action, identify any stale or missing information, and perform the next
non-destructive workflow step.

## Mode Notes

- Simple Claude: use Claude normally with the plain handoff note.
- Quoin + Claude: use Quoin session and task artifacts under
  `.workflow_artifacts/`.
- Simple Codex: use Codex normally with the plain handoff note.
- Quoin + Codex: use repo-local Quoin guidance and portable session-state
  semantics under `.workflow_artifacts/`.
- Simple OpenCode: start a fresh OpenCode session and rely on the repository
  alone. Same model and endpoint as the Quoin OpenCode run.
- Quoin + OpenCode: continue from the portable handoff under
  `.workflow_artifacts/` in a new OpenCode session.

## Expected Evidence

- Continuation summary.
- Prior context used.
- Next action or non-destructive step completed.
- For Quoin modes, session, memory, or task artifact references.

## Evaluation Notes

Score for accurate context reuse, stale-context detection, reduced rediscovery,
and quality of the next-step recommendation.
