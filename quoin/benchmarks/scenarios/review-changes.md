# Review Changes

## Purpose

Evaluate how well the workflow reviews a prepared diff for concrete defects,
missing tests, and mismatch with the stated goal.

## Starting State

Use a fixture repository with an existing branch or patch. The patch should
contain at least one reviewable risk, but the reviewer should not be told where
the risk is.

## Prompt

Review the current changes as a code reviewer. Prioritize correctness bugs,
behavioral regressions, missing tests, and scope drift. Provide findings first
with file and line references where possible.

## Mode Notes

- Simple Claude: use Claude normally as a code reviewer.
- Quoin + Claude: use Quoin review artifacts and any relevant current plan under
  `.workflow_artifacts/`.
- Simple Codex: use native Codex review behavior.
- Quoin + Codex: use repo-local Quoin guidance to produce portable review
  evidence under `.workflow_artifacts/` without assuming Codex command files.
- Simple OpenCode: ask OpenCode to review the diff in its native session. Same
  model and endpoint as the Quoin OpenCode run.
- Quoin + OpenCode: use the Quoin review command; the reviewer role stays
  read-only and writes its review under `.workflow_artifacts/`.

## Expected Evidence

- Review findings.
- Files inspected.
- Checks run, if any.
- For Quoin modes, `review-N.md` or equivalent review evidence.

## Evaluation Notes

Score for actionable findings, severity ordering, evidence quality, and
avoidance of vague or speculative comments.
