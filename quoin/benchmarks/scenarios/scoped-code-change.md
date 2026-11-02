# Scoped Code Change

## Purpose

Evaluate whether the workflow can implement a bounded change with appropriate
tests and minimal unrelated churn.

## Starting State

Use a clean checkout of the fixture repository and a small issue with a clear
behavioral target. The task should be feasible in one focused implementation
pass.

## Prompt

Implement the requested scoped code change. Keep the edit narrowly focused,
preserve existing style, run the relevant checks, and report exactly what
changed and what was not verified.

## Mode Notes

- Simple Claude: use Claude normally to implement and report.
- Quoin + Claude: use Quoin planning, implementation, gate, and review artifacts
  under `.workflow_artifacts/` where appropriate.
- Simple Codex: use native Codex implementation workflow.
- Quoin + Codex: use repo-local Quoin guidance while relying on native Codex
  approvals and sandboxing, with portable evidence under `.workflow_artifacts/`.
- Simple OpenCode: use OpenCode to implement and report. Same model and endpoint
  as the Quoin OpenCode run.
- Quoin + OpenCode: use the Quoin workflow commands for plan, implementation,
  gate and review, with artifacts under `.workflow_artifacts/`.

## Expected Evidence

- Code diff.
- Relevant test or static-check output.
- Final implementation summary.
- For Quoin modes, plan, gate, review, or cost-ledger evidence when produced.

## Evaluation Notes

Score for correctness, scope control, test relevance, and whether the final
report distinguishes completed work from residual risk.
